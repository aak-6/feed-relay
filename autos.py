#!/usr/bin/env python3
"""Monthly used-vehicle watch -> Discord. Stdlib only. Models, allowed years, search center, radius, lender
and labels come from FEED_CONFIG["autos"]; listing API key from FEED_CONFIG["autos"]["key"].
Writes state/auto.json (estimated payment) for listings.py."""
import json, math, os, re, statistics as st, time, urllib.parse, urllib.request, urllib.error, datetime as dt
from relay import get, post, log, UA, CFG, hook

NOW = dt.datetime.now(dt.timezone.utc)
A = CFG.get("autos") or {}
MODELS = A.get("models") or []          # [{"make","model","years":[...],"recall_names":{"2021":"Model%20Hybrid"}}]
KEY, MAX_MI, PICK_MI = A.get("key") or os.environ.get("LISTINGS_API_KEY"), A.get("max_miles", 100_000), A.get("pick_miles", 90_000)
CEN = A.get("center") or {}             # {"zip","lat","lng","radius"}
money = lambda x: f"${x:,.0f}"
IMG = re.compile(r"https://[^\s\"']+\.(?:jpg|jpeg|png|webp)(?:\?[^\s\"']*)?", re.I)

def rates():
    out = {}
    t = get("https://www.bankrate.com/loans/auto-loans/rates/") or ""
    tx = re.sub(r"\s+", " ", re.sub("<[^>]+>", " ", t))
    m = re.search(r"60-month new car (\d+\.\d+)% 48-month new car (\d+\.\d+)% 48-month used car (\d+\.\d+)% 36-month used car (\d+\.\d+)%", tx)
    if m: out.update(new60=float(m.group(1)), used48=float(m.group(3)), used36=float(m.group(4)))
    m = re.search(r"781 to 850 \(super prime\) (\d+\.\d+)% (\d+\.\d+)% 661 to 780 \(prime\) (\d+\.\d+)% (\d+\.\d+)%", tx)
    if m: out.update(sp_used=float(m.group(2)), prime_used=float(m.group(4)))
    L = A.get("lender") or {}
    if L.get("url"):
        m = re.search(L.get("regex", r"as low as (\d+\.\d+)% APR"), re.sub("<[^>]+>", " ", get(L["url"]) or ""))
        if m: out["lender"] = float(m.group(1))
    return out

def listings(md):
    ys = md["years"]
    q = {"vehicle.make": md["make"], "vehicle.model": md["model"], "vehicle.year": f"{min(ys)}-{max(ys)}",
         "zip": CEN.get("zip", ""), "distance": CEN.get("radius", 100), "limit": 20}
    rows, seen = [], set()
    for page in range(1, 6):
        q["page"] = page
        try:
            req = urllib.request.Request("https://api.auto.dev/listings?" + urllib.parse.urlencode(q),
                                         headers={"Authorization": f"Bearer {KEY}", "User-Agent": UA})
            d = json.loads(urllib.request.urlopen(req, timeout=30).read())
        except urllib.error.HTTPError as e: log("listings HTTP", e.code); break
        except Exception as e: log("listings", type(e).__name__); break
        items = d.get("data") or []
        if not items: break
        for it in items:
            vin = it.get("vin") or json.dumps(it.get("retailListing") or {})[:80]
            if vin in seen: continue
            seen.add(vin)
            v, rl = it.get("vehicle") or {}, it.get("retailListing") or {}
            try: p, y = float(rl.get("price") or it.get("price")), int(v.get("year") or it.get("year"))
            except Exception: continue
            mi = v.get("mileage") or v.get("miles") or rl.get("miles")
            if y not in ys or not (8000 < p < 80000) or (isinstance(mi, (int, float)) and mi > MAX_MI): continue
            dl = it.get("dealer") if isinstance(it.get("dealer"), dict) else {}
            loc = it.get("location"); lc = loc if isinstance(loc, dict) else {}
            dist = None
            if isinstance(loc, list) and len(loc) == 2 and CEN.get("lat"):
                dist = round(math.hypot((loc[0] - CEN["lng"]) * 60.0, (loc[1] - CEN["lat"]) * 69.0))
            imgs = IMG.findall(json.dumps(it))
            rows.append({"p": p, "y": y, "trim": v.get("trim") or "", "mi": mi, "dist": dist,
                         "city": dl.get("city") or lc.get("city") or rl.get("city") or "",
                         "url": rl.get("vdp") or rl.get("vdpUrl") or rl.get("url") or "", "img": imgs[0] if imgs else None})
        time.sleep(0.5)
    return rows

def recalls(md):
    out = {}
    for y in md["years"]:
        name = (md.get("recall_names") or {}).get(str(y), md["model"])
        t = get(f"https://api.nhtsa.gov/recalls/recallsByVehicle?make={md['make']}&model={name}&modelYear={y}", timeout=20)
        try: out[y] = len({r.get("NHTSACampaignNumber") for r in json.loads(t or "{}").get("results", [])})
        except Exception: pass
    return out

def run():
    if not MODELS or not KEY: log("autos: not configured"); return
    R = rates(); used = R.get("used48") or 7.5
    lines, mids, cards = [], [], []
    for md in MODELS:
        rows = listings(md)
        if not rows:
            lines.append(f"**{md['make']} {md['model']}** — no listings this month"); continue
        prices = [r["p"] for r in rows]; med = st.median(prices); mids.append(med)
        py = {}
        for r in rows: py.setdefault(r["y"], []).append(r["p"])
        rc = recalls(md)
        lines.append(f"**{md['make']} {md['model']} ({', '.join(map(str, md['years']))}) ≤{MAX_MI // 1000}k mi** — avg **{money(st.mean(prices))}** · median {money(med)} · n={len(prices)}\n"
                     + " · ".join(f"{y}: {money(st.median(py[y]))}" for y in sorted(py))
                     + (("\nNHTSA recall campaigns (fixed free at dealer — confirm by VIN): " + " · ".join(f"{y}: {n}" for y, n in sorted(rc.items()))) if rc else ""))
        bycity = {}
        for r in rows:
            if r["city"]: bycity.setdefault(r["city"], []).append(r["p"])
        cheap = sorted(((st.median(v), c, len(v)) for c, v in bycity.items() if len(v) >= 2))[:4]
        if cheap: lines.append("Cheapest markets (median): " + " · ".join(f"{c} {money(m)} (n={n})" for m, c, n in cheap))
        sane = [r for r in rows if isinstance(r["mi"], (int, float)) and r["mi"] <= PICK_MI] or rows
        for r in sorted(sane, key=lambda r: r["p"])[:3]:
            mi = f"{int(r['mi']):,} mi" if isinstance(r["mi"], (int, float)) else "mi n/a"
            where = r["city"] + (f" ({r['dist']} mi)" if r["dist"] is not None else "")
            e = {"title": f"{r['y']} {md['make']} {md['model']} {r['trim']} — {money(r['p'])}"[:250], "color": 0xC98500,
                 "description": f"{mi} · {where}"}
            if r["url"].startswith("http"): e["url"] = r["url"]
            if r["img"]: e["image"] = {"url": r["img"]}
            cards.append(e)
    mid = st.median(mids) if mids else float(A.get("price_default", 27000))
    tax = float(A.get("sales_tax", 0.0625)); down = float(A.get("down_pct", 0.10))
    fin = mid * (1 - down) * (1 + tax)
    pay = lambda apr, n: fin * (apr / 1200) / (1 - (1 + apr / 1200) ** -n)
    os.makedirs("state", exist_ok=True); json.dump({"payment": round(pay(used, 60)), "price": mid}, open("state/auto.json", "w"))
    rl = []
    if "used48" in R: rl.append(f"Used 48-mo avg **{R['used48']:.2f}%** · 36-mo {R['used36']:.2f}% · new 60-mo {R['new60']:.2f}% (Bankrate)")
    if "sp_used" in R: rl.append(f"Used by credit: super-prime {R['sp_used']:.2f}% · prime {R['prime_used']:.2f}% (Experian)")
    if "lender" in R: rl.append(f"{(A.get('lender') or {}).get('label', 'Lender')} from {R['lender']:.2f}% APR")
    embed = {"title": f"🚐 {A.get('title', 'VEHICLE WATCH')} — {NOW:%B %Y}", "color": 0xC98500, "description": "\n\n".join(lines)[:4000],
             "fields": [{"name": "Rates", "value": "\n".join(rl) or "rates unavailable", "inline": False},
                        {"name": "Payment at median", "value": f"{money(mid)} · {down:.0%} down + {tax:.2%} sales tax → finance {money(fin)}\n60 mo @ {used:.2f}% ≈ **{money(pay(used, 60))}/mo** · 48 mo ≈ {money(pay(used, 48))}/mo", "inline": False}],
             "footer": {"text": A.get("footer", "")[:2000] or "Monthly · dealer listings"}}
    post(hook("autos"), {"username": A.get("username", "Vehicle Watch"), "avatar_url": "https://cdn.jsdelivr.net/gh/jdecked/twemoji@latest/assets/72x72/1f690.png", "embeds": [embed]})
    if cards: post(hook("autos"), {"username": A.get("username", "Vehicle Watch"), "avatar_url": "https://cdn.jsdelivr.net/gh/jdecked/twemoji@latest/assets/72x72/1f690.png", "embeds": cards[:9]})

if __name__ == "__main__":
    run()
