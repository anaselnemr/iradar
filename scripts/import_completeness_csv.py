"""Import BL and PL from a CSS content-completeness CSV export.

    python scripts/import_completeness_csv.py <file.csv> [--apply]

Sibling of import_css_report.py, which reads the OTHER report - a workbook
with a sheet per page type and BL spelled out in words. This one reads the
flat completeness export, where every row is a product page and BL is a
NUMBER. The two never agree on format, so they do not share a parser.

Dry by default: it prints what it would do and changes nothing. Pass --apply
to write. That is deliberate - the file decides the ownership of thousands of
pages, and a mis-exported filter should cost a printout, not a restore.
"""
import csv
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "iradar"))
import db                                                        # noqa: E402

# The export writes the business line as its number. iRadar has always stored
# the name, because that is what the report the team reads says, and nobody
# asks "which pages belong to 7".
BL_NAMES = {"7": "ICW", "5": "DSI"}


def norm(u):
    """Same normalisation as import_css_report, so the two importers agree
    about which page a URL is."""
    u = (u or "").split("#")[0].split("?")[0].rstrip("/").lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return re.sub(r"/(de|ja|cn|tw|kr|jp)(?=/)", "", u)


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def index_pages(con):
    """Every address a tracked page answers to -> (id, bl, pl).

    canonical_url and redirect_from both count: the export lists plenty of
    pages under a legacy address that 301s to one already tracked, and without
    them the importer adds a duplicate every single run.
    """
    out = {}
    for r in con.execute("SELECT id, url, canonical_url, redirect_from, bl, pl"
                         " FROM urls"):
        val = (r["id"], r["bl"] or "", r["pl"] or "")
        out[norm(r["url"])] = val
        for alt in ("canonical_url", "redirect_from"):
            if r[alt]:
                out.setdefault(norm(r[alt]), val)
    return out


def plan(rows, pages):
    """(to_add, to_set, unchanged, no_data) without touching anything."""
    to_add, to_set, unchanged, no_data = [], [], 0, 0
    for r in rows:
        url = (r.get("product_page_url") or "").strip()
        if not url:
            continue
        hit = pages.get(norm(url))
        if not hit:
            to_add.append(url)
            continue
        uid, have_bl, have_pl = hit
        want_bl = BL_NAMES.get((r.get("bl") or "").strip(), "")
        want_pl = (r.get("pl") or "").strip()
        if not want_bl and not want_pl:
            no_data += 1                    # the export has nothing to give
        elif (have_bl, have_pl) == (want_bl, want_pl):
            unchanged += 1
        else:
            to_set.append((uid, url, have_bl, have_pl, want_bl, want_pl))
    return to_add, to_set, unchanged, no_data


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv
    path = args[0] if args else os.environ.get("COMPLETENESS_CSV", "")
    if not path:
        sys.exit("pass the .csv as the first argument")

    rows = read_csv(path)
    db.init_db()
    con = db.connect()
    to_add, to_set, unchanged, no_data = plan(rows, index_pages(con))

    print("rows in the export        : %d" % len(rows))
    print("  already correct         : %d" % unchanged)
    print("  BL/PL to write          : %d" % len(to_set))
    print("  pages to add            : %d" % len(to_add))
    print("  export has no BL/PL     : %d" % no_data)
    moves = {}
    for _uid, _u, hb, hp, wb, wp in to_set:
        moves["%s/%s -> %s/%s" % (hb or "-", hp or "-", wb or "-", wp or "-")] = \
            moves.get("%s/%s -> %s/%s" % (hb or "-", hp or "-", wb or "-", wp or "-"), 0) + 1
    for k, n in sorted(moves.items(), key=lambda x: -x[1]):
        print("      %-28s %d" % (k, n))

    if not apply:
        print("\nDRY RUN - nothing written. Pass --apply to write.")
        con.close()
        return

    added, skipped = db.add_urls(to_add)
    print("\n[urls] added %d, skipped %d duplicate(s)" % (added, skipped))
    # re-index so pages added a moment ago can take their BL/PL too
    pages = index_pages(con)
    n = 0
    for r in rows:
        url = (r.get("product_page_url") or "").strip()
        hit = pages.get(norm(url)) if url else None
        if not hit:
            continue
        want_bl = BL_NAMES.get((r.get("bl") or "").strip(), "")
        want_pl = (r.get("pl") or "").strip()
        if not want_bl and not want_pl:
            continue
        if (hit[1], hit[2]) == (want_bl, want_pl):
            continue
        con.execute("UPDATE urls SET bl=?, pl=? WHERE id=?",
                    (want_bl, want_pl, hit[0]))
        n += 1
    con.commit()
    con.close()
    print("[bl/pl] written on %d pages" % n)
    print("\nDONE. Levels for the new pages come from the app's own rebuild -"
          " open Settings or add them through the app if they look wrong.")


if __name__ == "__main__":
    main()
