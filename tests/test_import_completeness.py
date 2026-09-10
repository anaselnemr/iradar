"""The completeness-CSV importer: what it would change, before it changes it.

The file decides the ownership of thousands of pages, so the part worth
testing is the PLAN - which rows are new, which are a correction, and which
are already right - not the UPDATE statement.
"""
import csv
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
sys.path.insert(0, str(HERE.parent / "iradar"))
import import_completeness_csv as imp                            # noqa: E402


def _rows(*triples):
    return [{"product_page_url": u, "bl": bl, "pl": pl} for u, bl, pl in triples]


def test_bl_arrives_as_a_number_and_is_stored_as_a_name():
    """The export writes 7 and 5; iRadar has always stored ICW and DSI,
    because that is what the report the team reads says."""
    assert imp.BL_NAMES["7"] == "ICW"
    assert imp.BL_NAMES["5"] == "DSI"


def test_a_page_we_do_not_track_is_listed_for_adding():
    pages = {imp.norm("https://www.infineon.com/part/A"): (1, "ICW", "92")}
    add, set_, same, none = imp.plan(
        _rows(("https://www.infineon.com/part/B", "7", "92")), pages)
    assert add == ["https://www.infineon.com/part/B"]
    assert set_ == [] and same == 0


def test_a_page_that_already_agrees_is_left_alone():
    pages = {imp.norm("https://www.infineon.com/part/A"): (1, "ICW", "92")}
    add, set_, same, none = imp.plan(
        _rows(("https://www.infineon.com/part/A", "7", "92")), pages)
    assert (add, set_, same) == ([], [], 1)


def test_a_disagreement_is_a_correction_and_says_both_sides():
    """PL 86 was stale on 120 pages - the export moved them to 82. The plan
    carries the old values so the printout can show what is being overwritten
    rather than just how many rows were touched."""
    pages = {imp.norm("https://www.infineon.com/part/A"): (7, "ICW", "86")}
    add, set_, same, none = imp.plan(
        _rows(("https://www.infineon.com/part/A", "7", "82")), pages)
    assert set_ == [(7, "https://www.infineon.com/part/A", "ICW", "86",
                     "ICW", "82")]


def test_an_export_row_with_no_bl_or_pl_changes_nothing():
    """38 rows in the September export carry neither. Writing empties over
    good values because a filter did not cover them is data loss."""
    pages = {imp.norm("https://www.infineon.com/part/A"): (1, "ICW", "92")}
    add, set_, same, none = imp.plan(
        _rows(("https://www.infineon.com/part/A", "", "")), pages)
    assert (set_, same, none) == ([], 0, 1)


def test_a_page_is_matched_by_its_canonical_or_redirect_address():
    """The export lists plenty of pages under a legacy address that 301s to
    one already tracked. Without this the importer adds a duplicate every run
    and the ownership never reaches the real page."""
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT,"
                " canonical_url TEXT, redirect_from TEXT, bl TEXT, pl TEXT)")
    con.execute("INSERT INTO urls VALUES(1, 'https://www.infineon.com/new',"
                " '', 'https://www.infineon.com/old', 'ICW', '92')")
    pages = imp.index_pages(con)
    assert pages[imp.norm("https://www.infineon.com/old")][0] == 1
    assert pages[imp.norm("https://WWW.infineon.com/new/")][0] == 1


def test_the_real_export_parses_and_its_columns_are_where_we_think():
    p = Path(tempfile.mkdtemp()) / "c.csv"
    with open(p, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ispn_id", "ispn_name", "product_page_url", "division",
                    "bl", "pl", "hfg", "product_status"])
        w.writerow(["1", "X", "https://www.infineon.com/part/X", "CSS",
                    "7", "92", "PSOC", "active and preferred"])
    rows = imp.read_csv(str(p))
    assert rows[0]["product_page_url"] == "https://www.infineon.com/part/X"
    assert rows[0]["bl"] == "7" and rows[0]["pl"] == "92"
