"""One edit, counted once.

A change row is one PAGE. When infineon.com edits something shared - a
partner's filter list, an application link, a datasheet listed on 40 part
pages - that one edit lands as hundreds of change rows, and every count built
on rows ("1,466 updates") reads as hundreds of things to look at. Run 54 had
1,466 updated pages; 1,211 of them carried nothing but 15 shared edits.

So every change is broken into FACTS, the smallest exact statements it makes:

    text line gained / lost     "Electric vehicles" removed
    item added / removed        Documents: "AN241136 ..." added
    item changed in place       Design resources: "Launch Lab" changed (group)
    component appeared/went     Developer community removed
    price changed               Price changed
    images / layout             page-specific, never shared

Grouping never guesses: two pages share an edit only when they carry the
IDENTICAL fact (same line, same item, same field and new value). A fact found
on SHARED_MIN pages or more is an ES-wide edit; facts that land on (nearly)
the same set of pages are one edit and read as one line. A page whose every
fact belongs to ES-wide edits is covered by those lines; a page with any
fact of its own is a page change, listed with only its own facts.
"""
import json
from collections import Counter
from pathlib import Path

import compare

SHARED_MIN = 5       # pages carrying the identical fact before it is "ES-wide"
SAME_EDIT = 0.9      # facts whose page sets overlap this much are one edit
_LINE_MAX = 90       # a fact quotes at most this much of a line or title


def _q(s, n=_LINE_MAX):
    s = " ".join(str(s or "").split())
    return "“" + (s if len(s) <= n else s[:n - 1].rstrip() + "…") + "”"


def _labels():
    try:
        from runner import _DIFF_SECTIONS
        return dict(_DIFF_SECTIONS)
    except Exception:
        return {}


def facts(old_dir, new_dir, change, settings=None, labels=None):
    """[(key, label), ...] for one change. `change` is the change row (or a
    dict with the same columns): text_changed, images_*, visual_changed,
    summary and url_id decide the parts that are not read from disk."""
    settings = settings or {}
    labels = labels if labels is not None else _labels()
    ign = (settings.get("ignore_patterns") or "").splitlines()
    out = []
    try:
        det = compare.library_diff_detail(old_dir, new_dir)
    except Exception:
        det = {}
    named = set()        # item titles the library facts already speak for
    lib = []
    for cat, d in sorted(det.items()):
        name = labels.get(cat, cat.replace("_", " ").capitalize())
        gone = {compare._item_id(it)[0]: it for it in d["removed"]}
        came = {compare._item_id(it)[0]: it for it in d["added"]}
        # the same title gone and back under another link is ONE item
        # repointed, not a removal plus an addition
        for t in sorted(set(gone) & set(came)):
            lib.append(("r|%s|%s|%s" % (cat, t, compare._item_id(came[t])[1]),
                        "%s: %s link changed" % (name, _q(came[t].get("title"), 70))))
        for verb, items in (("added", came), ("removed", gone)):
            for t, it in sorted(items.items()):
                if t in gone and t in came:
                    continue
                lib.append(("%s|%s|%s|%s" % (verb[0], cat, t, compare._item_id(it)[1]),
                            "%s: %s %s" % (name, _q(it.get("title"), 70), verb)))
        named |= set(gone) | set(came)
        named |= {compare.strip_marks(str(it.get("title", ""))).lower()
                  for it in d["changed"]}
        for it in d["changed"]:
            if cat == "price":
                # every part has its own price, so the values never match -
                # the shared fact is that prices moved, one line for all
                lib.append(("c|price", "Price changed"))
                continue
            fields = it["fields"]
            key = "c|%s|%s|%s" % (cat, compare.strip_marks(str(it.get("title", ""))).lower(),
                                  json.dumps([(f[0], str(f[2])[:200]) for f in fields],
                                             ensure_ascii=False))
            lib.append((key, "%s: %s changed (%s)" % (
                name, _q(it.get("title"), 70),
                ", ".join(sorted({f[0].replace("_", " ") for f in fields})))))
    if change["text_changed"]:
        a = Counter(compare._load_text_lines(old_dir, ign))
        b = Counter(compare._load_text_lines(new_dir, ign))

        def _own(l):
            # a line that is just an item's title is that item, already said
            return l.strip() and compare.strip_marks(l).lower() not in named
        for l in sorted(a - b):
            if _own(l):
                out.append(("t-|" + l[:300], _q(l) + " removed"))
        for l in sorted(b - a):
            if _own(l):
                out.append(("t+|" + l[:300], _q(l) + " added"))
    out += lib
    # whole components coming and going, as the summary already names them
    for part in (change["summary"] or "").split(" · "):
        p = part.strip()
        low = p.lower()
        if p and (low.endswith((" added", " removed", " emptied", " filled"))
                  and "(" not in p):
            out.append(("s|" + low, p[0].upper() + p[1:]))
    # images and layout are judged per page: never grouped
    uid = change["url_id"]
    if change["images_changed"]:
        out.append(("i|%s" % uid, "images (+%d/-%d)" % (
            change["images_added"] or 0, change["images_removed"] or 0)))
    if change["visual_changed"] and not out:
        out.append(("v|%s" % uid, "layout shifted"))
    if not out:
        # the detector raised it and nothing above can say what: keep it as
        # its own page change, in the detector's words
        out.append(("x|%s" % uid, change["summary"] or "changed"))
    # one fact once per page
    seen, uniq = set(), []
    for k, l in out:
        if k not in seen:
            seen.add(k)
            uniq.append((k, l))
    return uniq


def facts_for(con, change, settings=None, labels=None):
    """The change's stored facts, or - for a change saved before facts were
    stored - worked out from its two snapshots when they still exist."""
    try:
        raw = change["facts"]
    except (IndexError, KeyError):
        raw = ""
    if raw:
        try:
            return [tuple(x) for x in json.loads(raw)]
        except Exception:
            pass
    dirs = con.execute(
        "SELECT o.dir, n.dir FROM snapshots o, snapshots n"
        " WHERE o.id=? AND n.id=?",
        (change["old_snapshot_id"], change["new_snapshot_id"])).fetchone()
    if (dirs and dirs[0] and dirs[1] and Path(dirs[0]).is_dir()
            and Path(dirs[1]).is_dir()):
        return facts(dirs[0], dirs[1], change, settings, labels)
    return [("x|%s" % change["url_id"], change["summary"] or "changed")]


def group(changes, fact_lists, shared_min=SHARED_MIN):
    """Split a set of changes into ES-wide edits and page changes.

    `fact_lists[i]` is facts() for `changes[i]`. Returns
      edits: [{"labels": [...], "pages": n, "id": example change id,
               "title": example title}], most pages first
      page_changes: [{"change": row, "facts": [labels of its OWN facts]}]
      covered: pages whose every fact is an ES-wide edit
    """
    pages_of = {}
    label_of = {}
    for i, fl in enumerate(fact_lists):
        for k, l in fl:
            pages_of.setdefault(k, set()).add(i)
            label_of.setdefault(k, l)
    shared = sorted((k for k, ps in pages_of.items()
                     if len(ps) >= shared_min and k[:2] not in ("i|", "v|", "x|")),
                    key=lambda k: (-len(pages_of[k]), k))
    groups = []          # [pages set, [keys]]
    for k in shared:
        ps = pages_of[k]
        for g in groups:
            inter = len(g[0] & ps)
            if inter and inter / len(g[0] | ps) >= SAME_EDIT:
                g[0] |= ps
                g[1].append(k)
                break
        else:
            groups.append([set(ps), [k]])
    shared_set = set(shared)
    edits = []
    for ps, keys in sorted(groups, key=lambda g: -len(g[0])):
        # the example: a page carrying this edit and as little else as possible
        ex = min(ps, key=lambda i: (len(fact_lists[i]), i))
        c = changes[ex]
        edits.append({"labels": [label_of[k] for k in keys], "pages": len(ps),
                      "id": c["id"], "title": _title(c)})
    page_changes = []
    covered = shared_pages = 0
    for i, fl in enumerate(fact_lists):
        own = [l for k, l in fl if k not in shared_set]
        if len(own) < len(fl):
            shared_pages += 1
        if own:
            page_changes.append({"change": changes[i], "facts": own})
        else:
            covered += 1
    return {"edits": edits, "page_changes": page_changes, "covered": covered,
            "shared_pages": shared_pages}


def _title(c):
    try:
        return c["title"] or c["url"]
    except (IndexError, KeyError):
        return ""


def record_run(con, run_id, settings=None):
    """Count a finished run's edits onto its row (runs.site_edits,
    runs.page_changes), so the Dashboard can say '15 ES-wide edits, 255
    page changes' instead of '1,466 updated'."""
    run = con.execute("SELECT started, finished FROM runs WHERE id=?",
                      (run_id,)).fetchone()
    if not run:
        return None
    rows = con.execute(
        "SELECT * FROM changes WHERE ts>=? AND ts<=?",
        (run[0], run[1] or "9999")).fetchall()
    labels = _labels()
    g = group(rows, [facts_for(con, r, settings, labels) for r in rows])
    con.execute("UPDATE runs SET site_edits=?, page_changes=? WHERE id=?",
                (len(g["edits"]), len(g["page_changes"]), run_id))
    return g
