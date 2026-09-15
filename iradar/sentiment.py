"""What the world says about the watched products.

The page watch answers "what changed on infineon.com"; this layer answers
"how did engineers react". Entities are the product FAMILIES (PSOC(tm) MCUs,
AURIX(tm), OPTIGA(tm)...) derived from the watched Product-family pages - not
individual part numbers, which have near-zero chatter. For each entity,
collectors poll free public sources and store one row per mention; an LLM
scores them (-1..+1 plus topic tags) when a key is configured, and everything
stays browsable unscored until then.

Sources (all keyless unless noted):
  stackexchange  api.stackexchange.com - electronics.SE + stackoverflow
  hn             hn.algolia.com - stories and comments
  reddit         official OAuth API - needs REDDIT_CLIENT_ID/REDDIT_SECRET in
                 the environment (the public JSON endpoint answers 403 from
                 here; verified 2026-07-21); skipped without them
  community      Infineon's own developer-community threads, read from the
                 flags.developer_community.threads list each capture stores
  youtube        needs YT_API_KEY in the environment - skipped without it
  llm            web-grounded summary per entity - needs ANTHROPIC_API_KEY

Collection never runs while a capture run is active (shared bandwidth and
bot-guard), and paces itself politely per host.
"""
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import db

UA = ("iRadar/1.0 (private change monitor; sentiment layer; "
      "contact: local)")

SENT_STATE = {"running": False, "message": "", "done": 0, "total": 0,
              "found": 0, "new": 0,
              # scoring runs on its own clock, and for long enough
              # that the page has to be able to watch it
              "scoring": False, "scored": 0, "to_score": 0,
              "stop_scoring": False,
              "tokens_in": 0, "tokens_out": 0}

# family names that are distinctive enough to search bare; everything else
# gets an "infineon" qualifier so generic names ("Contactless memories")
# do not vacuum up unrelated chatter
_DISTINCTIVE = ("PSOC", "PSoC", "XMC", "AIROC", "OPTIGA", "AURIX", "TRAVEO",
                "CIPURSE", "Calypso", "XENSIV", "CoolMOS", "CoolGaN",
                "CoolSiC", "EasyPIM", "HybridPACK", "ModusToolbox", "TriCore",
                "SECORA", "iMOTION", "EiceDRIVER", "OptiMOS", "StrongIRFET",
                # the wider brand shelf - every one is an Infineon trade name
                # distinctive enough to search bare
                "MOTIX", "PROFET", "LITIX", "HITFET", "CoolSET", "TRENCHSTOP",
                "CIPOS", "MERUS", "EZ-USB", "EZ-PD", "OPTIREG", "EasyPACK",
                "EconoPACK", "EconoDUAL")


# The other names the same silicon answers to. Infineon bought Cypress in
# 2020 and the internet did not rename its bookmarks: people still write
# "Cypress PSoC", part numbers ("CY8C...", the CYW43 in every Pi Pico W),
# and tool names (ModusToolbox) far more often than the family name. An
# alias is searched AS ITS OWN QUERY and stored under the family, and the
# relevance gate judges each hit against the query that found it.
_ALIASES = {
    "PSOC": ["CY8C", "Cypress PSoC", "ModusToolbox"],
    "AIROC": ["CYW43", "CYW20", "CYW55", "Cypress WICED"],
    "AURIX": ["TriCore"],
    "TRAVEO": ["Cypress Traveo"],
    "OPTIGA": ["OPTIGA Trust M"],
}

# a two-word cut of a long family name must not end on a joining word -
# "Bias and control" became the query "infineon Bias and", which finds
# nothing and reads like a bug
_QUERY_STOP = {"and", "for", "of", "the", "with", "to", "in"}


def _clean_family(title):
    """'PSOC(tm) MCUs | Infineon Technologies' -> 'PSOC MCUs'."""
    t = title.split("|")[0]
    t = t.replace("™", "").replace("®", "")     # (tm), (r)
    return re.sub(r"\s+", " ", t).strip()


_ENT_CACHE = {"at": 0.0, "val": None}
_PL_CACHE = {"at": 0.0, "val": None}


def entity_lines():
    """Which product line each searched term belongs to: {term: {...}}.

    DERIVED, NOT TYPED IN. Every watched page already carries its BL and PL
    from the CSS export, and a term like AIROC or OPTIGA is named by the
    titles and addresses of the pages it belongs to - so the product line of
    a term is the product line of the pages that name it. Nobody maintains a
    second list that goes stale the moment PIM moves a family.

    A term genuinely spans several lines (PSOC sits under more than one), so
    the whole set is kept and `pl` is only the one that carries the most
    pages - enough to sort and filter by, honest about the rest.
    """
    if _PL_CACHE["val"] is not None and time.time() - _PL_CACHE["at"] < 300:
        return _PL_CACHE["val"]
    con = db.connect()
    pages = con.execute(
        "SELECT COALESCE(NULLIF(u.title_override,''), NULLIF(s.page_title,''),"
        " '') t, u.url u, IFNULL(u.pl,'') pl, IFNULL(u.bl,'') bl FROM urls u"
        " LEFT JOIN (SELECT url_id, MAX(id) mid FROM snapshots"
        "            WHERE status='ok' GROUP BY url_id) m ON m.url_id=u.id"
        " LEFT JOIN snapshots s ON s.id=m.mid"
        " WHERE u.active=1 AND (IFNULL(u.pl,'')<>'' OR IFNULL(u.bl,'')<>'')"
    ).fetchall()
    con.close()
    hay = [((r["t"] + " " + r["u"]).lower(), r["pl"], r["bl"]) for r in pages]
    out = {}
    for e in entities():
        pls, bls = {}, {}
        # match on the term and every alias it is searched under, because
        # "CYW43" names AIROC pages that never write the word AIROC
        needles = [n.lower() for n in
                   ([e["entity"]] + list(e.get("queries") or []))]
        needles = [n[len("infineon "):] if n.startswith("infineon ") else n
                   for n in needles]
        needles = [n for n in needles if len(n) >= 3]
        for text, pl, bl in hay:
            if not any(n in text for n in needles):
                continue
            if pl:
                pls[pl] = pls.get(pl, 0) + 1
            if bl:
                bls[bl] = bls.get(bl, 0) + 1
        out[e["entity"]] = {
            "pls": sorted(pls, key=lambda k: -pls[k]),
            "bls": sorted(bls, key=lambda k: -bls[k]),
            "pl": max(pls, key=pls.get) if pls else "",
            "bl": max(bls, key=bls.get) if bls else "",
            "pages": sum(pls.values()) or sum(bls.values()),
        }
    _PL_CACHE.update(at=time.time(), val=out)
    return out


# The periods a pulse check is read over. "Monthly" is a WINDOW, not a
# calendar month: somebody asking what the market said this month on the 2nd
# does not want two days of data.
PERIODS = (("30d", "Last 30 days", 30), ("90d", "Last quarter", 91),
           ("365d", "Last year", 365))


def period_days(key):
    return dict((k, d) for k, _l, d in PERIODS).get(key or "", 0)


def _term_overrides():
    """What the team changed by hand: {'removed': set, 'custom': [{entity,
    queries}]}. Stored in settings as JSON so it survives every restart."""
    s = db.get_settings()
    try:
        removed = set(json.loads(s.get("sn_removed") or "[]"))
    except Exception:
        removed = set()
    try:
        custom = json.loads(s.get("sn_custom") or "[]")
    except Exception:
        custom = []
    return removed, custom


def entities():
    """Everything searched, [{entity, query, queries, origin}].

    Three origins. 'brand': a distinctive Infineon trade name (PSOC, AURIX,
    TRENCHSTOP...) found ANYWHERE in the watched pages' titles or addresses -
    which is what admits AURIX and TRAVEO, whose part pages are watched but
    whose product-family page is not. 'pages': a generic family name from
    the product hierarchy, searched as "infineon <name>". 'custom': added by
    hand on the page. Hand-removed terms are dropped whatever their origin.
    Cached for a minute - the product-page box calls this per view."""
    if _ENT_CACHE["val"] is not None and time.time() - _ENT_CACHE["at"] < 60:
        return _ENT_CACHE["val"]
    con = db.connect()
    fam_rows = con.execute(
        "SELECT COALESCE(NULLIF(u.title_override,''), NULLIF(s.page_title,''),"
        " u.url) t FROM urls u"
        " JOIN (SELECT url_id, MAX(id) mid FROM snapshots WHERE status='ok'"
        "       GROUP BY url_id) m ON m.url_id=u.id"
        " JOIN snapshots s ON s.id=m.mid"
        " WHERE u.active=1 AND u.ptype='Product' AND u.level IN ('PFP','PSFP')"
    ).fetchall()
    # one big haystack of everything we watch: titles and addresses
    hay = " ".join(
        (r["t"] or "") + " " + (r["u"] or "") for r in con.execute(
            "SELECT COALESCE(NULLIF(u.title_override,''),"
            " NULLIF(s.page_title,''), '') t, u.url u FROM urls u"
            " LEFT JOIN (SELECT url_id, MAX(id) mid FROM snapshots"
            "            WHERE status='ok' GROUP BY url_id) m ON m.url_id=u.id"
            " LEFT JOIN snapshots s ON s.id=m.mid WHERE u.active=1"))
    con.close()
    removed, custom = _term_overrides()

    out, seen = [], set()

    def _add(entity, queries, origin):
        if entity.lower() in seen or entity in removed:
            return
        qs = []
        for q in queries:
            if q.lower() not in seen:
                qs.append(q)
                seen.add(q.lower())
        if not qs:
            return
        seen.add(entity.lower())
        out.append({"entity": entity, "query": qs[0], "queries": qs,
                    "origin": origin})

    # the brand shelf, admitted by ANY watched page naming it
    canon = {}
    for d in _DISTINCTIVE:
        canon.setdefault(d.upper(), d if d.isupper() else d)
    for tok in canon.values():
        if re.search(r"(?<![a-z0-9])" + re.escape(tok), hay, re.I):
            name = tok.upper() if len(tok) <= 6 and "-" not in tok else tok
            _add(name, [tok] + _ALIASES.get(name, _ALIASES.get(tok, [])),
                 "brand")

    # the generic families from the product hierarchy, as before
    for r in fam_rows:
        name = _clean_family(r["t"])
        if not name:
            continue
        if any(re.search(r"(?<![a-z0-9])" + re.escape(d), name, re.I)
               for d in _DISTINCTIVE):
            continue                    # its brand already carries it
        words = [w for w in name.split()[:2]]
        if len(words) == 2 and words[1].lower() in _QUERY_STOP:
            words = words[:1]
        _add(name, ["infineon " + " ".join(words)], "pages")

    # hand-added terms win their spot whatever else exists
    for c in custom:
        _add(str(c.get("entity", "")).strip(),
             [q for q in (c.get("queries") or []) if q] or
             [str(c.get("entity", "")).strip()], "custom")

    _ENT_CACHE.update(at=time.time(), val=out)
    return out


def _get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _iso(epoch):
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(int(epoch)))
    except Exception:
        return ""


# --- parsers: pure functions over each API's payload (offline-testable) -----

def parse_stackexchange(payload, site):
    out = []
    for q in (payload or {}).get("items", []):
        out.append({
            "url": q.get("link") or "",
            "title": (q.get("title") or "").strip(),
            "body": "",
            "author": ((q.get("owner") or {}).get("display_name") or ""),
            "posted": _iso(q.get("creation_date") or 0),
            "extra": f"{site} | score {q.get('score', 0)}"
                     f" | {q.get('answer_count', 0)} answers",
        })
    return [m for m in out if m["url"] and m["title"]]


def parse_hn(payload):
    out = []
    for h in (payload or {}).get("hits", []):
        title = (h.get("title") or h.get("story_title") or "").strip()
        body = re.sub(r"<[^>]+>", " ", h.get("comment_text") or "")
        body = re.sub(r"\s+", " ", body).strip()[:400]
        hid = h.get("objectID") or ""
        out.append({
            "url": f"https://news.ycombinator.com/item?id={hid}" if hid else "",
            "title": title or (body[:80] + "…" if body else ""),
            "body": body,
            "author": h.get("author") or "",
            "posted": (h.get("created_at") or "")[:19],
            "extra": "hn",
        })
    return [m for m in out if m["url"] and m["title"]]


def parse_reddit(payload):
    out = []
    for c in ((payload or {}).get("data") or {}).get("children", []):
        d = c.get("data") or {}
        perma = d.get("permalink") or ""
        body = re.sub(r"\s+", " ", (d.get("selftext") or ""))[:400]
        out.append({
            "url": f"https://www.reddit.com{perma}" if perma else (d.get("url") or ""),
            "title": (d.get("title") or "").strip(),
            "body": body,
            "author": d.get("author") or "",
            "posted": _iso(d.get("created_utc") or 0),
            "extra": f"r/{d.get('subreddit', '')} | {d.get('num_comments', 0)} comments",
        })
    return [m for m in out if m["url"] and m["title"]]


# Brand names that mean something ELSE out in the world: XMC is also a PCIe
# mezzanine standard (VITA 42), Calypso an orchid genus / music / a transit
# standard, TriCore generic-sounding. A bare word-match is not enough for
# these - the text must also carry one of the family's context words.
# The same letters meaning something else entirely: "PSOC" in a health
# forum is a typo of PCOS (polycystic ovary syndrome) and has nothing to do
# with microcontrollers. No model needed - the give-away words are always
# in the text, so a token can carry NEGATIVE context: any of these present
# and the mention is rejected no matter what else matches.
_NEGATIVE_CTX = {
    "psoc": ("pcos", "ovar", "hormon", "fertilit", "cyst", "insulin",
             "metformin", "menstru", "period", "spironolactone", "diagnos"),
    "calypso": ("orchid", "song", "album", "band", "music", "beach"),
    "merus": ("winery", "wine", "cabernet"),
}

_AMBIGUOUS_CTX = {
    "xmc": ("infineon", "microcontroller", "mcu", "xmc1", "xmc4", "dave"),
    "calypso": ("infineon", "smart card", "smartcard", "javacard", "nfc",
                "transit"),
    "tricore": ("infineon", "aurix", "mcu", "microcontroller"),
}


def _relevant(m, query):
    """Does the STORED text actually involve the searched product?
    Full-text APIs match words anywhere in a discussion, so a search for
    'infineon Smart card' returns threads whose stored title/body never says
    either - and unrelated chatter poisons a sentiment average far worse than
    a dropped borderline mention. Precision first: the distinctive token (or
    'infineon', or the family phrase) must appear AS A WORD in what we keep -
    'Calypsoinae' (an orchid) must not pass as Calypso - and the ambiguous
    brands additionally need a context word."""
    text = (m.get("title", "") + " " + m.get("body", "")).lower()
    toks = query.lower().split()
    if "infineon" in toks:
        # A generic family name ("Diodes", "Bias") is only attributable when
        # the text actually says infineon - the phrase alone let "my garden
        # bias and control" pass for "infineon Bias". Precision first: a
        # generic-family thread that never names the maker is not evidence
        # about the maker.
        return "infineon" in text
    hay = text + " " + (m.get("url") or "").lower()
    for t in toks:
        # left edge hard; right edge allows digits/symbols but not letters,
        # so 'psoc6' and 'xmc4700' match while 'calypsoinae' does not
        if not re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z])", text):
            continue
        # the letters can belong to a different world entirely - "psoc" in
        # an r/PCOS thread is a typo for a syndrome, not a microcontroller.
        # The URL counts as evidence too: the subreddit is right there in it.
        neg = _NEGATIVE_CTX.get(t)
        if neg and any(n in hay for n in neg):
            continue
        ctx = _AMBIGUOUS_CTX.get(t)
        if not ctx or any(c in text for c in ctx):
            return True
    return False


# --- collectors -------------------------------------------------------------

# Probed 2026-07-28 with a real family name: these four answer with relevant
# hits. robotics and iot returned nothing for any Infineon term and are left
# out rather than costing a request per family for zero results.
SE_SITES = ("electronics", "stackoverflow", "arduino", "raspberrypi")


def collect_stackexchange(query):
    out = []
    for site in SE_SITES:
        for page in (1, 2, 3, 4):
            u = ("https://api.stackexchange.com/2.3/search/advanced?order=desc"
                 "&sort=creation&pagesize=100&page=%d&q=" % page
                 + urllib.parse.quote(query) + "&site=" + site)
            try:
                payload = _get_json(u)
            except Exception:
                break
            out += parse_stackexchange(payload, site)
            time.sleep(0.4)
            if not (payload or {}).get("has_more"):
                break
    return [m for m in out if _relevant(m, query)]


def collect_hn(query):
    out = []
    for page in (0, 1, 2, 3):
        u = ("https://hn.algolia.com/api/v1/search?hitsPerPage=100&page=%d"
             "&query=" % page + urllib.parse.quote(query))
        try:
            payload = _get_json(u)
        except Exception:
            break
        hits = parse_hn(payload)
        out += hits
        if len((payload or {}).get("hits") or []) < 100:
            break
        time.sleep(0.3)
    return [m for m in out if _relevant(m, query)]


_REDDIT_TOKEN = {"value": "", "until": 0.0}


def _reddit_token():
    cid = os.environ.get("REDDIT_CLIENT_ID", "")
    sec = os.environ.get("REDDIT_SECRET", "")
    if not cid or not sec:
        return ""
    if _REDDIT_TOKEN["value"] and time.time() < _REDDIT_TOKEN["until"]:
        return _REDDIT_TOKEN["value"]
    import base64
    req = urllib.request.Request(
        "https://www.reddit.com/api/v1/access_token", method="POST",
        data=b"grant_type=client_credentials",
        headers={"User-Agent": UA,
                 "Authorization": "Basic " + base64.b64encode(
                     f"{cid}:{sec}".encode()).decode()})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            tok = json.loads(r.read())
        _REDDIT_TOKEN.update(value=tok.get("access_token", ""),
                             until=time.time() + int(tok.get("expires_in", 0)) - 60)
        return _REDDIT_TOKEN["value"]
    except Exception:
        return ""


def _collect_reddit_oauth(query):
    """Official OAuth API only: the public search.json answers 403 from this
    network. Without REDDIT_CLIENT_ID/REDDIT_SECRET this source is skipped."""
    tok = _reddit_token()
    if not tok:
        return []
    u = ("https://oauth.reddit.com/search?limit=25&sort=new&t=year&q="
         + urllib.parse.quote(query))
    req = urllib.request.Request(u, headers={"User-Agent": UA,
                                             "Authorization": "Bearer " + tok})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            res = parse_reddit(json.loads(r.read()))
    except Exception:
        res = []
    time.sleep(1.0)
    return [m for m in res if _relevant(m, query)]


def parse_reddit_rss(xml_text):
    """Atom entries -> mentions. The RSS twin of /search.json: same results,
    different gate - .json answers 403 from a datacenter address while .rss
    answers 200 to our own UA (verified from the live box, 2026-07-28)."""
    import xml.etree.ElementTree as ET
    ns = {"a": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return []
    out = []
    for e in root.findall("a:entry", ns):
        def _t(tag):
            el = e.find("a:" + tag, ns)
            return (el.text or "") if el is not None else ""
        link = e.find("a:link", ns)
        who = e.find("a:author/a:name", ns)
        body = re.sub(r"<[^>]+>", " ", _t("content"))
        out.append({
            "url": (link.get("href") or "") if link is not None else "",
            "title": " ".join(_t("title").split()),
            "body": " ".join(body.split())[:400],
            "author": ((who.text or "").replace("/u/", "")
                       if who is not None else ""),
            "posted": _t("updated")[:19],
            "extra": "reddit",
        })
    return [m for m in out if m["url"] and m["title"]]


def collect_reddit(query):
    """OAuth when the keys exist (scores, full selftext); the public RSS
    feed otherwise, so Reddit needs no account at all. One request per
    family and a pause - two back-to-back calls already earn a 429."""
    if os.environ.get("REDDIT_CLIENT_ID") and os.environ.get("REDDIT_SECRET"):
        return _collect_reddit_oauth(query)
    u = ("https://www.reddit.com/search.rss?sort=new&limit=100&q="
         + urllib.parse.quote(query))
    try:
        req = urllib.request.Request(u, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            xml_text = r.read().decode("utf-8", "replace")
    except Exception:
        return []
    finally:
        time.sleep(2.5)
    return [m for m in parse_reddit_rss(xml_text) if _relevant(m, query)]


# --- GitHub -----------------------------------------------------------------
# Where embedded people actually file their complaints: a search for one
# family returned 420 issues. Unauthenticated search allows 10 requests a
# minute, which is the whole reason this paces itself; GITHUB_TOKEN raises it
# to 30 and the pause shortens to match. No token is required.

def _github_pause():
    return 2.2 if os.environ.get("GITHUB_TOKEN") else 6.5


def parse_github(payload):
    out = []
    for it in (payload or {}).get("items", []):
        body = re.sub(r"\s+", " ", (it.get("body") or ""))[:400]
        repo = (it.get("repository_url") or "").rsplit("/", 2)[-2:]
        out.append({
            "url": it.get("html_url") or "",
            "title": (it.get("title") or "").strip(),
            "body": body,
            "author": ((it.get("user") or {}).get("login") or ""),
            "posted": (it.get("created_at") or "")[:19],
            "extra": "%s | %s | %d comment(s)" % (
                "/".join(repo) if len(repo) == 2 else "github",
                it.get("state") or "", it.get("comments") or 0),
        })
    return [m for m in out if m["url"] and m["title"]]


def collect_github(query):
    """Issues and pull requests naming the product."""
    head = {"Accept": "application/vnd.github+json"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        head["Authorization"] = "Bearer " + tok
    out = []
    for page in (1, 2, 3, 4, 5):
        u = ("https://api.github.com/search/issues?per_page=30&page=%d"
             "&sort=created&order=desc&q=" % page + urllib.parse.quote(query))
        try:
            req = urllib.request.Request(u, headers={"User-Agent": UA, **head})
            with urllib.request.urlopen(req, timeout=20) as r:
                payload = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            break                      # 403 = rate limit; next pass gets it
        finally:
            time.sleep(_github_pause())
        batch = parse_github(payload)
        out += batch
        if len(batch) < 30:
            break                      # the well ran dry; stop paying pauses
    return [m for m in out if _relevant(m, query)]


# --- Discourse forums -------------------------------------------------------
# One collector, many forums: Discourse exposes the same /search.json
# everywhere, so adding a forum is one line here. Only hosts that answered an
# unauthenticated probe are listed - Adafruit, Nordic and the Raspberry Pi
# forum all return 403 to a robot and are deliberately absent.

DISCOURSE_HOSTS = (("forum.arduino.cc", "Arduino forum"),
                   ("community.platformio.org", "PlatformIO"))


def parse_discourse(payload, host, label):
    blurbs = {}
    for post in (payload or {}).get("posts", []) or []:
        tid = post.get("topic_id")
        if tid and tid not in blurbs:
            txt = re.sub(r"<[^>]+>", " ", post.get("blurb") or "")
            blurbs[tid] = re.sub(r"\s+", " ", txt).strip()[:400]
    out = []
    for t in (payload or {}).get("topics", []) or []:
        tid = t.get("id")
        out.append({
            "url": "https://%s/t/%s/%s" % (host, t.get("slug", ""), tid),
            "title": (t.get("title") or "").strip(),
            "body": blurbs.get(tid, ""),
            "author": "",
            "posted": (t.get("created_at") or "")[:19],
            "extra": "%s | %s repl(ies)" % (label,
                                            max(0, (t.get("posts_count") or 1) - 1)),
        })
    return [m for m in out if m["title"]]


def collect_forum(query):
    out = []
    for host, label in DISCOURSE_HOSTS:
        for page in (1, 2, 3, 4, 5):
            u = ("https://%s/search.json?page=%d&q=%s"
                 % (host, page, urllib.parse.quote(query)))
            try:
                payload = _get_json(u)
            except Exception:
                break
            batch = parse_discourse(payload, host, label)
            out += batch
            time.sleep(0.5)
            if len((payload or {}).get("topics") or []) < 20:
                break
    return [m for m in out if _relevant(m, query)]


# --- WordPress blogs --------------------------------------------------------
# Same trick as Discourse: /wp-json/wp/v2/posts is uniform, so the two
# embedded-news sites that matter cost one collector between them. The
# /search endpoint carries no date or excerpt; /posts carries both.

WP_HOSTS = (("hackaday.com", "Hackaday"),
            ("www.cnx-software.com", "CNX Software"))


def _untag(html):
    txt = re.sub(r"<[^>]+>", " ", html or "")
    txt = (txt.replace("&#8217;", "'").replace("&#8220;", '"')
              .replace("&#8221;", '"').replace("&amp;", "&")
              .replace("&#8230;", "…").replace("&nbsp;", " "))
    return re.sub(r"\s+", " ", txt).strip()


def parse_wordpress(payload, label):
    out = []
    for p in payload or []:
        out.append({
            "url": p.get("link") or "",
            "title": _untag(((p.get("title") or {}).get("rendered") or ""))[:300],
            "body": _untag(((p.get("excerpt") or {}).get("rendered") or ""))[:400],
            "author": "",
            "posted": (p.get("date") or "")[:19],
            "extra": label,
        })
    return [m for m in out if m["url"] and m["title"]]


def collect_blog(query):
    out = []
    for host, label in WP_HOSTS:
        u = ("https://%s/wp-json/wp/v2/posts?per_page=50&search=%s"
             % (host, urllib.parse.quote(query)))
        try:
            out += parse_wordpress(_get_json(u), label)
        except Exception:
            pass
        time.sleep(0.5)
    return [m for m in out if _relevant(m, query)]


# words that start many family names and identify none of them - matching on
# these filed AURIX pages under "32-bit FM Arm..." for as long as the
# community collector existed, because "32-bit" appears in both titles
_GENERIC_LEAD = {"32-bit", "16-bit", "8-bit", "arm", "infineon",
                 "automotive", "industrial", "smart", "high", "low"}


def entity_for(title):
    """Which product family a page title belongs to. A distinctive token
    (PSOC, AURIX, XMC...) wins outright; else the first family whose leading
    word appears in the title, provided that word actually identifies a
    family. Returns '' when there is nothing to say."""
    page = _clean_family(title or "")
    if not page:
        return ""
    tok = next((d for d in _DISTINCTIVE
                if re.search(r"\b" + re.escape(d), page, re.I)), None)
    if tok:
        return tok
    for e in entities():
        lead = e["entity"].split()[0]
        if lead.lower() in _GENERIC_LEAD:
            continue
        if re.search(r"(?<!\w)" + re.escape(lead), page, re.I):
            return e["entity"]
    return page


def collect_community():
    """Threads from Infineon's own developer community, as captured on each
    page (flags.developer_community.threads). Entity = the page's product
    family when derivable, else the page title. Zero network."""
    con = db.connect()
    rows = con.execute(
        "SELECT s.dir, s.page_title FROM snapshots s"
        " JOIN (SELECT url_id, MAX(id) mid FROM snapshots WHERE status='ok'"
        "       AND dir<>'' GROUP BY url_id) m ON s.id=m.mid"
        " JOIN urls u ON u.id=s.url_id WHERE u.active=1").fetchall()
    con.close()
    out = []
    for r in rows:
        p = Path(r["dir"]) / "sections.json"
        if not p.exists():
            continue
        try:
            sec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        threads = ((sec.get("flags") or {}).get("developer_community")
                   or {}).get("threads") or []
        if not threads:
            continue
        ent = entity_for(r["page_title"])
        for t in threads:
            out.append({
                "entity": ent or "Infineon",
                "url": t.get("url") or "",
                "title": (t.get("title") or "").strip(),
                "body": "",
                "author": "",
                "posted": "",
                "extra": (t.get("flag") or "") + (" | " + t["when"] if t.get("when") else ""),
            })
    return [m for m in out if m["url"] and m["title"]]


def _collect_youtube_api(query):
    key = os.environ.get("YT_API_KEY", "")
    if not key:
        return []
    u = ("https://www.googleapis.com/youtube/v3/search?part=snippet&type=video"
         "&maxResults=20&q=" + urllib.parse.quote(query) + "&key=" + key)
    try:
        payload = _get_json(u)
    except Exception:
        return []
    out = []
    for it in payload.get("items", []):
        vid = ((it.get("id") or {}).get("videoId") or "")
        sn = it.get("snippet") or {}
        out.append({
            "url": f"https://www.youtube.com/watch?v={vid}" if vid else "",
            "title": (sn.get("title") or "").strip(),
            "body": (sn.get("description") or "")[:300],
            "author": sn.get("channelTitle") or "",
            "posted": (sn.get("publishedAt") or "")[:19],
            "extra": "youtube",
        })
    return [m for m in out if m["url"] and m["title"] and _relevant(m, query)]


# --- storage ----------------------------------------------------------------

# The key the youtube.com web page itself ships to every visitor - public by
# construction, not a credential. The endpoint is what the search box calls.
_INNERTUBE_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"


def parse_innertube(payload):
    """Walk the web client's response for videoRenderer objects wherever the
    layout put them - the path shifts, the object shape does not."""
    vids = []

    def walk(node):
        if isinstance(node, dict):
            vr = node.get("videoRenderer")
            if isinstance(vr, dict):
                vids.append(vr)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    out = []
    for v in vids:
        vid = v.get("videoId") or ""
        title = "".join(r.get("text", "")
                        for r in (v.get("title") or {}).get("runs", []))
        chan = "".join(r.get("text", "")
                       for r in (v.get("ownerText") or {}).get("runs", []))
        when = (v.get("publishedTimeText") or {}).get("simpleText") or ""
        desc = ""
        sn = v.get("detailedMetadataSnippets") or []
        if sn:
            desc = "".join(r.get("text", "") for r in
                           (sn[0].get("snippetText") or {}).get("runs", []))
        out.append({
            "url": ("https://www.youtube.com/watch?v=" + vid) if vid else "",
            "title": title.strip(),
            "body": desc.strip()[:400],
            "author": chan.strip(),
            "posted": "",              # the web client only says "2 weeks ago"
            "extra": ("youtube | " + when) if when else "youtube",
        })
    return [m for m in out if m["url"] and m["title"]]


def collect_youtube(query):
    """The official API when a key is set (absolute dates); otherwise the
    web client's own search endpoint, which needs nothing."""
    if os.environ.get("YT_API_KEY"):
        return _collect_youtube_api(query)
    body = json.dumps({
        "context": {"client": {"clientName": "WEB",
                               "clientVersion": "2.20240701"}},
        "query": query}).encode()
    try:
        req = urllib.request.Request(
            "https://www.youtube.com/youtubei/v1/search?key=" + _INNERTUBE_KEY,
            data=body, headers={"User-Agent": UA,
                                "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            payload = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return []
    finally:
        time.sleep(1.0)
    return [m for m in parse_innertube(payload) if _relevant(m, query)]


# --- the source registry ----------------------------------------------------
# ONE list. The collector loop, the page's filter chips, the per-family
# source counts and the "which of these can run" note all read this - adding
# a source is one entry here plus its collector, and nothing else changes.
#
# key      goes in mentions.source (stable, never renamed once data exists)
# label    what people read
# ready()  whether it can run right now (keys present); the UI says why not
# scope    "entity" = searched once per product family; "global" = one pass

SOURCES = [
    {"key": "stackexchange", "label": "StackExchange",
     "collect": collect_stackexchange, "scope": "entity",
     "ready": lambda: True, "needs": ""},
    {"key": "github", "label": "GitHub",
     "collect": collect_github, "scope": "entity",
     "ready": lambda: True, "needs": ""},
    {"key": "forum", "label": "Forums",
     "collect": collect_forum, "scope": "entity",
     "ready": lambda: True, "needs": ""},
    {"key": "blog", "label": "Blogs",
     "collect": collect_blog, "scope": "entity",
     "ready": lambda: True, "needs": ""},
    {"key": "hn", "label": "Hacker News",
     "collect": collect_hn, "scope": "entity",
     "ready": lambda: True, "needs": ""},
    {"key": "reddit", "label": "Reddit",
     "collect": collect_reddit, "scope": "entity",
     "ready": lambda: True, "needs": ""},
    {"key": "youtube", "label": "YouTube",
     "collect": collect_youtube, "scope": "entity",
     "ready": lambda: True, "needs": ""},
    {"key": "community", "label": "Infineon community",
     "collect": None, "scope": "global",
     "ready": lambda: True, "needs": ""},
]

SOURCE_LABELS = {s["key"]: s["label"] for s in SOURCES}


def sources_ready():
    return [s for s in SOURCES if s["ready"]()]


def _store(con, entity, source, mentions, now):
    new = 0
    for m in mentions:
        cur = con.execute(
            "INSERT OR IGNORE INTO mentions(entity, source, url, title, body,"
            " author, posted, fetched, reason)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (entity, source, m["url"], m["title"][:300], m.get("body", ""),
             m.get("author", "")[:80], m.get("posted", ""), now,
             m.get("extra", "")[:120]))
        new += cur.rowcount
    return new


def run_collect():
    """One full collection pass: every product family through every source
    that is ready, then the global ones. The registry is the only list."""
    con = db.connect()
    try:
        ents = entities()
        live = sources_ready()
        per_entity = [s for s in live if s["scope"] == "entity"]
        glob = [s for s in live if s["scope"] == "global"]
        SENT_STATE.update(running=True, done=0, found=0, new=0,
                          total=len(ents) * len(per_entity) + len(glob),
                          message="collecting mentions…")
        now = db.now_iso()
        new = 0
        for e in ents:
            for src in per_entity:
                for q in e.get("queries") or [e["query"]]:
                    ms = src["collect"](q)
                    SENT_STATE["found"] += len(ms)
                    new += _store(con, e["entity"], src["key"], ms, now)
                    # COMMIT NOW, not per entity: the first INSERT opens a
                    # transaction, and with the network pauses between
                    # sources a per-entity commit held the write lock for
                    # minutes - long enough to kill a capture run waiting
                    # its 30 seconds to start. Milliseconds, not minutes.
                    con.commit()
                    SENT_STATE["new"] = new
                SENT_STATE["done"] += 1
                SENT_STATE["message"] = f"{e['entity'][:30]} — {src['label']}"
        # the global pass: today that is the captured community threads,
        # which carry their own entity and cost no network at all
        for src in glob:
            SENT_STATE["message"] = src["label"]
            ms = collect_community()
            SENT_STATE["found"] += len(ms)
            for m in ms:
                new += _store(con, m["entity"], src["key"], [m], now)
            con.commit()
            SENT_STATE["done"] += 1
        SENT_STATE["new"] = new
        con.commit()
        print(f"[sentiment] collected: {SENT_STATE['found']} mention(s) seen,"
              f" {new} new", flush=True)
    finally:
        SENT_STATE.update(running=False, message="")
        con.close()


def run_collect_background():
    if SENT_STATE["running"]:
        return False
    t = threading.Thread(target=run_collect, daemon=True)
    t.start()
    return True


# --- LLM scoring (needs ANTHROPIC_API_KEY; everything works unscored) -------

SCORE_MODEL = "claude-opus-5"
# Effort is the quality lever inside one model. Judging whether a forum post is
# annoyed or merely asking - and whether "great, another silent errata" is
# praise - is exactly the kind of call that repays thinking, and the whole
# point of the feed is that somebody trusts the number without reading the
# post. Thinking is on by default on this model; budget_tokens is a 400.
SCORE_EFFORT = "max"
# room for the reasoning AND the JSON: thinking is billed against max_tokens,
# and a truncated answer costs the whole batch
SCORE_MAX_TOKENS = 16000


def _client():
    """The Anthropic client, or None when no key is configured."""
    if not os.environ.get("ANTHROPIC_API_KEY", ""):
        return None
    try:
        import anthropic
    except ImportError:
        print("[sentiment] anthropic SDK not installed - scoring is off",
              flush=True)
        return None
    return anthropic.Anthropic()


def score_pending(limit=200):
    """Score unscored mentions -1..+1 with topic tags. Returns how many were
    scored, or -1 when no key is configured.

    limit=0 means every unscored mention, which is what the "score everything"
    run wants; anything else is a page at a time.
    """
    client = _client()
    if client is None:
        return -1
    con = db.connect()
    rows = con.execute("SELECT id, entity, title, body, reason FROM mentions"
                       " WHERE score IS NULL ORDER BY id DESC"
                       + ("" if not limit else " LIMIT %d" % int(limit))
                       ).fetchall()
    scored = 0
    SENT_STATE.update(scoring=True, scored=0, to_score=len(rows),
                      tokens_in=0, tokens_out=0,
                      message="scoring %d mention(s) with %s…"
                              % (len(rows), SCORE_MODEL))
    for i in range(0, len(rows), 20):
        if SENT_STATE.get("stop_scoring"):
            SENT_STATE["message"] = "scoring stopped"
            break
        batch = rows[i:i + 20]
        listing = "\n".join(
            f'{r["id"]}: [{r["entity"]}] {r["title"]} — {(r["body"] or "")[:160]}'
            for r in batch)
        # Few-shot with an aspect-signed tag set: the 2025-26 evaluations
        # of LLM sentiment all land on the same two findings - examples in
        # the prompt beat any zero-shot wording, and a single number hides
        # WHAT is bad. Tags carry their own sign (docs-, hardware+) so a
        # mixed mention keeps both truths, and sarcasm scores as meant, not
        # as worded.
        prompt = (
            "These are forum/social mentions of semiconductor products."
            " For each line judge the AUTHOR'S sentiment toward the product"
            " itself, -1.0 (very negative) to +1.0 (very positive).\n"
            "Rules:\n"
            "- A neutral how-do-I question is 0. A question that reports a"
            " defect ('why does X crash') leans negative.\n"
            "- Sarcasm scores as MEANT: 'great, another silent errata' is"
            " negative.\n"
            "- Mixed mentions: score the overall lean, and tag each aspect"
            " with its own sign.\n"
            "- Tags: 1-3 from docs, tooling, hardware, price, availability,"
            " performance, support, question, praise - each suffixed + or -"
            " when it has a direction (docs-, performance+), bare when"
            " neutral (question).\n"
            "Examples of the judgement wanted:\n"
            "  'Ten minutes from unboxing to blink, impressed' ->"
            " {\"score\": 0.8, \"topics\": [\"tooling+\", \"praise\"]}\n"
            "  'DAVE generated startup breaks with -O2' ->"
            " {\"score\": -0.6, \"topics\": [\"tooling-\"]}\n"
            "  'Love the chip, shame the datasheet is 400 pages of maybe' ->"
            " {\"score\": -0.1, \"topics\": [\"hardware+\", \"docs-\"]}\n"
            "  'How do I set up SPI DMA on this part?' ->"
            " {\"score\": 0, \"topics\": [\"question\"]}\n"
            "Answer ONLY a JSON array:"
            " [{\"id\": n, \"score\": x, \"topics\": [\"...\"],"
            " \"why\": \"<8 words\"}]\n\n" + listing)
        try:
            # streamed: max_tokens is large enough that a non-streaming call
            # can outlive the HTTP timeout on a slow batch
            with client.messages.stream(
                    model=SCORE_MODEL,
                    max_tokens=SCORE_MAX_TOKENS,
                    output_config={"effort": SCORE_EFFORT},
                    messages=[{"role": "user", "content": prompt}]) as stream:
                msg = stream.get_final_message()
            if msg.stop_reason == "refusal":
                print("[sentiment] batch declined, skipping", flush=True)
                continue
            txt = "".join(b.text for b in msg.content if b.type == "text")
            SENT_STATE["tokens_in"] = (SENT_STATE.get("tokens_in", 0)
                                       + msg.usage.input_tokens)
            SENT_STATE["tokens_out"] = (SENT_STATE.get("tokens_out", 0)
                                        + msg.usage.output_tokens)
            m = re.search(r"\[.*\]", txt, re.S)
            for v in json.loads(m.group(0)) if m else []:
                con.execute(
                    "UPDATE mentions SET score=?, topics=?, reason=? WHERE id=?",
                    (max(-1.0, min(1.0, float(v.get("score", 0)))),
                     ",".join(v.get("topics", []))[:80],
                     str(v.get("why", ""))[:120], int(v["id"])))
                scored += 1
            con.commit()
        except Exception as exc:
            print(f"[sentiment] scoring stopped: {exc}", flush=True)
            SENT_STATE["message"] = "scoring stopped: %s" % str(exc)[:120]
            break
        SENT_STATE.update(scored=scored,
                          message="scored %d of %d" % (scored, len(rows)))
    con.close()
    SENT_STATE.update(scoring=False, stop_scoring=False)
    return scored


def score_background(limit=0):
    """Score in a thread. 7,370 mentions at max effort is hours, not a
    request - the page polls /sentiment/status for the count."""
    if SENT_STATE.get("scoring"):
        return False
    SENT_STATE.update(scoring=True, stop_scoring=False, scored=0,
                      message="starting…")
    threading.Thread(target=score_pending, args=(limit,), daemon=True).start()
    return True
