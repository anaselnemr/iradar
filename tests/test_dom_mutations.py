"""DOM-level mutation battery: the END-TO-END sight test.

The compare-level battery mutates extracted FILES - it proves the comparison,
but not that the extractors would ever SEE the change. These cases mutate the
frozen page DOM itself, run the real extractors (TEXT_JS / IMAGES_JS /
SECTIONS_JS) before and after, and assert the production comparison flags it.
A field the extractor silently stopped reading fails HERE and nowhere else.

Skips cleanly without fixtures or Playwright.
"""
import json
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from run_tests import Skip                       # noqa: E402
import capture as C                              # noqa: E402
import compare as cmp                            # noqa: E402

FIX = HERE / "fixtures"
_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.I | re.S)

# find the text node whose normalised text equals `needle` and rewrite it
_EDIT_TEXT_JS = r"""
(args) => {
  const [needle, replacement] = args;
  const tw = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while (n = tw.nextNode()) {
    if (n.textContent.replace(/\s+/g, ' ').trim() === needle) {
      n.textContent = replacement;
      return true;
    }
  }
  return false;
}
"""

_REMOVE_IMG_JS = r"""
(src) => {
  for (const img of document.querySelectorAll('img')) {
    if ((img.currentSrc || img.src) === src) { img.remove(); return true; }
  }
  return false;
}
"""

# remove the container that renders the first document row (a table or the
# documents pane) - simulates a section whose markup was renamed away
_WIPE_DOCS_JS = r"""
(needle) => {
  const tw = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while (n = tw.nextNode()) {
    if (n.textContent.replace(/\s+/g, ' ').trim() === needle) {
      const box = n.parentElement.closest('table, #documents, [id^="ds-"], section');
      if (box) { box.remove(); return true; }
    }
  }
  return false;
}
"""


def _snap_from(pg, d):
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    (d / "text.txt").write_text(pg.evaluate(C.TEXT_JS), encoding="utf-8")
    (d / "images.json").write_text(json.dumps(pg.evaluate(C.IMAGES_JS)),
                                   encoding="utf-8")
    (d / "sections.json").write_text(
        json.dumps(pg.evaluate(C.SECTIONS_JS), ensure_ascii=False),
        encoding="utf-8")
    return str(d)


def _run_case(fixture, mutate):
    """Load a fixture, snapshot, mutate the DOM, snapshot again, compare.
    Returns (compare_result, baseline_sections)."""
    f = FIX / f"{fixture}.html"
    if not f.exists():
        raise Skip(f"no {fixture} fixture — run tests/capture_fixtures.py")
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        raise Skip("playwright not installed")
    html = _SCRIPT_RE.sub("", f.read_text(encoding="utf-8"))
    root = Path(tempfile.mkdtemp(prefix="ifxdom_"))
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page()
        pg.set_content(html, wait_until="load")
        base_sections = pg.evaluate(C.SECTIONS_JS)
        old = _snap_from(pg, root / "old")
        assert mutate(pg, base_sections), "mutation found nothing to change"
        new = _snap_from(pg, root / "new")
        b.close()
    return (cmp.compare_snapshots(old, new, change_dir=None, settings={}),
            base_sections)


# --- control ----------------------------------------------------------------

def test_untouched_dom_compares_clean():
    r, _ = _run_case("part", lambda pg, s: True)
    assert r["any_changed"] is False, f"clean DOM flagged: {r['summary']}"


# --- text -------------------------------------------------------------------

def test_editing_a_feature_line_is_seen():
    # '32 MHz core...' is body text AND a features item - the edit must
    # surface, proving DOM -> TEXT_JS -> compare end to end
    def mutate(pg, s):
        t = s["features"][0]["title"]
        return pg.evaluate(_EDIT_TEXT_JS, [t, t + " (revised)"])
    r, _ = _run_case("part", mutate)
    assert r["any_changed"] is True
    assert r["text_changed"] is True, "a visible text edit was not seen"


# --- library: documents ------------------------------------------------------

def test_editing_a_document_title_is_seen():
    def mutate(pg, s):
        return pg.evaluate(_EDIT_TEXT_JS,
                           [s["documents"][0]["title"], "Renamed datasheet v2"])
    r, _ = _run_case("part", mutate)
    assert r["library_changed"] is True, "a renamed document was not seen"
    per = r["library"]["per_section"]
    assert "documents" in per, f"documents not the changed section: {per}"


def test_wiping_the_documents_section_is_seen():
    # markup rename / extractor blindness at DOM level: the pane vanishes
    def mutate(pg, s):
        return pg.evaluate(_WIPE_DOCS_JS, s["documents"][0]["title"])
    r, base = _run_case("part", mutate)
    assert r["any_changed"] is True
    per = r["library"]["per_section"]
    assert per.get("documents", {}).get("removed", 0) >= 1, \
        f"vanished documents not reported: {per}"


# --- library: subheadline ----------------------------------------------------

def test_editing_the_subheadline_is_seen():
    def mutate(pg, s):
        t = s["subheadline"][0]["title"]
        return pg.evaluate(_EDIT_TEXT_JS, [t, "A completely new subheadline"])
    r, _ = _run_case("application", mutate)
    assert r["any_changed"] is True
    assert "subheadline" in r["library"]["per_section"] or r["text_changed"], \
        "subheadline edit invisible to both library and text"


# --- images -----------------------------------------------------------------

def test_removing_a_content_image_is_seen():
    def mutate(pg, s):
        imgs = pg.evaluate(C.IMAGES_JS)
        content = [u for u in imgs if "etc.clientlibs" not in u]
        return pg.evaluate(_REMOVE_IMG_JS, (content or imgs)[0])
    r, _ = _run_case("part", mutate)
    assert r["images_changed"] is True, "a removed image was not seen"
    assert r["images_removed"] >= 1
