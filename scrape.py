#!/usr/bin/env python3
"""Dagligt snapshot af Bourgogne-tilbud hos Supervin, Jysk Vin og Uhrskov.

Indlæser de offentlige kategorisider i en headless Chromium (som en almindelig
browser), gemmer produkternes tekst og priser som JSON i snapshots/<dato>/
og skriver en meta.json med antal og eventuelle fejl. Én kørsel = ca. 20 sidevisninger.
"""
import datetime
import json
import pathlib
import re
import sys
import traceback

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent
TODAY = datetime.date.today().isoformat()
OUT = ROOT / "snapshots" / TODAY

SUPERVIN = (
    "https://www.supervin.dk/vin/frankrig/bourgogne?Products_dk%5Bconfigure%5D%5BhitsPerPage%5D=48"
    "&Products_dk%5Bconfigure%5D%5Bfilters%5D=&Products_dk%5Btoggle%5D%5Bon_discount%5D=true"
    "&Products_dk%5BrefinementList%5D%5Bcountry%5D%5B0%5D=Frankrig"
    "&Products_dk%5BrefinementList%5D%5Barea%5D%5B0%5D=Bourgogne"
    "&Products_dk%5BrefinementList%5D%5Bfield_grapes%5D%5B0%5D=Chardonnay"
    "&Products_dk%5BrefinementList%5D%5Bfield_grapes%5D%5B1%5D=Pinot%20Noir"
    "&Products_dk%5BrefinementList%5D%5Battribute_set%5D%5B0%5D=Vin"
)
JYSKVIN = (
    "https://www.jyskvin.dk/bourgogne-vine?products_per_page=90&sorts=p.price-ASC"
    "&filter_type%5B%5D=447&filter_type%5B%5D=551&filter_drue%5B%5D=35&filter_drue%5B%5D=34"
)
UHRSKOV = (
    "https://www.uhrskov-vine.dk/produkter/vin/roedvin/frankrig-roedvin/bourgogne-roedvin"
    "?p={p}&product_list_limit=36&product_list_order=price&product_list_dir=asc"
)
UHRSKOV_OUTLET = "https://www.uhrskov-vine.dk/outlet?p={p}&product_list_limit=36"
HOMEPAGES = {
    "supervin": "https://www.supervin.dk/",
    "jyskvin": "https://www.jyskvin.dk/",
    "uhrskov": "https://www.uhrskov-vine.dk/",
}

JS_SUPERVIN_CARDS = """() => [...document.querySelectorAll('article[app-product-widget]')].map(a => ({
  name: (a.querySelector('img[alt]') || {}).alt || '',
  href: (a.querySelector('a[href]') || {href: ''}).href,
  text: a.innerText
}))"""

# Hver side har sine produkter i sidens egen server-renderede data (ng-state),
# inkl. alle tre prisniveauer (1 fl., "normal" 6-pris, kampagnepris) og udløbsdato.
JS_SUPERVIN_HITS = """() => {
  try {
    const st = JSON.parse(document.getElementById('ng-state').textContent);
    const res = JSON.parse(st['searchPageResults.hits'] || '{"hits":[]}');
    const stats = JSON.parse(st['searchStats.state'] || '{}');
    const keep = ['field_product', 'field_producer', 'field_vintage', 'district', 'facet_types',
      'field_grapes', 'default_price', 'price_table', 'wine_ratings', 'ratings', 'organic',
      'discount_expiration_date', 'commerce_stock_bool', 'is_leftover', 'content_label', 'node_path'];
    return {nbHits: stats.nbHits ?? null, hits: res.hits.map(h => {
      const o = {}; keep.forEach(k => { if (k in h) o[k] = h[k]; }); return o; })};
  } catch (e) { return null; }
}"""
JS_MAIN_TEXT = "() => (document.querySelector('main') || document.body).innerText"

JS_UHRSKOV_ITEMS = """() => [...document.querySelectorAll('.product-item')].map(i => ({
  text: i.innerText,
  href: (i.querySelector('a[href]') || {href: ''}).href
}))"""

JS_COUNTDOWNS = "() => [...document.querySelectorAll('[data-countdown]')].map(c => c.dataset.countdown)"


def log(msg):
    print(f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


def find_chromium():
    for p in ("/usr/bin/chromium", "/usr/bin/chromium-browser"):
        if pathlib.Path(p).exists():
            return p
    return None


def launch(pw):
    """Starter Chromium MED sandbox (Playwright slår den ellers fra som standard).
    Falder kun tilbage til uden sandbox, hvis det ikke kan lade sig gøre – og noterer det i meta.json."""
    exe = find_chromium()
    attempts = [(exe, True), (None, True), (exe, False), (None, False)] if exe \
        else [(None, True), (None, False)]
    last = None
    for path, sandbox in attempts:
        kwargs = {"headless": True, "chromium_sandbox": sandbox}
        if path:
            kwargs["executable_path"] = path
        try:
            browser = pw.chromium.launch(**kwargs)
            info = {"executable": path or "playwright-bundled", "sandbox": sandbox}
            if not sandbox:
                log("ADVARSEL: Chromium kører uden sandbox")
            return browser, info
        except Exception as e:
            last = e
            log(f"Start fejlede ({path or 'bundled'}, sandbox={sandbox}): {str(e).splitlines()[0][:200]}")
    raise last


def goto(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:
        pass  # nogle sider bliver aldrig helt "idle"; det er OK


def scrape_supervin(page, url=SUPERVIN, max_pages=12):
    """Bladrer med sidens egen URL-parameter (Products_dk[page]=N, 48 pr. side).
    Hver side er server-renderet med produktdata (inkl. alle tre prisniveauer) i ng-state,
    så vi undgår infinite scroll, som ikke virker pålideligt headless."""
    hits, cards, total = [], [], None
    for p in range(1, max_pages + 1):
        goto(page, f"{url}&Products_dk%5Bpage%5D={p}")
        page.wait_for_timeout(1500)
        data = page.evaluate(JS_SUPERVIN_HITS)
        if data is None:
            break
        if total is None:
            total = data["nbHits"]
        if not data["hits"]:
            break
        for h in data["hits"]:
            h["page"] = p
        hits.extend(data["hits"])
        cards.extend(page.evaluate(JS_SUPERVIN_CARDS))
        if total is not None and len(hits) >= total:
            break
    return {"url": url, "total_reported": total, "count": len(hits),
            "hits": hits, "cards": cards}


def scrape_jyskvin(page, url=JYSKVIN):
    goto(page, url)
    page.wait_for_timeout(1500)
    return {"url": url, "text": page.evaluate(JS_MAIN_TEXT),
            "countdowns": page.evaluate(JS_COUNTDOWNS)}


def scrape_magento_listing(page, url_tpl, max_pages=15):
    """Bladrer gennem en Magento-liste, til en side er tom eller gentager forrige side."""
    items, first_hrefs = [], set()
    total = None
    for p in range(1, max_pages + 1):
        goto(page, url_tpl.format(p=p))
        page.wait_for_timeout(1500)
        if total is None:
            m = re.search(r"(\d+)\s+produkter", page.evaluate(JS_MAIN_TEXT), re.I)
            total = int(m.group(1)) if m else None
        batch = page.evaluate(JS_UHRSKOV_ITEMS)
        if not batch or batch[0]["href"] in first_hrefs:
            break
        first_hrefs.add(batch[0]["href"])
        for it in batch:
            it["page"] = p
        items.extend(batch)
        if total and len(items) >= total:
            break
    return {"url": url_tpl, "total_reported": total, "count": len(items), "items": items}


def scrape_homepage(page, url):
    goto(page, url)
    page.wait_for_timeout(1500)
    return {"url": url, "text": page.evaluate(JS_MAIN_TEXT)[:6000]}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    meta = {"date": TODAY, "started": datetime.datetime.now().isoformat(timespec="seconds"),
            "results": {}, "errors": {}}
    jobs = {
        "supervin": lambda pg: scrape_supervin(pg),
        "jyskvin": lambda pg: scrape_jyskvin(pg),
        "uhrskov": lambda pg: scrape_magento_listing(pg, UHRSKOV),
        "uhrskov_outlet": lambda pg: scrape_magento_listing(pg, UHRSKOV_OUTLET, max_pages=5),
    }
    for name, url in HOMEPAGES.items():
        jobs[f"forside_{name}"] = (lambda u: (lambda pg: scrape_homepage(pg, u)))(url)

    with sync_playwright() as pw:
        browser, meta["browser"] = launch(pw)
        ctx = browser.new_context(locale="da-DK", viewport={"width": 1280, "height": 1600})
        for name, job in jobs.items():
            page = ctx.new_page()
            try:
                log(f"Henter {name} ...")
                data = job(page)
                (OUT / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))
                meta["results"][name] = data.get("count", len(data.get("text", "")))
                log(f"  ok: {meta['results'][name]}")
            except Exception as e:
                meta["errors"][name] = f"{type(e).__name__}: {str(e).splitlines()[0][:300]}"
                log(f"  FEJL i {name}: {e}")
                traceback.print_exc()
            finally:
                page.close()
        browser.close()

    meta["finished"] = datetime.datetime.now().isoformat(timespec="seconds")
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    log(f"Færdig: {meta['results']} fejl: {list(meta['errors'])}")
    return 0 if meta["results"] else 1


if __name__ == "__main__":
    sys.exit(main())
