"""Notes on pages, assets and updates.

Two things here are worth more than the rest: a note is USER-WRITTEN HTML that
every teammate's browser renders, so the escaping is a security boundary, not a
formatting nicety; and "your own words are yours" has to hold even for an admin.
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.pop("DASH_USER", None)
os.environ.pop("DASH_PASS", None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import db                                          # noqa: E402
from run_tests import Skip                         # noqa: E402

_D = Path(tempfile.mkdtemp(prefix="ifxnotes_"))
db.DATA_DIR = _D
db.SNAP_DIR = _D / "snapshots"
db.CHANGE_DIR = _D / "changes"
db.DB_PATH = _D / "watch.db"
db.init_db()

try:
    import auth                                    # noqa: E402
    import comments                                # noqa: E402
    import app as appmod                           # noqa: E402
except Exception as e:
    raise Skip(f"could not import the app: {e}")

ALICE = "alice@example.com"
BOSS = "boss@example.com"
PW = "note-test-password"


def _people():
    con = db.connect()
    con.execute("DELETE FROM users")
    con.execute("DELETE FROM comments")
    con.commit()
    con.close()
    a, _ = auth.create_user(ALICE, "Alice", "editor")
    b, _ = auth.create_user(BOSS, "Boss", "admin")
    auth.set_password(a, PW, must_change=False)
    auth.set_password(b, PW, must_change=False)
    return auth.by_email(ALICE), auth.by_email(BOSS)


def _client(email):
    c = appmod.app.test_client()
    c.post("/login", data={"email": email, "password": PW})
    return c


def _csrf(c):
    import re
    m = re.search(r'name="_csrf" value="([^"]+)"',
                  c.get("/notes").data.decode("utf-8", "replace"))
    return m.group(1) if m else ""


# --- the basics -------------------------------------------------------------

def test_a_note_is_stored_and_read_back():
    alice, _ = _people()
    cid, err = comments.add("page", 1, "Checked with marketing - intended.",
                            alice, label="https://example.com/x")
    assert err is None and cid
    rows = comments.for_target("page", 1)
    assert len(rows) == 1
    assert rows[0]["body"].startswith("Checked with marketing")
    assert rows[0]["author"] == "Alice", "the note does not say who wrote it"


def test_the_author_survives_their_account_being_deleted():
    # a note is a record of a decision; it has to keep saying who made it
    alice, boss = _people()
    comments.add("page", 1, "Mine", alice)
    auth.delete_user(alice["id"])
    rows = comments.for_target("page", 1)
    assert rows[0]["author"] == "Alice", \
        "the note lost its author when the account went"


def test_empty_and_oversized_notes_are_refused():
    alice, _ = _people()
    assert comments.add("page", 1, "   ", alice)[1] is not None
    assert comments.add("page", 1, "x" * (comments.MAX_BODY + 1), alice)[1] is not None
    assert comments.add("page", 1, "fine", alice)[1] is None


def test_notes_are_per_object():
    alice, _ = _people()
    comments.add("page", 1, "about the page", alice)
    comments.add("item", "asset:abc", "about the asset", alice)
    comments.add("change", 7, "about the update", alice)
    assert len(comments.for_target("page", 1)) == 1
    assert len(comments.for_target("item", "asset:abc")) == 1
    assert len(comments.for_target("change", 7)) == 1
    assert comments.for_target("page", 2) == []


def test_notes_read_oldest_first():
    alice, _ = _people()
    for n in ("one", "two", "three"):
        comments.add("page", 1, n, alice)
    assert [r["body"] for r in comments.for_target("page", 1)] == \
        ["one", "two", "three"], "a conversation must read downwards"


# --- who may do what --------------------------------------------------------

def test_only_the_author_can_edit_their_note():
    alice, boss = _people()
    cid, _ = comments.add("page", 1, "mine", alice)
    assert comments.edit(cid, "hacked", boss) is not None, \
        "an admin silently rewrote someone else's words"
    assert comments.for_target("page", 1)[0]["body"] == "mine"
    assert comments.edit(cid, "mine, revised", alice) is None
    row = comments.for_target("page", 1)[0]
    assert row["body"] == "mine, revised"
    assert row["edited"], "an edited note is not marked as edited"


def test_an_admin_can_delete_any_note_but_an_editor_only_their_own():
    alice, boss = _people()
    a1, _ = comments.add("page", 1, "alice one", alice)
    b1, _ = comments.add("page", 1, "boss one", boss)
    assert comments.remove(b1, alice, is_admin=False) is not None, \
        "an editor deleted someone else's note"
    assert comments.remove(a1, alice, is_admin=False) is None
    assert comments.remove(b1, alice, is_admin=True) is None
    assert comments.for_target("page", 1) == []


def test_resolving_records_who_and_when():
    alice, boss = _people()
    cid, _ = comments.add("page", 1, "is this right?", alice)
    assert comments.set_resolved(cid, True, boss) is None
    r = comments.for_target("page", 1)[0]
    assert r["resolved"] == 1 and r["resolved_by"] == "Boss" and r["resolved_at"]
    comments.set_resolved(cid, False, alice)
    r = comments.for_target("page", 1)[0]
    assert r["resolved"] == 0 and not r["resolved_by"]


def test_counts_split_open_from_done():
    alice, _ = _people()
    a, _ = comments.add("page", 1, "one", alice)
    comments.add("page", 1, "two", alice)
    comments.set_resolved(a, True, alice)
    assert comments.count_for("page", 1) == (2, 1)
    assert comments.open_total() == 1


# --- the security boundary --------------------------------------------------

def test_a_note_can_never_run_script():
    # every teammate's browser renders this text
    nasty = '<script>alert(1)</script> <img src=x onerror=alert(2)> </div>'
    out = comments.render(nasty)
    assert "<script>" not in out
    assert "onerror=" not in out or "&lt;img" in out
    assert "&lt;script&gt;" in out, "the tag was not escaped"


def test_links_are_made_clickable_but_still_escaped():
    out = comments.render('see https://infineon.com/a?b=1&c=2 please')
    assert 'href="https://infineon.com/a?b=1&amp;c=2"' in out, out
    assert 'rel="noopener nofollow"' in out
    # and a link cannot smuggle a quote out of its attribute
    out2 = comments.render('http://x.test/"onmouseover="alert(1)')
    assert 'onmouseover="alert' not in out2


def test_line_breaks_survive():
    assert comments.render("a\nb") == "a<br>b"


# --- through the app --------------------------------------------------------

def test_an_editor_can_post_a_note():
    # commenting IS the review work - an editor who cannot write one is useless
    _people()
    c = _client(ALICE)
    r = c.post("/comment/add", data={"kind": "page", "target": "1",
                                     "body": "from the form", "next": "/notes",
                                     "_csrf": _csrf(c)})
    assert r.status_code in (302, 200), f"editor could not post ({r.status_code})"
    assert any(x["body"] == "from the form" for x in comments.for_target("page", 1))


def test_a_stranger_cannot_post():
    _people()
    c = appmod.app.test_client()          # never signed in
    before = len(comments.for_target("page", 1))
    c.post("/comment/add", data={"kind": "page", "target": "1", "body": "hi"})
    assert len(comments.for_target("page", 1)) == before, "a stranger left a note"


def test_the_notes_page_renders_and_lists_them():
    alice, _ = _people()
    comments.add("page", 1, "findable later", alice)
    c = _client(ALICE)
    body = c.get("/notes").data.decode("utf-8", "replace")
    assert "findable later" in body, "a note is not findable from the notes page"


def test_renaming_yourself_renames_your_past_notes():
    # The author is frozen text so a DELETED account still says who wrote it.
    # But renaming yourself is not that case - you are the same person, and a
    # note signed with a name you no longer use reads as someone else.
    alice, _ = _people()
    comments.add("page", 1, "written before the rename", alice)
    auth.set_name(alice["id"], "Alice Hartmann")
    row = comments.for_target("page", 1)[0]
    assert row["author"] == "Alice Hartmann", \
        "an old note still carries the previous name"
    assert auth.by_email(ALICE)["name"] == "Alice Hartmann"


def test_you_can_rename_yourself_through_the_app():
    _people()
    c = _client(ALICE)
    r = c.post("/account/name", data={"name": "Ali", "_csrf": _csrf(c)})
    assert r.status_code in (302, 200), f"rename refused ({r.status_code})"
    assert auth.by_email(ALICE)["name"] == "Ali"
    # and it shows in the corner
    assert b"Ali" in c.get("/").data


def test_renaming_needs_no_password():
    # it is not a credential; asking for one just stops people fixing a typo
    alice, _ = _people()
    assert auth.set_name(alice["id"], "Whatever They Like") is None


def test_the_add_route_will_not_redirect_off_site():
    _people()
    c = _client(ALICE)
    r = c.post("/comment/add", data={"kind": "page", "target": "1",
                                     "body": "x", "next": "https://evil.test/",
                                     "_csrf": _csrf(c)})
    assert "evil.test" not in r.headers.get("Location", "")
