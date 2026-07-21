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

UA = ("InfineonWatch/1.0 (private change monitor; sentiment layer; "
      "contact: local)")

SENT_STATE = {"running": False, "message": "", "done": 0, "total": 0,
              "found": 0, "new": 0}

# family names that are distinctive enough to search bare; everything else
# gets an "infineon" qualifier so generic names ("Contactless memories")
# do not vacuum up unrelated chatter
_DISTINCTIVE = ("PSOC", "PSoC", "XMC", "AIROC", "OPTIGA", "AURIX", "TRAVEO",
                "CIPURSE", "Calypso", "XENSIV", "CoolMOS", "CoolGaN",
                "CoolSiC", "EasyPIM", "HybridPACK", "ModusToolbox", "TriCore",
                "SECORA", "iMOTION", "EiceDRIVER", "OptiMOS", "StrongIRFET")


def _clean_family(title):
    """'PSOC(tm) MCUs | Infineon Technologies' -> 'PSOC MCUs'."""
    t = title.split("|")[0]
    t = t.replace("™", "").replace("®", "")     # (tm), (r)
    return re.sub(r"\s+", " ", t).strip()


def entities():
    """The searchable product families from the watched hierarchy:
    [{entity, query}] - entity is the display name, query the search string."""
    con = db.connect()
    rows = con.execute(
        "SELECT COALESCE(NULLIF(u.title_override,''), NULLIF(s.page_title,''),"
        " u.url) t FROM urls u"
        " JOIN (SELECT url_id, MAX(id) mid FROM snapshots WHERE status='ok'"
        "       GROUP BY url_id) m ON m.url_id=u.id"
        " JOIN snapshots s ON s.id=m.mid"
        " WHERE u.active=1 AND u.ptype='Product' AND u.level IN ('PFP','PSFP')"
    ).fetchall()
    con.close()
    out, seen = [], set()
    for r in rows:
        name = _clean_family(r["t"])
        if not name:
            continue
        # prefix match, not whole-word: "XMC1000" and "PSOC4" belong to the
        # XMC / PSOC families just as much as the bare token does
        tok = next((d for d in _DISTINCTIVE
                    if re.search(r"\b" + re.escape(d), name, re.I)), None)
        if tok:
            # collapse the series pages to ONE canonical family: "PSOC 4 ...",
            # "PSOC Edge ..." and "PSOC Control ..." are all the PSOC family
            # out in the world - three copies would triple-store every mention
            entity, query = tok, tok
        else:
            # generic name: a compact query beats the full mouthful -
            # "infineon FM3" finds what "infineon FM3 32-bit Arm Cortex-M3
            # microcontroller (MCU) families" never would
            entity = name
            query = "infineon " + " ".join(name.split()[:2])
        if entity.lower() in seen:
            continue
        seen.add(entity.lower())
        out.append({"entity": entity, "query": query})
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


def _relevant(m, query):
    """Does the STORED text actually involve the searched product?
    Full-text APIs match words anywhere in a discussion, so a search for
    'infineon Smart card' returns threads whose stored title/body never says
    either - and unrelated chatter poisons a sentiment average far worse than
    a dropped borderline mention. Precision first: the distinctive token (or
    'infineon', or the family phrase) must appear in what we keep."""
    text = (m.get("title", "") + " " + m.get("body", "")).lower()
    toks = query.lower().split()
    if "infineon" in toks:
        rest = " ".join(t for t in toks if t != "infineon")
        return "infineon" in text or (rest and rest in text)
    return any(t in text for t in toks)


# --- collectors -------------------------------------------------------------

def collect_stackexchange(query):
    out = []
    for site in ("electronics", "stackoverflow"):
        u = ("https://api.stackexchange.com/2.3/search/advanced?order=desc"
             "&sort=creation&pagesize=30&q=" + urllib.parse.quote(query)
             + "&site=" + site)
        try:
            out += parse_stackexchange(_get_json(u), site)
        except Exception:
            pass
        time.sleep(0.4)
    return [m for m in out if _relevant(m, query)]


def collect_hn(query):
    u = ("https://hn.algolia.com/api/v1/search?hitsPerPage=30&query="
         + urllib.parse.quote(query))
    try:
        return [m for m in parse_hn(_get_json(u)) if _relevant(m, query)]
    except Exception:
        return []


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


def collect_reddit(query):
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
    ents = entities()
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
        page = _clean_family(r["page_title"] or "")
        ent = next((e["entity"] for e in ents
                    if e["entity"].split()[0].lower() in page.lower()), page)
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


def collect_youtube(query):
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
    """One full collection pass over every entity and source."""
    con = db.connect()
    try:
        ents = entities()
        SENT_STATE.update(running=True, done=0, found=0, new=0,
                          total=len(ents) * 3 + 2,
                          message="collecting mentions…")
        now = db.now_iso()
        new = 0
        for e in ents:
            for src, fn in (("stackexchange", collect_stackexchange),
                            ("hn", collect_hn), ("reddit", collect_reddit)):
                ms = fn(e["query"])
                SENT_STATE["found"] += len(ms)
                new += _store(con, e["entity"], src, ms, now)
                SENT_STATE["done"] += 1
                SENT_STATE["new"] = new
                SENT_STATE["message"] = f"{e['entity'][:30]} — {src}"
            con.commit()
        # community threads (keyless, local) + youtube (only with a key)
        ms = collect_community()
        SENT_STATE["found"] += len(ms)
        for m in ms:
            new += _store(con, m["entity"], "community", [m], now)
        SENT_STATE["done"] += 1
        if os.environ.get("YT_API_KEY"):
            for e in ents:
                ms = collect_youtube(e["query"])
                SENT_STATE["found"] += len(ms)
                new += _store(con, e["entity"], "youtube", ms, now)
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

SCORE_MODEL = "claude-haiku-4-5-20251001"


def score_pending(limit=200):
    """Score unscored mentions -1..+1 with topic tags. Returns how many were
    scored, or -1 when no key is configured."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return -1
    con = db.connect()
    rows = con.execute("SELECT id, entity, title, body, reason FROM mentions"
                       " WHERE score IS NULL ORDER BY id DESC LIMIT ?",
                       (limit,)).fetchall()
    scored = 0
    for i in range(0, len(rows), 20):
        batch = rows[i:i + 20]
        listing = "\n".join(
            f'{r["id"]}: [{r["entity"]}] {r["title"]} — {(r["body"] or "")[:160]}'
            for r in batch)
        prompt = (
            "These are forum/social mentions of semiconductor products. For"
            " each line, judge the AUTHOR'S sentiment toward the product"
            " (-1 very negative, 0 neutral/question, +1 very positive) and"
            " give 1-3 topic tags from: docs, tooling, hardware-bug, price,"
            " availability, performance, support, praise, question.\n"
            "Answer ONLY a JSON array: [{\"id\": n, \"score\": x, \"topics\":"
            " [\"...\"], \"why\": \"<8 words\"}]\n\n" + listing)
        try:
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                method="POST",
                headers={"content-type": "application/json",
                         "x-api-key": key,
                         "anthropic-version": "2023-06-01"},
                data=json.dumps({"model": SCORE_MODEL, "max_tokens": 1500,
                                 "messages": [{"role": "user",
                                               "content": prompt}]}).encode())
            with urllib.request.urlopen(req, timeout=60) as resp:
                txt = json.loads(resp.read())["content"][0]["text"]
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
            break
    con.close()
    return scored
