r"""BhashaBuzzer ops — health watchdog + backend auto-restart (stdlib only).

Loop (default every 20 s):
  1. GET http://127.0.0.1:8765/healthz (timeout 5 s).
  2. On failure: (re)start ``uvicorn backend.main:app`` on the target port as a
     DETACHED background process whose cwd is the repo root derived from THIS
     file's location (scripts/..). A wedged previous instance squatting the
     port is terminated first — but ONLY if its image name is python/pythonw
     (never any other process). Then wait 6 s, re-check healthz, log outcome.
  3. Verify the trycloudflare (cloudflared) tunnel process is alive. If it is
     gone, log CRITICAL and keep watching — the watchdog deliberately does NOT
     restart cloudflared (a quick tunnel prints a NEW random trycloudflare URL
     at boot, so an unattended restart would silently change the public URL;
     a human must restart it and re-share the address).

Everything is appended to scripts/watchdog.log with ISO timestamps (mirrored
to the console; under pythonw the console part is skipped automatically).
The watchdog itself never reads .env — the uvicorn child simply inherits
this process's environment.

HOW TO RUN IN THE BACKGROUND (this host = Windows)
==================================================
  Preferred — no console window, survives the shell that launched it:

      pythonw scripts\watchdog.py

  Wrapper (does the above for you):

      scripts\run_prod.bat

  git-bash, detached:

      python scripts/watchdog.py >/dev/null 2>&1 &

STOPPING IT
===========
  The first log line records the watchdog's own PID:

      2026-10-14T21:00:00+05:30 INFO watchdog up pid=4242 url=...

  then:   taskkill /F /PID 4242
  (or:    wmic process where "commandline like '%watchdog.py%'" get processid)

SAFE TEST MODES (monitor only — no restarts, no kills, port 8765 untouched)
==========================================================================
  python scripts/watchdog.py --once                      # single cycle
  python scripts/watchdog.py --dry-run --port 8765 --interval 5 --max-cycles 3

RESTART DRILL — real restart on a scratch port, prod port 8765 untouched
=======================================================================
  python scripts/watchdog.py --port 8799 --interval 4 --max-cycles 3
  # 8799 is dead -> watchdog detects it, spawns a DETACHED scratch uvicorn of
  # the SAME app (backend.main:app) from THIS worktree, waits 6 s, sees it
  # healthy, then exits. Kill the scratch server afterwards using the
  # "spawned detached uvicorn pid=NNNN" line in watchdog.log.
"""
from __future__ import annotations

import argparse
import datetime
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent                 # repo root from THIS file's location
LOG_FILE = SCRIPTS_DIR / "watchdog.log"        # spec: logs to scripts/watchdog.log
UVICORN_LOG = SCRIPTS_DIR / "uvicorn_last_start.log"  # child stdout/stderr trail
IS_WINDOWS = os.name == "nt"

TUNNEL_MSG = (
    "cloudflared/trycloudflare tunnel is NOT RUNNING — the public URL is DOWN. "
    "The watchdog will NOT restart it (a quick-tunnel restart prints a NEW "
    "random URL that must be re-shared by a human); restart manually: "
    "cloudflared tunnel --url http://127.0.0.1:8765"
)


def _stamp() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def log(level: str, msg: str) -> None:
    """Timestamped line -> scripts/watchdog.log (and console when one exists)."""
    line = f"{_stamp()} {level} {msg}"
    try:
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass  # never die because logging failed
    if sys.stdout is not None:  # pythonw: sys.stdout is None
        try:
            print(line, flush=True)
        except OSError:
            pass


def check_health(url: str) -> tuple[bool, str]:
    """GET the health endpoint; return (ok, human detail)."""
    try:
        with urllib.request.urlopen(url, timeout=5.0) as resp:
            body = resp.read(200).decode("utf-8", "replace").strip()
            return True, f"HTTP {resp.status} {body[:120]}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code} from {url}"
    except Exception as exc:  # URLError / timeout / ConnectionRefusedError ...
        return False, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------- process utils
def port_pids(port: int) -> list[int]:
    """PIDs holding a LISTENING socket on ``port`` (netstat-based, Windows)."""
    if not IS_WINDOWS:
        return []
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True,
                             text=True, timeout=15).stdout
    except Exception:
        return []
    pids: set[int] = set()
    for row in out.splitlines():
        parts = row.split()
        if (len(parts) >= 5 and parts[3] == "LISTENING"
                and parts[1].split(":")[-1] == str(port)):
            try:
                pids.add(int(parts[4]))
            except ValueError:
                pass
    pids.discard(os.getpid())  # never signal ourselves
    return sorted(pids)


def proc_name(pid: int) -> str:
    """Image name of a PID ('python.exe'); '' when it cannot be determined."""
    if IS_WINDOWS:
        try:
            out = subprocess.run(["tasklist", "/FO", "CSV", "/FI", f"PID eq {pid}"],
                                 capture_output=True, text=True, timeout=15).stdout
            for row in out.splitlines():
                if row.startswith('"') and not row.startswith('"Image'):
                    return row.split('","', 1)[0].strip('"')
        except Exception:
            pass
    return ""


def kill_pid(pid: int) -> None:
    if IS_WINDOWS:
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=15)
        except Exception:
            pass
    else:
        try:
            import signal
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass


def tunnel_alive() -> bool:
    """Is a cloudflared (trycloudflare tunnel) process running?"""
    if IS_WINDOWS:
        try:
            out = subprocess.run(["tasklist", "/FO", "CSV"], capture_output=True,
                                  text=True, timeout=15).stdout
            return '"cloudflared' in out.lower()
        except Exception:
            return True  # cannot tell — do not cry wolf
    try:
        out = subprocess.run(["pgrep", "-f", "cloudflared"], capture_output=True,
                             text=True, timeout=15)
        return bool(out.stdout.strip())
    except Exception:
        return True


# ------------------------------------------------------------------- restart
def restart_backend(host: str, port: int, app: str, dry: bool) -> None:
    """(Re)start uvicorn ``app`` on host:port detached, cwd = REPO_ROOT."""
    for pid in port_pids(port):
        name = proc_name(pid)
        if name.lower().startswith(("python", "pythonw")):
            if dry:
                log("DRY", f"would terminate wedged backend pid={pid} ({name}) on port {port}")
            else:
                log("WARN", f"terminating wedged backend pid={pid} ({name}) on port {port}")
                kill_pid(pid)
        else:
            log("ERROR", f"port {port} is held by pid={pid} ({name or 'unknown'}) "
                         f"which is NOT python — not killing it; restart may fail to bind")
    cmd = [sys.executable, "-m", "uvicorn", app, "--host", host, "--port", str(port)]
    if dry:
        log("DRY", f"would start detached: {' '.join(cmd)}  cwd={REPO_ROOT}")
        return
    kwargs: dict = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:
        kwargs["start_new_session"] = True
    with UVICORN_LOG.open("ab") as fh:
        fh.write(f"\n=== watchdog restart {_stamp()} ===\n".encode("utf-8"))
        proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT),
                                 stdin=subprocess.DEVNULL,
                                 stdout=fh, stderr=subprocess.STDOUT, **kwargs)
    log("INFO", f"spawned detached uvicorn pid={proc.pid} on port {port} cwd={REPO_ROOT}")


def main() -> int:
    ap = argparse.ArgumentParser(description="BhashaBuzzer health watchdog (stdlib only)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--app", default="backend.main:app")
    ap.add_argument("--health-path", default="/healthz")
    ap.add_argument("--interval", type=float, default=20.0,
                    help="seconds between checks (spec: 20)")
    ap.add_argument("--restart-wait", type=float, default=6.0,
                    help="seconds between restart and recheck (spec: 6)")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--max-cycles", type=int, default=0,
                    help="exit after N cycles (0 = run forever)")
    ap.add_argument("--dry-run", action="store_true",
                    help="monitor only — never start or kill anything")
    args = ap.parse_args()

    url = f"http://{args.host}:{args.port}{args.health_path}"
    log("INFO", f"watchdog up pid={os.getpid()} url={url} interval={args.interval}s "
                f"restart_wait={args.restart_wait}s dry_run={args.dry_run} "
                f"repo_root={REPO_ROOT}")
    strikes = 0
    cycle = 0
    try:
        while True:
            cycle += 1
            ok, detail = check_health(url)
            if ok:
                strikes = 0
                log("OK", detail)
            else:
                strikes += 1
                log("WARN", f"health check FAILED (strike {strikes}): {detail}")
                restart_backend(args.host, args.port, args.app, args.dry_run)
                if args.dry_run:
                    log("DRY", f"would wait {args.restart_wait}s and recheck {url}")
                else:
                    time.sleep(args.restart_wait)
                    ok2, detail2 = check_health(url)
                    log("OK" if ok2 else "ERROR", f"post-restart recheck: {detail2}")
            if tunnel_alive():
                log("OK", "cloudflared tunnel process alive")
            else:
                log("CRITICAL", TUNNEL_MSG)
            if args.once:
                log("INFO", "--once given; exiting")
                return 0
            if args.max_cycles and cycle >= args.max_cycles:
                log("INFO", f"--max-cycles {args.max_cycles} reached; exiting")
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log("INFO", "interrupted; exiting")
        return 0


if __name__ == "__main__":
    sys.exit(main())
