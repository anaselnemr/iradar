"""Backfill urls.level from the source report's own "Page Type" column.

Infineon's taxonomy, straight from the sheet rather than guessed:

    PC   > PFP  > PSFP  > PEFP  > PTP     products, ending in part pages
    ADP  > AOP  > ASP / AFB                applications

The PTP sheet has no Page Type column - every row in it IS a part type page.

Run after importing the URLs; safe to re-run.
"""
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\anasn\Desktop\InfineonWatch")
import db  # noqa: E402
import openpyxl  # noqa: E402

XLSX = sys.argv[1] if len(sys.argv) > 1 \
    else r"C:\Users\anasn\Downloads\CSS_Pages_Report_v12.xlsx"

wb = openpyxl.load_workbook(XLSX, read_only=True)
levels = {}                      # normalized url -> level
for ws in wb.worksheets:
    rows = ws.iter_rows(values_only=True)
    try:
        headers = [str(h or "").strip().lower() for h in next(rows)]
    except StopIteration:
        continue
    try:
        u_i = next(i for i, h in enumerate(headers)
                   if "url" in h and "capitalized" not in h)
    except StopIteration:
        continue
    pt_i = headers.index("page type") if "page type" in headers else None
    for row in rows:
        if u_i >= len(row) or not row[u_i]:
            continue
        u = str(row[u_i]).strip().rstrip("/")
        if not u.startswith("http"):
            continue
        if ws.title.strip().upper() == "PTP":
            levels[u] = "PTP"          # the whole sheet is part type pages
        elif pt_i is not None and pt_i < len(row) and row[pt_i]:
            levels[u] = str(row[pt_i]).strip().upper()
wb.close()

con = db.connect()
db.init_db()
con = db.connect()
hit = miss = 0
for u, lvl in levels.items():
    cur = con.execute("UPDATE urls SET level=? WHERE rtrim(url,'/')=?", (lvl, u))
    if cur.rowcount:
        hit += 1
    else:
        miss += 1
con.commit()

print(f"levels in the sheet : {len(levels)}")
print(f"applied to the list : {hit}")
print(f"no matching url     : {miss}  (redirected/removed rows)")
print("\nlevels now on the list:")
for r in con.execute("SELECT ptype, level, COUNT(*) n FROM urls"
                     " GROUP BY ptype, level ORDER BY ptype, n DESC"):
    print(f"   {r['ptype']:<18} {r['level'] or '(none)':<8} {r['n']:>5}")
con.close()
