"""Deterministic teardown of the review-bot's long-lived OS processes.

The naive stop ("read daemon.pid + listener.pid, kill those two") is NOT
reliable, and silently leaves the bot running:

- The daemon's health-check (`dispatcher._restart_listener`) respawns the
  listener under a NEW pid whenever it looks unhealthy. So the pid in
  `listener.pid` can be stale the moment you read it, and killing it leaves
  the freshly-respawned listener alive.
- A second / orphaned daemon (e.g. a prior-session `poll_dispatch.py --watch`
  that the singleton check didn't catch, or one not recorded in `daemon.pid`)
  keeps respawning the listener after you kill the recorded one.

So this script trusts BOTH the pid files AND a command-line process scan, and
the kill order is load-bearing: every pass kills the **daemon(s) first** so
nothing can respawn the listener between our listener-kill and the verify
pass, then re-scans. It loops until a scan finds zero matching processes (or
`--max-passes` is hit), then removes the pid files.

Killing the OS-level `monitor_dispatch.py` process is *enough* to retire the
Claude-Code-managed Monitor *task* too: with its script gone, the wrapper
fires a benign `Monitor script failed (exit 1)` notification and deregisters
itself. So the parent session needs no `TaskStop` (one issued afterward always
returned `No task found`); this script's monitor kill is the whole teardown.

Output: one JSON line
  {"status": "ok"|"survivors_remain", "killed": {role:[pids]},
   "survivors": [{"pid":N,"cmd":"..."}], "passes": N, "pid_files_removed": [...]}

Exit code 0 when no survivors remain, 1 otherwise.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import subprocess_util as su

SCRIPT_DIR = Path(__file__).resolve().parent
CFG_DIR = SCRIPT_DIR.parent / "cfg"

# Command-line signatures, matched case-insensitively against the full command
# line of each candidate process. Keep these SPECIFIC so unrelated python /
# node processes on the machine never match.
SIGNATURES = {
    "daemon":   re.compile(r"poll_dispatch\.py", re.I),
    "listener": re.compile(r"event\W+\+?subscribe|im\.message\.receive_v1", re.I),
    "monitor":  re.compile(r"monitor_dispatch\.py", re.I),
}

PID_FILES = {
    "daemon":   CFG_DIR / "daemon.pid",
    "listener": CFG_DIR / "listener.pid",
    "monitor":  CFG_DIR / "monitor.pid",
}

# Kill order is load-bearing: daemon first (it respawns the listener), then
# listener, then monitor. Re-scanning each pass catches a listener that the
# daemon respawned just before we killed the daemon.
KILL_ORDER = ["daemon", "listener", "monitor"]


class ScanFailed(RuntimeError):
    """The process scan itself broke — NOT the same as "nothing matched"."""


def _scan_processes():
    """Return [(pid, cmdline), ...] for candidate processes.

    Delegates to `subprocess_util.iter_processes` (Win32 EnumProcesses + PEB
    read on Windows, `ps` on POSIX). Raises `ScanFailed` when the enumeration
    breaks, because the previous behaviour — swallow the error, return [] —
    made a broken scan indistinguishable from a clean machine, so `stop`
    reported `status:"ok"` and deleted the pid files while the daemon was
    still alive. That orphaned daemon then respawned a listener every health
    check, forever, unreachable by any later stop.
    """
    try:
        return list(su.iter_processes())
    except Exception as exc:
        raise ScanFailed(str(exc)) from exc


def _match_role(cmd):
    """Return the role whose signature matches *cmd*, daemon-priority first."""
    for role in KILL_ORDER:
        if SIGNATURES[role].search(cmd):
            return role
    return None


def _collect_targets(my_pid):
    """Build {role: set(pids)} from a process scan PLUS the pid files.

    Propagates `ScanFailed` — the pid files alone are not a safe basis for
    declaring the bot down (they only ever name the most recent instance).
    """
    targets = {role: set() for role in KILL_ORDER}
    for pid, cmd in _scan_processes():
        if pid == my_pid:
            continue
        role = _match_role(cmd)
        if role:
            targets[role].add(pid)
    # Pid-file fallback — catches a process the scan somehow missed.
    for role, pid_file in PID_FILES.items():
        pid = su.read_pid_file(pid_file)
        if pid and pid != my_pid and su.is_process_alive(pid):
            targets[role].add(pid)
    return targets


def stop(max_passes=5, settle_seconds=0.8):
    my_pid = os.getpid()
    killed = {role: [] for role in KILL_ORDER}
    passes = 0

    def _scan_failed(exc):
        # Fail loud and change nothing. Deleting pid files here is what
        # permanently orphans a surviving daemon.
        return {
            "status": "scan_failed",
            "error": str(exc),
            "killed": killed,
            "survivors": [],
            "passes": passes,
            "pid_files_removed": [],
        }

    # Every scan lives inside this guard, not just a probe before the loop.
    # `_collect_targets` and the final verification scan both re-scan, and a
    # transient failure on pass 2+ used to escape as an uncaught ScanFailed —
    # straight past the structured "scan_failed" contract this function exists
    # to provide. A separate pre-loop scan is also pure waste on Windows: it is
    # a full EnumProcesses + per-process PEB read that the first
    # `_collect_targets` immediately repeats.
    try:
        for _ in range(max_passes):
            passes += 1
            targets = _collect_targets(my_pid)
            remaining = sum(len(v) for v in targets.values())
            if remaining == 0:
                break
            # Daemon(s) first so the listener can't be respawned mid-pass.
            for role in KILL_ORDER:
                for pid in sorted(targets[role]):
                    su.kill_process(pid)
                    if pid not in killed[role]:
                        killed[role].append(pid)
            # Let any in-flight respawn land, then re-scan next pass.
            time.sleep(settle_seconds)

        # Final verification scan (after the last kill settled).
        survivors = []
        for pid, cmd in _scan_processes():
            if pid == my_pid:
                continue
            if _match_role(cmd):
                survivors.append({"pid": pid, "cmd": cmd[:160]})
    except ScanFailed as exc:
        return _scan_failed(exc)

    # Remove a pid file only once its process is confirmed dead. Removing them
    # while a process survives is what makes an orphan permanent: the file is
    # the only record of that pid, so no later stop can ever find it again.
    # A stale-but-dead pid file is the lesser evil (the next `start` re-checks
    # liveness before honouring the singleton).
    removed = []
    survivor_pids = {entry["pid"] for entry in survivors}
    for pid_file in PID_FILES.values():
        recorded = su.read_pid_file(pid_file)
        if recorded in survivor_pids or (recorded and su.is_process_alive(recorded)):
            continue
        try:
            os.remove(pid_file)
            removed.append(pid_file.name)
        except OSError:
            pass

    return {
        "status": "ok" if not survivors else "survivors_remain",
        "killed": killed,
        "survivors": survivors,
        "passes": passes,
        "pid_files_removed": removed,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Deterministically stop the review-bot daemon + listener "
                    "+ monitor processes (pid files AND command-line scan).")
    parser.add_argument("--max-passes", type=int, default=5,
                        help="Max kill/re-scan passes (default 5).")
    parser.add_argument("--settle-seconds", type=float, default=0.8,
                        help="Pause between passes to let respawns land "
                             "(default 0.8).")
    args = parser.parse_args()

    result = stop(max_passes=args.max_passes, settle_seconds=args.settle_seconds)
    # Force UTF-8 so the cmd ('...') survivors strings don't trip GBK stdout.
    import io
    out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    out.write(json.dumps(result, ensure_ascii=False))
    out.write("\n")
    out.flush()
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
