"""Tests for the URL/type classification and helpers in db.py.

Pure functions - no browser, no network, no live database. These decide what
type and level a page is from its address alone (used at add time AND re-applied
at capture, so a mistyped page self-heals), plus the URL-normalising helpers.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "iradar"))
import db  # noqa: E402


def test_ptype_from_url_shape():
    cases = {
        "https://www.infineon.com/": "Home",
        "https://www.infineon.com/part/XMC1100-Q024F0008-AB": "Part",
        "https://www.infineon.com/evaluation-board/KIT_X": "Part",
        "https://www.infineon.com/products/microcontroller/psoc": "Product",
        "https://www.infineon.com/applications/consumer-electronics": "Application",
        "https://www.infineon.com/application/coffee-machines": "Application",
        "https://www.infineon.com/design-resources/development-tools": "Design resources",
        "https://softwaretools.infineon.com/tools/com.ifx.x": "Design resources",
        "https://www.infineon.com/gated/infineon-whitepaper": "Promo",
    }
    for url, want in cases.items():
        got = db.ptype_for_url(url)
        assert got == want, f"{url}\n  got {got!r} want {want!r}"


def test_part_levels():
    _, lvl, _ = db.type_level_for("https://www.infineon.com/part/XMC1100-Q024F0008-AB")
    assert lvl == "PTP", lvl
    _, lvl, _ = db.type_level_for("https://www.infineon.com/evaluation-board/KIT_X")
    assert lvl == "EB", lvl


def test_design_resource_levels_by_depth():
    # 1 segment = Category, 2 = Family, 3 = Sub-Family
    assert db.dr_level_for("https://www.infineon.com/design-resources/tools") == "DRC"
    assert db.dr_level_for("https://www.infineon.com/design-resources/tools/sdk") == "DRF"
    assert db.dr_level_for("https://www.infineon.com/design-resources/tools/sdk/x") == "DRSF"


def test_home_tagged_home():
    pt, lvl, _ = db.type_level_for("https://www.infineon.com/")
    assert pt == "Home" and lvl == "HOME", (pt, lvl)


def test_strip_lang_removes_locale_segment():
    assert db.strip_lang("https://www.infineon.com/de/part/x") == \
        "https://www.infineon.com/part/x"
    # a non-locale first segment is left alone
    assert db.strip_lang("https://www.infineon.com/part/x") == \
        "https://www.infineon.com/part/x"


def test_strip_junk_params_drops_query_on_infineon():
    # the query string is dropped for infineon hosts so the same page under two
    # tracking links collapses to one address
    out = db.strip_junk_params(
        "https://www.infineon.com/part/x?utm_source=news&foo=1")
    assert "?" not in out, out
    assert out.rstrip("/") == "https://www.infineon.com/part/x"
