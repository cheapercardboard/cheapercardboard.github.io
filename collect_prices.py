"""
MTG sealed price collector, version 2.

    python3 collect_prices.py              test run: first 5 stores
    python3 collect_prices.py --all        every Shopify store in store_websites.csv
    python3 collect_prices.py --rebuild    no internet needed: rebuild the website data from saved files

(Use `py -3` instead of `python3` on Windows.)

What's new: reads only each store's sealed-product collections (falls back to the
full catalog), saves raw titles so fixes never need a re-run, groups products by
official set (Scryfall), merges duplicate listings, and writes store_report.csv
showing what happened at every store.
"""
import argparse, collections, csv, datetime, difflib, html, json, os, re, shutil, sys, time, unicodedata
import urllib.error, urllib.parse, urllib.request, urllib.robotparser

INPUT, CACHE_DIR, HISTORY = "store_websites.csv", "cache", "price_history.csv"
SUSPICIOUS, REPORT, SETS_FILE = "suspicious_prices.csv", "store_report.csv", "scryfall_sets.json"
ASSUMED = "assumed_matches.csv"
HISTORY_JSON = os.path.join("data", "history.json")  # one number per product per day; the workflow commits it
HISTORY_DAYS = 90
DROPPED, CHANGES, WATCHLIST, WATCH_REPORT = "dropped_listings.csv", "changes_since_last_build.csv", "must_keep.csv", "watchlist_report.csv"
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL") or "contact@example.com"  # placeholder; set a real project address
PAGE_SIZE, TIMEOUT, MAX_BYTES, CACHE_VERSION = 250, 30, 15_000_000, 2

# ---------- fetching ----------
LOOSE = ("booster", "display", "bundle", "fat pack", "commander", "prerelease", "jumpstart", "starter", "precon")
COLL_INCLUDE = ("booster", "sealed", "commander", "bundle", "prerelease", "jumpstart", "display", "fat pack", "precon", "collector")
COLL_EXCLUDE = ("single", "accessor", "sleeve", "suppl", "pokemon", "yu-gi-oh", "yugioh", "lorcana", "one piece",
                "dragon ball", "digimon", "flesh", "secret lair", "promo", "token", "deck box", "gift card",
                "dice", "playmat", "binder", "sports", "funko", "board game")


def get_json(url, agent):
    url = urllib.parse.quote(url, safe=":/?&=%+@#,;")
    req = urllib.request.Request(url, headers={"User-Agent": agent, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read(MAX_BYTES).decode("utf-8", errors="replace"))


def robots_for(site, agent):
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(site + "/robots.txt")
    try:
        rp.read()
    except Exception:
        return None
    return rp


def ok_by_robots(rp, agent, url):
    return rp is None or rp.can_fetch(agent, url)


def best_price(variants):
    parsed = []
    for v in variants or []:
        try:
            parsed.append((float(v.get("price")), bool(v.get("available"))))
        except (TypeError, ValueError):
            pass
    if not parsed:
        return None, False
    live = [p for p, a in parsed if a]
    return (min(live), True) if live else (min(p for p, _ in parsed), False)


def fetch_pages(base, agent, delay, max_pages):
    """Read a products.json style feed page by page. Returns (products, hit_cap)."""
    out = []
    for page in range(1, max_pages + 1):
        batch = get_json(f"{base}?limit={PAGE_SIZE}&page={page}", agent).get("products", [])
        out.extend(batch)
        if len(batch) < PAGE_SIZE:
            return out, False
        time.sleep(delay)
    return out, True


def sealed_collections(site, agent):
    data = get_json(f"{site}/collections.json?limit=250", agent).get("collections", [])
    picks = []
    for c in data:
        text = (c.get("title", "") + " " + c.get("handle", "")).lower()
        if c.get("products_count") == 0:
            continue
        if any(w in text for w in COLL_INCLUDE) and not any(w in text for w in COLL_EXCLUDE):
            picks.append(c["handle"])
    return picks[:25]


def desc_snippet(h):
    t = re.sub(r"<[^>]+>", " ", h or "")
    return re.sub(r"\s+", " ", html.unescape(t)).strip()[:500]


def collect_store(site, agent, delay, max_pages, coll_pages):
    rp = robots_for(site, agent)
    products, capped, method, used = [], False, "full catalog", []
    if ok_by_robots(rp, agent, site + "/collections.json"):
        try:
            used = sealed_collections(site, agent)
        except Exception:
            used = []
    if used:
        method = "collections"
        for h in used:
            try:
                got, cap = fetch_pages(f"{site}/collections/{h}/products.json", agent, delay, coll_pages)
                products.extend(got)
                capped = capped or cap
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    raise
            time.sleep(delay)
    else:
        products, capped = fetch_pages(f"{site}/products.json", agent, delay, max_pages)
    raw, seen = [], set()
    for p in products:
        h, title = p.get("handle", ""), p.get("title", "")
        if h in seen or not any(w in title.lower() for w in LOOSE):
            continue
        seen.add(h)
        price, avail = best_price(p.get("variants"))
        if price is None:
            continue
        tags = p.get("tags", "")
        raw.append({"t": title.strip(), "h": h, "pt": p.get("product_type", ""),
                    "tg": " ".join(tags) if isinstance(tags, list) else tags,
                    "v": p.get("vendor", ""), "p": price, "a": avail, "b": desc_snippet(p.get("body_html"))})
    return {"method": method, "collections": used, "seen": len(products), "capped": capped, "raw": raw}


# ---------- sets (Scryfall) ----------
SET_TYPES = {"core", "expansion", "masters", "draft_innovation", "commander", "starter", "funny", "box"}


def set_norm(s):
    s = s.lower().replace("’", "'").replace("`", "'").replace("´", "'").replace("'", "")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s)).strip()


def alias_keys(name):
    """Other ways stores write a set name: no leading 'The', before the colon, singular, etc."""
    n = set_norm(name)
    keys = set()
    for pre in ("the ", "marvels ", "universes beyond ", "magic the gathering "):
        if n.startswith(pre):
            keys.add(n[len(pre):])
    if ":" in name:
        head = set_norm(name.split(":")[0])
        if len(head) >= 6 and head != "magic":
            keys.add(head)
    for k in list(keys | {n}):
        if k.endswith("s") and len(k.split()) >= 2:
            keys.add(k[:-1])
    keys.discard(n)
    return {k for k in keys if len(k) >= 5}


def build_name_index(allsets):
    """Exact set names win; aliases only fill gaps (newest sets claim an alias first)."""
    usable = sorted((s for s in allsets if s["type"] in SET_TYPES), key=lambda s: s.get("released", ""), reverse=True)
    index = {}
    for st in usable:
        index.setdefault(set_norm(st["name"]), st)
    for st in usable:
        for k in alias_keys(st["name"]):
            index.setdefault(k, st)
    return sorted(index.items(), key=lambda kv: -len(kv[0]))


def load_sets(agent):
    fresh = os.path.exists(SETS_FILE) and time.time() - os.path.getmtime(SETS_FILE) < 30 * 86400
    if not fresh:
        try:
            req = urllib.request.Request("https://api.scryfall.com/sets",
                                         headers={"User-Agent": agent, "Accept": "application/json;q=0.9,*/*;q=0.8"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                data = json.loads(r.read().decode("utf-8"))["data"]
            keep = [{"code": s["code"], "name": s["name"], "released": s.get("released_at", ""),
                     "type": s["set_type"], "parent": s.get("parent_set_code", "")} for s in data]
            json.dump(keep, open(SETS_FILE, "w", encoding="utf-8"))
        except Exception as e:
            print(f"Could not download the set list ({type(e).__name__}); using saved copy if any.")
    if not os.path.exists(SETS_FILE):
        print("No set list available: products will not be grouped by set.")
        return {}, []
    allsets = json.load(open(SETS_FILE, encoding="utf-8"))
    by_code = {s["code"]: s for s in allsets}
    return by_code, build_name_index(allsets)


ABBREV = {"tmnt": "teenage mutant ninja turtles", "lotr": "lord of the rings tales of middle earth",
          "ltr": "lord of the rings tales of middle earth", "lord of the rings": "lord of the rings tales of middle earth",
          "spiderman": "spider man", "mh3": "modern horizons 3", "msh": "marvel super heroes",
          "marvel commander": "marvel super heroes commander", "warhammer 40k": "warhammer 40 000", "40k": "40 000"}


def expand_abbrev(title):
    t = set_norm(title)
    for ab, full in ABBREV.items():
        if full not in t and re.search(r"\b" + ab + r"\b", t):
            t = re.sub(r"\b" + ab + r"\b", full, t)
    if re.search(r"\bmarvel\b", t) and "super heroes" not in t and "spider" not in t:
        t = re.sub(r"\bmarvel\b", "marvel super heroes", t)  # "Commander Decks - Marvel - Fantastic Four"
    return t


def fuzzy_set(title, names):
    """Tolerate small typos ('Duskmorn', 'Modern Horizon 3'). Only used for titles that already look like sealed MTG."""
    tokens = expand_abbrev(title).split()
    best, best_len = None, 0
    for key, st in names:
        k = len(key.split())
        if len(key) < 8 or k > len(tokens) or len(key) <= best_len:
            continue
        for i in range(len(tokens) - k + 1):
            w = " ".join(tokens[i:i + k])
            if abs(len(w) - len(key)) <= 3 and difflib.SequenceMatcher(None, w, key).ratio() >= 0.88:
                best, best_len = st, len(key)
                break
    return best


def match_set(title, by_code, names):
    t = " " + expand_abbrev(title) + " "
    for n, s in names:
        if n and " " + n + " " in t:
            return s
    for code in re.findall(r"[\[(]([A-Za-z0-9]{3,5})[\])]", title):
        s = by_code.get(code.lower())
        if s and s["type"] in SET_TYPES:
            return s
    return None


def group_set(s, by_code):
    p = by_code.get(s.get("parent", ""))
    return p if p else s


# ---------- classification ----------
OTHER_GAMES = ["pokemon", "pokémon", "yu-gi-oh", "yugioh", "lorcana", "one piece", "dragon ball", "digimon",
               "flesh and blood", "star wars unlimited", "star wars: unlimited", "weiss", "union arena",
               "contested realm", "battle spirits", "vanguard", "football", "basketball", "baseball", "hockey",
               "soccer", "topps", "panini", "nfl", "nba", "mlb", "upper deck", "bowman", "prizm", "donruss",
               "altered", "cyberpunk", "cybercel", "gundam", "riftbound", "shadowverse", "naruto", "universus", "funko",
               "metazoo", "force of will", "grand archive", "my hero academia", "disney"]
NOT_SEALED = ["sleeve", "playmat", "play mat", "deck box", "binder", "dice", "single", "token", "storage",
              "toploader", "album", "life counter", "spindown", "poster", "ticket", "event", "in store only",
              "in-store only", "secret lair", "prerelease card", "promo pack", "case", "custom", "accessories", "damaged", "dented", "opened", "funko", "comic", "bulk", "lot of",
              "pop", "pop!", "rpg", "role-playing", "role playing", "narrator", "figure", "action figure", "statue", "plush",
              "lego", "puzzle", "board game", "miniature", "vinyl", "graphic novel", "chase"]
NON_ENGLISH = ["japanese", "japan", "korean", "chinese", "german", "french", "italian", "spanish",
               "portuguese", "russian", "(jp)", "(jpn)"]
SINGLE_PATTERNS = [r"\[[^\]]*promos?\]", r"\(bundle\)\s*\[", r"\(pre\)\s*$", r"\b(psa|bgs|cgc|graded)\b", r"\bbundles? of \d", r"\[#\d+", r"\([a-z0-9]{2,5}-[a-z]\)\s*$"]
MARKERS = ["magic: the gathering", "magic the gathering", "magic: the", "mtg"]
PRICE_RANGE = {"Collector Booster Box": (80, 3000), "Play Booster Box": (40, 3000), "Set Booster Box": (40, 3000),
               "Draft Booster Box": (40, 3000), "Jumpstart Booster Box": (40, 3000), "Booster Box": (40, 3000),
               "Commander Deck": (15, 500), "Commander Deck Set": (40, 3000), "Bundle": (15, 1500),
               "Prerelease Kit": (15, 300)}
VARIANT_TYPES = {"Commander Deck", "Commander Deck Set", "Bundle", "Prerelease Kit"}


PICKUP_ONLY = r"(?:pick-?\s?up|pickup)\s+only|in[- ]store\s+(?:pick-?\s?up\s+)?only"
_WORD_RX = {}


def find_word(words, text):
    """First listed word found as a whole word (optionally plural), so 'case' does not match 'Showcase' or 'event' 'Eventide'."""
    for w in words:
        if w not in _WORD_RX:
            _WORD_RX[w] = re.compile((r"\b" if w[0].isalnum() else "") + re.escape(w) + (r"s?\b" if w[-1].isalnum() else ""))
        if _WORD_RX[w].search(text):
            return w
    return None


def has_marker(r):
    """True when the listing itself says Magic / MTG (title, product type, tags or vendor)."""
    hay = " ".join([r["t"], r.get("pt") or "", r.get("tg") or "", r.get("v") or ""]).lower()
    return any(m in hay for m in MARKERS) or re.search(r"\bmagic\b", " ".join([r.get("pt") or "", r.get("tg") or "", r.get("v") or ""]).lower()) is not None


def classify_why(title, product_type, tags, vendor, set_found):
    """Returns (type, why). type is None when the listing is not MTG sealed; why names the rule that rejected it."""
    text = title.lower()
    hay = " ".join([text, (product_type or "").lower(), (tags or "").lower(), (vendor or "").lower()])
    w = find_word(OTHER_GAMES, hay)
    if w:
        return None, f"other game or sport: '{w}'"
    if re.search(PICKUP_ONLY, text):
        return None, "store pick-up only (can't be shipped)"
    w = find_word(NOT_SEALED + NON_ENGLISH, text)
    if w:
        return None, f"not a sealed English product: '{w}'"
    if any(re.search(p, text) for p in SINGLE_PATTERNS):
        return None, "looks like a single card or a lot"
    known = any(m in hay for m in MARKERS) or set_found
    text = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text))  # "Play! Booster" -> "play booster"
    box = re.search(r"\b(box|display)\b", text) is not None
    if "jumpstart" in text and (box or "booster" in text) and not re.search(r"\bpack\b", text):
        return "Jumpstart Booster Box", ""
    if "booster" in text and box:
        for key, name in (("collector", "Collector Booster Box"), ("play booster", "Play Booster Box"),
                          ("set booster", "Set Booster Box"), ("draft booster", "Draft Booster Box"),
                          ("booster draft", "Draft Booster Box")):
            if key in text and (known or key == "play booster"):
                return name, ""
        if known:
            return "Booster Box", ""
    if "commander deck" in text or ("commander" in text and "precon" in text):
        multi = box or re.search(r"\b(set of|full set|complete set|all \d|\d+\s*(?:ct|count|pack)?\s*decks|assortment|\d+ct|decks? set)\b", text) is not None
        return ("Commander Deck Set" if multi else "Commander Deck"), ""
    if ("fat pack" in text or "bundle" in text) and known:
        return "Bundle", ""
    if "prerelease" in text and known and re.search(r"\b(kit|pack)\b", text):
        return "Prerelease Kit", ""
    return None, ""


def classify(*args):
    return classify_why(*args)[0]


# ---------- title cleanup and matching ----------
NOISE = [r"magic the gathering", r"the gathering", r"magic", r"mtg", r"english", r"sealed", r"brand new",
         r"factory sealed", r"pre ?order", r"wotc", r"wizards of the coast", r"universes beyond", r"sale",
         r"new", r"tcg", r"\d+\s*(?:(?:play|set|draft|collector) )?(?:ct|count|packs?|boosters?|boxes)", r"ccg", r"cards?", r"wizards", r"carton",
         r"pre ?sale", r"expected", r"limit \d+( pp| per \w+)?", r"20\d\d", r"\d+\s?x\s?\d+", r"x\d+"]
GENERIC = {"jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "x", "woc", "vary", "play", "set", "draft", "booster", "box", "jumpstart", "commander", "deck",
           "decks", "bundle", "fat", "pack", "prerelease", "kit", "edition", "of", "the", "and", "all"}
PAREN_NOISE = r"\s*(\d+\s*(ct|count|packs?|boxes|decks?)?|english|sale|release.*|ships.*|pre-?order.*|pre-?sale.*|expected.*|limit.*)\s*"
COLORS = {"red", "blue", "green", "white", "black", "gold", "yellow", "multi", "colorless"}
PRE_TAIL = re.compile(r"[\s\-\u2013:|(\[]*\bpre[\s-]?(?:order|sale)\b[\s:)\]]*(?:(?:for|releases?|released|release date|ships?|on|arrives|available|expected|until)\b.*)?$", re.I)
PRE_WORD = re.compile(r"\bpre[\s-]?(?:order|sale)\b[\s:]*", re.I)


def strip_preorder(t):
    """'... - Pre-Order for November 6th' loses the whole tail; 'PRE-ORDER: Name' only loses the marker."""
    return PRE_WORD.sub(" ", PRE_TAIL.sub(" ", t))


def strip_codes(t):
    """Drop set codes and counts written in capitals, like [FDN] or (24CT). Colour words such as (GREEN) stay."""
    return re.sub(r"[\[(]\s*([A-Z0-9]{2,5})\s*[\])]", lambda m: m.group(0) if m.group(1).lower() in COLORS else " ", t)


def norm(title):
    t = tidy_edges(strip_preorder(strip_codes(title))).lower()
    t = t.replace("&", " and ").replace("\u2019", "'").replace("`", "'").replace("\u00b4", "'")
    t = re.sub(r"\s-\w+(?:-\w+)+\s*$", " ", t)  # store codes like " -StarTrek-CDC"
    t = re.sub(r"\b[a-z]*\d{4,}[a-z0-9]*\b", " ", t)  # item numbers like D51710000
    t = re.sub(r"\b(?:styles?|colou?rs?|designs?)?\s*may vary\b", " ", t)
    t = re.sub(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", " ", t)
    t = re.sub(r"[\[(]([^\])]*)[\])]", lambda m: " " if re.fullmatch(PAREN_NOISE, m.group(1)) else " " + m.group(1) + " ", t)
    t = re.sub(r"[^\w\s]", " ", t.replace("'", ""))
    t = re.sub(r"\b(release date|ships|available|releases?)\b.*$", " ", t)
    for n in NOISE:
        t = re.sub(r"\b" + n + r"\b", " ", t)
    t = re.sub(r"\bdisplay\b", "box", t)
    t = re.sub(r"\bboxes\b", "box", t)
    t = re.sub(r"\b(bundle|deck|kit|pack)s\b", r"\1", t)
    t = re.sub(r"\bcollectors\b", "collector", t)
    t = re.sub(r"\bbndl\b", "bundle", t)
    return re.sub(r"\s+", " ", t).strip()


def tidy_edges(t):
    t = strip_codes(t)  # "( NOV )" left behind after "(PRE-ORDER NOV)" loses its first words
    t = re.sub(r"[\[(]\s*[\])]", " ", t)
    return re.sub(r"^[\s:\-|\]\)]+", "", t)


def clean_name(title):
    t = tidy_edges(strip_preorder(strip_codes(title)))
    t = re.sub(r"^\s*(magic[:\s-]*the[\s-]*gathering|mtg\b)\s*[:|\-\u2013\u2014]*\s*", "", t, flags=re.I)
    t = re.sub(r"\s*[\[(]\s*\d+\s*ct\s*[\])]\s*", " ", t, flags=re.I)
    t = re.sub(r"\s*\([^)]*\bcard\b[^)]*\)", " ", t, flags=re.I)  # "(100-Card Deck, ...)"
    t = re.sub(r"\s*\*\*[^*]*\*\*", " ", t)  # "**Local Pick-Up Only**"
    t = re.sub(r"^\W*(\*[^*]*\*\W*)+", "", t)  # "*BREAK*"
    t = re.sub(r"\s-\w+(?:-\w+)+\s*$", " ", t)  # store codes like " -StarTrek-CDC"
    t = re.sub(r"\b(?:WOC\s+)?[A-Z]?\d{5,}\w*\b", " ", t)  # item numbers like "WOC D51710000"
    t = re.sub(r"\s*-?\s*\b\d+\s?x\s?\d+\b", " ", t, flags=re.I)  # "4x1"
    t = re.sub(r"\s*[\[(](?:expected|pre-?sale)[^\])]*(?:[\])]|$)", " ", t, flags=re.I)
    t = re.sub(r"\s*-?\s*limit \d+\s*(?:pp|per \w+)?\s*$", "", t, flags=re.I)
    t = re.sub(r"\b(wizards\s+)?magic\s+mtg\b", " ", t, flags=re.I)
    t = re.sub(r"\b(20\d\d|wizards|universes beyond)\b", " ", t, flags=re.I)
    t = re.sub(r"^\s*(ccg|tcg|cards)\s*[:\-]?\s+", "", t, flags=re.I)
    t = re.sub(r"\b(mtg|magic the gathering|magic: the gathering|english|tcg|ccg|trading cards?|collectible card game)\b", " ", t, flags=re.I)
    t = re.sub(r"(\s*[-\u2013|]\s*){2,}", " - ", t)
    if len(t) > 90:
        t = t[:87].rstrip() + "..."
    return re.sub(r"\s+", " ", t).strip(" -:") or title.strip()


def remainder(title, set_info, ptype=None, parent=None):
    skip, joined, fuzzy = set(), set(), set()
    for si in (set_info, parent):
        if si:
            ws = set_norm(si["name"]).split()
            fuzzy |= set(ws)
            skip |= {w.rstrip("s") for w in ws} | {si["code"]}
            joined.add("".join(ws))
    names = [set_norm(si["name"]) for si in (set_info, parent) if si]
    for ab, full in ABBREV.items():
        if any(full in n or n in full for n in names):
            skip.add(ab)
    generic = GENERIC - {"commander", "draft"} if ptype == "Bundle" else GENERIC  # "Commander Bundle" is not the plain Bundle

    def is_set_word(w):
        return w.rstrip("s") in skip or w in joined or any(difflib.SequenceMatcher(None, w, k).ratio() >= 0.8 for k in fuzzy if len(k) > 3 and len(w) > 3)
    words = ["commander" if (w == "commanders" and ptype == "Bundle") else w for w in norm(title).split()]
    return " ".join(w for w in words if w not in generic and not is_set_word(w))


# ---------- cache / outputs ----------
def cache_path(site):
    return os.path.join(CACHE_DIR, urllib.parse.urlparse(site).netloc.replace(":", "_") + ".json")


def cache_is_fresh(site, hours):
    p = cache_path(site)
    if not os.path.exists(p) or time.time() - os.path.getmtime(p) > hours * 3600:
        return False
    try:
        c = json.load(open(p, encoding="utf-8"))
        return c.get("v") == CACHE_VERSION and not c.get("error")
    except Exception:
        return False


def load_caches():
    out = []
    for fn in sorted(os.listdir(CACHE_DIR)) if os.path.isdir(CACHE_DIR) else []:
        try:
            c = json.load(open(os.path.join(CACHE_DIR, fn), encoding="utf-8"))
        except Exception:
            continue
        if c.get("v") == CACHE_VERSION:
            out.append(c)
    return out


def pack_count(title):
    m = re.search(r"\b(\d{1,2})\s*-?\s*(?:(?:play|set|draft|collector) )?(?:ct|count|pk|pks|packs?|boosters?)\b", title.lower())
    return int(m.group(1)) if m else None


def resolve_booster_box(title, code, play_sets, draft_sets):
    """A plain 'Booster Box' is ambiguous. Use the pack count in the title, plus what other stores call
    the same set's box (Play Booster sets never have Draft boxes, and vice versa)."""
    n = pack_count(title)
    if code in play_sets and n in (None, 30, 36):
        return "Play Booster Box", (f"{n} packs in title; " if n else "") + "this set's boxes are Play Boosters"
    if n == 30:
        return "Set Booster Box", "30 packs in title"
    if n == 12:
        return "Collector Booster Box", "12 packs in title"
    if code in draft_sets and n in (None, 36):
        return "Draft Booster Box", (f"{n} packs in title; " if n else "") + "this set's boxes are Draft Boosters"
    return None, ""


NUMWORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
SETFILLER = set(NUMWORDS) | {"includes", "include", "complete", "full", "contains", "na", "n", "a", "decks"}


def deck_count(text):
    """'Set of 4', '(4 decks)', 'all four', '4ct' ... -> number of decks in a multi-deck set."""
    t = re.sub(r"[^\w\s]", " ", (text or "").lower())
    for w, n in NUMWORDS.items():
        t = re.sub(r"\b" + w + r"\b", str(n), t)
    m = (re.search(r"\b(?:set of|all|complete|full set of|includes?|contains?)\s+(\d)\b", t)
         or re.search(r"\b(\d)\s+(?:[a-z]+\s+)?(?:commander\s+)?decks?\b", t)
         or re.search(r"\b(\d)\s*(?:ct|count)\b", t))
    return int(m.group(1)) if m and 2 <= int(m.group(1)) <= 6 else None


def count_for(ptype, o):
    """Pack count for bundles / deck count for deck sets: from the title first, then the product description."""
    if ptype == "Bundle":
        n = pack_count(o["title"])
        if n is None:
            d = pack_count(o.get("desc", ""))
            n = d if d and 6 <= d <= 12 else None
        return n
    if ptype == "Commander Deck Set":
        return deck_count(o["title"]) or deck_count(o.get("desc", ""))
    return None


def plurality(counter):
    (top, tc), *rest = counter.most_common()
    return top if tc >= 2 and (not rest or tc >= 2 * rest[0][1]) else None


DISC = {"collector", "collectors", "gift", "starter", "special"}  # words that make a genuinely different product
UNSPEC = {"variants", "variant", "varieties", "variety", "assorted", "random"}


def cluster(items, ptype, has_set, assumed):
    """Group listings of the same commander deck / bundle / kit. Word order and extra words don't matter;
    Collector's editions stay separate; 'Gift' only separates a named product if a store sells both versions;
    bundles must agree on pack count and deck sets on deck count."""
    info = []
    for _, o, rem, _ in items:
        toks = set(rem.split())
        disc = set(toks & DISC)
        if ptype == "Bundle" and re.search(r"\bbooster\b", o["title"].lower()):
            disc.add("boosterbundle")  # a 3-pack booster bundle is not a normal Bundle
        core = toks - DISC - UNSPEC
        if ptype == "Commander Deck Set":
            core = {t for t in core if not t.isdigit() and t not in SETFILLER}
        info.append([frozenset(disc), frozenset(core), count_for(ptype, o)])
    seen = collections.defaultdict(set)
    for (store, _, _, _), (d, c, _) in zip(items, info):
        if c:
            seen[(store, c)].add("gift" in d)
    split = {c for (_, c), v in seen.items() if len(v) == 2}
    for row in info:
        if row[1] and row[1] not in split:
            row[0] = row[0] - {"gift"}
    if ptype in ("Bundle", "Commander Deck Set"):  # fill a missing count when most matching listings agree
        valid = (lambda n: 6 <= n <= 12) if ptype == "Bundle" else (lambda n: 2 <= n <= 6)
        unit = "packs" if ptype == "Bundle" else "decks"
        stated = collections.defaultdict(collections.Counter)
        for d, c, n in info:
            if n and valid(n):
                stated[(d, c)][n] += 1
        overall = collections.Counter()
        for counter in stated.values():
            overall.update(counter)
        for k, row in enumerate(info):
            opts = stated.get((row[0], row[1]))
            if row[2] is None:
                n = plurality(opts) if opts else None
                if n is None and ptype == "Commander Deck Set" and overall:
                    n = plurality(overall)
                if n:
                    row[2] = n
                    assumed.append([items[k][0], items[k][1]["title"], f"{ptype} ({n} {unit})",
                                    "most other matching listings state this count", items[k][1]["url"]])
    for k, row in enumerate(info):
        items[k][1]["n"] = row[2]
    parent = list(range(len(items)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            (di, ci, ni), (dj, cj, nj) = info[i], info[j]
            if di != dj or ni != nj:
                continue
            if not ci and not cj:
                ok = True
            elif ci and cj:
                jac = len(ci & cj) / len(ci | cj)
                small = min(len(ci), len(cj))
                ok = (jac >= 0.6 or (len(ci & cj) == small and small >= 2)) if has_set else jac >= 0.8
            else:
                ok = False
            if ok:
                parent[find(i)] = find(j)
    out = collections.defaultdict(list)
    for i, it in enumerate(items):
        out[find(i)].append(it)
    return list(out.values())


def variant_name(g, ptype, lst):
    """Rebuild a clean display name from the set plus the words that make this product unique."""
    rems = collections.Counter(it[2] for it in lst)
    rep = sorted(rems, key=lambda r: (-rems[r], -len(r)))[0]
    toks = set(rep.split())
    shared = set.intersection(*[set(it[2].split()) for it in lst])  # "Gift" only shows if every store says it
    disc, core = toks & DISC & shared, toks - DISC - UNSPEC
    if ptype == "Commander Deck Set":
        core = {t for t in core if not t.isdigit() and t not in SETFILLER}
    ns = collections.Counter(o.get("n") for _, o, _, _ in lst if o.get("n"))
    n = ns.most_common(1)[0][0] if ns else None
    deck = ""
    if core:
        best = []
        for _, o, _, _ in lst:
            words = []
            for w in re.split(r"\s+", clean_name(o["title"])):
                w2 = w.strip(" -:|,;()[]")
                nt = ["commander" if (t == "commanders" and ptype == "Bundle") else t for t in norm(w2).split()]
                if w2 and nt and all(t in core for t in nt):
                    words.append(w2)
            if len(words) > len(best):
                best = words
        deck = " ".join(best)
    coll = " (Collector's Edition)" if disc & {"collector", "collectors"} else ""
    if ptype == "Bundle":
        kind = ("Gift " if "gift" in disc else "") + ("Booster " if any(re.search(r"\bbooster\b", o["title"].lower()) for _, o, _, _ in lst) else "")
        return f"{g['name']} {deck + ' ' if deck else ''}{kind}Bundle{f' ({n} packs)' if n else ''}{coll}"
    if ptype == "Commander Deck Set":
        return f"{g['name']} Commander Deck Set" + (f" ({n} decks)" if n else "") + (f" - {deck}" if deck else "") + coll
    return f"{g['name']} {ptype}" + (f" - {deck}" if deck else "") + coll


def slugify(text):
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", t)).strip("-")[:90]


def update_history(products, today):
    """Record each product's lowest in-stock price for today (None if nothing is in stock)."""
    hist = {"v": 1, "products": {}}
    try:
        with open(HISTORY_JSON, encoding="utf-8") as f:
            hist = json.load(f)
    except Exception:
        pass
    prods = hist.setdefault("products", {})
    for pr in products:
        lows = [o["price"] for o in pr["offers"] if o["in_stock"]]
        prods.setdefault(pr["id"], {})[today] = min(lows) if lows else None
    cutoff = (datetime.date.fromisoformat(today) - datetime.timedelta(days=HISTORY_DAYS)).isoformat()
    for pid in list(prods):
        prods[pid] = {d: v for d, v in prods[pid].items() if d >= cutoff}
        if not prods[pid]:
            del prods[pid]
    os.makedirs(os.path.dirname(HISTORY_JSON), exist_ok=True)
    with open(HISTORY_JSON, "w", encoding="utf-8") as f:
        json.dump(hist, f, separators=(",", ":"), sort_keys=True)
    return prods


PAGE_NOTE = ("The line is the cheapest in-stock price across the stores we track, checked twice a day and saved once a day. "
             "A product's history starts the first day we collect it.")


def write_product_pages(products, prods, site_dir):
    """One static page per product, built from site/product-template.html."""
    tpl_path = os.path.join(site_dir, "product-template.html")
    if not os.path.exists(tpl_path):
        print(f"{tpl_path} not found, so no product pages were made.")
        return 0
    tpl = open(tpl_path, encoding="utf-8").read()
    pdir = os.path.join(site_dir, "p")
    shutil.rmtree(pdir, ignore_errors=True)
    os.makedirs(pdir)
    for pr in products:
        series = sorted(prods.get(pr["id"], {}).items())[-60:]
        data = {"name": pr["name"], "type": pr["type"], "set": pr["set"], "released": pr["released"], "note": PAGE_NOTE,
                "history": [{"d": d, "low": v} for d, v in series], "offers": pr["offers"]}
        blob = json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/").replace("<!--", "<\\!--")
        with open(os.path.join(pdir, pr["id"] + ".html"), "w", encoding="utf-8") as f:
            f.write(tpl.replace("/*PRODUCT_DATA*/null", blob))
    return len(products)


def build_outputs(out_js, sets):
    by_code, names = sets
    today = datetime.date.today().isoformat()
    groups = collections.defaultdict(list)  # (set code, type) -> [(store, offer, remainder, set)]
    susp, report, dropped = [], [], []  # dropped: every sealed-looking listing that did NOT make the page, with the reason
    caches = load_caches()
    stage = []  # (cache, raw item, matched set, type)
    for c in caches:
        for r in c.get("raw", []):
            r["t"] = html.unescape(r["t"])
            sm = match_set(r["t"], by_code, names) if names else None
            ptype, why = classify_why(r["t"], r["pt"], r["tg"], r["v"], sm is not None)
            if not ptype:
                if why or re.search(r"booster|display|bundle|prerelease|\bbox\b|commander deck", r["t"].lower()):
                    dropped.append([c["store"], r["t"], why or "no recognizable sealed product type", r["p"], f"{c['site']}/products/{r['h']}"])
                continue
            if sm is None and names:
                sm = fuzzy_set(r["t"], names)
            stage.append((c, r, sm, ptype))
    play_sets, draft_sets = set(), set()  # which sets other stores explicitly call Play / Draft boxes
    for c, r, sm, ptype in stage:
        if sm and ptype in ("Play Booster Box", "Draft Booster Box"):
            (play_sets if ptype.startswith("Play") else draft_sets).add(group_set(sm, by_code)["code"])
    assumed, kept_by = [], collections.Counter()
    for c, r, sm, ptype in stage:
        url = f"{c['site']}/products/{r['h']}"
        if ptype == "Booster Box":
            new, why = resolve_booster_box(r["t"], group_set(sm, by_code)["code"] if sm else "", play_sets, draft_sets)
            if new:
                assumed.append([c["store"], r["t"], new, why, url])
                ptype = new
        offer = {"title": r["t"], "price": round(r["p"], 2), "in_stock": r["a"], "url": url, "desc": r.get("b", ""), "mtg": has_marker(r)}
        lo, hi = PRICE_RANGE[ptype]
        if not lo <= r["p"] <= hi:
            susp.append([c["store"], r["t"], ptype, offer["price"], offer["url"]])
            continue
        g = group_set(sm, by_code) if sm else None
        groups[(g["code"] if g else "", ptype)].append((c["store"], offer, remainder(r["t"], sm, ptype, g), g))
        kept_by[c["site"]] += 1
    for c in caches:
        report.append([c["store"], c["site"], c.get("method", ""), len(c.get("collections", [])), c.get("seen", 0),
                       len(c.get("raw", [])), kept_by[c["site"]], c.get("capped", False), c.get("error", ""), c.get("fetched", "")])

    products = []
    for (code, ptype), items in groups.items():
        g = items[0][3]
        merge_all = bool(code) and ptype not in VARIANT_TYPES
        clusters = [items] if merge_all else cluster(items, ptype, bool(code), assumed)
        for lst in clusters:
            if ptype in ("Bundle", "Prerelease Kit") and not any(o.get("mtg") for _, o, _, _ in lst) and len({st for st, _, _, _ in lst}) < 4:
                for st, o, _, _ in lst:  # "Marvel Super Heroes" is also a Funko / RPG line: need Magic wording somewhere
                    dropped.append([st, o["title"], "no Magic: The Gathering wording in any listing of this product (may be another franchise's product)", o["price"], o["url"]])
                continue
            per_store = {}
            for store, o, _, _ in lst:
                cur = per_store.get(store)
                if cur is None:
                    per_store[store] = o
                    continue
                keep, lose = (o, cur) if (o["in_stock"], -o["price"]) > (cur["in_stock"], -cur["price"]) else (cur, o)
                per_store[store] = keep
                dropped.append([store, lose["title"], f"same store lists this product twice; kept '{keep['title'][:45]}' at ${keep['price']}",
                                lose["price"], lose["url"]])
            counts = collections.Counter(clean_name(o["title"]) for _, o, _, _ in lst)
            common = sorted(counts, key=lambda n: (-counts[n], len(n)))[0]
            base = ptype.replace("Jumpstart ", "") if g and g["name"].lower().endswith("jumpstart") else ptype
            name = f"{g['name']} {base}" if merge_all else ((variant_name(g, ptype, lst) if g else None) or common)
            offers = [{"store": s, "price": o["price"], "in_stock": o["in_stock"], "url": o["url"]} for s, o in per_store.items()]
            if len(offers) >= 4:
                med = sorted(o["price"] for o in offers)[len(offers) // 2]
                for o in [o for o in offers if o["price"] < 0.4 * med]:
                    susp.append([o["store"], name, ptype, o["price"], o["url"]])
                    offers.remove(o)
            products.append({"name": name, "type": ptype, "set": g["name"] if g else "", "released": g["released"] if g else "",
                             "offers": offers})
    merged = {}  # two groups that ended up with the same set, type and name are the same product
    for pr in products:
        key = (pr["set"], pr["type"], pr["name"])
        if key not in merged:
            merged[key] = pr
            continue
        by_store = {o["store"]: o for o in merged[key]["offers"]}
        for o in pr["offers"]:
            cur = by_store.get(o["store"])
            if cur is None:
                by_store[o["store"]] = o
            else:
                keep, lose = (o, cur) if (o["in_stock"], -o["price"]) > (cur["in_stock"], -cur["price"]) else (cur, o)
                by_store[o["store"]] = keep
                dropped.append([o["store"], pr["name"], "same store lists this product twice; kept the cheaper in-stock one", lose["price"], lose["url"]])
        merged[key]["offers"] = list(by_store.values())
    products = list(merged.values())
    products.sort(key=lambda p: (p["set"].lower(), p["name"].lower()))
    used = set()
    for pr in products:  # a stable web address per product
        base = slugify(pr["name"]) or "product"
        slug, k = base, 2
        while slug in used:
            slug, k = f"{base}-{k}", k + 1
        used.add(slug)
        pr["id"] = slug
    prods = update_history(products, today)
    n_pages = write_product_pages(products, prods, os.path.dirname(out_js) or ".")
    print(f"Wrote {n_pages} product pages and updated {HISTORY_JSON}.")

    prev = {}  # what the page showed last time, so anything that vanished is explained
    if os.path.exists(out_js):
        try:
            old = json.loads(open(out_js, encoding="utf-8").read().split("window.PRICES = ", 1)[1].rsplit(";", 1)[0])
            for pr in old.get("products", []):
                for o in pr["offers"]:
                    prev[o["url"]] = (o["store"], pr["name"], o["price"])
            os.makedirs("backups", exist_ok=True)
            shutil.copyfile(out_js, os.path.join("backups", "data_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".js"))
            for oldf in sorted(os.listdir("backups"))[:-10]:
                os.remove(os.path.join("backups", oldf))
        except Exception:
            prev = {}
    os.makedirs(os.path.dirname(out_js) or ".", exist_ok=True)
    with open(out_js, "w", encoding="utf-8") as f:
        f.write("// Generated by collect_prices.py. Do not edit by hand.\n")
        f.write("window.PRICES = " + json.dumps({"updated": today, "products": products}, separators=(",", ":"), ensure_ascii=False) + ";\n")
    new_urls = {o["url"] for pr in products for o in pr["offers"]}
    why_by_url = {row[4]: row[2] for row in dropped}
    why_by_url.update({row[4]: "price looked wrong (outside the normal range, or far below other stores)" for row in susp})
    gone = [[st, nm, pr, url, why_by_url.get(url, "no longer in the store's product feed (removed by the store, or the store wasn't re-collected)")]
            for url, (st, nm, pr) in prev.items() if url not in new_urls]
    with open(CHANGES, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["store", "product", "price", "url", "why_it_is_gone"]); w.writerows(gone)
    with open(DROPPED, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["store", "title", "why", "price", "url"]); w.writerows(dropped)
    watch_missing = 0
    if os.path.exists(WATCHLIST):  # listings you told us must always be there
        rows = []
        for r in csv.DictReader(open(WATCHLIST, newline="", encoding="utf-8-sig")):
            store, prod = (r.get("store") or "").lower(), (r.get("product") or "").lower()
            try:
                cap = float(r.get("max_price") or 0) or None
            except ValueError:
                cap = None
            hits = [o["price"] for pr in products if prod in pr["name"].lower() for o in pr["offers"]
                    if store in o["store"].lower() and (cap is None or o["price"] <= cap)]
            watch_missing += not hits
            rows.append(["OK" if hits else "MISSING", r.get("store"), r.get("product"), r.get("max_price"), min(hits) if hits else ""])
        with open(WATCH_REPORT, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f); w.writerow(["status", "store", "product", "max_price", "found_price"]); w.writerows(rows)
    with open(SUSPICIOUS, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["store", "title", "type", "price", "url"]); w.writerows(susp)
    with open(ASSUMED, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["store", "title", "treated_as", "why", "url"]); w.writerows(assumed)
    with open(REPORT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["store", "website", "method", "collections_used", "products_seen", "sealed_candidates", "kept", "hit_page_cap", "error", "fetched"])
        w.writerows(report)
    return len(products), sum(1 for p in products if not p["set"]), len(susp), len(gone), watch_missing, len(dropped)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--rebuild", action="store_true", help="no downloading; rebuild from saved files")
    ap.add_argument("--delay", type=float, default=2.0)
    ap.add_argument("--max-pages", type=int, default=40, help="pages of 250 when reading a full catalog")
    ap.add_argument("--coll-pages", type=int, default=6, help="pages of 250 per sealed collection")
    ap.add_argument("--refresh-hours", type=float, default=12)
    ap.add_argument("--out", default=os.path.join("site", "data.js"))
    ap.add_argument("--shard", default="", help="i/N: only do every Nth store (used by the GitHub workflow)")
    ap.add_argument("--stores", default="", help='only stores whose name contains one of these words, e.g. "Many Realms,Flipside"')
    ap.add_argument("--pages-only", action="store_true", help="only make the product pages from site/data.js and data/history.json (no store requests)")
    ap.add_argument("--no-build", action="store_true", help="collect only; do not write the website data")
    args = ap.parse_args()
    try:  # Windows consoles can crash on accented or emoji characters in store names
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if args.pages_only:  # used by GitHub: build the product pages from data you already collected
        try:
            raw = open(args.out, encoding="utf-8").read()
            products = json.loads(raw.split("window.PRICES = ", 1)[1].rsplit(";", 1)[0])["products"]
            prods = json.load(open(HISTORY_JSON, encoding="utf-8")).get("products", {})
        except Exception as e:
            print(f"Could not read {args.out} or {HISTORY_JSON}: {e}")
            sys.exit(1)
        if any("id" not in p for p in products):
            print("data.js has no product ids. Run collect_prices.py (or --rebuild) on your computer first, then push again.")
            sys.exit(1)
        print(f"Wrote {write_product_pages(products, prods, os.path.dirname(args.out) or '.')} product pages from {args.out}.")
        return

    try:
        import ssl, certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        urllib.request.install_opener(urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx)))
        print("Using certifi certificates.")
    except ImportError:
        print("certifi not installed; using Python's default certificates.")
    agent = f"MTGSealedPriceTracker/0.2 (community project; contact: {CONTACT_EMAIL})"

    if not args.rebuild:
        try:
            with open(INPUT, newline="", encoding="utf-8-sig") as f:
                stores = [r for r in csv.DictReader(f) if r["is_shopify"] == "yes" and r["website"]]
        except FileNotFoundError:
            print(f"Cannot find {INPUT}. Put it in the same folder as this script (the folder you opened the terminal in).")
            return
        if args.stores:
            wanted = [w.strip().lower() for w in args.stores.split(",") if w.strip()]
            stores = [r for r in stores if any(w in r["name"].lower() for w in wanted)]
        elif not args.all:
            stores = stores[:5]
        if args.shard:
            i, n = (int(x) for x in args.shard.split("/"))
            stores = [r for k, r in enumerate(stores) if k % n == i]
        print(f"{len(stores)} stores to check this run.")
        os.makedirs(CACHE_DIR, exist_ok=True)
        today = datetime.date.today().isoformat()
        try:
            for n, s in enumerate(stores, 1):
                site, name = s["website"], s["name"]
                if cache_is_fresh(site, args.refresh_hours):
                    print(f"[{n}/{len(stores)}] {name}: recent data saved, skipping")
                    continue
                rec = {"v": CACHE_VERSION, "store": name, "site": site, "fetched": today, "raw": []}
                try:
                    rec.update(collect_store(site, agent, args.delay, args.max_pages, args.coll_pages))
                    print(f"[{n}/{len(stores)}] {name}: {rec['method']}, {rec['seen']} products read, {len(rec['raw'])} sealed candidates"
                          + (" (hit page cap)" if rec["capped"] else ""))
                    new_hist = not os.path.exists(HISTORY)
                    with open(HISTORY, "a", newline="", encoding="utf-8") as hf:
                        w = csv.writer(hf)
                        if new_hist:
                            w.writerow(["date", "store", "title", "price", "in_stock", "url"])
                        w.writerows([[today, name, r["t"], r["p"], r["a"], f"{site}/products/{r['h']}"] for r in rec["raw"]])
                except urllib.error.HTTPError as e:
                    rec["error"] = f"HTTP {e.code}"
                    print(f"[{n}/{len(stores)}] {name}: failed (HTTP {e.code})")
                except Exception as e:
                    rec["error"] = f"{type(e).__name__}: {str(getattr(e, 'reason', e))[:80]}"
                    print(f"[{n}/{len(stores)}] {name}: failed ({rec['error']})")
                json.dump(rec, open(cache_path(site), "w", encoding="utf-8"))
                time.sleep(args.delay)
        except KeyboardInterrupt:
            print("\nStopped early. Building from what was saved so far.")

    if args.no_build:
        print("Collected. Not building the website data (--no-build).")
        return
    n_prod, n_noset, n_susp, n_gone, n_watch, n_drop = build_outputs(args.out, load_sets(agent))
    print(f"Done: {n_prod} products ({n_noset} without a matched set) written to {args.out}.")
    print(f"  {n_susp} suspicious prices set aside ({SUSPICIOUS}); {n_drop} listings left out, each with a reason ({DROPPED}).")
    print(f"  {n_gone} listings were on the page last time and are gone now; see {CHANGES} for why.")
    if os.path.exists(WATCHLIST):
        print(f"  Watchlist: {'ALL OK' if not n_watch else str(n_watch) + ' MISSING, see ' + WATCH_REPORT}.")


if __name__ == "__main__":
    main()
