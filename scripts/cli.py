import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "iradar"))
"""Command-line helper (optional - everything can also be done in the web UI).

  python cli.py add <url> [<url> ...]     add URLs
  python cli.py add-file <path>           add every URL found in a txt/csv file
  python cli.py list                      list tracked URLs
  python cli.py run                       run a check right now (blocking)
"""
import re
import sys

import db
import runner

URL_RE = re.compile(r"https?://[^\s\"',;<>]+")


def main():
    db.init_db()
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]
    if cmd == "add":
        added, skipped = db.add_urls(sys.argv[2:])
        print(f"Added {added}, skipped {skipped} duplicates.")
    elif cmd == "add-file":
        text = open(sys.argv[2], encoding="utf-8", errors="replace").read()
        urls = list(dict.fromkeys(URL_RE.findall(text)))
        added, skipped = db.add_urls(urls)
        print(f"Found {len(urls)} URLs: added {added}, skipped {skipped} duplicates.")
    elif cmd == "list":
        con = db.connect()
        for r in con.execute("SELECT * FROM urls ORDER BY id"):
            print(f"[{r['id']:>4}] {'ON ' if r['active'] else 'OFF'} "
                  f"{r['last_status'] or 'new':<10} {r['url']}")
        con.close()
    elif cmd == "run":
        print("Running check...")
        summary = runner.run_check(kind="cli")
        if summary is None:
            print("A check is already running.")
        else:
            print(f"Done: {summary['ok']} ok, {summary['changed']} changed, "
                  f"{summary['errors']} errors out of {summary['total']}.")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
