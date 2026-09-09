"""Tests for compare.py — the diff logic that decides whether a page updated.

Several of this session's worst bugs lived here: the positional line diff
calling a re-ordered block an update, duplication being mistaken for a move,
ignore-patterns. Each case below is a real regression guard, built from tiny
snapshot dirs written to a temp folder — no browser, no network, no database.
"""
import json
import tempfile
from pathlib import Path

import compare as cmp


# --- helpers ---------------------------------------------------------------

def _snap(lines=None, images=None):
    """A throwaway snapshot dir with text.txt and images.json."""
    d = Path(tempfile.mkdtemp(prefix="ifxtest_"))
    (d / "text.txt").write_text("\n".join(lines or []), encoding="utf-8")
    (d / "images.json").write_text(json.dumps(images or []), encoding="utf-8")
    return str(d)


def _cmp(old_lines, new_lines, old_imgs=None, new_imgs=None, settings=None):
    return cmp.compare_snapshots(
        _snap(old_lines, old_imgs), _snap(new_lines, new_imgs),
        change_dir=None, settings=settings or {})


def _snap_rev(lines=None, images=None, rev=None):
    """A snapshot whose meta.json carries a text_rev (or none, = rev 1)."""
    d = Path(_snap(lines, images))
    if rev is not None:
        (Path(d) / "meta.json").write_text(json.dumps({"text_rev": rev}),
                                           encoding="utf-8")
    return str(d)


# --- text diff -------------------------------------------------------------

def test_text_diff_counts_adds_and_removes():
    ch, added, removed, _ = cmp.text_diff(
        _snap(["a", "b", "c"]), _snap(["a", "c", "d"]))
    assert ch is True
    assert removed == 1 and added == 1, (added, removed)


def test_identical_text_is_no_change():
    r = _cmp(["a", "b", "c"], ["a", "b", "c"])
    assert r["any_changed"] is False
    assert r["text_changed"] is False


def test_ignore_patterns_drop_matching_lines():
    # a timestamp line changing must NOT count as an update
    pats = [r"^\d+ (day|hour|minute)s? ago$"]
    r = _cmp(["Overview", "2 hours ago"], ["Overview", "5 hours ago"],
             settings={"ignore_patterns": "\n".join(pats)})
    assert r["text_changed"] is False, "ignore-pattern line still flagged"


# --- the reorder classifier (the AURIX / expansion-order bug) --------------

def test_pure_reorder_is_not_an_update():
    # same lines, different order -> reordered, not changed
    old = ["View product", "TC489", "View product", "TC487"]
    new = ["View product", "TC487", "View product", "TC489"]
    r = _cmp(old, new)
    assert r["text_reordered"] is True, "reorder not detected"
    assert r["text_changed"] is False, "reorder wrongly counted as an update"
    assert r["any_changed"] is False, "reorder wrongly flagged the page"


def test_duplication_is_not_a_reorder():
    # a block rendered twice changes the multiset, so it must still flag -
    # otherwise the filter-dropdown / mega-menu duplication would hide
    old = ["Applications", "Automotive", "Motor control"]
    new = ["Applications", "Automotive", "Automotive", "Motor control"]
    r = _cmp(old, new)
    assert r["text_reordered"] is False, "duplication mistaken for a move"
    assert r["text_changed"] is True, "duplication (an added line) not flagged"


def test_real_add_still_flags():
    r = _cmp(["a", "b"], ["a", "b", "brand new line"])
    assert r["text_changed"] is True
    assert r["text_reordered"] is False
    assert r["lines_added"] == 1 and r["lines_removed"] == 0


def test_real_removal_still_flags():
    r = _cmp(["a", "b", "c"], ["a", "c"])
    assert r["text_changed"] is True
    assert r["lines_removed"] == 1


# --- image diff (URL set) --------------------------------------------------

def test_image_added_and_removed():
    r = _cmp(["x"], ["x"],
             old_imgs=["https://a/1.png", "https://a/2.png"],
             new_imgs=["https://a/2.png", "https://a/3.png"])
    assert r["images_changed"] is True
    assert r["images_added"] == 1 and r["images_removed"] == 1


def test_same_images_no_change():
    imgs = ["https://a/1.png", "https://a/2.png"]
    r = _cmp(["x"], ["x"], old_imgs=imgs, new_imgs=list(reversed(imgs)))
    assert r["images_changed"] is False, "image set order should not matter"


def test_ignore_image_patterns():
    r = _cmp(["x"], ["x"],
             old_imgs=["https://a/hero.png"],
             new_imgs=["https://a/hero.png", "https://a/etc.clientlibs/x.png"],
             settings={"ignore_image_patterns": r"/etc\.clientlibs/"})
    assert r["images_changed"] is False, "ignored image path still flagged"


# --- library diff ----------------------------------------------------------

def test_library_add_is_a_change():
    old = str(_mk_sections({"documents": [{"title": "Datasheet", "url": "u1"}]}))
    new = str(_mk_sections({"documents": [
        {"title": "Datasheet", "url": "u1"}, {"title": "App Note", "url": "u2"}]}))
    lib = cmp.library_diff(old, new)
    assert lib["changed"] is True and lib["added"] >= 1


def _mk_sections(sections):
    d = Path(tempfile.mkdtemp(prefix="ifxtest_"))
    (d / "sections.json").write_text(json.dumps(sections), encoding="utf-8")
    (d / "text.txt").write_text("", encoding="utf-8")
    (d / "images.json").write_text("[]", encoding="utf-8")
    return d


# --- text-extractor revision boundary (the silent re-baseline) -------------

def test_text_diff_suppressed_across_rev_change():
    # baseline on the old walk (rev 1), new capture on the new walk (rev 2):
    # the text is not comparable, so even a big line diff must NOT flag - this
    # is what stops 4,000 pages "updating" the run after a TEXT_JS change
    old = cmp.compare_snapshots(
        _snap_rev(["Home", "Forum thread A", "Member001"], rev=1),
        _snap_rev(["Home"], rev=2), change_dir=None, settings={})
    assert old["text_changed"] is False, "text diff not suppressed across revs"
    assert old["any_changed"] is False, "re-normalised page wrongly flagged"


def test_text_diff_active_within_same_rev():
    # both sides on the new rev -> normal diffing resumes and a real edit flags
    r = cmp.compare_snapshots(
        _snap_rev(["Home", "Old copy"], rev=2),
        _snap_rev(["Home", "New copy"], rev=2), change_dir=None, settings={})
    assert r["text_changed"] is True, "same-rev text edit must still flag"


def test_missing_meta_counts_as_rev1():
    # a legacy snapshot (no meta.json) is rev 1; against a rev-2 capture the
    # text is still cross-rev and suppressed
    r = cmp.compare_snapshots(
        _snap(["A", "B", "C"]),                       # no meta.json -> rev 1
        _snap_rev(["A"], rev=2), change_dir=None, settings={})
    assert r["text_changed"] is False


def test_non_text_signal_still_fires_across_rev():
    # the rev boundary suppresses TEXT ONLY. A genuine change that lands during
    # the re-baseline - here a swapped image - must still be caught, or the
    # re-baseline run would be a blind spot.
    r = cmp.compare_snapshots(
        _snap_rev(["Home"], images=["https://a/1.png"], rev=1),
        _snap_rev(["Home"], images=["https://a/2.png"], rev=2),
        change_dir=None, settings={})
    assert r["images_changed"] is True, "image change lost across rev boundary"
    assert r["any_changed"] is True, "real change hidden during re-baseline"


# --- screenshot-revision boundary (the feed-hidden re-baseline) -------------

def _snap_shot(lines, fill, text_rev=3, shot_rev=None, size=(300, 500)):
    """A snapshot with a solid-colour shot.png and rev stamps."""
    from PIL import Image
    d = Path(_snap(lines, []))
    Image.new("RGB", size, fill).save(d / "shot.png")
    meta = {"text_rev": text_rev}
    if shot_rev is not None:
        meta["shot_rev"] = shot_rev
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return str(d)


def test_visual_suppressed_across_shot_rev_change():
    # old shot = full page (rev 1), new shot = feed-hidden (rev 2): the pixels
    # differ everywhere the feeds used to be, but that must NOT flag - it is the
    # same re-normalisation as the text rev, one row over
    r = cmp.compare_snapshots(
        _snap_shot(["Home"], "#ffffff", shot_rev=1),
        _snap_shot(["Home"], "#202020", shot_rev=2),   # wildly different pixels
        change_dir=None, settings={})
    assert r["visual_changed"] is False, "visual not suppressed across shot_rev"
    assert r["pixel_ratio"] == 0 and r["height_delta"] == 0
    assert r["any_changed"] is False


def test_visual_active_within_same_shot_rev():
    # both on the new shot_rev -> normal pixel diffing resumes and a real
    # visual change flags
    r = cmp.compare_snapshots(
        _snap_shot(["Home"], "#ffffff", shot_rev=2),
        _snap_shot(["Home"], "#000000", shot_rev=2),
        change_dir=None, settings={})
    assert r["visual_changed"] is True, "same-rev visual change must still flag"


def test_text_still_fires_across_shot_rev_boundary():
    # shot_rev gate suppresses the PICTURE only - a real text edit within the
    # same text_rev must survive a screenshot-rev change
    r = cmp.compare_snapshots(
        _snap_shot(["Home", "Old copy"], "#ffffff", text_rev=3, shot_rev=1),
        _snap_shot(["Home", "New copy"], "#ffffff", text_rev=3, shot_rev=2),
        change_dir=None, settings={})
    assert r["text_changed"] is True, "text edit lost across shot_rev boundary"


# --- image-collector revision boundary (the feed-free inventory) ------------

def _snap_img_rev(lines, images, img_rev):
    d = Path(_snap(lines, images))
    (d / "meta.json").write_text(json.dumps({"text_rev": 3, "img_rev": img_rev}),
                                 encoding="utf-8")
    return str(d)


def test_images_suppressed_across_img_rev_change():
    # rev-1 inventory had the news thumbnail; rev-2 dropped it - the removal
    # must NOT flag (one retired press image flagged 585 pages in run 127)
    r = cmp.compare_snapshots(
        _snap_img_rev(["Home"], ["https://a/hero.png", "https://a/news-thumb.png"], 1),
        _snap_img_rev(["Home"], ["https://a/hero.png"], 2),
        change_dir=None, settings={})
    assert r["images_changed"] is False, "images not suppressed across img_rev"
    assert r["any_changed"] is False


def test_images_active_within_same_img_rev():
    r = cmp.compare_snapshots(
        _snap_img_rev(["Home"], ["https://a/hero.png"], 2),
        _snap_img_rev(["Home"], ["https://a/hero2.png"], 2),
        change_dir=None, settings={})
    assert r["images_changed"] is True, "same-rev image change must still flag"


def test_text_still_fires_across_img_rev_boundary():
    r = cmp.compare_snapshots(
        _snap_img_rev(["Home", "Old"], ["https://a/1.png"], 1),
        _snap_img_rev(["Home", "New"], ["https://a/1.png"], 2),
        change_dir=None, settings={})
    assert r["text_changed"] is True, "text edit lost across img_rev boundary"


# --- robustness ------------------------------------------------------------

def test_truncated_screenshot_does_not_crash_the_comparison():
    # a capture killed mid-write leaves a truncated shot.png; the visual
    # signal must degrade to nothing instead of throwing and taking the
    # text/image/library signals down with it (found by the mutation battery)
    from PIL import Image
    old = _snap(["a", "b"], [])
    new = _snap(["a", "CHANGED"], [])
    Image.new("RGB", (300, 400), "#fff").save(Path(old) / "shot.png")
    Image.new("RGB", (300, 400), "#fff").save(Path(new) / "shot.png")
    good = (Path(new) / "shot.png").read_bytes()
    (Path(new) / "shot.png").write_bytes(good[: len(good) // 3])
    r = cmp.compare_snapshots(old, new, change_dir=None, settings={})
    assert r["visual_changed"] is False and r["pixel_ratio"] == 0
    assert r["text_changed"] is True, "other signals must survive a bad shot"


# --- library_diff_detail: the view derives from the detector's rules --------
# Change #5584 was counted 'Documents (~1)' by library_diff while the page
# showed nothing: the view had a PARALLEL diff (added/removed only, raw title
# keys). detail() now shares _item_id/_item_body, so count>0 <=> detail shown.

def _sects(sections):
    d = Path(tempfile.mkdtemp(prefix="ifxtest_"))
    (d / "sections.json").write_text(json.dumps(sections), encoding="utf-8")
    (d / "text.txt").write_text("", encoding="utf-8")
    (d / "images.json").write_text("[]", encoding="utf-8")
    return str(d)


def test_item_identity_survives_url_wobble():
    # a board flapped add+remove when a capture appended a #fragment / ?query /
    # trailing slash / casing to its link - same title+page = same item now
    for wobble in ("https://a/board/X#tab", "https://a/board/X?src=1",
                   "https://a/board/X/", "https://A/Board/X"):
        assert cmp._item_id({"title": "SmartSense2X", "url": wobble}) == \
               cmp._item_id({"title": "SmartSense2X", "url": "https://a/board/X"}), wobble


def test_lazy_image_placeholder_is_not_a_change():
    # a partner logo's base64 placeholder resolving to the CDN url is a lazy
    # load, not an edit - _real_img treats a data: URI like a missing image
    assert cmp._real_img("https://a/logo.png") is True
    assert cmp._real_img("data:image/png;base64,iVBORw0KGgo") is False
    assert cmp._real_img("") is False
    old = _sects({"partners": [{"title": "Arnold NextG", "url": "u",
                                "img": "data:image/png;base64,iVB"}]})
    new = _sects({"partners": [{"title": "Arnold NextG", "url": "u",
                                "img": "https://assets.infineon.com/logo.png"}]})
    assert cmp.library_diff(old, new)["changed"] is False, "lazy logo flagged"
    # but two REAL logos differing is still a real change
    new2 = _sects({"partners": [{"title": "Arnold NextG", "url": "u",
                                 "img": "https://a/logo-v2.png"}]})
    old2 = _sects({"partners": [{"title": "Arnold NextG", "url": "u",
                                 "img": "https://a/logo-v1.png"}]})
    assert cmp.library_diff(old2, new2)["changed"] is True


def test_community_state_reads_thread_list_not_placeholder():
    import tempfile, json as _json
    def _snap_comm(exists, has_content, nthreads):
        d = Path(tempfile.mkdtemp(prefix="ifxcomm_"))
        threads = [{"title": f"t{i}", "url": f"u{i}"} for i in range(nthreads)]
        (d / "sections.json").write_text(_json.dumps({"flags": {
            "developer_community": {"exists": exists, "has_content": has_content,
                                    "threads": threads}}}), encoding="utf-8")
        return str(d)
    # the run-134 flap: mid-load (has_content False, 1 thread) vs settled
    # (True, 5). Threads>0 both -> 'has discussions' both -> no transition
    assert cmp._community_state(_snap_comm(True, False, 1)) == "has discussions"
    assert cmp._community_state(_snap_comm(True, True, 5)) == "has discussions"
    assert cmp._community_state(_snap_comm(True, False, 0)) == "empty"
    assert cmp._community_state(_snap_comm(False, False, 0)) == "absent"


def test_detail_shows_modified_in_place_item():
    # the 5584 case: same title+url, meta/dtype casing changed
    old = _sects({"documents": [{"title": "PQR-XMC4100", "url": "u1",
                                 "meta": "Product qualification report | 02_00",
                                 "dtype": "Product qualification report"}]})
    new = _sects({"documents": [{"title": "PQR-XMC4100", "url": "u1",
                                 "meta": "Product Qualification Report | 02_00",
                                 "dtype": "Product Qualification Report"}]})
    det = cmp.library_diff_detail(old, new)
    assert "documents" in det, "modified item invisible to the view again"
    ch = det["documents"]["changed"]
    assert len(ch) == 1 and ch[0]["title"] == "PQR-XMC4100"
    fields = {f for f, _, _ in ch[0]["fields"]}
    assert "meta" in fields and "dtype" in fields


def test_detail_and_count_agree_on_every_shape():
    # the invariant: whatever the detector counts, the detail shows - and
    # whatever the detail shows, the detector counted. Exercised over every
    # tricky shape: casing-only retitle (detector-silent), lazy poster
    # (silent), img both-present (real), price string, stock tick (exempt),
    # an unlabeled category (about), and a plain add/remove.
    cases = [
        # (old sections, new sections)
        ({"documents": [{"title": "Datasheet", "url": "u", "meta": "m"}]},
         {"documents": [{"title": "DATASHEET", "url": "u", "meta": "m"}]}),
        ({"videos": [{"title": "Tour", "url": "v", "img": "poster.png"}]},
         {"videos": [{"title": "Tour", "url": "v", "img": ""}]}),
        ({"videos": [{"title": "Tour", "url": "v", "img": "a.png"}]},
         {"videos": [{"title": "Tour", "url": "v", "img": "b.png"}]}),
        ({"price": "EUR 12 | 260 in stock"}, {"price": "EUR 12 | 259 in stock"}),
        ({"price": ""}, {"price": "EUR 249.00"}),
        ({"about": [{"title": "Intro", "url": "", "meta": "old words"}]},
         {"about": [{"title": "Intro", "url": "", "meta": "new words"}]}),
        ({"documents": [{"title": "A", "url": "1"}]},
         {"documents": [{"title": "A", "url": "1"}, {"title": "B", "url": "2"}]}),
        ({"images": [{"title": "x", "url": "i1"}]},        # _NOT_CONTENT: both silent
         {"images": [{"title": "y", "url": "i2"}]}),
    ]
    for old_s, new_s in cases:
        old, new = _sects(old_s), _sects(new_s)
        counted = cmp.library_diff(old, new)["changed"]
        shown = bool(cmp.library_diff_detail(old, new))
        assert counted == shown, (
            f"view and detector disagree on {old_s} -> {new_s}: "
            f"counted={counted} shown={shown}")


# --- the developer-community tri-state --------------------------------------
# absent / empty / has discussions is tracked EVERY run; a transition flags.
# What sits INSIDE the widget (threads, authors, dates, pixels) never does -
# it is excluded from the text, image and screenshot signals.

def _snap_comm(state):
    d = Path(_snap(["Page body"], []))
    flags = {}
    if state != "absent":
        flags = {"developer_community": {"exists": True,
                                         "has_content": state == "has discussions"}}
    else:
        flags = {"developer_community": {"exists": False, "has_content": False}}
    (d / "sections.json").write_text(json.dumps({"flags": flags}),
                                     encoding="utf-8")
    return str(d)


def test_community_state_transitions_flag():
    for frm, to in (("absent", "has discussions"),
                    ("has discussions", "empty"),
                    ("empty", "absent")):
        r = cmp.compare_snapshots(_snap_comm(frm), _snap_comm(to),
                                  change_dir=None, settings={})
        assert r["community_changed"] is True, f"{frm} -> {to} did not flag"
        assert r["any_changed"] is True
        assert r["community_from"] == frm and r["community_to"] == to
        assert "developer community" in r["summary"]


def test_community_same_state_is_silent():
    r = cmp.compare_snapshots(_snap_comm("has discussions"),
                              _snap_comm("has discussions"),
                              change_dir=None, settings={})
    assert r["community_changed"] is False and r["any_changed"] is False


# --- screenshot: feeds stay IN the picture, the judgement masks them ---------

def _snap_masked(fill_main, fill_feed, feed_rect=(40, 380, 320, 100),
                 height=520, extra=None):
    """A snapshot whose shot.png has a 'feed' box painted at feed_rect and a
    meta.json declaring that region in shot_ignore."""
    from PIL import Image, ImageDraw
    d = Path(_snap(["Page body"], []))
    im = Image.new("RGB", (400, height), fill_main)
    x, y, w_, h_ = feed_rect
    ImageDraw.Draw(im).rectangle([x, y, x + w_, y + h_], fill=fill_feed)
    if extra:
        ImageDraw.Draw(im).rectangle(extra, fill=(200, 30, 30))
    im.save(d / "shot.png")
    (d / "meta.json").write_text(json.dumps({
        "text_rev": 3, "shot_rev": 3, "img_rev": 2,
        "shot_ignore": [{"x": x, "y": y, "w": w_, "h": h_}]}),
        encoding="utf-8")
    return str(d)


def test_feed_churn_inside_the_picture_never_flags():
    # the community box is IN both screenshots and its content changed
    # completely - masked at compare, so nothing flags
    r = cmp.compare_snapshots(
        _snap_masked("#ffffff", "#3355aa"),
        _snap_masked("#ffffff", "#aa5533"),
        change_dir=None, settings={})
    assert r["visual_changed"] is False, "masked feed churn flagged pixels"
    assert r["any_changed"] is False


def test_real_change_outside_the_feed_still_flags():
    r = cmp.compare_snapshots(
        _snap_masked("#ffffff", "#3355aa"),
        _snap_masked("#ffffff", "#3355aa", extra=[50, 60, 250, 200]),
        change_dir=None, settings={})
    assert r["visual_changed"] is True, "real page change hidden by the mask"


def test_feed_growth_does_not_count_as_page_growth():
    # the feed box grew 120px and the page with it: judged height is the page
    # MINUS each side's feed, so nothing flags
    r = cmp.compare_snapshots(
        _snap_masked("#ffffff", "#3355aa", feed_rect=(40, 380, 320, 100),
                     height=520),
        _snap_masked("#ffffff", "#aa5533", feed_rect=(40, 380, 320, 220),
                     height=640),
        change_dir=None, settings={})
    assert r["visual_changed"] is False, (
        "feed growth read as the page growing: %s%% hd=%s"
        % (r["pixel_ratio"], r["height_delta"]))


# --- whole verdict ---------------------------------------------------------

def test_summary_reads_no_change_when_nothing_moved():
    r = _cmp(["a"], ["a"])
    assert r["summary"] == "no change"


# --- the structured read has a revision too ---------------------------------
# Text, images and the screenshot all re-baseline silently when their method
# changes. The library did not, so when rev 5 moved the price/stock block out
# of the raw walk it was the ONE signal with nothing to suppress it: every page
# came up with a section difference and nothing a human could see behind it.

def _doc(name):
    return {"title": name, "url": "https://x/%s.pdf" % name, "meta": "Datasheet"}


def _sec_snap(tmp, name, docs, rev=None):
    d = Path(tmp) / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "sections.json").write_text(
        json.dumps({"documents": [_doc(n) for n in docs]}), encoding="utf-8")
    meta = {"text_rev": 5, "shot_rev": 5, "img_rev": 4}
    if rev is not None:
        meta["sections_rev"] = rev
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return str(d)


def test_a_sections_revision_change_re_baselines_silently():
    tmp = tempfile.mkdtemp(prefix="ifxsec_")
    old = _sec_snap(tmp, "old", ["a", "b"])                # unstamped -> rev 1
    new = _sec_snap(tmp, "new", ["a", "b", "c"], rev=2)
    r = cmp.compare_snapshots(old, new)
    assert not r["library_changed"],         "a change of extraction method was reported as a change of content"
    assert not r["any_changed"], r["summary"]


def test_the_same_revision_still_reports_a_real_library_change():
    tmp = tempfile.mkdtemp(prefix="ifxsec2_")
    old = _sec_snap(tmp, "old", ["a", "b"], rev=2)
    new = _sec_snap(tmp, "new", ["a", "b", "c"], rev=2)
    r = cmp.compare_snapshots(old, new)
    assert r["library_changed"], "a genuine library change went unreported"
    assert r["any_changed"], r["summary"]


def test_every_signal_carries_a_revision():
    # the next person to change an extractor should not have to remember which
    # of the four is the one without a stamp
    import capture
    for name in ("TEXT_REV", "SHOT_REV", "IMG_REV", "SECTIONS_REV"):
        assert isinstance(getattr(capture, name, None), int), f"{name} is missing"


def test_a_trademark_mark_is_decoration_not_identity():
    """Infineon renders them inconsistently: the same datasheet came back as
    "PSOC(tm) 4 ..." one week and "PSOC 4 ..." the next. Keyed on the raw
    title that is one item removed and a different one added, on every page
    carrying it."""
    import compare as cmp
    same = lambda a, b: cmp._item_id({"title": a, "url": "https://x/a.pdf"}) == \
                        cmp._item_id({"title": b, "url": "https://x/a.pdf"})
    assert same("AN85951 - PSOC\u2122 4 MCU guide", "AN85951 - PSOC 4 MCU guide")
    assert same("AIROC(TM) Wi-Fi", "AIROC Wi-Fi")
    assert same("Bluetooth(R) combo", "Bluetooth combo")
    assert same("OPTIGA\u2122 TPM", "OPTIGA TPM")


def test_the_mark_rule_does_not_eat_real_words():
    """(C) and (R2) are revisions, not trademarks - stripping them would merge
    two genuinely different documents into one."""
    import compare as cmp
    diff = lambda a, b: cmp._item_id({"title": a, "url": "https://x/a.pdf"}) != \
                        cmp._item_id({"title": b, "url": "https://x/a.pdf"})
    assert diff("Rev(C) board notes", "Rev board notes")
    assert diff("Board (R2) revision", "Board revision")
    assert cmp.strip_marks("(C) 2026 Infineon") == "(C) 2026 Infineon"


def test_a_real_version_bump_still_flags():
    """The thing this must never suppress: a republished document."""
    import compare as cmp
    a = {"title": "PSOC\u2122 Edge datasheet", "url": "https://x/d.pdf",
         "meta": "Datasheet | PDF | EN | 15_00 | 3.55MB | May 04, 2026"}
    b = {"title": "PSOC Edge datasheet", "url": "https://x/d.pdf",
         "meta": "Datasheet | PDF | EN | 16_00 | 3.76MB | Aug 12, 2026"}
    assert cmp._item_id(a) == cmp._item_id(b), "should be the same document"
    assert cmp._item_body(a) != cmp._item_body(b), "its version change must show"


# --- the product table's shape ----------------------------------------------

def _snap_with_table(tmp, rev=4, count=7, columns=None, exists=True):
    """A capture carrying nothing but a product-table flag."""
    import json, pathlib
    d = pathlib.Path(tmp)
    d.mkdir(parents=True, exist_ok=True)
    flags = {"product_table": {"exists": exists, "has_content": bool(count),
                               "count": count, "columns": columns or []}}
    (d / "sections.json").write_text(json.dumps({"flags": flags}), encoding="utf-8")
    (d / "meta.json").write_text(json.dumps({"sections_rev": rev}), encoding="utf-8")
    return str(d)


def test_a_reordered_products_table_is_not_a_change():
    """The whole point of the request: structure, not sequence."""
    import tempfile, compare as cmp
    t = tempfile.mkdtemp()
    a = _snap_with_table(t + "/a", columns=["Part", "Flash", "GPIO"])
    b = _snap_with_table(t + "/b", columns=["GPIO", "Part", "Flash"])
    assert cmp._table_state(a) == cmp._table_state(b), \
        "a table dealt in a different order is the same table"


def test_a_column_appearing_or_disappearing_is_a_change():
    import tempfile, compare as cmp
    t = tempfile.mkdtemp()
    a = _snap_with_table(t + "/a", columns=["Part", "Flash"])
    b = _snap_with_table(t + "/b", columns=["Part", "Flash", "Vdd"])
    assert cmp._table_state(a) != cmp._table_state(b)


def test_the_number_of_products_moving_is_a_change():
    import tempfile, compare as cmp
    t = tempfile.mkdtemp()
    a = _snap_with_table(t + "/a", count=7, columns=["Part"])
    b = _snap_with_table(t + "/b", count=9, columns=["Part"])
    assert cmp._table_state(a) != cmp._table_state(b)


def test_no_table_is_not_the_same_as_an_empty_one():
    import tempfile, compare as cmp
    t = tempfile.mkdtemp()
    none = _snap_with_table(t + "/n", exists=False, count=None, columns=[])
    empty = _snap_with_table(t + "/e", exists=True, count=0, columns=[])
    assert cmp._table_state(none) == (None, None)
    assert cmp._table_state(empty) != (None, None)


def test_the_table_is_silent_across_the_revision_that_introduced_it():
    """A rev-3 baseline never stored columns; that must not read as a table
    that lost all of them."""
    import tempfile, compare as cmp
    t = tempfile.mkdtemp()
    old = _snap_with_table(t + "/o", rev=3, count=None, columns=[])
    new = _snap_with_table(t + "/n", rev=4, count=7, columns=["Part", "Flash"])
    res = cmp.compare_snapshots(old, new, change_dir=None, settings={})
    assert not res["table_changed"], "the rev boundary must suppress this"


# --- editorial limits --------------------------------------------------------

def _copy(**sections):
    import json, tempfile, pathlib
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "sections.json").write_text(json.dumps(sections), encoding="utf-8")
    return str(d)


def test_copy_within_every_limit_reports_nothing():
    import compare as cmp
    d = _copy(about=[{"title": "a" * 400}],
              subheadline=[{"title": "s" * 140}],
              meta_description="m" * 140,
              features=[{"title": "f" * 30}, {"title": "g" * 12}],
              benefits=[{"title": "b" * 20}, {"title": "c" * 20}],
              overview=[{"title": "o" * 440}])
    assert cmp.copy_findings(d) == {}


def test_a_finding_is_keyed_by_its_block():
    """Keyed, not a flat list, so the flag can sit beside the block it is
    about and the dashboard can filter on one block at a time."""
    import compare as cmp
    got = cmp.copy_findings(_copy(subheadline=[{"title": "s" * 187}]))
    assert got == {"subheadline": "187 over 150"}, got


def test_a_bullet_is_judged_on_its_own_length_not_the_list_total():
    import compare as cmp
    fine = cmp.copy_findings(_copy(features=[{"title": "f" * 30}] * 12))
    assert "features" not in fine, fine
    over = cmp.copy_findings(_copy(features=[{"title": "f" * 55},
                                             {"title": "ok"}]))
    assert over["features"] == "55 over 40", over


def test_too_few_and_too_many_are_both_caught():
    import compare as cmp
    assert cmp.copy_findings(_copy(features=[{"title": "one"}]))["features"]         == "1, needs 2-12"
    assert cmp.copy_findings(_copy(benefits=[{"title": "b"}] * 13))["benefits"]         == "13, allows 2-12"


def test_a_block_that_is_not_there_is_not_a_breach():
    """Most tracked pages are part pages and carry no marketing copy at all.
    Judging them for its absence flagged 2,819 of 2,820 measured pages."""
    import compare as cmp
    assert cmp.copy_findings(_copy(subheadline=[])) == {}
    assert cmp.copy_findings(_copy()) == {}
    got = cmp.copy_findings(_copy(subheadline=[{"title": "s" * 200}]))
    assert got["subheadline"] == "200 over 150"


def test_the_meta_description_is_measured_as_a_plain_string():
    import compare as cmp
    got = cmp.copy_findings(_copy(meta_description="m" * 200))
    assert got["meta_description"] == "200 over 150", got


def test_the_limit_itself_comes_from_settings():
    """The whole point of making it editable: 1,907 pages sit at 151-199 and
    whether that is a breach is an editorial decision, not a code one."""
    import compare as cmp
    page = _copy(meta_description="m" * 157)
    assert cmp.copy_findings(page)["meta_description"] == "157 over 150"
    relaxed = cmp.copy_findings(page, {"copy_meta_description_chars": "160"})
    assert relaxed == {}, relaxed


def test_a_limit_of_zero_switches_the_rule_off():
    import compare as cmp
    page = _copy(features=[{"title": "f" * 900}])
    assert "features" in cmp.copy_findings(page)
    off = cmp.copy_findings(page, {"copy_features_chars": "0",
                                   "copy_features_min": "0",
                                   "copy_features_max": "0"})
    assert off == {}, off


def test_a_nonsense_setting_falls_back_instead_of_crashing():
    import compare as cmp
    got = cmp.copy_findings(_copy(subheadline=[{"title": "s" * 200}]),
                            {"copy_subheadline_chars": "not a number"})
    assert got["subheadline"] == "200 over 150"


def test_storing_screenshots_as_webp_does_not_change_the_verdict():
    """Screenshots became lossless WEBP to reclaim 1.19GB. They are an INPUT
    to the pixel diff, so the only acceptable saving is one the diff cannot
    perceive: same percentage, same regions, and an unchanged page still
    exactly 0. A lossy format would fail this test, which is the point."""
    import tempfile
    from pathlib import Path
    from PIL import Image, ImageDraw
    import db
    tmp = Path(tempfile.mkdtemp())
    a = Image.new("RGB", (500, 700), "white")
    d = ImageDraw.Draw(a)
    for y in range(0, 700, 20):
        d.text((15, y), "antialiased text row %d" % y, fill="black")
    b = a.copy()
    ImageDraw.Draw(b).rectangle([300, 200, 420, 250], fill="red")

    def run(fmt):
        o, n = tmp / (fmt + "o"), tmp / (fmt + "n")
        o.mkdir(parents=True, exist_ok=True), n.mkdir(parents=True, exist_ok=True)
        if fmt == "png":
            a.save(o / "shot.png"), b.save(n / "shot.png")
        else:
            a.save(o / "shot.webp", "WEBP", lossless=True, method=4)
            b.save(n / "shot.webp", "WEBP", lossless=True, method=4)
        c = tmp / (fmt + "c"); c.mkdir(exist_ok=True)
        return cmp.visual_diff(str(o), str(n), str(c)), c

    (pct_p, _, n_p), _ = run("png")
    (pct_w, _, n_w), cdir = run("webp")
    assert (pct_p, n_p) == (pct_w, n_w), \
        "webp storage moved the verdict: png=%s webp=%s" % (pct_p, pct_w)

    # an identical page must read exactly zero, not "nearly" zero
    s1, s2 = tmp / "s1", tmp / "s2"
    s1.mkdir(exist_ok=True), s2.mkdir(exist_ok=True)
    a.save(s1 / "shot.webp", "WEBP", lossless=True, method=4)
    a.save(s2 / "shot.webp", "WEBP", lossless=True, method=4)
    pct0, _, n0 = cmp.visual_diff(str(s1), str(s2), str(tmp / "s3"))
    assert pct0 == 0.0 and n0 == 0, "webp invented a change: %s%%" % pct0

    # reviewer-facing art is webp (lossy is fine there - never read back)
    names = sorted(p.name for p in cdir.iterdir())
    assert "old_boxed.webp" in names and "new_boxed.webp" in names, names
    assert not any(x.endswith(".png") for x in names), \
        "a diff picture is still PNG: %s" % names
    # the resolver prefers webp but still finds a pre-migration png
    assert db.shot_path(tmp / "webpo").name == "shot.webp"
    assert db.shot_path(tmp / "pngo").name == "shot.png"


def test_copy_detail_carries_the_text_that_broke_the_rule():
    """A badge says "58 over 40". Rewriting needs the bullet that is 58 long,
    and the other eleven, because a feature list is rewritten as a set."""
    import compare as cmp
    d = cmp.copy_detail(_copy(features=[{"title": "f" * 55},
                                        {"title": "short"},
                                        {"title": "g" * 44}]))["features"]
    assert d["blocks"] == ["f" * 55, "short", "g" * 44]
    assert d["over"] == [0, 2], d["over"]          # by index, so "line 1, 3"
    assert d["problems"] == ["55 over 40"]
    assert (d["chars"], d["lo"], d["hi"]) == (40, 2, 12)


def test_a_block_breaking_both_of_its_rules_now_says_both():
    """The verdicts used to be assigned into one dict slot, so a list that was
    too long AND had an over-long bullet reported only the bullet - and the
    editor fixed the wording, re-ran, and was told about the count."""
    import compare as cmp
    got = cmp.copy_findings(_copy(features=[{"title": "f" * 55}] * 13))
    assert got["features"] == "13, allows 2-12; 55 over 40", got


def test_copy_detail_keeps_blocks_that_are_within_their_limits():
    """Same reason: the sheet shows all twelve bullets, not the two that are
    over, so it has to hold a clean block too - with no problems against it."""
    import compare as cmp
    d = cmp.copy_detail(_copy(features=[{"title": "fine"}, {"title": "also"}]))
    assert d["features"]["problems"] == []
    assert d["features"]["blocks"] == ["fine", "also"]
    # ...and a block the page does not have stays absent, as ever
    assert "overview" not in d


def test_a_parameter_table_under_a_features_heading_is_not_judged_as_copy():
    """Some part pages put the spec table under a Features heading, so the
    capture files it as features and the 40-character rule starts reporting
    "Flash 576 kByte" as an over-long marketing bullet."""
    import compare as cmp
    table = [{"title": "CAN Nodes 3"},
             {"title": "CPU ARM Cortex-M4F"},
             {"title": "DMA Channels 3 DMA controllers with 216 channels"},
             {"title": "DSP Functionality Yes"},
             {"title": "External Bus Interface No"}]
    assert cmp.copy_findings(_copy(features=table)) == {}
    assert "features" not in cmp.copy_detail(_copy(features=table))


def test_the_test_is_the_block_not_the_page_type():
    """Part pages carry real feature bullets too - judging by page type would
    have switched the rule off on a hundred pages that need it."""
    import compare as cmp
    real = [{"title": "Very low power consumption"},
            {"title": "A bullet that runs a long way past the forty limit"}]
    assert cmp.copy_findings(_copy(features=real))["features"] == "50 over 40"
    # one stray Yes is a sentence, not a table: two rows is the threshold
    one = real + [{"title": "Hardware crypto Yes"}]
    assert "features" in cmp.copy_findings(_copy(features=one))


def test_benefits_keep_their_rule_whatever_they_look_like():
    """Anas's call, and the right one: benefits are written by a person on
    every page that has them, and a rule that switches itself off is worse
    than one that occasionally complains."""
    import compare as cmp
    shaped = [{"title": "Integrated gate driver Yes"},
              {"title": "Hardware protection to avoid damage in the lab No"},
              {"title": "Safe operating voltage"}]
    assert "benefits" in cmp.copy_findings(_copy(benefits=shaped))
