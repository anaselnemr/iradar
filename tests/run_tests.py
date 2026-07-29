#!/usr/bin/env python
"""Zero-dependency test runner for InfineonWatch.

No pytest, no install: just `python tests/run_tests.py` (or `.venv/Scripts/
python.exe tests/run_tests.py`). It imports every tests/test_*.py, runs every
function named test_*, and reports PASS/FAIL with a one-line reason.

Why this exists: for months a change to the extractor or the diff logic could
only be checked by a live, hours-long, network-dependent run - and several
plausible-but-wrong fixes shipped because there was no fast check. These tests
run offline in seconds. Add one whenever a bug is found: it should fail before
the fix and pass after.

  python tests/run_tests.py            # all tests
  python tests/run_tests.py compare    # only files whose name contains "compare"
  KEEP_BROWSER tests need Playwright; they SKIP cleanly if it is missing.
"""
import importlib.util
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "iradar"))     # so tests can `import compare, db, app, runner`


class Skip(Exception):
    """Raise from a test to skip it (e.g. a fixture or Playwright is absent)."""


def _load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main(argv):
    filt = argv[1] if len(argv) > 1 else ""
    files = sorted(p for p in HERE.glob("test_*.py") if filt in p.name)
    if not files:
        print(f"no test files match {filt!r}")
        return 1

    npass = nfail = nskip = 0
    failures = []
    t0 = time.time()
    for f in files:
        try:
            mod = _load(f)
        except Skip as e:
            print(f"  SKIP  {f.name} (module) — {e}")
            nskip += 1
            continue
        except Exception:
            print(f"  ERROR loading {f.name}")
            traceback.print_exc()
            nfail += 1
            failures.append(f.name)
            continue
        tests = [(n, getattr(mod, n)) for n in dir(mod)
                 if n.startswith("test_") and callable(getattr(mod, n))]
        for name, fn in tests:
            label = f"{f.name}::{name}"
            try:
                fn()
                print(f"  ok    {label}")
                npass += 1
            except Skip as e:
                print(f"  SKIP  {label} — {e}")
                nskip += 1
            except AssertionError as e:
                print(f"  FAIL  {label} — {e or 'assertion failed'}")
                nfail += 1
                failures.append(label)
            except Exception as e:
                print(f"  FAIL  {label} — {type(e).__name__}: {e}")
                traceback.print_exc()
                nfail += 1
                failures.append(label)

    dt = time.time() - t0
    print(f"\n{npass} passed, {nfail} failed, {nskip} skipped  ({dt:.1f}s)")
    if failures:
        print("failed:\n  " + "\n  ".join(failures))
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
