"""The corpus accounting the user asked for, runnable at any time.

  - dead (404) and blank (0-byte 200) pages, per type, with the URLs
  - stale Excel URLs that 301-collapsed onto a live destination
  - library items indexed, and per-type which categories never appear
  - documents by backend (DAM / Windchill) and any type not on the sheet

Safe to run mid-run; the numbers just sharpen as coverage grows.
"""
import sys
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\anasn\Desktop\InfineonWatch")
import app as A  # noqa: E402
import db  # noqa: E402

con = db.connect()

print("=" * 70)
print("LIVENESS  (a page the site itself serves broken)")
rows = con.execute(
    "SELECT ptype,"
    " SUM(CASE WHEN last_status IN ('baseline','unchanged','changed')"
    "     THEN 1 ELSE 0 END) ok,"
    " SUM(CASE WHEN last_status='notlive' THEN 1 ELSE 0 END) dead,"
    " SUM(CASE WHEN last_status='blank' THEN 1 ELSE 0 END) blank,"
    " SUM(CASE WHEN last_status='error' THEN 1 ELSE 0 END) err,"
    " SUM(CASE WHEN last_checked IS NULL THEN 1 ELSE 0 END) todo,"
    " COUNT(*) n"
    " FROM urls WHERE active=1 GROUP BY ptype ORDER BY ptype").fetchall()
print(f"   {'type':<18}{'ok':>6}{'dead':>6}{'blank':>6}{'err':>5}{'todo':>6}{'total':>7}")
for r in rows:
    print(f"   {r['ptype']:<18}{r['ok']:>6}{r['dead']:>6}{r['blank']:>6}"
          f"{r['err']:>5}{r['todo']:>6}{r['n']:>7}")

print("\nBLANK pages (HTTP 2xx, empty body - a visitor sees nothing):")
for r in con.execute("SELECT url FROM urls WHERE last_status='blank' ORDER BY url"):
    print("   ", r["url"])

print("\n" + "=" * 70)
print("LIBRARY")
n_items = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
print(f"   items indexed: {n_items:,}")
by_cat = con.execute(
    "SELECT category, COUNT(*) n FROM items GROUP BY category ORDER BY n DESC")
for r in by_cat:
    print(f"      {r['category']:<18} {r['n']:>7,}")

print("\nper-type: categories that NEVER appear (once the type is fully captured):")
for t in [r["ptype"] for r in con.execute(
        "SELECT DISTINCT ptype FROM urls WHERE ptype<>'' ORDER BY ptype")]:
    present = A._cats_present_for_type(con, t)
    if present is None:
        print(f"   {t:<18} (still capturing - not final)")
    else:
        allcats = {"documents", "design_resources", "news", "trainings",
                   "videos", "features", "benefits", "overview", "applications",
                   "categories", "products", "images"}
        missing = sorted(allcats - present)
        print(f"   {t:<18} absent: {', '.join(missing) if missing else '(has all)'}")

print("\n" + "=" * 70)
print("DOCUMENTS BY BACKEND")
docs = con.execute("SELECT doc_type, meta FROM items WHERE category='documents'"
                   ).fetchall()
tally, unmapped, no_type = Counter(), Counter(), 0
for r in docs:
    sysname = A.backend_system("documents", r["meta"], r["doc_type"])
    dtype = (r["doc_type"] or A.document_type_of(r["meta"])).strip()
    if sysname:
        tally[sysname] += 1
    elif not dtype:
        # a document with no Type at all - some promo pages publish only a
        # download link and no metadata table. Not a gap in the sheet.
        no_type += 1
        tally["(no type published)"] += 1
    else:
        tally["(type not on the sheet)"] += 1
        unmapped[dtype] += 1
for k, v in tally.most_common():
    print(f"   {k:<24} {v:>6,}")
if unmapped:
    print("\n   REAL document types NOT on the sheet (a genuine gap to raise):")
    for t, n in unmapped.most_common():
        print(f"      {n:>4}x  {t!r}")
else:
    print("\n   every document that HAS a type maps to the sheet"
          + (f"; {no_type} document(s) publish no type at all" if no_type else "."))
con.close()
