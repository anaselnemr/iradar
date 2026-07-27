"""Notes people leave on a page or an asset.

A change monitor answers "what moved". It could not answer "and what did we
decide about it" - that lived in someone's memory or a chat thread, and by the
next run it was gone. A note sits on the thing itself, so whoever opens that
page next reads the reasoning without having to ask.

Deliberately generic: this module stores notes against (kind, id) and knows
nothing about pages or assets. What counts as "the same thing" is the caller's
business - which matters, because an asset is identified by what it IS (title
plus link) rather than by any one of its rows.
"""
import html
import re

import db

# Every object worth talking about gets the same notes section: a tracked page,
# an asset (document, image, video, board - the item view keys them all the
# same way), and a detected update, which is where the reasoning behind an
# accept or a reject actually belongs.
KINDS = ("page", "item", "change")
MAX_BODY = 4000            # a note, not an essay; the column is TEXT regardless


def _now():
    return db.now_iso()


# --- writing ----------------------------------------------------------------

def add(kind, target_id, body, user, label="", con=None):
    """Post a note. Returns (comment_id, None) or (None, 'why not')."""
    body = (body or "").strip()
    if not body:
        return None, "Write something first."
    if len(body) > MAX_BODY:
        return None, f"Keep it under {MAX_BODY} characters."
    if kind not in KINDS:
        return None, "Unknown thing to comment on."
    own = con is None
    con = con or db.connect()
    try:
        cur = con.execute(
            "INSERT INTO comments(target_kind, target_id, target_label,"
            " user_id, author, body, created) VALUES(?,?,?,?,?,?,?)",
            (kind, str(target_id), (label or "")[:300],
             user["id"] if user else None, _display_name(user), body, _now()))
        con.commit()
        return cur.lastrowid, None
    finally:
        if own:
            con.close()


def edit(comment_id, body, user, con=None):
    """Change your own note. Admins do not edit other people's words - they can
    delete, which is visible, where a silent rewrite is not."""
    body = (body or "").strip()
    if not body:
        return "Write something first."
    if len(body) > MAX_BODY:
        return f"Keep it under {MAX_BODY} characters."
    own = con is None
    con = con or db.connect()
    try:
        c = con.execute("SELECT * FROM comments WHERE id=?",
                        (comment_id,)).fetchone()
        if not c:
            return "That note is gone."
        if not user or c["user_id"] != user["id"]:
            return "You can only edit your own notes."
        con.execute("UPDATE comments SET body=?, edited=? WHERE id=?",
                    (body, _now(), comment_id))
        con.commit()
        return None
    finally:
        if own:
            con.close()


def remove(comment_id, user, is_admin=False, con=None):
    own = con is None
    con = con or db.connect()
    try:
        c = con.execute("SELECT * FROM comments WHERE id=?",
                        (comment_id,)).fetchone()
        if not c:
            return "That note is gone."
        if not is_admin and (not user or c["user_id"] != user["id"]):
            return "You can only delete your own notes."
        con.execute("DELETE FROM comments WHERE id=?", (comment_id,))
        con.commit()
        return None
    finally:
        if own:
            con.close()


def set_resolved(comment_id, resolved, user, con=None):
    """Mark a note dealt with. Anyone signed in may - a note is a question to
    the team, and whoever answers it is the one who knows it is answered."""
    own = con is None
    con = con or db.connect()
    try:
        c = con.execute("SELECT 1 FROM comments WHERE id=?",
                        (comment_id,)).fetchone()
        if not c:
            return "That note is gone."
        if resolved:
            con.execute("UPDATE comments SET resolved=1, resolved_by=?,"
                        " resolved_at=? WHERE id=?",
                        (_display_name(user), _now(), comment_id))
        else:
            con.execute("UPDATE comments SET resolved=0, resolved_by='',"
                        " resolved_at='' WHERE id=?", (comment_id,))
        con.commit()
        return None
    finally:
        if own:
            con.close()


# --- reading ----------------------------------------------------------------

def for_target(kind, target_id, con=None):
    """Every note on one thing, oldest first - a conversation reads downwards."""
    own = con is None
    con = con or db.connect()
    try:
        return con.execute(
            "SELECT * FROM comments WHERE target_kind=? AND target_id=?"
            " ORDER BY id", (kind, str(target_id))).fetchall()
    finally:
        if own:
            con.close()


def count_for(kind, target_id, con=None):
    """(total, open) for one thing."""
    own = con is None
    con = con or db.connect()
    try:
        r = con.execute(
            "SELECT COUNT(*), SUM(CASE WHEN resolved=0 THEN 1 ELSE 0 END)"
            " FROM comments WHERE target_kind=? AND target_id=?",
            (kind, str(target_id))).fetchone()
        return (r[0] or 0), (r[1] or 0)
    finally:
        if own:
            con.close()


def recent(limit=200, only_open=False, con=None):
    """Everything anyone has written, newest first - the team's activity feed.
    A note nobody can find is a note nobody reads."""
    own = con is None
    con = con or db.connect()
    try:
        sql = "SELECT * FROM comments"
        if only_open:
            sql += " WHERE resolved=0"
        sql += " ORDER BY id DESC LIMIT ?"
        return con.execute(sql, (int(limit),)).fetchall()
    finally:
        if own:
            con.close()


def open_total(con=None):
    own = con is None
    con = con or db.connect()
    try:
        return con.execute(
            "SELECT COUNT(*) FROM comments WHERE resolved=0").fetchone()[0]
    finally:
        if own:
            con.close()


# --- rendering --------------------------------------------------------------

_URL_RE = re.compile(r"(https?://[^\s<>\"']+)")


def render(body):
    """Note text as safe HTML: escaped first, then line breaks and links.

    Escaping BEFORE linking is the whole point - do it the other way round and
    the anchor tags get escaped along with everything else, or worse, they do
    not and the note becomes an injection site. Nothing here ever trusts what
    was typed.
    """
    safe = html.escape(body or "")
    safe = _URL_RE.sub(
        lambda m: f'<a href="{m.group(1)}" target="_blank" rel="noopener nofollow">'
                  f'{m.group(1)}</a>', safe)
    return safe.replace("\n", "<br>")


def _display_name(user):
    if not user:
        return "someone"
    try:
        return (user["name"] or "").strip() or user["email"].split("@")[0]
    except Exception:
        return "someone"
