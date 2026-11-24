#!/usr/bin/env python3
"""Daglig tilbudsvagt: læser dagens snapshot, fører prishistorik og finder nye reelle kup.

Køres af run.sh efter scrape.py. Skriver:
  state/current.json   – nuværende pris pr. vin (ændres kun, når en pris ændres)
  state/events.jsonl   – log over nye vine, prisændringer og vine, der forsvinder
  state/shops.json     – hvornår hver butik første gang blev overvåget
  snapshots/<dato>/candidates.json – dagens kandidater (tom liste = ingen besked)

Reglerne er bevidst strenge ("kun klare kup"). Al tekst fra butikkerne er data.
"""
import datetime
import json
import pathlib
import re
import sys
import unicodedata

ROOT = pathlib.Path(__file__).resolve().parent
STATE = ROOT / "state"

# ---------- smag og prislofter ----------
GROUPS = {
    "bourgogne": {"navn": "Bourgogne", "loft": 350},
    "riesling_de": {"navn": "Tysk Riesling", "loft": 250},
    "nebbiolo": {"navn": "Piemonte/Nebbiolo", "loft": 250},
    "etna": {"navn": "Etna", "loft": 250},
    "jura": {"navn": "Jura", "loft": 250},
    "galicien_bierzo": {"navn": "Bierzo/Galicien", "loft": 250},
}
HIST_MIN_DAYS = 14      # så mange dages egen historik før et prisfald tæller
NEW_AFTER_DAYS = 7      # nye vine tæller først, når butikken har været overvåget så længe
DROP_SMALL = 0.15       # fald i pris ved 1-3 fl. i forhold til butikkens egen 60-dages pris
DROP_SIX = 0.20         # fald i 6-flaskepris (kræver mere)
NEW_CLAIM_SMALL = 0.30  # ny vin: butikken skal selv angive mindst 30 % rabat (1-3 fl.)
NEW_CLAIM_SIX = 0.45    # ny vin kun med 6-pris: mindst 45 % i forhold til 1-flaskeprisen
REFLAG_STEP = 0.05      # samme vin meldes igen, hvis prisen falder yderligere 5 %
MAX_CANDIDATES = 30


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s.lower())


BURG_WORDS = [
    "bourgogne", "burgund", "chablis", "macon", "pouilly-fuisse", "pouilly fuisse", "pouilly-vinzelles",
    "pouilly-loche", "saint-veran", "st-veran", "vire-clesse", "bouzeron", "rully", "mercurey", "givry",
    "montagny", "marsannay", "fixin", "gevrey", "morey", "chambolle", "vougeot", "vosne", "echezeaux",
    "nuits", "cote de nuits", "cote de beaune", "hautes-cotes", "hautes cotes", "ladoix", "aloxe",
    "corton", "pernand", "savigny", "chorey", "beaune", "pommard", "volnay", "monthelie", "auxey",
    "meursault", "puligny", "chassagne", "saint-aubin", "st-aubin", "saint-romain", "santenay",
    "maranges", "irancy", "saint-bris", "montrachet", "musigny", "chambertin", "bonnes-mares",
    "coteaux bourguignons",
]
DE_REGIONS = ["mosel", "saar", "ruwer", "nahe", "rheingau", "rheinhessen", "pfalz", "franken", "baden",
              "wurttemberg", "ahr", "mittelrhein", "sachsen", "saale", "bergstrasse", "tyskland", "germany"]
DE_HINTS = ["weingut", "kabinett", "spatlese", "auslese", "trocken", "grosses gewachs", "erste lage",
            "grosse lage", "vdp", "feinherb"] + DE_REGIONS
NEBB_WORDS = ["nebbiolo", "barolo", "barbaresco", "roero", "gattinara", "ghemme", "boca", "lessona",
              "bramaterra", "carema", "valtellina", "sforzato", "sfursat", "chiavennasca", "spanna"]
ETNA_WORDS = ["etna", "nerello", "carricante"]
JURA_WORDS = ["jura", "arbois", "l'etoile", "chateau-chalon", "savagnin", "poulsard", "ploussard", "trousseau"]
GAL_WORDS = ["bierzo", "ribeira sacra", "valdeorras", "galicia", "galicien", "rias baixas", "monterrei",
             "ribeiro", "mencia", "godello", "souson", "caino", "merenzao", "brancellao"]
NOT_WINE = ["gavekort", "cremant", "gin ", "whisky", "rom ", "glas ", "kasse med", "smagekasse", "vinpakke",
            "-tema", "luksus-"]
# Jysk Vins kort viser "PRODUCENT, LAND"; druefiltret alene giver også fx australsk Mencía
JYSK_COUNTRY = {"nebbiolo": "italien", "etna": "italien", "jura": "frankrig", "galicien_bierzo": "spanien"}


def _has(t, words):
    return any(w in t for w in words)


def classify_text(text, hint=None):
    """Gruppe ud fra fri tekst (navn, producent, land, område, drue). hint = gruppe fra kategorien."""
    t = norm(text)
    if _has(t, NOT_WINE):
        return None
    if hint == "bourgogne":
        return "bourgogne" if "gamay" not in t else None
    if hint == "nebbiolo":
        return "nebbiolo"
    if hint == "riesling_de":
        return "riesling_de" if "riesling" in t and _has(t, DE_HINTS) else None
    if hint == "jysk_bourgogne":   # Jysk: Pinot/Chardonnay/Aligoté – kun fra Bourgogne
        return "bourgogne" if "frankrig" in t and _has(t, BURG_WORDS) else None
    if hint == "jysk_riesling":
        return "riesling_de" if "tyskland" in t else None
    if hint in GROUPS:
        return hint
    # uden hint (fx Laudrups samlede tilbudsliste)
    if _has(t, ETNA_WORDS):
        return "etna"
    if _has(t, GAL_WORDS):
        return "galicien_bierzo"
    if "riesling" in t and _has(t, DE_HINTS):
        return "riesling_de"
    if _has(t, NEBB_WORDS):
        return "nebbiolo"
    if "frankrig" in t and _has(t, JURA_WORDS):
        return "jura"
    if ("bourgogne" in t or "burgund" in t) and _has(t, ["pinot noir", "chardonnay", "aligote"]):
        return "bourgogne"
    # fx Uhrskovs outlet, hvor teksten kun har producent og appellation (Meursault, Volnay ...)
    if _has(t, BURG_WORDS) and not _has(t, ["gamay", "beaujolais", "passetoutgrain"]):
        return "bourgogne"
    return None


def classify_supervin(h):
    grapes = [norm(g) for g in (h.get("field_grapes") or [])]
    area, district = norm(h.get("area")), norm(h.get("district"))
    country = norm(h.get("country") if isinstance(h.get("country"), str) else " ".join(h.get("country") or []))
    name = norm(h.get("field_product"))
    where = " ".join([area, district, country, name])
    if "cremant" in name:
        return None
    if area == "bourgogne":
        return "bourgogne" if set(grapes) & {"pinot noir", "chardonnay", "aligote"} else None
    if "riesling" in grapes and (_has(where, DE_REGIONS)):
        return "riesling_de"
    if "nebbiolo" in grapes or _has(where, NEBB_WORDS[1:]):
        return "nebbiolo"
    if _has(where, ["etna"]) or set(grapes) & {"nerello mascalese", "nerello cappuccio", "carricante"}:
        return "etna"
    if _has(" ".join([area, district]), ["jura"]) or set(grapes) & {"savagnin", "poulsard", "ploussard", "trousseau"}:
        return "jura"
    if _has(where, GAL_WORDS) or set(grapes) & {"mencia", "godello", "souson", "caino", "merenzao", "brancellao"}:
        return "galicien_bierzo"
    return None


# ---------- prisudtræk ----------
def P(s):
    return float(s.replace(".", "").replace(",", "."))


NUM = r"(\d{1,3}(?:\.\d{3})*,\d{2})"


def items_supervin(d):
    out = []
    for h in d.get("hits") or []:
        g = h.get("group") or classify_supervin(h)
        if not g:
            continue
        prices, before, exp = {}, None, None
        for x in (h.get("price_table") or {}).get("pricings") or []:
            disc = x.get("discount") or {}
            q = int(x.get("min") or 1)
            prices[q] = disc.get("price") or x.get("inc")
            if q == 1:
                before = x.get("inc")
            if disc.get("to"):
                exp = str(disc["to"])[:10]
        key = h.get("node_path") or h.get("objectID")
        out.append({"id": key, "name": h.get("field_product"), "group": g, "prices": prices,
                    "before": before, "exp": exp if exp and not exp.startswith("2080") else None,
                    "url": f"https://www.supervin.dk/{h.get('node_path')}" if h.get("node_path") else None,
                    "sold_out": h.get("commerce_stock_bool") in (0, False)})
    return out


def items_jysk(d):
    out = []
    for c in d.get("items") or []:
        t = re.sub(r"\s+", " ", c.get("text") or "")
        g = classify_text(t, c.get("hint"))
        if not g or (c.get("hint") in JYSK_COUNTRY and JYSK_COUNTRY[c["hint"]] not in norm(t)):
            continue
        prices = {int(q): P(p) for p, q in re.findall(NUM + r" DKK v/(\d+) stk", t)}
        m = re.search(NUM + r" DKK v/1 stk\. " + NUM + r" DKK (?!v/)", t)
        before = P(m.group(2)) if m else None
        out.append({"id": c["href"], "name": t[:140], "group": g, "prices": prices, "before": before,
                    "exp": (c.get("countdown") or "")[:10] or None, "url": c["href"],
                    "sold_out": "udsolgt" in t.lower()})
    return out


def items_uhrskov(d, hint):
    out = []
    for c in d.get("items") or []:
        t = re.sub(r"\s+", " ", c.get("text") or "")
        g = classify_text(t, hint)
        if not g:
            continue
        prices = {int(q): P(p) for p, q in re.findall(NUM + r"\s*DKK\s*ved (\d+) stk", t)}
        m = re.search(r"Før:\s*" + NUM, t)
        out.append({"id": c["href"], "name": re.split(NUM, t)[0][:140], "group": g, "prices": prices,
                    "before": P(m.group(1)) if m else None, "exp": None, "url": c["href"],
                    "sold_out": "udsolgt" in t.lower() or "Læg i kurv" not in t})
    return out


def items_laudrup(d):
    out = []
    for c in d.get("items") or []:
        t = re.sub(r"\s+", " ", c.get("text") or "")
        g = classify_text(t)
        if not g:
            continue
        prices, before = {}, None
        m = re.search(r"Fra " + NUM + r" kr\.?.*?Spar (\d+)% ved køb af (\d+) stk", t)
        if m:
            p, pct, q = P(m.group(1)), int(m.group(2)), int(m.group(3))
            prices[q] = p
            before = round(p / (1 - pct / 100), 2) if pct < 100 else None
        else:
            nums = [P(x) for x in re.findall(NUM + r" kr", t)]
            if nums:
                prices[1] = nums[0]
                before = nums[1] if len(nums) > 1 and nums[1] > nums[0] else None
        if not prices:
            continue
        out.append({"id": c["href"], "name": t[:140], "group": g, "prices": prices, "before": before,
                    "exp": None, "url": c["href"], "sold_out": "udsolgt" in t.lower()})
    return out


SOURCES = {  # snapshot-fil -> (butik, parser)
    "supervin.json": ("supervin", items_supervin),
    "jyskvin_tilbud.json": ("jyskvin", items_jysk),
    "uhrskov.json": ("uhrskov", lambda d: items_uhrskov(d, "bourgogne")),
    "uhrskov_hvid.json": ("uhrskov", lambda d: items_uhrskov(d, "bourgogne")),
    "uhrskov_outlet.json": ("uhrskov", lambda d: items_uhrskov(d, None)),
    "uhrskov_nebbiolo.json": ("uhrskov", lambda d: items_uhrskov(d, "nebbiolo")),
    "uhrskov_tysk_hvid.json": ("uhrskov", lambda d: items_uhrskov(d, "riesling_de")),
    "laudrup.json": ("laudrup", items_laudrup),
}


def effective(prices):
    small = [p for q, p in prices.items() if q <= 3 and p]
    return (min(small) if small else None, prices.get(6), prices.get(12))


# ---------- historik ----------
def load_json(p, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def read_events():
    ev = {}
    p = STATE / "events.jsonl"
    if p.exists():
        for line in p.read_text().splitlines():
            try:
                e = json.loads(line)
                ev.setdefault(e["k"], []).append(e)
            except Exception:
                pass
    return ev


def tw_median(events, field, today, window=60):
    """Tidsvægtet median af en pris over de seneste `window` dage (før i dag)."""
    end = datetime.date.fromisoformat(today)
    start = end - datetime.timedelta(days=window)
    segs, cur, cur_d = [], None, None
    for e in sorted(events, key=lambda e: e["d"]):
        d = datetime.date.fromisoformat(e["d"])
        if cur_d is not None and cur is not None:
            a, b = max(cur_d, start), min(d, end)
            if b > a:
                segs.append((cur, (b - a).days))
        cur = None if e["e"] == "gone" else e.get(field)
        cur_d = d
    if cur_d is not None and cur is not None:
        a = max(cur_d, start)
        if end > a:
            segs.append((cur, (end - a).days))
    days = sum(n for _, n in segs)
    if not days:
        return None, 0
    acc = 0
    for v, n in sorted(segs):
        acc += n
        if acc >= days / 2:
            return v, days
    return segs[-1][0], days


def main(date=None):
    today = date or datetime.date.today().isoformat()
    snap = ROOT / "snapshots" / today
    meta = load_json(snap / "meta.json", {})
    STATE.mkdir(exist_ok=True)
    current = load_json(STATE / "current.json", {})
    sources = load_json(STATE / "sources.json", {})   # kilde -> første overvågningsdag
    events = read_events()
    new_events, cands = [], []

    # saml dagens vine pr. kilde (fil). En kilde, der fejlede, ændrer ingenting i historikken.
    seen, src_ok = {}, {}
    for fname, (shop, parse) in SOURCES.items():
        f = snap / fname
        if not f.exists() or fname[:-5] in (meta.get("errors") or {}):
            src_ok[fname] = False
            continue
        try:
            items = parse(load_json(f, {}))
        except Exception as e:
            print(f"parse-fejl {fname}: {e}", file=sys.stderr)
            src_ok[fname] = False
            continue
        src_ok[fname] = True
        sources.setdefault(fname, today)
        for it in items:
            seen[f"{shop}|{it['id']}"] = dict(it, shop=shop, src=fname)

    for fname, ok in list(src_ok.items()):
        prev_n = sum(1 for v in current.values() if v.get("src") == fname and not v.get("gone"))
        now_n = sum(1 for v in seen.values() if v["src"] == fname)
        if ok and prev_n >= 10 and now_n < prev_n * 0.5:   # under halvdelen af i går: nok en delvis fejl
            src_ok[fname] = False

    # Butikkens "normale" påståede rabat (median). Nye vine skal ligge klart under den,
    # ellers er rabatten bare butikkens sædvanlige anker (fx Supervins 1-flaskepris).
    typical = {}
    for it in seen.values():
        it["ps"], it["p6"], it["p12"] = effective(it["prices"])
        one = it["prices"].get(1) or it.get("before")
        t = typical.setdefault(it["shop"], {"small": [], "six": []})
        if it["ps"] and it.get("before") and it["ps"] < it["before"]:
            t["small"].append(it["ps"] / it["before"])
        if it["p6"] and one:
            t["six"].append(it["p6"] / one)
    for t in typical.values():
        for kind in ("small", "six"):
            v = sorted(t[kind])
            t[kind] = v[len(v) // 2] if len(v) >= 5 else None

    def ev(k, kind, it=None, prev=None):
        e = {"d": today, "k": k, "e": kind}
        if it:
            e.update({"ps": it["ps"], "p6": it["p6"], "p12": it["p12"]})
        new_events.append(e)
        events.setdefault(k, []).append(e)

    for k, it in seen.items():
        old = current.get(k)
        hist_before_today = list(events.get(k, []))
        if not src_ok.get(it["src"]):
            continue
        if old is None:
            current[k] = {"shop": it["shop"], "src": it["src"], "name": it["name"], "group": it["group"],
                          "url": it["url"], "ps": it["ps"], "p6": it["p6"], "p12": it["p12"],
                          "before": it["before"], "exp": it["exp"], "first": today, "since": today}
            ev(k, "new", it)
            base_age = (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(sources[it["src"]])).days
            if base_age >= NEW_AFTER_DAYS and not it["sold_out"]:
                c = judge_new(it, typical.get(it["shop"], {}))
                if c:
                    cands.append(c)
                    current[k]["flag"] = c["pris"]
            continue
        changed = (old.get("ps"), old.get("p6"), old.get("p12")) != (it["ps"], it["p6"], it["p12"])
        came_back = bool(old.get("gone"))
        old.update({"src": it["src"], "name": it["name"], "group": it["group"], "url": it["url"],
                    "before": it["before"], "exp": it["exp"]})
        old.pop("gone", None)
        if changed or came_back:
            old.update({"ps": it["ps"], "p6": it["p6"], "p12": it["p12"], "since": today})
            ev(k, "back" if came_back else "chg", it)
            if not it["sold_out"]:
                c = judge_drop(it, hist_before_today, today, old.get("flag"))
                if c:
                    cands.append(c)
                    old["flag"] = c["pris"]
                elif old.get("flag") and (it["ps"] or it["p6"] or 0) > old["flag"] * 1.10:
                    old.pop("flag", None)   # prisen er steget igen: et senere fald må meldes på ny

    for k, v in current.items():
        if k not in seen and not v.get("gone") and src_ok.get(v.get("src")):
            v["gone"] = today
            ev(k, "gone")

    # Fald i butikkens egen pris vejer tungest; nye vine (kun butikkens egen rabat) højst 8 om dagen.
    strong = sorted([c for c in cands if c["type"].startswith("fald")], key=lambda c: -c["styrke"])
    weak = sorted([c for c in cands if c["type"].startswith("ny")], key=lambda c: -c["styrke"])[:8]
    flagged = {c["url"] for c in strong + weak}
    for c in cands:   # vine, der ikke kom med, må gerne meldes senere
        if c["url"] not in flagged:
            for v in current.values():
                if v.get("url") == c["url"] and v.get("flag") == c["pris"]:
                    v.pop("flag", None)
    cands = (strong + weak)[:MAX_CANDIDATES]
    (STATE / "current.json").write_text(json.dumps(dict(sorted(current.items())), ensure_ascii=False, indent=0))
    (STATE / "sources.json").write_text(json.dumps(sources, ensure_ascii=False, indent=1))
    with open(STATE / "events.jsonl", "a") as fh:
        for e in new_events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    out = {"date": today, "regler": {"grupper": GROUPS, "fald_1_3_fl": DROP_SMALL, "fald_6_fl": DROP_SIX,
                                     "historik_dage": HIST_MIN_DAYS},
           "kilder": {f: {"ok": ok, "vine": sum(1 for v in seen.values() if v["src"] == f),
                          "overvaaget_siden": sources.get(f)} for f, ok in src_ok.items()},
           "candidates": cands}
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "candidates.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"watch: {len(seen)} vine, {len(new_events)} hændelser, {len(cands)} kandidater")
    return out


def _base(it, kind, price, qty, ref=None, hist_days=None):
    loft = GROUPS[it["group"]]["loft"]
    claimed = (1 - price / it["before"]) if it.get("before") else None
    drop = (1 - price / ref) if ref else None
    styrke = (drop or 0) * 2 + (claimed or 0) * 0.5 + max(0, 1 - price / loft) * 0.5 + (0.3 if qty <= 3 else 0)
    return {"type": kind, "butik": it["shop"], "gruppe": GROUPS[it["group"]]["navn"], "navn": it["name"],
            "url": it["url"], "pris": price, "antal": qty, "priser": it["prices"],
            "butikkens_foerpris": it.get("before"), "butikkens_rabat": round(claimed, 3) if claimed else None,
            "egen_60d_pris": ref, "reelt_fald": round(drop, 3) if drop else None, "historik_dage": hist_days,
            "udloeber": it.get("exp"), "loft": loft, "styrke": round(styrke, 3)}


def judge_drop(it, hist, today, flag):
    loft = GROUPS[it["group"]]["loft"]
    ref_s, days_s = tw_median(hist, "ps", today)
    ref_6, days_6 = tw_median(hist, "p6", today)
    small_ok = it["ps"] and it["ps"] <= loft and not (it["p6"] and it["ps"] > it["p6"] * 1.3)
    if small_ok and ref_s and days_s >= HIST_MIN_DAYS and it["ps"] <= ref_s * (1 - DROP_SMALL):
        if not flag or it["ps"] <= flag * (1 - REFLAG_STEP):
            q = min(q for q, p in it["prices"].items() if p == it["ps"])
            c = _base(it, "fald", it["ps"], q, ref_s, days_s)
            c["tidligere_6_pris"] = ref_6
            return c
    if it["p6"] and it["p6"] <= loft and ref_6 and days_6 >= HIST_MIN_DAYS and it["p6"] <= ref_6 * (1 - DROP_SIX):
        if not flag or it["p6"] <= flag * (1 - REFLAG_STEP):
            return _base(it, "fald_6", it["p6"], 6, ref_6, days_6)
    return None


def judge_new(it, typical):
    """Ny vin uden egen historik: kun hvis rabatten er stor OG klart større end butikkens sædvane."""
    loft = GROUPS[it["group"]]["loft"]
    b = it.get("before")
    t_small = typical.get("small") or 1.0
    t_six = typical.get("six") or 1.0
    if it["ps"] and it["ps"] <= loft and b:
        r = it["ps"] / b
        if r <= 1 - NEW_CLAIM_SMALL and r <= t_small - 0.10:
            q = min(q for q, p in it["prices"].items() if p == it["ps"])
            return dict(_base(it, "ny", it["ps"], q), butikkens_typiske_rabat=round(1 - t_small, 3))
    if it["p6"] and it["p6"] <= loft:
        one = it["prices"].get(1) or b
        if one:
            r = it["p6"] / one
            if r <= 1 - NEW_CLAIM_SIX and r <= t_six - 0.15:
                return dict(_base(it, "ny_6", it["p6"], 6), butikkens_typiske_rabat=round(1 - t_six, 3))
    return None


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
