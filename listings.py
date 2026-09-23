#!/usr/bin/env python3
"""Monthly home buy/rent board -> Discord. Stdlib only. Everything specific comes from FEED_CONFIG["homes"]:
areas (lng/lat polygons + notes), zips, income components, loan/tax assumptions, rate source, labels, webhooks."""
import csv, io, json, os, re, statistics as st, urllib.parse, urllib.request, datetime as dt
from relay import get, post, log, UA, CFG, hook

NOW = dt.datetime.now(dt.timezone.utc)
C = CFG.get("homes") or {}
AREAS = C.get("areas") or []                     # [{"name","poly","note"}], order = ranking
ZIPS = C.get("zips") or {}
MKT = C.get("market", "")
MIN_P, MAX_P = C.get("min_price", 200_000), C.get("max_price", 450_000)
TAX_RATE, INS_MO, HOA_DEFAULT = C.get("tax_rate", 0.02), C.get("ins_mo", 200), C.get("hoa", 40)
FEE = 0.0 if C.get("fee_exempt") else C.get("loan_fee", 0.0)
RESID, UTIL = C.get("residual_min", 0), C.get("util_per_sqft", 0.14)
PHOTO_SRC = C.get("photo_source", {})            # {"<MLS source>": "<redfin datasource id>"}
AV = lambda n: f"https://cdn.jsdelivr.net/gh/jdecked/twemoji@latest/assets/72x72/{n}.png"
money = lambda x: f"${x:,.0f}"

# income: taxable base + untaxed allowances; deductions as a share of base
P = C.get("pay") or {}
BASE, ALLOW = float(P.get("base") or 0), sum(float(x) for x in (P.get("allowances") or []))
HOUSING_ALLOW = float(P.get("housing_allowance") or 0)
NET = BASE * (1 - float(P.get("deduct_rate", 0.21))) + ALLOW if BASE else float(P.get("net") or 0) or None
GROSS = BASE + ALLOW if BASE else None
UW_GROSS = BASE + ALLOW * float(P.get("gross_up", 1.0)) if BASE else None

def pmt(p, rate, n=360): r = rate / 1200; return p * r / (1 - (1 + r) ** -n)

def rates():
    src = C.get("rate_source") or {}
    t = (get(src["url"], timeout=25) or "") if src.get("url") else ""
    tx = re.sub(r"\s+", " ", re.sub("<[^>]+>", " ", t))
    m = re.search(src.get("regex", r"(?!x)x"), tx)
    return (float(m.group(1)) if m else None, float(m.group(2)) if m and m.lastindex and m.lastindex >= 2 else None)

def cost(price, rate, hoa=None):
    pi = pmt(price * (1 + FEE), rate); tax = price * TAX_RATE / 12; h = HOA_DEFAULT if hoa is None else hoa
    return {"pi": pi, "tax": tax, "ins": INS_MO, "hoa": h, "total": pi + tax + INS_MO + h}

def max_price(budget, rate):
    lo, hi = 50_000, 900_000
    for _ in range(40):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if cost(mid, rate)["total"] <= budget else (lo, mid)
    return lo

def other_debt():
    try: v = float(json.load(open("state/auto.json"))["payment"])
    except Exception: v = float(C.get("auto_payment_default", 0))
    return v + float(C.get("monthly_debts", 0))

def zillow_zip(url):
    out = {}
    if not ZIPS: return out
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=180) as r:
            rd = csv.reader(io.TextIOWrapper(r, encoding="utf-8")); hdr = next(rd); zi = hdr.index("RegionName")
            for row in rd:
                if row[zi] in ZIPS:
                    v = [float(x) for x in row[-13:] if x]
                    if v: out[row[zi]] = (v[-1], 100 * (v[-1] / v[0] - 1) if len(v) >= 13 else None)
    except Exception as e: log("zillow fail", type(e).__name__)
    return out

def sales(poly):
    q = {"al": 1, "market": MKT, "num_homes": 350, "ord": "price-asc", "page_number": 1, "poly": poly, "status": 9,
         "uipt": "1,2,3", "v": 8, "num_beds": C.get("beds", 3), "num_baths": C.get("baths", 2), "min_price": MIN_P, "max_price": MAX_P}
    t = get("https://www.redfin.com/stingray/api/gis-csv?" + urllib.parse.urlencode(q), timeout=40) or ""
    rows = []
    for r in csv.DictReader(io.StringIO(t)):
        if not r.get("PRICE"): continue
        f = lambda k: float(r.get(k) or 0) or None
        mls, ds = r.get("MLS#") or "", PHOTO_SRC.get(r.get("SOURCE") or "")
        rows.append({"price": float(r["PRICE"]), "lot": f("LOT SIZE"), "hoa": f("HOA/MONTH"), "sqft": f("SQUARE FEET"),
                     "dom": int(r["DAYS ON MARKET"]) if (r.get("DAYS ON MARKET") or "").isdigit() else None, "yr": r.get("YEAR BUILT") or "",
                     "addr": r["ADDRESS"], "city": r["CITY"], "bd": r["BEDS"], "ba": r["BATHS"], "type": r["PROPERTY TYPE"],
                     "url": next((v for k, v in r.items() if k and k.startswith("URL")), ""),
                     "photo": f"https://ssl.cdn-redfin.com/photo/{ds}/mbphotov3/{mls[-3:]}/genMid.{mls}_0.jpg" if ds and mls.isdigit() else None})
    return rows

def rentals(poly):
    q = {"al": 1, "isRentals": "true", "market": MKT, "num_homes": 350, "poly": poly, "num_beds": C.get("beds", 3),
         "num_baths": C.get("baths", 2), "v": 8, "ord": "price-asc", "page_number": 1, "uipt": "1,2,3,4"}
    t = get("https://www.redfin.com/stingray/api/v1/search/rentals?" + urllib.parse.urlencode(q), timeout=40) or "{}"
    try: homes = json.loads(t).get("homes", [])
    except Exception: homes = []
    out = []
    for h in homes:
        hd, rx = h.get("homeData", {}), h.get("rentalExtension", {})
        rent = (rx.get("rentPriceRange") or {}).get("min"); beds = (rx.get("bedRange") or {}).get("max") or 0
        baths = (rx.get("bathRange") or {}).get("max") or 0
        if not rent or rent < C.get("min_rent", 1200) or beds < C.get("beds", 3) or baths < C.get("baths", 2): continue
        a = hd.get("addressInfo", {}); rid = rx.get("rentalId")
        ranges = (hd.get("photosInfo") or {}).get("photoRanges") or []
        ver = ranges[0].get("version") if ranges else None
        out.append({"rent": rent, "kind": {5: "Apt", 13: "Townhouse", 3: "Condo", 6: "House"}.get(hd.get("propertyType"), "Home"),
                    "addr": a.get("formattedStreetLine", ""), "zip": a.get("zip", ""), "bd": beds, "ba": baths,
                    "sqft": (rx.get("sqftRange") or {}).get("max"), "name": rx.get("propertyName") or "",
                    "url": "https://www.redfin.com" + (hd.get("url") or ""),
                    "photo": f"https://ssl.cdn-redfin.com/photo/rent/{rid}/mbphoto/genMid.0_{ver}.jpg" if rid and ver else None})
    return out

def card(title, url, desc, photo, color):
    e = {"title": title[:250], "url": url, "description": desc[:1000], "color": color}
    if photo: e["image"] = {"url": photo}
    return e

def run():
    rate, rate_prev = rates(); rate = rate or C.get("rate_fallback", 7.0)
    debts = other_debt()
    S = [sales(a["poly"]) for a in AREAS]; R = [rentals(a["poly"]) for a in AREAS]
    zhvi = zillow_zip("https://files.zillowstatic.com/research/public_csvs/zhvi/Zip_zhvi_bdrmcnt_3_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv")
    zori = zillow_zip("https://files.zillowstatic.com/research/public_csvs/zori/Zip_zori_uc_sfrcondomfr_sm_month.csv")
    log(f"homes sales={[len(x) for x in S]} rentals={[len(x) for x in R]} zhvi={len(zhvi)} zori={len(zori)}")
    L = C.get("labels") or {}

    if NET:
        cap = min(NET * 0.35, NET - debts - RESID - 1900 * UTIL, (UW_GROSS * 0.41 - debts) if UW_GROSS else 9e9)
        ceiling, sweet = max_price(cap, rate), max_price(NET * 0.30, rate)
        allow_cover = max_price(HOUSING_ALLOW, rate) if HOUSING_ALLOW else None
        plan = ((f"Projected gross **{money(GROSS)}/mo** · est. take-home ≈ {money(NET)}\n" if GROSS else f"Take-home **{money(NET)}/mo**\n") +
                f"Other debts ≈ {money(debts)}\n" + (f"Housing allowance alone covers **≤{money(allow_cover)}** · " if allow_cover else "") +
                f"Comfort (30% net): **≤{money(sweet)}** · Stretch: **≤{money(ceiling)}** → ≈{money(cap)}/mo")
    else:
        sweet, ceiling, plan = MIN_P, MAX_P, "Income not configured."
    fields = [{"name": "Rates", "value": f"{L.get('rate', '30Y')} **{rate:.2f}%**" + (f" (yr ago {rate_prev:.2f}%)" if rate_prev else ""), "inline": True},
              {"name": "Your range", "value": plan, "inline": False}]
    desc, cards = [], []
    for a, rows in zip(AREAS, S):
        inr = [r for r in rows if r["price"] <= ceiling]; pr = [r["price"] for r in rows]
        pool = [r for r in inr if (r["lot"] or 0) <= 8000 and (r["yr"] or "0").isdigit() and int(r["yr"] or 0) >= 1995] or inr
        best = sorted(pool, key=lambda r: r["price"] / (r["sqft"] or 1e9))[:3]
        cap_s = "+" if len(rows) >= 350 else ""
        desc.append(f"**{a['name']}** — {a.get('note', '')}\n{len(rows)}{cap_s} active · median {money(st.median(pr)) if pr else '—'} · {len(inr)} ≤{money(ceiling)}")
        for r in best:
            c = cost(r["price"], rate, r["hoa"]); lot = f"{r['lot']:,.0f} sf lot" if r["lot"] else r["type"]
            desc.append(f"• [{r['addr']}, {r['city']}]({r['url']}) · **{money(r['price'])}** · {r['bd']}/{r['ba']} · {int(r['sqft'] or 0):,} sf · {lot} · {r['yr'] or '?'} · ≈{money(c['total'])}/mo")
            cards.append(card(f"{r['addr']}, {r['city']} — {money(r['price'])}", r["url"],
                              f"{a['name']} · {r['bd']} bd / {r['ba']} ba · {int(r['sqft'] or 0):,} sf · {lot} · built {r['yr'] or '?'} · {r['dom'] or '?'} days listed\n≈ **{money(c['total'])}/mo** all-in at {rate:.2f}%",
                              r["photo"], 0x199E70))
    zl = [f"{ZIPS[z]} {z}: {money(v)}" + (f" ({y:+.1f}%)" if y is not None else "") for z, (v, y) in sorted(zhvi.items(), key=lambda kv: kv[1][0])]
    if zl: fields.append({"name": "Typical 3-bed value (Zillow, YoY)", "value": "\n".join(zl), "inline": False})
    if L.get("cost_basis"): fields.append({"name": "Cost basis", "value": L["cost_basis"], "inline": False})
    post(hook("home_buy"), {"username": "Home — Buy", "avatar_url": AV("1f3e1"), "embeds": [
        {"title": f"🏡 BUY — {L.get('board', 'Home board')} · {NOW:%B %Y}", "color": 0x199E70, "description": "\n".join(desc)[:4000],
         "fields": fields, "footer": {"text": L.get("buy_footer", "Redfin active listings · Zillow ZHVI")}}]})
    if cards: post(hook("home_buy"), {"username": "Home — Buy", "avatar_url": AV("1f3e1"), "embeds": cards[:9]})

    rdesc, rf, rcards = [], [], []
    for a, rows in zip(AREAS, R):
        rs = [r["rent"] for r in rows]
        rdesc.append(f"**{a['name']}** — {len(rows)} listed · median {money(st.median(rs)) if rs else '—'}")
        for r in sorted(rows, key=lambda r: r["rent"])[:3]:
            rdesc.append(f"• [{r['addr']}]({r['url']}) {r['zip']} · **{money(r['rent'])}/mo** · {r['bd']}/{r['ba']:g} · {r['kind']}")
            rcards.append(card(f"{(r['name'] + ' — ') if r['name'] else ''}{r['addr']} — {money(r['rent'])}/mo", r["url"],
                               f"{a['name']} · {r['bd']} bd / {r['ba']:g} ba · {r['kind']}" + (f" · {int(r['sqft']):,} sf" if r.get("sqft") else ""),
                               r["photo"], 0x3987E5))
    allr = [r["rent"] for v in R for r in v]
    if allr and HOUSING_ALLOW:
        rf.append({"name": "Rent vs allowance", "value": f"Median 3/2 rent {money(st.median(allr))} vs housing allowance {money(HOUSING_ALLOW)} → {money(HOUSING_ALLOW - st.median(allr))}/mo left", "inline": False})
    zr = [f"{ZIPS[z]} {z}: {money(v)}" + (f" ({y:+.1f}%)" if y is not None else "") for z, (v, y) in sorted(zori.items(), key=lambda kv: kv[1][0])]
    if zr: rf.append({"name": "Typical rent (Zillow ZORI, YoY)", "value": "\n".join(zr), "inline": False})
    post(hook("home_rent"), {"username": "Home — Rent", "avatar_url": AV("1f511"), "embeds": [
        {"title": f"🔑 RENT — {L.get('board', 'Home board')} · {NOW:%B %Y}", "color": 0x3987E5, "description": "\n".join(rdesc)[:4000], "fields": rf,
         "footer": {"text": L.get("rent_footer", "Redfin rentals")}}]})
    if rcards: post(hook("home_rent"), {"username": "Home — Rent", "avatar_url": AV("1f511"), "embeds": rcards[:9]})

if __name__ == "__main__":
    run()
