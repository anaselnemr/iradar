"""What a reviewer marked while reading an update.

A note says what you decided. A highlight says WHERE YOU WERE LOOKING when you
decided it - the two lines that mattered out of eighty, the corner of a
screenshot nobody else would have spotted. Reviewing four thousand pages is
mostly the work of finding the part that matters; this stops that work from
being thrown away the moment the tab closes.

Deliberately dumb about the page it marks. This module stores an anchor and
hands it back; what an anchor MEANS is the reviewer's browser's business,
because only the browser knows where a selection landed.
"""
import json

import db

KINDS = ("text", "box")
MAX_LABEL = 400
MAX_NOTE = 1000
# Four pens. More than this and picking one becomes its own decision, which is
# not the job - the point is to mark something in under a second.
COLORS = ("y", "g", "b", "p")


def _now():
    return db.now_iso()


def _author(user):
    if not user:
        return ""
    return (user["name"] or "").strip() or (user["email"] or "").split("@")[0]


def _clean_anchor(kind, anchor):
    """Keep an anchor to its declared shape. A stored anchor is replayed into
    the DOM by the browser, so what goes in has to be what comes out - never a
    blob some other page's script wrote."""
    if not isinstance(anchor, dict):
        return None, "Nothing to mark."
    if kind == "text":
        block = str(anchor.get("block") or "").strip()[:120]
        try:
            start, end = int(anchor.get("start")), int(anchor.get("end"))
        except (TypeError, ValueError):
            return None, "That selection could not be pinned down."
        if not block or start < 0 or end <= start:
            return None, "That selection could not be pinned down."
        return {"block": block, "start": start, "end": end}, None
    img = str(anchor.get("img") or "").strip()[:80]
    if not img:
        return None, "That mark is not on any picture."
    box = {}
    for k in ("x", "y", "w", "h"):
        try:
            v = float(anchor.get(k))
        except (TypeError, ValueError):
            return None, "That mark could not be pinned down."
        # fractions of the picture, clamped - a box that starts off the image
        # would be invisible on replay, which reads as a highlight that
        # silently vanished
        box[k] = max(0.0, min(1.0, round(v, 5)))
    if box["w"] <= 0 or box["h"] <= 0:
        return None, "Drag a box first."
    return dict(img=img, **box), None


def add(change_id, kind, anchor, user, label="", note="", color="y", con=None):
    """Mark something. Returns (row, None) or (None, 'why not')."""
    if kind not in KINDS:
        return None, "Unknown kind of highlight."
    anchor, err = _clean_anchor(kind, anchor)
    if err:
        return None, err
    if color not in COLORS:
        color = "y"
    own = con is None
    con = con or db.connect()
    try:
        cur = con.execute(
            "INSERT INTO highlights(change_id, kind, anchor, label, note,"
            " color, user_id, author, created) VALUES(?,?,?,?,?,?,?,?,?)",
            (int(change_id), kind, json.dumps(anchor),
             (label or "").strip()[:MAX_LABEL], (note or "").strip()[:MAX_NOTE],
             color, user["id"] if user else None, _author(user), _now()))
        con.commit()
        row = con.execute("SELECT * FROM highlights WHERE id=?",
                          (cur.lastrowid,)).fetchone()
        return row, None
    finally:
        if own:
            con.close()


def for_change(change_id, con=None):
    own = con is None
    con = con or db.connect()
    try:
        return con.execute("SELECT * FROM highlights WHERE change_id=?"
                           " ORDER BY id", (int(change_id),)).fetchall()
    finally:
        if own:
            con.close()


def remove(hl_id, user, is_admin=False, con=None):
    """Take a mark off. Yours to remove, or an admin's."""
    own = con is None
    con = con or db.connect()
    try:
        r = con.execute("SELECT * FROM highlights WHERE id=?",
                        (hl_id,)).fetchone()
        if not r:
            return "That highlight is already gone."
        mine = user and r["user_id"] == user["id"]
        if not (mine or is_admin):
            return "That is someone else's highlight."
        con.execute("DELETE FROM highlights WHERE id=?", (hl_id,))
        con.commit()
        return None
    finally:
        if own:
            con.close()


def as_json(rows):
    """The shape the page's script replays: anchors already parsed."""
    out = []
    for r in rows or []:
        try:
            anchor = json.loads(r["anchor"] or "{}")
        except Exception:
            anchor = {}
        out.append({"id": r["id"], "kind": r["kind"], "anchor": anchor,
                    "label": r["label"], "note": r["note"],
                    "color": r["color"], "author": r["author"],
                    "created": r["created"]})
    return out
