# -*- coding: utf-8 -*-
"""What a mention actually says.

A mention arrives as a link in a list. For a third of them that is all there
ever was: the community threads come off the "Developer community" widget on
each product page, which gives a title, a date and a url - no body. Scored on
the title alone, every one of those reads as trouble, because a forum title
IS a problem statement. "Fail message" scores negative; the thread was
answered the next day and marked solved by Infineon.

So before scoring, go and read the thing. Each source has one cheap, correct
way in, and it is never the HTML:

    community      Khoros LiQL, batched - body, solved, reply count, board.
                   The HTML sits behind Cloudflare and refuses a datacentre
                   outright; the API answers fine, but ONLY with a browser
                   User-Agent - a bare urllib request gets 403 from Railway.
    stackexchange  the official API: body and whether an answer was accepted
    youtube        the Data API when YT_API_KEY is set, else the watch
                   page's shortDescription blob. The page works from a
                   laptop and never from Railway: YouTube 429s a
                   datacentre address on the first request.
    github         the issues API (a token lifts 60/hour to 5,000)
    forum          Discourse's .json - the whole thread, not just the first
    hn             the Algolia item API - the comment tree
    blog           the page, tags stripped
    reddit         nothing: every JSON route 403s anything not a browser, and
                   these rows already carry a body from collection

`context` is the second half of the answer and often the whole of it -
"SOLVED · 3 replies", "closed · 7 comments", "2 answers, ACCEPTED". A
question that got answered is not a complaint, and that fact lives in the
metadata rather than in the prose.
"""
import gzip
import html as htmllib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

# Cloudflare fronts community.infineon.com and judges the client before it
# judges the request. Python's default agent is refused from the datacentre;
# this one is not. Proven from inside Railway, not assumed.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
CAP = 8000          # bounded evidence, not a claim of a complete conversation
TIMEOUT = 25


def get(url, accept="text/html,application/xhtml+xml", timeout=TIMEOUT,
        tries=4):
    """Fetch a page, backing off when the far end says to slow down.

    429 and 503 are the only errors worth retrying: they mean "later", not
    "no". Everything else - 403, 404, a gone thread - is an answer, and
    asking again just wastes the next mention's turn. YouTube starts
    throttling a few hundred pages in and stays polite about it, so the wait
    doubles rather than repeating.
    """
    wait = 3
    for n in range(tries):
        try:
            rq = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept-Encoding": "gzip", "Accept": accept,
                "Accept-Language": "en-US,en;q=0.9"})
            r = urllib.request.urlopen(rq, timeout=timeout)
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code not in (429, 503) or n == tries - 1:
                raise
            retry_after = e.headers.get("Retry-After") if e.headers else None
            try:
                wait = max(wait, int(retry_after))
            except (TypeError, ValueError):
                pass
            time.sleep(wait)
            wait *= 2


_TAG = re.compile(r"<[^>]+>")
# every space a page can produce, not just the ASCII one: &nbsp; decodes
# to U+00A0, which looks like a space, is not one, and would travel all
# the way into the scoring prompt glued between two words. "whitespace
# that is not a newline" says that in one class and cannot rot - spelling
# the codepoints out literally put invisible characters in this file.
_SPACE = re.compile(r"[^\S\n]+")
# the zero-width family is not whitespace at all, so nothing above catches
# it; built from codepoints because writing them out is writing nothing
_ZERO_WIDTH = re.compile("[" + "".join(map(chr, (0x200b, 0x200c, 0x200d,
                                                 0xfeff, 0x2060))) + "]")
_EDGE = re.compile(r" *\n[ \n]*")
_DEAD = re.compile(r"(?is)<(script|style|noscript)\b.*?</\1>")
_BREAK = re.compile(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>")


def detag(s):
    """HTML down to what a person would say is on the page."""
    s = _DEAD.sub(" ", s or "")
    s = _BREAK.sub("\n", s)
    s = _ZERO_WIDTH.sub("", htmllib.unescape(_TAG.sub(" ", s)))
    return _EDGE.sub("\n", _SPACE.sub(" ", s)).strip()


# --- community: one call per 40 threads --------------------------------------

# every Khoros permalink shape: thread, blog article, tkb article, single
# message, board, contest page - the number after the two-letter code is the
# message id, which is the only thing LiQL wants
THREAD_ID = re.compile(r"/(?:td|ba|ta|m|bc|cp)-p/(\d+)")
_LIQL_FIELDS = ("id,subject,body,post_time,author.login,conversation.solved,"
                "board.title,metrics.views,replies.count(*)")


def community_batch(thread_ids):
    """{thread id: (text, context, author, posted)} for up to 40 ids.

    LIMIT is not optional. LiQL applies a default of 25 and says nothing
    about it - ask for forty threads and twenty-five come back, with no
    error, no count, no truncation flag. That quietly lost a third of the
    community threads before anyone counted the answers against the
    questions, so the limit is stated and it is stated as what we asked for.
    """
    ids = [str(i) for i in thread_ids]
    if not ids:
        return {}
    q = ("SELECT " + _LIQL_FIELDS + " FROM messages WHERE id IN ('"
         + "','".join(ids) + "') LIMIT " + str(len(ids)))
    d = json.loads(get("https://community.infineon.com/api/2.0/search?q="
                       + urllib.parse.quote(q), "application/json"))
    out = {}
    for it in (d.get("data") or {}).get("items") or []:
        bits = []
        if (it.get("conversation") or {}).get("solved"):
            bits.append("SOLVED")
        n = (it.get("replies") or {}).get("count")
        if n is not None:
            bits.append("%s replies" % n)
        if (it.get("board") or {}).get("title"):
            bits.append("board: " + it["board"]["title"])
        out[str(it.get("id"))] = (
            detag(it.get("body") or "")[:CAP], " · ".join(bits),
            (it.get("author") or {}).get("login") or "",
            (it.get("post_time") or "")[:19])
    if out:
        # Confirmed against the source API: replies use topic.id, not
        # conversation.id. A bounded batch retains opening posts on failure.
        q = ("SELECT id,topic.id,body,author.login,is_solution FROM messages"
             " WHERE topic.id IN ('" + "','".join(out) + "') AND depth > 0 LIMIT 160")
        try:
            replies = json.loads(get('https://community.infineon.com/api/2.0/search?q='
                                     + urllib.parse.quote(q), 'application/json'))
            groups = {}
            for r in (replies.get('data') or {}).get('items') or []:
                root = str((r.get('topic') or {}).get('id') or '')
                if root in out and str(r.get('id')) != root:
                    groups.setdefault(root, []).append(r)
            for root, value in list(out.items()):
                rs = sorted(groups.get(root, []), key=lambda r: not r.get('is_solution'))[:5]
                parts = [('Accepted answer' if r.get('is_solution') else 'Reply') +
                         ' — ' + ((r.get('author') or {}).get('login') or 'unknown author') +
                         '\n' + detag(r.get('body') or '') for r in rs]
                text, ctx, author, posted = value
                out[root] = (with_replies(text, parts), ctx +
                             (' · %d reply excerpts read' % len(parts) if parts else
                              ' · replies not retrieved'), author, posted)
        except Exception:
            out = {k: (v[0], v[1] + ' · replies unavailable', v[2], v[3]) for k, v in out.items()}
    return out


def with_replies(opening, replies):
    if not replies:
        return opening[:CAP]
    return ('[Original post]\n' + opening[:4000] +
            '\n\n[Replies / outcome — other authors]\n' + '\n\n'.join(replies)[:3800])[:CAP]


# --- one url at a time -------------------------------------------------------

def _stackexchange(url):
    m = re.search(r"/questions/(\d+)", url)
    if not m:
        return None
    host = urllib.parse.urlparse(url).netloc
    site = host.replace(".stackexchange.com", "").replace(".com", "")
    d = json.loads(get("https://api.stackexchange.com/2.3/questions/%s?site=%s"
                       "&filter=withbody" % (m.group(1), site),
                       "application/json"))
    it = (d.get("items") or [None])[0]
    if not it:
        return None
    ctx = "%d answers%s" % (it.get("answer_count") or 0,
                            ", ACCEPTED" if it.get("accepted_answer_id") else "")
    text = detag(it.get('body') or '')
    aid = it.get('accepted_answer_id')
    if aid:
        try:
            answers = json.loads(get('https://api.stackexchange.com/2.3/answers/%s?site=%s&filter=withbody'
                                     % (aid, site), 'application/json'))
            parts = ['Accepted answer — ' + ((a.get('owner') or {}).get('display_name') or 'unknown author')
                     + '\n' + detag(a.get('body') or '') for a in answers.get('items', [])]
            text = with_replies(text, parts)
            ctx += ' · answer text read' if parts else ' · answer text unavailable'
        except Exception:
            ctx += ' · answer text unavailable'
    return (text[:CAP], ctx,
            (it.get("owner") or {}).get("display_name") or "", "")


_YT_DESC = re.compile(r'"shortDescription":"(.*?)","isCrawlable"', re.S)
_YT_VIEWS = re.compile(r'"viewCount":"(\d+)"')
_YT_ID = re.compile(r"(?:v=|youtu\.be/|/embed/|/shorts/)([A-Za-z0-9_-]{11})")


def youtube_batch(video_ids):
    """{video id: (text, context, author, posted)} for up to 50 ids.

    The watch page works from a laptop and NOT from Railway - YouTube answers
    a datacentre address with 429 on the first request and never relents - so
    on the server this is the only way in. It needs YT_API_KEY (the same key
    sentiment.py's search collector already looks for); without one the server
    simply leaves these mentions unread, which is why the backfill for them
    was run from a laptop. 50 videos per unit, 10,000 units a day.
    """
    key = os.environ.get("YT_API_KEY", "")
    ids = [i for i in video_ids if i]
    if not key or not ids:
        return {}
    d = json.loads(get("https://www.googleapis.com/youtube/v3/videos"
                       "?part=snippet,statistics&id=" + ",".join(ids[:50])
                       + "&key=" + key, "application/json"))
    out = {}
    for it in d.get("items") or []:
        sn = it.get("snippet") or {}
        views = (it.get("statistics") or {}).get("viewCount")
        out[it.get("id")] = ((sn.get("description") or "").strip()[:CAP],
                             ("%s views" % views) if views else "",
                             sn.get("channelTitle") or "",
                             (sn.get("publishedAt") or "")[:19])
    return out


def _youtube(url):
    vid = _YT_ID.search(url)
    if vid:                       # the API when a key is configured: see above
        got = youtube_batch([vid.group(1)]).get(vid.group(1))
        if got:
            text, ctx, author, posted = got
            key = os.environ.get('YT_API_KEY', '')
            try:
                payload = json.loads(get('https://www.googleapis.com/youtube/v3/commentThreads'
                    '?part=snippet&textFormat=plainText&order=relevance&maxResults=20&videoId='
                    + vid.group(1) + '&key=' + key, 'application/json'))
                parts = []
                for item in payload.get('items', []):
                    c = ((item.get('snippet') or {}).get('topLevelComment') or {}).get('snippet') or {}
                    parts.append((c.get('authorDisplayName') or 'unknown author') + ': ' + (c.get('textDisplay') or ''))
                text = with_replies(text, parts)
                ctx += ' · description + %d sampled comments (no transcript)' % len(parts)
            except Exception:
                ctx += ' · description only; comments unavailable (no transcript)'
            return text, ctx, author, posted
    h = get(url)
    m = _YT_DESC.search(h)
    if not m:
        return None
    try:                                   # it is a JSON string, escapes and all
        desc = json.loads('"%s"' % m.group(1))
    except ValueError:
        desc = m.group(1)
    v = _YT_VIEWS.search(h)
    return (desc.strip()[:CAP], ("%s views" % v.group(1)) if v else "", "", "")


_GH = re.compile(r"github\.com/([^/]+)/([^/]+)/(issues|pull)/(\d+)")


def _github(url):
    m = _GH.search(url)
    if not m:
        return None
    owner, repo, kind, num = m.groups()
    path = "repos/%s/%s/issues/%s" % (owner, repo, num)
    d = None
    try:                    # gh, when it is there, brings its own credentials
        # encoding spelled out: text=True decodes with the LOCALE's codec, and
        # on Windows that is cp1252, which dies on the first issue body with
        # an emoji in it - silently, in a worker thread, losing that mention
        p = subprocess.run(["gh", "api", path], capture_output=True,
                           encoding="utf-8", errors="replace", timeout=60)
        if p.returncode == 0:
            d = json.loads(p.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        d = None
    if d is None:           # 60/hour unauthenticated, 5,000 with a token
        rq = urllib.request.Request(
            "https://api.github.com/" + path,
            headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
        tok = os.environ.get("GITHUB_TOKEN", "")
        if tok:
            rq.add_header("Authorization", "Bearer " + tok)
        d = json.loads(urllib.request.urlopen(rq, timeout=TIMEOUT).read())
    ctx = "%s · %s · %s comments" % (kind, d.get("state") or "",
                                     d.get("comments") or 0)
    text = (d.get('body') or '').strip()
    if d.get('comments') and os.environ.get('GITHUB_TOKEN'):
        try:
            rq = urllib.request.Request('https://api.github.com/' + path + '/comments?per_page=20',
                headers={'User-Agent': UA, 'Accept': 'application/vnd.github+json',
                         'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN']})
            comments = json.loads(urllib.request.urlopen(rq, timeout=TIMEOUT).read())
            text = with_replies(text, [((c.get('user') or {}).get('login') or 'unknown author')
                                      + ': ' + (c.get('body') or '') for c in comments])
            ctx += ' · %d sampled comments read' % len(comments)
        except Exception:
            ctx += ' · comment text unavailable'
    elif d.get('comments'):
        ctx += ' · issue body only (comment reading needs GITHUB_TOKEN)'
    return (text[:CAP], ctx,
            (d.get("user") or {}).get("login") or "",
            (d.get("created_at") or "")[:19])


def _forum(url):
    d = json.loads(get(url.split("?")[0].rstrip("/") + ".json",
                       "application/json"))
    posts = (d.get("post_stream") or {}).get("posts") or []
    if not posts:
        return None
    text = with_replies(detag(posts[0].get('cooked') or ''),
                        [(p.get('username') or 'unknown author') + ': ' +
                         detag(p.get('cooked') or '') for p in posts[1:6]])
    return (text[:CAP], "%d posts" % len(posts), posts[0].get("username") or "",
            (posts[0].get("created_at") or "")[:19])


def _hn(url):
    m = re.search(r"id=(\d+)", url)
    if not m:
        return None
    d = json.loads(get("https://hn.algolia.com/api/v1/items/" + m.group(1),
                       "application/json"))
    parts, seen = [], [0]

    def walk(node):
        seen[0] += 1
        t = detag(node.get("text") or "")
        if t:
            parts.append(t)
        for c in node.get("children") or []:
            if len(parts) < 12:
                walk(c)
    walk(d)
    return ("\n---\n".join(parts)[:CAP], "%d comments" % max(0, seen[0] - 1),
            d.get("author") or "", (d.get("created_at") or "")[:19])


_ARTICLE = re.compile(r"(?is)<article\b.*?</article>")
_MAIN = re.compile(r"(?is)<main\b.*?</main>")


def _blog(url):
    h = get(url)
    m = _ARTICLE.search(h) or _MAIN.search(h)
    return (detag(m.group(0) if m else h)[:CAP], "", "", "")


# reddit is absent on purpose - see the module docstring
FETCHERS = {"stackexchange": _stackexchange, "youtube": _youtube,
            "github": _github, "forum": _forum, "hn": _hn, "blog": _blog}


def fetch_one(source, url):
    """(text, context, author, posted), or None when this one cannot be read."""
    fn = FETCHERS.get(source)
    if not fn or not url:
        return None
    try:
        return fn(url)
    except Exception:
        return None            # one unreadable page must not stop the pass
