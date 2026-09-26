#!/usr/bin/env python3
"""Generic RSS/JSON -> Discord webhook relay. Stdlib only.
All targets, labels, locations and webhook URLs come from one encrypted config (env FEED_CONFIG, or a local
config.local.json that is never committed). This file contains no personal data."""
import json, os, re, sys, time, html, hashlib, functools, datetime as dt, urllib.request, urllib.parse, urllib.error
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

# ---------------- used-laptop hunter ----------------
# Two watch profiles, both tuned for USED / REFURBISHED machines (replaces the old new-computer deal feed).
#   study  : ThinkPad T480 (not T480s) with i5-8350U or 16GB, at/under a price cap
#   gaming : used gaming laptop in a price band with a GPU/CPU that clears current MMO/FPS minimums
# Thresholds are overridable via FEED_CONFIG["hunter"]; nothing personal lives in this file.
HC = CFG.get("hunter") or {}
STUDY_MAX = float(HC.get("study_max", 215))   # delivered total
STUDY_ON = bool(HC.get("study", False))        # T480 hunt closed (bought); set hunter.study=true to revive
GAME_MIN, GAME_MAX = float(HC.get("game_min", 300)), float(HC.get("game_max", 500))
HUNT_Q = HC.get("slickdeals_q") or (["thinkpad t480"] if STUDY_ON else []) + ["refurbished gaming laptop", "rtx 3060 laptop",
                                    "rtx 3050 laptop", "rtx 4050 laptop", "rtx 4060 laptop", "rtx 5050 laptop",
                                    "ebay coupon", "ebay refurbished coupon"]
COMP_FEEDS = [("Slickdeals", sd(q)) for q in HUNT_Q] + [("dealnews", "https://www.dealnews.com/c39/Computers/?rss=1")]
REDDIT = HC.get("reddit") or ["hardwareswap", "laptopdeals", "hardwareswap", "GameDeals"]

JUNK = re.compile(r"\bparts\b|for parts|as[- ]is|not working|no (?:ssd|hdd|ram|os|battery|charger|storage)|"
                  r"\bbios (?:lock|password)|locked|cracked|broken|damaged|read desc|motherboard|\blcd\b|screen only|"
                  r"\blot of\b|\bboard\b only|replacement|keyboard only|palmrest|\bfan\b only", re.I)
T480 = re.compile(r"\bt480\b(?!s)", re.I)
GOOD_GPU = re.compile(r"\b(?:gtx ?1660 ?ti|rtx ?20[678]0|rtx ?30[5-8]0(?: ?ti)?|rtx ?40[5-9]0|"
                      r"rx ?(?:5600m|5700m|6[5-8]\d0m|6[5-8]\d0s|7600s|7600m))\b", re.I)
WEAK_GPU = re.compile(r"\b(?:gtx ?1050|gtx ?1650|mx ?\d{3}|iris|uhd|vega|radeon graphics)\b", re.I)
SIX_CORE = re.compile(r"\b(?:i[579][- ]?(?:8750|8850|9750|9850|10750|10850|10870|10875|10980)h\w*|"
                      r"i[579][- ]?1[1-4]\d{3}h\w*|i7[- ]?11800h|"
                      r"ryzen ?[579](?: pro)? ?[4-8]\d{3}h\w*|r[579][- ]?[4-8]\d{3}h\w*|"
                      r"core ?(?:ultra )?[579] ?\d{3}h\w*)\b", re.I)
FOUR_CORE_H = re.compile(r"\bi5[- ]?(?:8300|9300|10300)h\b|\bryzen ?5 ?3550h\b", re.I)
PRICE_ANY = re.compile(r"\$\s?([\d,]{2,5}(?:\.\d{2})?)")

def first_price(t):
    """hardwareswap titles: [H] ... [W] PayPal, $450 shipped -> take the $ after [W] if present, else first $."""
    w = re.split(r"\[W\]", t, flags=re.I)
    for part in ([w[1]] if len(w) > 1 else []) + [t]:
        m = PRICE_ANY.search(part)
        if m:
            try: return money(m.group(1))
            except Exception: pass
    return None

def comp_score(it):
    t = it["title"]; blob = t + " " + (it.get("desc") or "")
    if BLOCK.search(t): return None
    # ---- eBay tech / refurbished coupon codes (stackable on the listings this scanner finds) ----
    if (re.search(r"\bebay\b", t, re.I) and re.search(r"coupon|promo|\bcode\b|\d+% off|\$\d+ off", t, re.I)
            and re.search(r"refurb|tech|electronic|laptop|computer|sitewide|select|certified", t, re.I)):
        return {"price": None, "tier": "🏷 EBAY CODE", "profile": "coupon",
                "why": "stack on a used/refurb laptop", "notes": ["check expiry, min spend and eligible categories"],
                "specs": "", "store": "eBay"}
    if JUNK.search(t): return None
    if re.search(r"\[W\][^\[]*\b(?:t480|laptop|gpu|rtx)", t, re.I) and not re.search(r"\[H\][^\[]*(?:laptop|t480|rtx|notebook)", t, re.I):
        return None                                   # a WANT post, not a sale
    price = it.get("price") if it.get("price") is not None else first_price(t)
    if price is None and it.get("desc"):
        price = first_price(it["desc"]); body_price = price is not None
    else: body_price = False
    ship = it.get("ship") or 0.0
    total = (price + ship) if price is not None else None
    notes = ["price read from post body, verify"] if body_price else []
    # ---- study profile ----
    if STUDY_ON and T480.search(t):
        good_cpu = re.search(r"8350u", blob, re.I); ram16 = re.search(r"\b(?:16|24|32|40|64) ?gb\b", blob, re.I)
        if not (good_cpu or ram16): return None
        if total is None or total > STUDY_MAX: return None
        if re.search(r"\bfhd\b|1920|1080p|\bips\b", blob, re.I): notes.append("FHD mentioned")
        else: notes.append("verify screen is 1920x1080 IPS")
        if re.search(r"backlit", blob, re.I): notes.append("backlit")
        tier = "🎯 T480 MATCH" if (good_cpu and ram16) else "🟢 T480 candidate"
        return {"price": price, "ship": ship or None, "total": total, "tier": tier, "profile": "study",
                "why": ("i5-8350U" if good_cpu else "") + (" · 16GB+" if ram16 else ""), "notes": notes,
                "specs": specs(blob), "store": store_of(it)}
    # ---- gaming profile ----
    if not re.search(r"laptop|notebook|legion|nitro|victus|\btuf\b|\brog\b|zephyrus|strix|predator|helios|\bomen\b|"
                     r"alienware|\bg1[56]\b|aorus|\bmsi\b|katana|\bgf6[35]\b|ideapad gaming|loq", t, re.I): return None
    if re.search(r"desktop|\btower\b|\bpc\b(?! gaming laptop)", t, re.I) and not re.search(r"laptop|notebook", t, re.I): return None
    gpu = GOOD_GPU.search(blob)
    if not gpu: return None
    if total is None or not (GAME_MIN <= total <= GAME_MAX): return None
    cpu6 = SIX_CORE.search(blob)
    if FOUR_CORE_H.search(blob): return None          # 4-core H chips miss current MMO 6-core minimum
    if cpu6: notes.append("6+ core CPU")
    else: notes.append("verify CPU is 6+ cores")
    if re.search(r"\b(?:16|32) ?gb\b", blob, re.I): notes.append("16GB+")
    elif re.search(r"\b8 ?gb\b", blob, re.I): notes.append("8GB: plan a RAM upgrade")
    g = gpu.group(0).upper().replace("  ", " ")
    strong = re.search(r"30[6-8]0|40[6-9]0|20[78]0|6[6-8]\d0", g)
    tier = "🎮 STRONG" if (strong and cpu6) else "🎮 SOLID"
    return {"price": price, "ship": ship or None, "total": total, "tier": tier, "profile": "gaming",
            "why": g, "notes": notes, "specs": specs(blob), "store": store_of(it)}

# ---- optional eBay source (official Browse API; only runs when EBAY_CLIENT_ID/SECRET are set) ----
EBAY_ID, EBAY_SEC = os.environ.get("EBAY_CLIENT_ID"), os.environ.get("EBAY_CLIENT_SECRET")
def ebay_token():
    import base64
    body = urllib.parse.urlencode({"grant_type": "client_credentials",
                                   "scope": "https://api.ebay.com/oauth/api_scope"}).encode()
    req = urllib.request.Request("https://api.ebay.com/identity/v1/oauth2/token", data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": "Basic " + base64.b64encode(f"{EBAY_ID}:{EBAY_SEC}".encode()).decode()})
    try:
        with urllib.request.urlopen(req, timeout=20) as r: return json.loads(r.read()).get("access_token")
    except Exception as e:
        log(f"ebay token fail {type(e).__name__}"); return None

def ebay_search(tok, q, lo, hi):
    qs = urllib.parse.urlencode({"q": q, "category_ids": "177", "limit": "100", "sort": "newlyListed",
        "filter": f"price:[{int(lo)}..{int(hi)}],priceCurrency:USD,itemLocationCountry:US,"
                  "conditionIds:{1000|1500|2000|2010|2020|2030|2500|3000},buyingOptions:{FIXED_PRICE|AUCTION}"})
    req = urllib.request.Request("https://api.ebay.com/buy/browse/v1/item_summary/search?" + qs, headers={
        "Authorization": f"Bearer {tok}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=25) as r: data = json.loads(r.read())
    except Exception as e:
        log(f"ebay search fail {type(e).__name__}"); return []
    out = []
    for x in data.get("itemSummaries") or []:
        fb = (x.get("seller") or {}).get("feedbackPercentage")
        try:
            if fb is not None and float(fb) < 97: continue          # skip weak sellers
        except Exception: pass
        p = float((x.get("price") or {}).get("value") or 0) or None
        sh = 0.0
        for so in x.get("shippingOptions") or []:
            try: sh = float((so.get("shippingCost") or {}).get("value") or 0); break
            except Exception: pass
        when = None
        try: when = dt.datetime.fromisoformat((x.get("itemCreationDate") or "").replace("Z", "+00:00"))
        except Exception: pass
        auc = "AUCTION" in (x.get("buyingOptions") or [])
        out.append({"title": x.get("title") or "", "link": (x.get("itemWebUrl") or "").split("?")[0],
                    "when": when, "desc": f"{x.get('condition','')} · seller {fb}% · " + ("auction" if auc else "buy it now"),
                    "thumb": None, "img": ((x.get("image") or {}).get("imageUrl")), "source": "eBay",
                    "price": p, "ship": sh})
    return out

def ebay_items():
    if not (EBAY_ID and EBAY_SEC): return []
    tok = ebay_token()
    if not tok: return []
    items = []
    for q in (["thinkpad t480 8350u", "thinkpad t480 16gb"] if STUDY_ON else []):
        items += ebay_search(tok, q, 90, STUDY_MAX); time.sleep(0.5)
    for q in ["gaming laptop rtx 3060", "gaming laptop rtx 3050", "gaming laptop rtx 2060",
              "gaming laptop rtx 4050", "gaming laptop gtx 1660 ti", "gaming laptop rtx 3070"]:
        items += ebay_search(tok, q, GAME_MIN - 40, GAME_MAX); time.sleep(0.5)
    log(f"ebay browse items {len(items)}")
    return items

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
    if sc.get("ship"): f.append({"name": "Shipping", "value": f"${sc['ship']:,.2f}", "inline": True})
    if sc.get("total") is not None and sc.get("ship"): f.append({"name": "Total", "value": f"${sc['total']:,.2f}", "inline": True})
    if sc.get("why"): f.append({"name": "Match", "value": sc["why"].strip(" ·")[:80], "inline": True})
    if sc.get("notes"): f.append({"name": "Check", "value": " · ".join(sc["notes"])[:200], "inline": False})
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
    comp_hook = hook("computers")
    comp = collect(COMP_FEEDS)
    sub = REDDIT[s.get("rr", 0) % len(REDDIT)]; s["rr"] = s.get("rr", 0) + 1   # reddit rate-limits per host: one per run
    comp += parse_feed(get(f"https://www.reddit.com/r/{sub}/new/.rss"), f"r/{sub}")
    comp += ebay_items()
    stats = {}
    for name, items, scorer, hk, user, avatar, color in [
        ("computers", comp, comp_score, comp_hook, "Laptop Hunter", "1f4bb", 0x3987E5)]:
        hits = []
        for it in items:
            if not it["title"] or not fresh(it, 24 * 7 if it.get("source") == "eBay" else MAX_AGE_H): continue
            k = key(it)
            if k in s["seen"]: continue
            sc = scorer(it)
            if sc is None: continue
            s["seen"][k] = NOW.timestamp(); hits.append((it, sc))
        hits.sort(key=lambda x: (0 if x[1].get("tier", "").startswith(("🎯", "🎮 STRONG")) else 1, x[1].get("total") or 9e9))
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

def short_url(h):
    return f"https://www.tripadvisor.com/Hotel_Review-{h['key']}-Reviews.html" if re.match(r"^g\d+-d\d+$", h.get("key", "")) else h["url"]

@functools.lru_cache(maxsize=None)
def rate_quotes(key, ci, co):
    """One quote per hotel/date pair, shared by every program that wants the same stay."""
    r = xotelo("rates", hotel_key=key, chk_in=ci, chk_out=co)
    return tuple(x["rate"] for x in (r or {}).get("rates", []) if x.get("rate"))

def stay_windows(n):
    """n=1: Fri & Sat nights; n>=2: Fri/Sun check-ins. Looks `horizon_days` out (default 150) and
    samples `windows` dates evenly across it, rotating the sample daily so every weekend gets priced
    over a week of runs without raising per-run cost."""
    horizon = int(H.get("horizon_days", 150)); k = int(H.get("windows", 16))
    end = NOW.date() + dt.timedelta(days=horizon)
    d, allw = NOW.date() + dt.timedelta(days=2), []
    while d + dt.timedelta(days=n) <= end + dt.timedelta(days=1):
        if (n == 1 and d.weekday() in (4, 5)) or (n >= 2 and d.weekday() in (4, 6)):
            allw.append((d, d + dt.timedelta(days=n)))
        d += dt.timedelta(days=1)
    if len(allw) <= k: return allw
    step = len(allw) / k; off = NOW.timetuple().tm_yday % max(int(step), 1)
    return [allw[min(int(i * step) + off, len(allw) - 1)] for i in range(k)]

# ---- fallback rate source: Google Hotels via SerpApi (free plan 250 searches/mo) ----
SERP_KEY = os.environ.get("SERPAPI_KEY", "")
SERP_CACHE = "state/serp_hotels.json"
# query types: which programs each one feeds (by nights + brand shape)
SERP_TYPES = [("lux", 1, "luxury hotels in {c}", "4,5"), ("hyatt", 1, "Hyatt hotels in {c}", ""),
              ("value", 2, "hotels in {c}", ""), ("lux", 2, "luxury hotels in {c}", "4,5"), ("value", 1, "hotels in {c}", "")]

def serp_search(q, ci, co, hclass):
    prm = {"engine": "google_hotels", "q": q, "check_in_date": ci, "check_out_date": co, "adults": 2,
           "currency": "USD", "gl": "us", "hl": "en", "sort_by": 8 if hclass else 3, "rating": 8, "api_key": SERP_KEY}
    # luxury searches sort by guest rating (cheapest-first buries FHR/Edit-grade hotels); value searches cheapest-first
    if hclass: prm["hotel_class"] = hclass
    t = get("https://serpapi.com/search.json?" + urllib.parse.urlencode(prm), timeout=45, tries=2)
    try: d = json.loads(t) if t else {}
    except Exception: d = {}
    if d.get("error"): log("serp: " + str(d["error"])[:80])
    out = []
    for p in d.get("properties") or []:
        nightly = ((p.get("rate_per_night") or {}).get("extracted_lowest"))
        if not nightly: continue
        imgs = p.get("images") or []
        out.append({"name": p.get("name", "")[:80], "rating": p.get("overall_rating") or 0, "reviews": p.get("reviews") or 0,
                    "url": p.get("link") or "", "img": (imgs[0].get("thumbnail") if imgs else "") or "",
                    "nightly": float(nightly), "total": None,   # Google's total_rate is pre-tax -> tax factor applied downstream
                    "token": p.get("property_token") or p.get("name", "")})
    return out

def serp_rows(progs, tax):
    """Spend at most `serp_per_run` searches (default 8/day ~ 240/mo), accumulate a 7-day cache, price every program from it."""
    os.makedirs("state", exist_ok=True)
    try: cache = json.load(open(SERP_CACHE))
    except Exception: cache = {}
    today = NOW.date().isoformat(); fresh = (NOW - dt.timedelta(days=7)).isoformat()
    cache = {k: v for k, v in cache.items() if v.get("ts", "") >= fresh and k.split("|")[2] > today}
    want = {(p.get("serp_kind", "value"), int(p.get("nights", 1))) for p in progs}   # only search what the thin programs need
    combos = []
    for city in H["geos"]:
        for kind, n, qf, hc in SERP_TYPES:
            if (kind, n) not in want: continue
            for ci, co in stay_windows(n):
                combos.append((kind, n, qf.format(c=city), hc, city, ci.isoformat(), co.isoformat()))
    combos.sort(key=lambda c: (c[5], c[4], c[0], c[1]))
    todo = [c for c in combos if f"{c[2]}|{c[3]}|{c[5]}|{c[6]}" not in cache]
    budget = int(H.get("serp_per_run", 8))
    if todo:
        import random
        rnd = random.Random(NOW.timetuple().tm_yday); rnd.shuffle(todo)     # rotate dates + cities day to day
        pri = set(H.get("serp_priority") or [])                             # e.g. DC gets half of each day's searches
        first = [c for c in todo if c[4] in pri][:budget // 2] if pri else []
        pick = first + [c for c in todo if c not in first][:budget - len(first)]
        for kind, n, q, hc, city, ci, co in pick:
            cache[f"{q}|{hc}|{ci}|{co}"] = {"ts": NOW.isoformat(), "city": city, "n": n, "props": serp_search(q, ci, co, hc)}
    json.dump(cache, open(SERP_CACHE, "w"))
    entries = []
    for k, v in cache.items():
        _, _, ci, co = k.split("|")
        entries.append((v["city"], v["n"], ci, co, v["props"], "serp:"))
    rows = match_rows(entries, progs, tax)
    log(f"hotels serp: searches_used={min(budget, len(todo))} cached_combos={len(cache)} rows={len(rows)}")
    return rows

def match_rows(entries, progs, tax):
    """entries: (city, nights, check_in, check_out, [props], key_prefix) -> one row per (property, program, window)."""
    rows = []
    for city, nights, ci, co, props, pre in entries:
        ci_d, co_d = dt.date.fromisoformat(ci), dt.date.fromisoformat(co)
        v = {"n": nights, "city": city}
        for h in props:
            for p in progs:
                if int(p.get("nights", 1)) != v["n"] or (p.get("until") and co > p["until"]): continue
                if not re.search(p.get("match_re") or ".", h["name"], re.I): continue
                if p.get("exclude_re") and re.search(p["exclude_re"], h["name"], re.I): continue
                if h["rating"] < p.get("min_rating", 0): continue
                total = round(h["total"]) if h.get("total") else round(h["nightly"] * v["n"] * tax)
                credit = total if p.get("cert") else float(p.get("credit", 0))
                stack = bool(p.get("bonus_re") and re.search(p["bonus_re"], h["name"], re.I))
                if stack: credit += float(p.get("bonus_credit", 0))
                rows.append({**h, "key": pre + str(h["token"]), "city": v["city"], "pid": p["id"], "ci": ci_d, "co": co_d,
                             "n": v["n"], "total": total, "stack": stack, "credit": credit, "oop": max(round(total - credit), 0)})
    return rows

BP_ENTRIES = []

# ---- primary rate source: Blue Pillow B2A (live multi-OTA quotes; anonymous key, 120 req/min, 60k/day) ----
BP = "https://api.b2a.bluepillow.com/v1/"
def bp_call(path, body, key=None):
    import uuid
    hdr = {"Content-Type": "application/json", "User-Agent": UA, "Idempotency-Key": str(uuid.uuid4())}
    if key: hdr["Authorization"] = "Bearer " + key
    for i in range(3):
        try:
            req = urllib.request.Request(BP + path, data=json.dumps(body).encode(), headers=hdr, method="POST")
            with urllib.request.urlopen(req, timeout=60) as r: return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and i < 2: time.sleep(5 * (i + 1)); continue
            log(f"bp {path} HTTP {e.code}"); return {}
        except Exception as e:
            if i < 2: time.sleep(3); continue
            log(f"bp {path} {type(e).__name__}"); return {}

def bp_link(u, ci, co):
    """Trim Blue Pillow's tracking-heavy link to the property page with dates (keeps Discord posts short)."""
    m = re.match(r"(https://www\.bluepillow\.com/search/[0-9a-f]+)", u)
    return f"{m.group(1)}?begin={ci}&end={co}&adults=2&currency=USD" if m else u

def bp_rows(progs, tax):
    geos = H.get("bp_geos") or {}
    if not geos: return []
    key = os.environ.get("BLUEPILLOW_KEY") or (bp_call("keys", {"label": "feed-relay"}) or {}).get("key")
    if not key: log("bp: no key"); return []
    k = int(H.get("bp_windows", 10)); combos = []
    for n in sorted({int(p.get("nights", 1)) for p in progs}):
        ws = stay_windows(n)
        if len(ws) > k: ws = [ws[int(i * len(ws) / k)] for i in range(k)]
        for city, (lat, lon, rad) in geos.items():
            for w in ws:
                combos.append((city, n, w[0].isoformat(), w[1].isoformat(), lat, lon, rad, "price_asc"))
    def one(c):
        city, n, ci, co, lat, lon, rad, sort = c
        body = {"location": {"type": "coordinates", "value": {"lat": lat, "lon": lon, "radius_km": rad}},
                "dates": {"check_in": ci, "check_out": co}, "guests": {"adults": 2}, "currency": "USD",
                "user_country": "US", "sort": sort, "page": {"limit": 100},
                "availability_mode": "include_unavailable"}   # server-side strict/min_rating filters drop nearly everything
        props = []
        for r in (bp_call("search/stays", body, key) or {}).get("results") or []:
            pr = r.get("price") or {}
            if r.get("availability_status") != "available" or not pr.get("amount_per_night"): continue
            if (r.get("rating") or 0) < float(H.get("bp_min_rating", 3.8)): continue
            if (r.get("property_type") or "hotel") not in ("hotel", "bb", "resort"): continue      # no apartments/hostels/rentals
            if re.search(r"\b(suite|room|studio|apartment|condo) (above|in|near)\b", r.get("name") or "", re.I): continue
            props.append({"name": (r.get("name") or "")[:80], "rating": r.get("rating") or 0, "reviews": r.get("rating_count") or 0,
                          "url": bp_link(r.get("web_url") or "", ci, co), "img": r.get("thumbnail_url") or "", "nightly": float(pr["amount_per_night"]),
                          "total": None, "token": r.get("cluster_id") or r.get("id")})
        return (city, n, ci, co, props, "bp:")
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=int(H.get("bp_threads", 6))) as ex:
        entries = list(ex.map(one, combos))
    BP_ENTRIES[:] = entries
    got = sum(1 for e in entries if e[4])
    log(f"hotels bp: searches={len(combos)} with_results={got} props={sum(len(e[4]) for e in entries)}")
    return match_rows(entries, progs, tax) if got else []

def run_hotels():
    """Blue Pillow (live multi-OTA) first; programs it can't fill (luxury FHR/Edit lists) fall back to Xotelo, then SerpApi."""
    if not H.get("geos"): log("hotels: no config"); return
    os.makedirs("state", exist_ok=True)                                 # cache dir for the workflow's actions/cache
    tax = float(H.get("tax") or 1.15); progs = H.get("programs") or []
    rows = bp_rows(progs, tax)
    have = {}
    cap = H.get("max_oop", 150); certs = {p["id"] for p in progs if p.get("cert")}
    for r in rows:
        if r["pid"] in certs or r["oop"] <= cap: have[r["pid"]] = have.get(r["pid"], 0) + 1
    missing = [p for p in progs if have.get(p["id"], 0) < int(H.get("min_rows", 3))]
    if missing:
        log("hotels: filling " + ",".join(p["id"] for p in missing) + " from fallback sources")
        rows += xotelo_rows(missing, tax)
    if len(rows) < 5:
        # every source down: don't overwrite the channel with an empty board
        log("hotels: no usable prices from any source - skipping post"); sys.exit(1)
    post_hotels(rows, progs)

def xotelo_rows(progs, tax):
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
    cands = {}
    for p in progs:
        inc = re.compile(p.get("match_re") or ".", re.I); exc = re.compile(p.get("exclude_re") or r"(?!x)x", re.I)
        c = [h for h in pool if inc.search(h["name"]) and not exc.search(h["name"]) and h["rating"] >= p.get("min_rating", 0)
             and h["reviews"] >= p.get("min_reviews", 0) and h["min"] <= p.get("max_list_min", 99999)]
        key = (lambda h: -h["max"]) if p.get("cert") else (lambda h: (h["min"], -h["rating"]))
        cands[p["id"]] = sorted(c, key=key)[:p.get("max_hotels", 25)]
    jobs = [(p, h, w) for p in progs for h in cands[p["id"]] for w in stay_windows(int(p.get("nights", 1)))
            if not p.get("until") or w[1].isoformat() <= p["until"]]
    def price(job):
        p, h, (ci, co) = job
        rates = rate_quotes(h["key"], ci.isoformat(), co.isoformat())
        if not rates: return None
        nightly = min(rates)
        if h["max"] and nightly > h["max"] * 1.5: return None          # stale/outlier quote
        n = (co - ci).days; total = round(nightly * n * tax)
        credit = total if p.get("cert") else float(p.get("credit", 0))
        stack = bool(p.get("bonus_re") and re.search(p["bonus_re"], h["name"], re.I))
        if stack: credit += float(p.get("bonus_credit", 0))
        return {**h, "pid": p["id"], "ci": ci, "co": co, "n": n, "nightly": nightly, "total": total, "stack": stack,
                "credit": credit, "oop": max(round(total - credit), 0)}
    # canary: if the rate source is dead, fail fast instead of burning ~15 min on empty quotes
    probe = [(h["key"], w[0].isoformat(), w[1].isoformat()) for (p, h, w) in jobs[::max(len(jobs) // 8, 1)]][:8]
    with ThreadPoolExecutor(max_workers=8) as ex:
        alive = sum(1 for q in ex.map(lambda a: rate_quotes(*a), probe) if q)
    rows = []
    if alive and jobs:
        with ThreadPoolExecutor(max_workers=8) as ex:
            rows = [r for r in ex.map(price, jobs) if r]
        log(f"hotels pool={len(pool)} jobs={len(jobs)} quotes={rate_quotes.cache_info().currsize} priced={len(rows)}")
    else:
        log(f"hotels: primary source returned nothing for {len(probe)} canary quotes")
    if len(rows) < max(3, len(jobs) // 20) and SERP_KEY:
        rows = serp_rows(progs, tax)                                      # last-resort source
    return rows

def post_hotels(rows, progs):
    embeds, free_hits = [], 0
    for p in progs:
        per = {}
        pref = lambda r: float(H.get("city_bonus", {}).get(r["city"], 0))
        keyf = (lambda r: -(r["total"] + pref(r))) if p.get("cert") else (lambda r: (r["oop"] - pref(r), -r["rating"]))
        for r in rows:
            if r["pid"] == p["id"] and (r["key"] not in per or keyf(r) < keyf(per[r["key"]])): per[r["key"]] = r
        cap = p.get("max_oop", H.get("max_oop", 150))
        top = sorted([r for r in per.values() if p.get("cert") or r["oop"] <= cap], key=keyf)[:6]
        lines = []
        for r in top:
            day = f"{r['ci']:%a %b %-d}" + (f"→{r['co']:%a %-d}" if r["n"] > 1 else "")
            if p.get("cert"):
                lines.append(f"• [{r['name'][:46]}]({short_url(r)}) · {r['city']} · {day} · saves **${r['total']:,}** · 🟢 $0")
            else:
                free_hits += r["oop"] == 0
                tag = "🟢 **$0 out of pocket**" if r["oop"] == 0 else f"you pay **${r['oop']:,}**"
                stk = f" · 🔗 stacks ${r['credit']:,.0f}" if r.get("stack") else ""
                lines.append(f"• [{r['name'][:46]}]({short_url(r)}) · {r['city']} · {day} · ${r['nightly']:,.0f}/nt → ${r['total']:,} all-in · {tag}{stk} · ★{r['rating']}")
        e = {"title": p.get("label", p["id"]), "color": int(p.get("color", 0x3987E5)),
             "description": "\n".join(lines) or f"_Nothing under ${H.get('max_oop', 150)} out of pocket in the next {H.get('horizon_days', 150)} days._", "footer": {"text": p.get("footer", "")[:2000]}}
        if top and top[0]["img"].startswith("https://"): e["thumbnail"] = {"url": top[0]["img"]}
        embeds.append(e)
    head = f"🟢 **{free_hits} zero-spend** · 💵 best stays ≤ ${H.get('max_oop', 150)} out of pocket — {NOW:%a %b %-d} (DC · VA · MD, next ~5 months)"
    if H.get("note"): head += "\n_" + H["note"] + "_"
    # Discord caps a message at 6,000 embed chars / 10 embeds -> pack embeds into as few messages as fit
    def esize(e): return len(e["title"]) + len(e["description"]) + len(e["footer"]["text"])
    batch, size, first = [], 0, True
    for e in embeds + [None]:
        if e is None or (batch and (size + esize(e) > 5800 or len(batch) == 10)):
            ok = post(hook("hotels"), {"username": "Hotel Credits", "avatar_url": AV_HOTEL,
                                      **({"content": head} if first else {}), "embeds": batch})
            log(f"hotels post {'ok' if ok else 'FAILED'} embeds={len(batch)} chars={size}")
            if not ok: sys.exit(1)
            batch, size, first = [], 0, False
        if e is not None: batch.append(e); size += esize(e)

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "deals"
    {"deals": run_deals, "hotels": run_hotels}[mode]()
