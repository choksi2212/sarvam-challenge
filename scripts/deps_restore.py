r"""BhashaBuzzer ops — instant dependency recovery (stdlib only) + post-mortem.

One command, run from anywhere:

    python scripts/deps_restore.py            # verify + repair + re-verify
    python scripts/deps_restore.py --force    # uninstall + reinstall all five

Installs — ALWAYS with this interpreter's own pip (``sys.executable -m pip``,
never a bare ``pip`` that may target a different Python):

    fastapi  uvicorn[standard]  httpx  websockets  pydantic

and verifies each import afterwards. Exit code 0 == all five import cleanly in
the interpreter that ran this script; 1 = something is still broken (read the
output above).

================================================================================
POST-MORTEM — the "vanishing packages" of 2026-10-14 (why this script exists)
================================================================================
Twice that night the session's Python "lost" installed packages (first
fastapi, later httpx / websockets / pydantic). Investigation on this host
(`where python`, `where pip`, `python -m pip -V`, site-packages mtimes, and
the hermes tools/ dir mtimes) found the cause — three overlapping mechanisms,
no single malicious "wipe":

1. FIVE python.exe are on PATH here, and ``pip`` and ``python`` resolve to
   DIFFERENT interpreters:

       python -> C:\Users\HP\AppData\Local\hermes\tools\python-3.14.7+...\python.exe
       pip    -> C:\Program Files\Python311\Scripts\pip.exe          (Python 3.11!)

   plus Python 3.13, the WindowsApps store stub, and a Hermes env venv that
   appears on PATH 3x (pyvenv.cfg: include-system-site-packages = false).
   A bare ``pip install ...`` lands packages in Python 3.11 while ``python``
   imports from the Hermes 3.14 tools build — so packages appear/vanish
   depending on which command an agent used. Proof of the misdirected
   installs: Python 3.11's site-packages holds strays httpx 0.13.3 +
   pydantic 1.10.6, and Python 3.13 has its own httpx 0.28.1 / pydantic 2.11.5.

2. The Hermes tools Python is a MANAGED, DISPOSABLE toolchain. The whole
   distribution was re-extracted at 2026-10-14 16:50 (tools/ dir, facts.json
   and pip-26.2.1 all carry that mtime; site-packages held ONLY pip +
   the build's frozen README.txt afterwards). A toolchain re-sync REPLACES
   site-packages with the shipped set, silently dropping everything agents
   had installed into the shared tools Python.

3. Interpreter FLIPPING made it look intermittent: any session that activated
   the Hermes env venv (or got a shifted PATH) resolved ``python`` to an
   interpreter whose site-packages never had the packages — nothing was wiped
   at all, the interpreter just changed under us.

The entire current app stack (fastapi 0.143.1, starlette, pydantic 2.14.1,
uvicorn 0.54.0, httptools, watchfiles, websockets 17.2, httpx 0.28.1, ...)
carries install mtimes of 18:54–18:55 that same evening — that was the manual
recovery; this script makes it a one-liner for next time.

MITIGATION (encoded here): pin every install to the interpreter that will
import the packages — ``sys.executable -m pip`` — verify imports immediately,
and never trust a bare ``pip`` on this host.
================================================================================
"""
from __future__ import annotations

import argparse
import importlib
import shutil
import subprocess
import sys
import time

PACKAGES = ["fastapi", "uvicorn[standard]", "httpx", "websockets", "pydantic"]
# pip distribution name -> module imported for verification
IMPORT_NAMES = {"uvicorn[standard]": "uvicorn"}


def log(msg: str) -> None:
    print(f"[deps_restore {time.strftime('%Y-%m-%dT%H:%M:%S')}] {msg}", flush=True)


def import_ok(pkg: str) -> tuple[bool, str]:
    """Try importing the package's module; return (ok, human detail)."""
    mod = IMPORT_NAMES.get(pkg, pkg.split("[")[0])
    try:
        m = importlib.import_module(mod)
        ver = getattr(m, "__version__", None) or getattr(m, "VERSION", "?")
        return True, f"{mod} {ver}"
    except Exception as exc:  # noqa: BLE001 — report, never crash
        return False, f"import {mod} failed: {type(exc).__name__}: {exc}"


def run(cmd: list[str]) -> int:
    log("$ " + " ".join(cmd))
    try:
        return subprocess.run(cmd).returncode
    except OSError as exc:
        log(f"  spawn failed: {exc}")
        return 1


def pip(args: list[str]) -> int:
    """Pinned pip: ALWAYS the interpreter running this script."""
    return run([sys.executable, "-m", "pip", *args])


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Verify/repair BhashaBuzzer python deps (stdlib only).")
    ap.add_argument("--force", action="store_true",
                    help="uninstall + reinstall even if the import already works")
    args = ap.parse_args()

    log(f"interpreter : {sys.executable}")
    log(f"python      : {sys.version.split()[0]}  prefix: {sys.prefix}")
    r = subprocess.run([sys.executable, "-m", "pip", "-V"],
                       capture_output=True, text=True)
    log(f"pip         : {(r.stdout or r.stderr).strip()}")
    bare = shutil.which("pip")
    if bare:
        log(f"note        : PATH's bare `pip` is {bare}")
        if "Python311" in bare or "Python313" in bare:
            log("WARNING     : bare `pip` on this PATH targets a DIFFERENT "
                "python (3.11/3.13) than this interpreter — always use `python -m pip`.")

    broken: list[str] = []
    for pkg in PACKAGES:
        ok, detail = import_ok(pkg)
        if ok and not args.force:
            log(f"OK          {pkg} already importable ({detail})")
            continue
        log(f"REPAIR      {pkg} ({detail if not ok else 'forced reinstall'})")
        pip(["uninstall", "-y", pkg.split("[")[0]])   # rc ignored: may not exist
        rc = pip(["install", "--no-cache-dir", "--", pkg])
        ok2, detail2 = import_ok(pkg)
        if rc == 0 and ok2:
            log(f"RESTORED    {pkg} -> {detail2}")
        else:
            log(f"STILL-BROKEN {pkg} (pip rc={rc}, {detail2})")
            broken.append(pkg)

    log("-" * 60)
    if broken:
        log(f"FAILED: {', '.join(broken)} — inspect output above; retry with "
            "--force, or check the interpreter/PATH.")
        return 1
    log(f"ALL DEPENDENCIES VERIFIED ({len(PACKAGES)} packages import cleanly "
        f"in {sys.executable})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
