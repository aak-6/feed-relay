#!/usr/bin/env python3
"""Generic RSS/JSON -> Discord webhook relay. Stdlib only.
All targets, labels, locations and webhook URLs come from one encrypted config (env FEED_CONFIG, or a local
config.local.json that is never committed). This file contains no personal data."""
import json, os, re, sys, time, html, hashlib, datetime as dt, urllib.request, urllib.parse, urllib.error
from email.utils import parsedate_to_datetime

def _load_cfg():
    raw = os.environ.get("FEED_CONFIG")
    if not raw and os.path.exists("config.local.json"):
        raw = open("config.local.json").read()
    try: return json.loads(raw or "{}")
    except Exception: return {}
CFG = _load_cfg()
def hook(name): return (CFG.get("hooks") or {}).get(name)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
STATE = os.environ.get("STATE_FILE", "state/seen.json")
DRY = os.environ.get("DRY_RUN") == "1"
NOW = dt.datetime.now(dt.timezone.utc)
MAX_AGE_H = 48
MAX_POSTS = 6  # per channel per run - anti-flood

BLOCK = re.compile(CFG.get("block_re") or r"(?!x)x", re.I)

def log(*a): print(*a, file=sys.stderr, flush=True)

def get(url, timeout=20, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(3 * (i + 1)); continue
            log(f"GET {e.code} {urllib.parse.urlsplit(url).netloc}"); return None
        except Exception as e:
            if i < tries - 1: time.sleep(2); continue
            log(f"GET fail {type(e).__name__} {urllib.parse.urlsplit(url).netloc}"); return None

def post(hook, payload):
    if not hook:
        log("post skipped: no webhook configured"); return False
    if DRY:
        log("DRY ->", json.dumps(payload)[:300]); return True
    data = json.dumps(payload).encode()
    for i in range(6):
        try:
            req = urllib.request.Request(hook, data=data, headers={"Content-Type": "application/json", "User-Agent": UA})
            urllib.request.urlopen(req, timeout=20).read(); return True
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try: wait = float(json.loads(e.read().decode()).get("retry_after", 2))
                except Exception: wait = 2
                time.sleep(min(wait + 0.5, 30)); continue
            if e.code >= 500: time.sleep(2 * (i + 1)); continue
            log(f"webhook HTTP {e.code}"); return False
        except Exception as e:
            log(f"webhook err {type(e).__name__}"); time.sleep(2 * (i + 1))
    return False

def load_state():
    try:
        with open(STATE) as f: return json.load(f)
    except Exception: return {"seen": {}, "rr": 0}

def save_state(s):
    cutoff = (NOW - dt.timedelta(days=21)).timestamp()
    s["seen"] = {k: v for k, v in s["seen"].items() if v > cutoff}
    os.makedirs(os.path.dirname(STATE) or ".", exist_ok=True)
    with open(STATE, "w") as f: json.dump(s, f)

def clean(t):
    t = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", t or "", flags=re.S)
    for _ in range(3):                                   # some feeds double/triple-encode HTML (dealnews)
        t = re.sub(r"<[^>]+>", " ", html.unescape(t))
    return re.sub(r"\s+", " ", t).replace("\u2019", "'").strip()

def parse_feed(xml, source):
    out = []
    if not xml: return out
    items = re.findall(r"<item\b.*?>(.*?)</item>", xml, re.S) or re.findall(r"<entry\b.*?>(.*?)</entry>", xml, re.S)
    for it in items:
        def tag(n):
            m = re.search(rf"<{n}\b[^>]*>(.*?)</{n}>", it, re.S); return m.group(1) if m else ""
        title = clean(tag("title"))
        link = clean(tag("link"))
        if not link:
            m = re.search(r'<link[^>]*href="([^"]+)"', it); link = html.unescape(m.group(1)) if m else ""
        link = re.sub(r"[?&]utm_[^&]+", "", link)
        when = None
        for n in ("pubDate", "updated", "published"):
            v = clean(tag(n))
            if v:
                try: when = parsedate_to_datetime(v)
                except Exception:
                    try: when = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
                    except Exception: pass
                break
        raw = tag("content:encoded") or tag("content") or ""
        desc = clean(tag("description"))
        thumb = re.search(r"Thumb Score:\s*([+-]?\d+)", raw)
        img = re.search(r"<img[^>]+src=[\"']([^\"']+)[\"']", html.unescape(raw or tag("description")))
        out.append({"title": title, "link": link, "when": when, "desc": desc,
                    "thumb": int(thumb.group(1)) if thumb else None,
                    "img": img.group(1) if img else None, "source": source})
    return out

MONEY = r"\$\s?([\d,]+(?:\.\d{2})?)"
def money(s): return float(s.replace(",", ""))

def price_info(title, desc):
    """Return (price, reg, pct) best-effort."""
    text = f"{title} {desc}"
    price = reg = pct = None
    for m in re.finditer(MONEY + r"(\s+off)?", title):
        if not m.group(2): price = money(m.group(1)); break
    roundup = re.search(r"up to|from \$|sale now|deals? (right now|roundup)|best .* deals", title, re.I)
    r = re.search(r"(?:reg\.?|list|was|orig\.?|msrp)[:\s]*" + MONEY, text, re.I)
    if r: reg = money(r.group(1))
    off = re.search(MONEY + r"\s+off", text, re.I)
    if not reg and off and price: reg = price + money(off.group(1))
    p = re.search(r"(\d{2})%\s*off", text, re.I)
    if roundup: return price, None, None
    if reg and price and reg > price: pct = round(100 * (1 - price / reg))
    elif p: pct = int(p.group(1))
    return price, reg, pct

def sd(q):  # Slickdeals keyword RSS (deals forums only)
    return "https://slickdeals.net/newsearch.php?" + urllib.parse.urlencode(
        {"q": q, "searcharea": "deals", "searchin": "first", "rss": 1})

def fresh(it, hours=MAX_AGE_H):
    return it["when"] is None or (NOW - it["when"]).total_seconds() < hours * 3600

def key(it):
    base = re.sub(r"\W+", "", it["title"].lower())[:80]
    return hashlib.sha1(base.encode()).hexdigest()[:16]

# ---------------- computers ----------------
COMP_Q = ["macbook", "mac mini", "mac studio", "imac", "gaming laptop", "gaming desktop",
          "gaming pc", "rtx 5070", "rtx 5080", "rtx 5090", "prebuilt"]
COMP_FEEDS = [("Slickdeals", sd(q)) for q in COMP_Q] + [
    ("Slickdeals Front Page", "https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1"),
    ("dealnews", "https://www.dealnews.com/c39/Computers/?rss=1"),
    ("9to5Toys", "https://9to5toys.com/feed/"),
]
REDDIT = ["buildapcsales", "laptopdeals", "AppleDeals"]
COMP_IS = re.compile(r"macbook|mac ?mini|mac ?studio|\bimac\b|laptop|notebook|desktop|gaming pc|\bprebuilt\b|\brtx ?50[6-9]0|\bomen\b|legion|\brog\b|alienware|predator|zephyrus|tuf gaming|nitro|victus|\blof?q\b|mini pc", re.I)
COMP_NOT = re.compile(r"\bcase\b|sleeve|charger|\bstand\b|\bdock|\bhub\b|cable|keyboard|mouse|protector|\bskin\b|backpack|\bbag\b|cooling pad|cooler|extender|adapter|\bmonitor\b|webcam|headset|\bram\b|\bssd\b|ddr5|screen|battery|power bank|tablet|ipad|chromebook|software|office|windows 11 key|\bdesk\b|chair|lamp|shelf|mount|\bgpu\b only", re.I)
DEEP_WORDS = re.compile(r"price mistake|price error|all[- ]time low|lowest price|record low|new low|clearance|open[- ]box", re.I)

def comp_score(it):
    t = it["title"]
    if not COMP_IS.search(t) or COMP_NOT.search(t) or BLOCK.search(t): return None
    price, reg, pct = price_info(t, it["desc"])
    if price is not None and price < 250: return None           # junk tier
    th = it["thumb"] or 0
    offm = re.search(r"\$([\d,]{3,})\s+off", t, re.I)
    dollars_off = money(offm.group(1)) if offm else 0
    deep = DEEP_WORDS.search(t) or DEEP_WORDS.search(it["desc"] or "")
    if (pct or 0) >= 25 or th >= 25 or deep or dollars_off >= 300 or (it["source"].startswith("r/") and (pct or 0) >= 20):
        tier = "🔥 EXTREME" if (pct or 0) >= 40 or re.search(r"price (mistake|error)", t, re.I) else "💰 DEEP"
        return {"price": price, "reg": reg, "pct": pct, "tier": tier, "off": dollars_off or None,
                "specs": specs(t + " " + (it["desc"] or "")), "store": store_of(it)}
    return None


SPEC_PATTERNS = [
    ("CPU", r"(Apple M\d(?: Pro| Max| Ultra)?|\bM[1-6](?: Pro| Max| Ultra)?\b|Core Ultra \d \d{3}\w*|(?:Intel )?Core i[3579][- ]\d{4,5}\w*|Ryzen (?:AI )?\d(?: \w+)? \d{3,4}\w*|Snapdragon X\w* ?\w*)"),
    ("GPU", r"((?:GeForce )?RTX ?\d{4}(?: ?Ti)?(?: SUPER)?|Radeon RX ?\d{4}\w*|Arc [AB]\d{3})"),
    ("RAM", r"(\d{1,3}\s?GB(?= (?:DDR\d|RAM|LPDDR|unified|memory|/)|\s?RAM)|\d{1,3}GB/\d)"),
    ("Storage", r"(\d(?:\.\d)?\s?TB(?: SSD)?|\d{3,4}\s?GB SSD|(?<=/)\d{3,4}\s?GB)"),
    ("Screen", r"(1[0-8](?:\.\d)?(?:\"|-inch| inch|\u201d)[^,;|]{0,30}?(?:\d{3,4}p|OLED|IPS|\d{2,3}\s?Hz|QHD|FHD|4K|2\.5K|3K|Retina)?)"),
]
def specs(text):
    out = []
    for name, pat in SPEC_PATTERNS:
        m = re.search(pat, text, re.I)
        if m:
            v = m.group(1).strip().rstrip("/")
            if name == "RAM" and "/" in v: v = v.split("/")[0]
            out.append(f"**{name}** {v}")
    return " · ".join(out)

def store_of(it):
    m = re.search(r"^\s*([A-Z][\w&.' -]{1,40}?)\s+(?:\[[\w.]+\]\s+)?(?:has|via|is offering|offers)\b", it.get("desc") or "")
    if m: return m.group(1).strip()
    m = re.search(r"\b(?:at|@|from)\s+(Amazon|Best ?Buy|Walmart|Newegg|B&H|Costco|Target|Micro Center|Adorama|Dell|HP|Lenovo|Apple|Woot|eBay|Antonline)\b", it["title"], re.I)
    return m.group(1) if m else None

# ---------------- food / grocery ----------------
_F = CFG.get("food") or {}
FOOD_BRANDS = _F.get("brands") or ["doordash", "dashpass", "grubhub", "uber eats", "ubereats", "uber one", "instacart", "gopuff"]
FOOD_FEEDS = [("Slickdeals", sd(q)) for q in (_F.get("queries") or ["doordash", "grubhub", "uber eats", "instacart"])]
FOOD_BRAND_RE = re.compile("|".join(re.escape(b).replace(r"\ ", r"\s?") for b in FOOD_BRANDS), re.I)
FOOD_PROMO = re.compile(r"\boff\b|free|promo|code|credit|%|back|bogo|\bdeal\b|coupon|membership|trial|\$\d", re.I)
FOOD_NOT = re.compile(r"365 by|\[sns\]|subscribe ?& ?save|airpods|iphone|nintendo|switch|playstation|xbox|gift ?cards?,|lego", re.I)

def food_score(it):
    t = it["title"]
    if BLOCK.search(t) or FOOD_NOT.search(t): return None
    b = FOOD_BRAND_RE.search(t)
    if not b or not FOOD_PROMO.search(t): return None
    ymmv = bool(re.search(r"ymmv|targeted|select (accounts|users)|new (customers|users)|first order", t + " " + it["desc"], re.I))
    return {"brand": b.group(0), "ymmv": ymmv}

# ---------------- runner ----------------
def collect(feeds):
    items = []
    for src, url in feeds:
        items += parse_feed(get(url), src)
        time.sleep(0.6)
    return items

def embed_deal(it, sc, color):
    f = []
    if sc.get("price") is not None: f.append({"name": "Price", "value": f"${sc['price']:,.2f}", "inline": True})
    if sc.get("reg"): f.append({"name": "Reg", "value": f"${sc['reg']:,.0f}", "inline": True})
    if sc.get("off") and not sc.get("pct"): f.append({"name": "Off", "value": f"${sc['off']:,.0f}", "inline": True})
    if sc.get("pct"): f.append({"name": "Off", "value": f"{sc['pct']}%", "inline": True})
    if it["thumb"] is not None: f.append({"name": "Thumbs", "value": f"{it['thumb']:+d}", "inline": True})
    if sc.get("ymmv"): f.append({"name": "Note", "value": "YMMV / targeted", "inline": True})
    if sc.get("store"): f.append({"name": "Store", "value": sc["store"][:60], "inline": True})
    if sc.get("price") is not None and sc.get("reg"):
        f.append({"name": "You save", "value": f"${sc['reg'] - sc['price']:,.0f}", "inline": True})
    desc = []
    if sc.get("specs"): desc.append(sc["specs"])
    if it.get("desc"): desc.append("> " + re.sub(r"\s+", " ", it["desc"])[:280])
    e = {"title": it["title"][:250], "url": it["link"], "color": color, "fields": f,
         "description": "\n".join(desc)[:1000] or None,
         "footer": {"text": it["source"]}}
    if not e.get("description"): e.pop("description", None)
    if it["when"]: e["timestamp"] = it["when"].astimezone(dt.timezone.utc).isoformat()
    if it["img"] and it["img"].startswith("https://"): e["thumbnail"] = {"url": it["img"]}
    return e

def run_deals():
    s = load_state(); first = not s["seen"]
    comp_hook = hook("computers"); food_hook = hook("food")
    comp = collect(COMP_FEEDS)
    sub = REDDIT[s.get("rr", 0) % len(REDDIT)]; s["rr"] = s.get("rr", 0) + 1   # reddit rate-limits per host: one per run
    comp += parse_feed(get(f"https://www.reddit.com/r/{sub}/new/.rss"), f"r/{sub}")
    food = collect(FOOD_FEEDS)
    stats = {}
    for name, items, scorer, hk, user, avatar, color in [
        ("computers", comp, comp_score, comp_hook, "Computer Deals", "1f4bb", 0x3987E5),
        ("food", food, food_score, food_hook, "Food & Grocery Promos", "1f354", 0xD95926)]:
        hits = []
        for it in items:
            if not it["title"] or not fresh(it, 96 if name == "food" else MAX_AGE_H): continue
            k = key(it)
            if k in s["seen"]: continue
            sc = scorer(it)
            if sc is None: continue
            s["seen"][k] = NOW.timestamp(); hits.append((it, sc))
        hits.sort(key=lambda x: (-(x[1].get("pct") or 0), -(x[0]["thumb"] or 0)))
        if first: hits = hits[:3]   # seed run: prove the pipe, don't flood
        sent = 0
        for it, sc in hits[:MAX_POSTS]:
            content = f"**{sc['tier']}**" if sc.get("tier") else None
            ok = post(hk, {"username": user,
                             "avatar_url": f"https://cdn.jsdelivr.net/gh/jdecked/twemoji@latest/assets/72x72/{avatar}.png",
                             "content": content, "embeds": [embed_deal(it, sc, color)]})
            sent += ok; time.sleep(1.2)
        stats[name] = {"scanned": len(items), "new_hits": len(hits), "posted": sent}
    save_state(s); log(json.dumps(stats))

# ---------------- hotels ----------------
# Config "hotels": {"geos": {label: tripadvisor_geo}, "tax": 1.15, "note": str,
#   "programs": [{"id","label","credit","nights","match_re","exclude_re","min_rating","max_list_min","color","footer","cert":bool}]}
H = CFG.get("hotels") or {}
AV_HOTEL = "https://cdn.jsdelivr.net/gh/jdecked/twemoji@latest/assets/72x72/1f3e8.png"

def xotelo(path, **q):
    t = get("https://data.xotelo.com/api/" + path + "?" + urllib.parse.urlencode(q), timeout=25)
    try: return (json.loads(t) or {}).get("result") if t else None
    except Exception: return None

def stay_windows(n):
    """n=1: Fri & Sat single nights; n>=2: Fri and Sun check-ins for n nights; next ~10 weeks."""
    d, out = NOW.date() + dt.timedelta(days=2), []
    while d < NOW.date() + dt.timedelta(days=75):
        if (n == 1 and d.weekday() in (4, 5)) or (n >= 2 and d.weekday() in (4, 6)):
            out.append((d, d + dt.timedelta(days=n)))
        d += dt.timedelta(days=1)
    return out[:int(H.get('windows', 12))]

def run_hotels():
    if not H.get("geos"): log("hotels: no config"); return
    from concurrent.futures import ThreadPoolExecutor
    pool, seen = [], set()
    for g in H["geos"].values():
        for off in (0, 100):
            for h in ((xotelo("list", location_key=g, limit=100, offset=off, sort="best_value") or {}).get("list") or []):
                if h["key"] in seen: continue
                seen.add(h["key"]); rv = h.get("review_summary") or {}; pr = h.get("price_ranges") or {}
                city = next((k for k, v in H["geos"].items() if h["key"].startswith(v + "-")), "")
                pool.append({"city": city, "key": h["key"], "name": h["name"], "rating": rv.get("rating") or 0,
                             "reviews": rv.get("count") or 0, "min": pr.get("minimum") or 9999, "max": pr.get("maximum") or 0,
                             "url": h.get("url"), "img": h.get("image") or ""})
            time.sleep(1)
    tax = float(H.get("tax") or 1.15)
    progs = H.get("programs") or []
    cands = {}
    for p in progs:
        inc = re.compile(p.get("match_re") or ".", re.I); exc = re.compile(p.get("exclude_re") or r"(?!x)x", re.I)
        c = [h for h in pool if inc.search(h["name"]) and not exc.search(h["name"]) and h["rating"] >= p.get("min_rating", 0)
             and h["reviews"] >= p.get("min_reviews", 0) and h["min"] <= p.get("max_list_min", 99999)]
        cands[p["id"]] = sorted(c, key=lambda h: -h["rating"])[:p.get("max_hotels", 25)]
    jobs = [(p, h, w) for p in progs for h in cands[p["id"]] for w in stay_windows(int(p.get("nights", 1)))]
    def price(job):
        p, h, (ci, co) = job
        r = xotelo("rates", hotel_key=h["key"], chk_in=ci.isoformat(), chk_out=co.isoformat())
        rates = [x["rate"] for x in (r or {}).get("rates", []) if x.get("rate")]
        if not rates: return None
        nightly = min(rates)
        if h["max"] and nightly > h["max"] * 1.5: return None          # stale/outlier quote
        n = (co - ci).days; total = round(nightly * n * tax)
        credit = total if p.get("cert") else float(p.get("credit", 0))
        stack = bool(p.get("bonus_re") and re.search(p["bonus_re"], h["name"], re.I))
        if stack: credit += float(p.get("bonus_credit", 0))
        return {**h, "pid": p["id"], "ci": ci, "co": co, "n": n, "nightly": nightly, "total": total, "stack": stack,
                "credit": credit, "oop": max(round(total - credit), 0)}
    with ThreadPoolExecutor(max_workers=8) as ex:
        rows = [r for r in ex.map(price, jobs) if r]
    log(f"hotels pool={len(pool)} jobs={len(jobs)} priced={len(rows)}")

    embeds, free_hits = [], 0
    for p in progs:
        per = {}
        keyf = (lambda r: -r["total"]) if p.get("cert") else (lambda r: (r["oop"], -r["rating"]))
        for r in rows:
            if r["pid"] == p["id"] and (r["key"] not in per or keyf(r) < keyf(per[r["key"]])): per[r["key"]] = r
        top = sorted(per.values(), key=keyf)[:6]
        lines = []
        for r in top:
            day = f"{r['ci']:%a %b %-d}" + (f"→{r['co']:%a %-d}" if r["n"] > 1 else "")
            if p.get("cert"):
                lines.append(f"• [{r['name'][:46]}]({r['url']}) · {r['city']} · {day} · saves **${r['total']:,}** · 🟢 $0")
            else:
                free_hits += r["oop"] == 0
                tag = "🟢 **$0 out of pocket**" if r["oop"] == 0 else f"you pay **${r['oop']:,}**"
                stk = f" · 🔗 stacks ${r['credit']:,.0f}" if r.get("stack") else ""
                lines.append(f"• [{r['name'][:46]}]({r['url']}) · {r['city']} · {day} · ${r['nightly']:,.0f}/nt → ${r['total']:,} all-in · {tag}{stk} · ★{r['rating']}")
        e = {"title": p.get("label", p["id"]), "color": int(p.get("color", 0x3987E5)),
             "description": "\n".join(lines) or "_Nothing in range this week._", "footer": {"text": p.get("footer", "")[:2000]}}
        if top and top[0]["img"].startswith("https://"): e["thumbnail"] = {"url": top[0]["img"]}
        embeds.append(e)
    head = (f"🟢 **{free_hits} zero-spend stay(s)** — {NOW:%a %b %-d}" if free_hits else f"{NOW:%a %b %-d} — no $0 stays today; closest options below")
    if H.get("note"): head += "\n_" + H["note"] + "_"
    post(hook("hotels"), {"username": "Hotel Credits", "avatar_url": AV_HOTEL, "content": head, "embeds": embeds[:10]})

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "deals"
    {"deals": run_deals, "hotels": run_hotels}[mode]()
