import sys, re, json, os
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0,'.'); import db
from openpyxl import load_workbook

# the workbook to read. Overridable so the app can re-apply it automatically
# without this path being buried in a script somebody has to remember to run.
PATH=os.environ.get("CSS_REPORT_XLSX") or r"C:\Users\anasn\Downloads\CSS_Pages_Report_v15_SW.xlsx"

def norm(u):
    u=(u or "").split("#")[0].split("?")[0].rstrip("/").lower()
    u=re.sub(r"^https?://","",u); u=re.sub(r"^www\.","",u)
    return re.sub(r"/(de|ja|cn|tw|kr|jp)(?=/)","",u)

db.init_db()   # ensure the bl/pl/cm/owner/meta_flag columns exist
con=db.connect()

# our pages: normalized url/canonical -> id
byurl={}
for r in con.execute("SELECT id,url,canonical_url,redirect_from FROM urls"):
    byurl[norm(r["url"])]=r["id"]
    for alt in ("canonical_url","redirect_from"):
        # redirect_from matters: the report lists many pages under a legacy
        # address that 301s to one we already track. Without this the importer
        # re-adds that address every run, the next run deletes it again, and the
        # ownership on it never reaches the real page.
        if r[alt]: byurl.setdefault(norm(r[alt]), r["id"])

wb=load_workbook(PATH, read_only=True, data_only=True)
def rows(title):
    ws=wb[title]; hdr=[str(c.value).strip() if c.value else '' for c in next(ws.iter_rows(min_row=1,max_row=1))]
    for r in ws.iter_rows(min_row=2, values_only=True):
        yield {hdr[i]: (r[i] if i<len(r) else None) for i in range(len(hdr))}, hdr

# ---------- 1. collect all sheet URLs, add the missing ----------
def urlof(row, hdr):
    for k in ("URL","Column1"):
        if k in row and row[k] and "http" in str(row[k]): return str(row[k]).strip()
    return None
missing=[]
for title in wb.sheetnames:
    for row,hdr in rows(title):
        u=urlof(row,hdr)
        if u and norm(u) not in byurl: missing.append(u)
missing=list(dict.fromkeys(missing))
added,skipped=db.add_urls(missing)
print(f"[urls] {len(missing)} missing across sheets -> added {added}, skipped {skipped}")
# refresh map with the new ids
con.close(); con=db.connect()
byurl={}
for r in con.execute("SELECT id,url,canonical_url FROM urls"):
    byurl[norm(r["url"])]=r["id"]
    if r["canonical_url"]: byurl.setdefault(norm(r["canonical_url"]), r["id"])

def setf(uid, **kw):
    cols=",".join(f"{k}=?" for k in kw)
    con.execute(f"UPDATE urls SET {cols} WHERE id=?", (*kw.values(), uid))

# ---------- 2. Product: BL, PL, CM, Owner ----------
prod_by_url={}   # normalized product url -> (bl,pl) for PTP hierarchy lookup
n=0
for row,hdr in rows("Product"):
    u=row.get("URL");  uid=byurl.get(norm(u)) if u else None
    bl=(row.get("BL") or "").strip(); pl=str(row.get("PL") or "").strip()
    cm=(row.get("CM") or "").strip(); owner=(row.get("Owner (PL) — PMG") or row.get("Owner (PL) - PMG") or "").strip()
    if u: prod_by_url[norm(u)]=(bl,pl)
    if uid:
        setf(uid, bl=bl, pl=pl, cm=cm, owner=owner); n+=1
print(f"[Product] set BL/PL/CM/Owner on {n} pages")

# ---------- 3. Promo: Content Owner -> CM ----------
n=0
for row,hdr in rows("Promo"):
    u=row.get("Column1"); uid=byurl.get(norm(u)) if u else None
    cm=(row.get("Content Owner") or "").strip()
    if uid and cm: setf(uid, cm=cm); n+=1
print(f"[Promo] set CM (content owner) on {n} pages")

# ---------- 4. Application: CM, Owner + page-type check ----------
n=flags=0
for row,hdr in rows("Application"):
    u=row.get("URL"); uid=byurl.get(norm(u)) if u else None
    cm=(row.get("CM") or "").strip(); owner=(row.get("Page Owner") or "").strip()
    ptype_sheet=(row.get("Page Type") or "").strip()
    if not uid: continue
    ours=con.execute("SELECT level FROM urls WHERE id=?", (uid,)).fetchone()["level"] or ""
    flag=""
    if ptype_sheet and ours and ptype_sheet.upper()!=ours.upper():
        flag=f"page-type: sheet says {ptype_sheet}, we have {ours}"; flags+=1
    setf(uid, cm=cm, owner=owner, meta_flag=flag); n+=1
print(f"[Application] set CM/Owner on {n} pages, {flags} page-type mismatches flagged")

# ---------- 5. PTP/EB: PL from sheet, BL from product hierarchy, flag misalign ----------
def pl_aligned(part_pl, family_pl):
    """A family usually covers SEVERAL product lines and the sheet writes that
    as a range - '80/81', '80, 81', '80-81'. A part sitting at PL 80 under it is
    aligned, not misaligned; a plain string compare called all 200 of those a
    problem. So the family PL is read as the SET of lines it spans."""
    lines={t for t in re.split(r"[^0-9A-Za-z]+", str(family_pl)) if t}
    return not lines or str(part_pl).strip() in lines

def family_bl_pl(uid):
    """Walk the part's breadcrumb to its deepest product URL, match a Product row."""
    r=con.execute("SELECT breadcrumb FROM urls WHERE id=?", (uid,)).fetchone()
    try: crumbs=json.loads(r["breadcrumb"] or "[]")
    except: crumbs=[]
    prod_urls=[norm(c.get("url","")) for c in crumbs if "/products/" in (c.get("url") or "")]
    for pu in reversed(prod_urls):           # deepest first
        # exact, then any product row whose url is a prefix
        if pu in prod_by_url: return prod_by_url[pu], pu
        for purl,(bl,pl) in prod_by_url.items():
            if pu==purl or pu.startswith(purl+"/") or purl.startswith(pu+"/"):
                return (bl,pl), purl
    return None, None
n=blset=flags=0
for row,hdr in rows("PTP"):
    u=row.get("URL"); uid=byurl.get(norm(u)) if u else None
    pl_sheet=str(row.get("PL") or "").strip()
    if not uid: continue
    fam,famurl=family_bl_pl(uid)
    bl = fam[0] if fam else ""
    flag=""
    if fam and pl_sheet and fam[1] and not pl_aligned(pl_sheet, fam[1]):
        flag=f"PL {pl_sheet} != family {famurl.rsplit('/',1)[-1]} PL {fam[1]}"; flags+=1
    if bl: blset+=1
    setf(uid, pl=pl_sheet, bl=bl, meta_flag=flag); n+=1
print(f"[PTP/EB] set PL on {n} pages, BL from hierarchy on {blset}, {flags} PL misalignments flagged")

con.commit(); con.close(); wb.close()
print("\nDONE.")
