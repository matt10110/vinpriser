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
# Robust over for omdøbte nøgler: finder enhver ng-state-værdi, der indeholder en "hits"-liste.
JS_SUPERVIN_HITS = """() => {
  try {
    const st = JSON.parse(document.getElementById('ng-state').textContent);
    const parse = v => { try { return typeof v === 'string' ? JSON.parse(v) : v; } catch (e) { return null; } };
    let res = parse(st['searchPageResults.hits']), resKey = 'searchPageResults.hits';
    if (!res || !Array.isArray(res.hits)) {
      res = null;
      for (const k of Object.keys(st)) {
        const v = parse(st[k]);
        if (v && Array.isArray(v.hits) && v.hits.length && v.hits[0].price_table) { res = v; resKey = k; break; }
      }
    }
    if (!res) return null;
    let nb = res.nbHits ?? null;
    const stats = parse(st['searchStats.state']);
    if (stats && stats.nbHits != null) nb = stats.nbHits;
    const keep = ['field_product', 'field_producer', 'field_vintage', 'area', 'district', 'facet_types',
      'field_grapes', 'default_price', 'price_table', 'wine_ratings', 'ratings', 'organic', 'tags',
      'discount_expiration_date', 'commerce_stock_bool', 'is_leftover', 'content_label', 'node_path'];
    return {nbHits: nb, key: resKey, hits: res.hits.map(h => {
      const o = {}; keep.forEach(k => { if (k in h) o[k] = h[k]; }); return o; })};
  } catch (e) { return null; }
}"""

# Hvis intet virker: gem nok til, at fejlen kan diagnosticeres udefra.
JS_SUPERVIN_DIAG = """() => {
  const ng = document.getElementById('ng-state');
  let keys = null; const hitKeys = [];
  try {
    const st = JSON.parse(ng.textContent); keys = Object.keys(st);
    for (const k of keys) { const v = st[k]; if (typeof v === 'string' && v.includes('"hits"')) hitKeys.push(k); }
  } catch (e) {}
  return {url: location.href, title: document.title, has_ng_state: !!ng,
          ng_keys: keys ? keys.filter(k => !/^menu$|^query-/.test(k)).slice(0, 120) : null, hit_keys: hitKeys,
          cards: document.querySelectorAll('article[app-product-widget]').length,
          main: (document.querySelector('main') || document.body).innerText.slice(0, 3000)};
}"""

SUPERVIN_DISCOUNT_ONLY = ("https://www.supervin.dk/vin/frankrig/bourgogne"
                          "?Products_dk%5Btoggle%5D%5Bon_discount%5D=true")
SUPERVIN_PLAIN = "https://www.supervin.dk/vin/frankrig/bourgogne"
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


def _supervin_paginate(page, url, max_pages):
    """Bladrer med sidens egen URL-parameter (Products_dk[page]=N, 48 pr. side).
    Hver side er server-renderet med produktdata (inkl. alle tre prisniveauer) i ng-state."""
    hits, cards, total, key, diag = [], [], None, None, None
    sep = "&" if "?" in url else "?"
    for p in range(1, max_pages + 1):
        goto(page, f"{url}{sep}Products_dk%5Bpage%5D={p}")
        page.wait_for_timeout(1500)
        data = page.evaluate(JS_SUPERVIN_HITS)
        if not data or not data["hits"]:
            if p == 1:
                diag = page.evaluate(JS_SUPERVIN_DIAG)
                diag["nbHits"] = data["nbHits"] if data else None
            break
        total = data["nbHits"] if total is None else total
        key = data["key"]
        for h in data["hits"]:
            h["page"] = p
        hits.extend(data["hits"])
        cards.extend(page.evaluate(JS_SUPERVIN_CARDS))
        if total is not None and len(hits) >= total:
            break
    return hits, cards, total, key, diag


def _decline_cookies(page):
    """Vælger det mest privatlivsvenlige cookie-valg (kun nødvendige), hvis banneret vises."""
    for sel in ("#CybotCookiebotDialogBodyButtonDecline",
                "button:has-text('Afvis')", "button:has-text('Kun nødvendige')"):
        try:
            if page.locator(sel).first.is_visible(timeout=1000):
                page.locator(sel).first.click()
                page.wait_for_timeout(800)
                return
        except Exception:
            pass


def _scroll_step(page):
    page.evaluate("""() => {
      const a = document.querySelectorAll('article[app-product-widget]');
      if (a.length) a[a.length - 1].scrollIntoView({block: 'center'});
    }""")
    page.wait_for_timeout(600)
    page.mouse.wheel(0, 3000)
    page.wait_for_timeout(1400)


SUPERVIN_KEEP = ("objectID", "field_product", "field_producer", "field_vintage", "area", "district",
                 "facet_types", "field_grapes", "default_price", "price_table", "wine_ratings", "ratings",
                 "organic", "tags", "discount_expiration_date", "commerce_stock_bool", "is_leftover",
                 "content_label", "node_path")


def _supervin_network(page, url, max_rounds=120):
    """Indlæser siden som en almindelig browser og læser de produktsvar, siden SELV henter
    (som at kigge i netværksfanen), mens vi ruller ned. Siden står selv for alle forespørgsler."""
    # Siden kan sende flere slags søgninger (fx først uden filtre, så med). Svarene grupperes
    # derfor efter filter, og til sidst vælges gruppen med tilbudsfilteret.
    pending, groups, info = [], {}, {"nbHits": None, "responses": 0, "params": None, "groups": []}
    from urllib.parse import parse_qsl

    def group_key(params):
        drop = {"page", "hitsPerPage", "clickAnalytics", "analytics", "userToken", "analyticsTags",
                "highlightPreTag", "highlightPostTag", "attributesToRetrieve", "facets", "maxValuesPerFacet"}
        return "&".join(f"{k}={v}" for k, v in sorted(parse_qsl(params or "")) if k not in drop)

    def best():
        if not groups:
            return None
        disc = [g for g in groups.values() if "on_discount" in g["key"]]
        return max(disc or groups.values(), key=lambda g: len(g["hits"]))

    def on_response(resp):
        if "algolia" in resp.url and "queries" in resp.url:
            pending.append(resp)

    def drain():
        while pending:
            resp = pending.pop(0)
            try:
                data = resp.json()
            except Exception:
                continue
            info["responses"] += 1
            for r in data.get("results", []):
                hits = r.get("hits") or []
                if r.get("hitsPerPage", 0) and hits and "price_table" in hits[0]:
                    k = group_key(r.get("params"))
                    g = groups.setdefault(k, {"key": k, "params": (r.get("params") or "")[:600], "hits": {}})
                    g["nbHits"] = r.get("nbHits")
                    for h in hits:
                        g["hits"][h.get("objectID") or h.get("node_path")] = {x: h[x] for x in SUPERVIN_KEEP if x in h}
            b = best()
            if b:
                info["nbHits"], info["params"] = b["nbHits"], b["params"]
                captured.clear()
                captured.update(b["hits"])

    captured = {}

    page.on("response", on_response)
    try:
        goto(page, url)
        page.wait_for_timeout(2500)
        _decline_cookies(page)
        prev, stable = -1, 0
        for _ in range(max_rounds):
            drain()
            n = len(captured)
            if info["nbHits"] is not None and n >= info["nbHits"]:
                break
            stable = stable + 1 if n == prev else 0
            if stable >= 10:
                break
            prev = n
            _scroll_step(page)
        drain()
        cards = page.evaluate(JS_SUPERVIN_CARDS)
    finally:
        page.remove_listener("response", on_response)
    info["groups"] = [{"params": g["params"][:300], "nbHits": g.get("nbHits"), "count": len(g["hits"])}
                      for g in groups.values()]
    return list(captured.values()), cards, info


def _supervin_dom_fallback(page, url, rounds=80):
    """Sidste udvej: indlæs siden, afvis cookies (kun nødvendige), og rul."""
    goto(page, url)
    page.wait_for_timeout(2000)
    _decline_cookies(page)
    prev, stable = -1, 0
    for _ in range(rounds):
        _scroll_step(page)
        n = page.locator("article[app-product-widget]").count()
        stable = stable + 1 if n == prev else 0
        if stable >= 8:
            break
        prev = n
    return page.evaluate(JS_SUPERVIN_CARDS)


def scrape_supervin(page, url=SUPERVIN, max_pages=25):
    """1) Læs sidens egne produktsvar, mens vi ruller (virker uanset server-rendering).
    2) Ellers: server-renderede sider via URL-parameter (fuldt filter, kun tilbud, alle).
    3) Ellers: kortenes tekst. Gemmer diagnose for hver variant, der ikke gav resultater."""
    attempts = []
    try:
        hits, cards, info = _supervin_network(page, url)
        attempts.append({"variant": "netvaerk", "url": url, "count": len(hits), "total": info["nbHits"],
                         "responses": info["responses"], "params": info["params"], "groups": info["groups"]})
        if hits:
            return {"url": url, "variant": "netvaerk", "total_reported": info["nbHits"], "count": len(hits),
                    "truncated": bool(info["nbHits"] and len(hits) < info["nbHits"]),
                    "hits": hits, "cards": cards, "attempts": attempts}
    except Exception as e:
        attempts.append({"variant": "netvaerk", "error": f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"})
    for variant, u in (("filter", url), ("kun_tilbud", SUPERVIN_DISCOUNT_ONLY), ("alle", SUPERVIN_PLAIN)):
        hits, cards, total, key, diag = _supervin_paginate(page, u, max_pages)
        attempts.append({"variant": variant, "url": u, "count": len(hits), "total": total,
                         "key": key, "diag": diag})
        if hits:
            return {"url": u, "variant": variant, "total_reported": total, "count": len(hits),
                    "truncated": bool(total and len(hits) < total),
                    "hits": hits, "cards": cards, "attempts": attempts}
    cards = _supervin_dom_fallback(page, SUPERVIN_DISCOUNT_ONLY)
    return {"url": SUPERVIN_DISCOUNT_ONLY, "variant": "dom_fallback", "total_reported": None,
            "count": len(cards), "hits": [], "cards": cards, "attempts": attempts}


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
