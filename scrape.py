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

# De første ~48 hits ligger i sidens egen server-renderede data med alle tre prisniveauer
JS_SUPERVIN_PRICE_TABLES = """() => {
  try {
    const st = JSON.parse(document.getElementById('ng-state').textContent);
    return JSON.parse(st['searchPageResults.hits']).hits.map(h => ({
      name: h.field_product, price_table: h.price_table, ratings: h.ratings
    }));
  } catch (e) { return null; }
}"""

JS_SUPERVIN_SCROLL = """() => {
  const a = document.querySelectorAll('article[app-product-widget]');
  if (a.length) a[a.length - 1].scrollIntoView({block: 'center'});
}"""
JS_SUPERVIN_LOADER = """() => {
  const l = document.querySelector('.loading-wrapper');
  if (l) l.scrollIntoView({block: 'center'}); else window.scrollTo(0, document.body.scrollHeight);
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


def goto(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:
        pass  # nogle sider bliver aldrig helt "idle"; det er OK


def scrape_supervin(page, url=SUPERVIN, max_rounds=60):
    goto(page, url)
    page.wait_for_timeout(2000)
    price_tables = page.evaluate(JS_SUPERVIN_PRICE_TABLES)
    total_text = page.evaluate(JS_MAIN_TEXT)
    m = re.search(r"(\d+)\s+produkter fundet", total_text, re.I)
    total = int(m.group(1)) if m else None

    prev, stable = -1, 0
    for _ in range(max_rounds):
        n = page.locator("article[app-product-widget]").count()
        if total and n >= total:
            break
        stable = stable + 1 if n == prev else 0
        if stable >= 6:
            break
        prev = n
        page.evaluate(JS_SUPERVIN_SCROLL)
        page.wait_for_timeout(700)
        page.evaluate(JS_SUPERVIN_LOADER)
        page.wait_for_timeout(1800)

    cards = page.evaluate(JS_SUPERVIN_CARDS)
    return {"url": url, "total_reported": total, "count": len(cards),
            "cards": cards, "price_tables_first_page": price_tables}


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
        exe = find_chromium()
        try:
            browser = pw.chromium.launch(headless=True, executable_path=exe) if exe \
                else pw.chromium.launch(headless=True)
        except Exception:
            log(f"Kunne ikke starte {exe}; prøver Playwrights egen Chromium")
            browser = pw.chromium.launch(headless=True)
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
