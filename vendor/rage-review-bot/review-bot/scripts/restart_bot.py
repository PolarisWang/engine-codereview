# -*- coding: utf-8 -*-
"""Partial restart of the review-bot's long-lived processes.

`/review-bot restart [components]` — kill and relaunch only what needs it,
without a full stop + `/review-bot start` (which recycles the parent session
and posts a fresh greeting to the Lark group).

Why partial matters, per component:

* **daemon** (`pythonw poll_dispatch.py --watch`) — imports nearly every script
  in `scripts/` once at startup, so any Python edit is invisible to it until it
  restarts. This is the one you almost always want.
* **listener** (`lark-cli event +subscribe`) — a Node process. No Python edit of
  ours can make it stale, and respawning it burns the app-wide Feishu
  long-connection quota (a respawn loop once locked the app out entirely with
  `1000040350`). So it is never restarted unless named explicitly.
* **monitor** (`monitor_dispatch.py`) — runs under the Claude Code **Monitor
  tool**, not as a free-standing OS process. This script can kill it, but only
  the session can re-create it, so `monitor` reports `needs_session_action`
  and SKILL.md carries the call to re-issue.

Default is `stale`: compare each component's process start time against the
mtime of the code it loaded, and restart only what is running old code
(DESIGN §1.1.6).

Usage:
    python restart_bot.py                      # stale components only
    python restart_bot.py --components daemon
    python restart_bot.py --components daemon,monitor
    python restart_bot.py --components all     # daemon + listener + monitor
    python restart_bot.py --dry-run            # report, change nothing
"""
import argparse
import glob
import json
import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import subprocess_util

SKILL_DIR = SCRIPT_DIR.parent
CFG_DIR = SKILL_DIR / "cfg"

# How long to wait for a killed process to actually disappear. Relaunching on
# top of a survivor would leave two daemons racing the same topics.
_DEATH_TIMEOUT_S = 8.0
_DEATH_POLL_S = 0.2
# How long to wait for the relaunched process to publish its new pid.
_LAUNCH_TIMEOUT_S = 20.0

COMPONENTS = ("daemon", "listener", "monitor")
# Marker that tells a still-running daemon not to respawn the listener
# while we are replacing it (DESIGN §1.1.6).
LISTENER_GUARD = CFG_DIR / subprocess_util.LISTENER_RESTART_GUARD_NAME


def _pid_file(component):
    return CFG_DIR / ("%s.pid" % component)


def _read_pid(component):
    try:
        raw = _pid_file(component).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None


def _newest_mtime(paths):
    newest = 0.0
    for path in paths:
        try:
            newest = max(newest, os.path.getmtime(path))
        except OSError:
            continue
    return newest


def _watched_code(component):
    """Files whose edit makes this component stale.

    The daemon's import closure is most of `scripts/`, so it watches the whole
    directory: over-restarting a cheap local process beats missing an edit and
    then debugging a fix that is on disk but not running. The listener watches
    nothing — it runs none of our Python.
    """
    if component == "daemon":
        return (glob.glob(str(SCRIPT_DIR / "*.py"))
                + glob.glob(str(SCRIPT_DIR / "templates" / "*.py")))
    if component == "monitor":
        return [str(SCRIPT_DIR / "monitor_dispatch.py"),
                str(SCRIPT_DIR / "subprocess_util.py")]
    return []


def inspect(component):
    """Report one component's pid / liveness / staleness, touching nothing."""
    pid = _read_pid(component)
    alive = subprocess_util.is_process_alive(pid) if pid else False
    started = subprocess_util.process_start_time(pid) if alive else None
    newest = _newest_mtime(_watched_code(component))
    # An unknown start time on a live process counts as NOT stale: guessing
    # "restart" there would bounce a healthy daemon on every invocation.
    stale = bool(alive and started and newest and newest > started)
    return {"component": component, "pid": pid, "alive": alive,
            "started_at": started, "newest_code_mtime": newest or None,
            "stale": stale,
            "reason": ("dead" if (pid and not alive) else
                       "no_pid_file" if not pid else
                       "stale_code" if stale else "current")}


def _await_death(pid):
    deadline = time.time() + _DEATH_TIMEOUT_S
    while time.time() < deadline:
        if not subprocess_util.is_process_alive(pid):
            return True
        time.sleep(_DEATH_POLL_S)
    return not subprocess_util.is_process_alive(pid)


def _launch_daemon():
    """Detached, hidden, outliving this process — the path `start` uses."""
    vbs = SCRIPT_DIR / "run_poll.vbs"
    if not vbs.exists():
        return False, "run_poll.vbs missing at %s" % vbs
    try:
        subprocess_util.detached_popen(
            ["wscript", "//nologo", str(vbs)], cwd=str(SCRIPT_DIR))
    except OSError as exc:
        return False, "wscript launch failed: %s" % exc
    return True, None


def _launch_listener():
    starter = SCRIPT_DIR / "start_listener.py"
    if not starter.exists():
        return False, "start_listener.py missing"
    try:
        proc = subprocess_util.hidden_run(
            [sys.executable, str(starter)], capture_output=True, text=True,
            timeout=60, encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return False, "start_listener failed: %s" % str(exc)[:200]
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()[:300]
    return True, None


def _await_pid_change(component, old_pid):
    """Wait for the relaunched process to publish a NEW live pid.

    Confirming from the pid file rather than from "we called the launcher" is
    the difference between a check and a hope: a `pythonw` daemon that dies on
    an import error exits silently, and reporting that as success is how a fix
    sits on disk for a day while the operator believes it shipped.
    """
    deadline = time.time() + _LAUNCH_TIMEOUT_S
    while time.time() < deadline:
        pid = _read_pid(component)
        if pid and pid != old_pid and subprocess_util.is_process_alive(pid):
            return pid
        time.sleep(0.4)
    return None


def _live_listeners():
    """PIDs of every running listener, by command-line signature.

    Reuses `stop_bot.SIGNATURES` rather than re-declaring the pattern —
    a second copy of a matcher is exactly the drift that put the ticket
    regex out of sync with the router.

    Returns None when the process scan itself fails, which callers must treat
    as "unknown", never as "none running": acting on a failed scan as though
    nothing were alive is what orphaned listeners in the first place.
    """
    try:
        import stop_bot
        signature = stop_bot.SIGNATURES["listener"]
    except Exception:  # noqa: BLE001 — matcher unavailable, decline to guess
        return None
    try:
        return [pid for pid, cmd in subprocess_util.iter_processes()
                if cmd and signature.search(cmd)]
    except OSError:
        return None


def _reap_competing_listeners(keep_pid):
    """Kill every listener except `keep_pid`. Returns the reaped pids.

    The guard stops the daemon from *deciding* to respawn, but it cannot undo a
    decision already made: `_health_check_listener` reads the guard and only
    then calls `_restart_listener`, so a daemon that passed that check
    microseconds before we wrote the marker still spawns. That window is not
    hypothetical here — `--components listener` is run precisely when the
    listener is wedged, which is exactly when the daemon is also finding it
    unhealthy, so both sides are primed to act at the same moment.

    Prevention plus reconciliation, therefore: after our listener is confirmed
    up, anything else answering to the listener signature is a loser of that
    race and gets killed. One live listener is the invariant that matters —
    the app-wide Feishu long-connection quota is what a duplicate spends
    (a respawn loop once locked the app out entirely with 1000040350).
    """
    live = _live_listeners()
    if live is None:
        return None
    reaped = []
    for pid in live:
        if pid == keep_pid:
            continue
        subprocess_util.kill_process(pid)
        reaped.append(pid)
    return reaped


def _kill(component, result):
    """Kill one component's process. Returns True if it is gone afterwards."""
    if not result["alive"]:
        return True
    subprocess_util.kill_process(result["pid"])
    if _await_death(result["pid"]):
        return True
    result.update(ok=False, action="kill_failed",
                  error="pid %s still alive after %.0fs"
                        % (result["pid"], _DEATH_TIMEOUT_S))
    return False


def _launch(component, result):
    """Relaunch one component and confirm it from the pid file."""
    if component == "monitor":
        # Killed — but the Monitor tool task belongs to the Claude session,
        # so only the session can bring it back. Reporting a restart we cannot
        # perform would leave the dispatch loop dead behind a success message.
        monitor_py = str(SCRIPT_DIR / "monitor_dispatch.py").replace("\\", "/")
        result.update(action="killed_needs_session_action",
                      needs_session_action=True,
                      hint=("re-issue the Monitor tool with "
                            "command=python \"%s\"" % monitor_py))
        return

    launcher = _launch_daemon if component == "daemon" else _launch_listener
    ok, err = launcher()
    if not ok:
        result.update(ok=False, action="launch_failed", error=err)
        return

    new_pid = _await_pid_change(component, result["pid"])
    if new_pid is None:
        result.update(ok=False, action="launch_unconfirmed",
                      error=("no new %s.pid within %.0fs — the process likely "
                             "died at startup; rerun it in a visible console "
                             "to see the error" % (component, _LAUNCH_TIMEOUT_S)))
        return
    result.update(action="restarted", new_pid=new_pid)

    if component == "listener":
        # Close the guard's TOCTOU window (see `_reap_competing_listeners`).
        reaped = _reap_competing_listeners(new_pid)
        if reaped is None:
            # A failed scan is reported, not silently treated as "all clear".
            result["listener_reap"] = "scan_failed"
        elif reaped:
            result["listener_reap"] = sorted(reaped)


def restart_components(components, dry_run=False):
    """Restart several components: kill them ALL first, then relaunch.

    The two phases are not cosmetic. Restarting per-component (kill+launch,
    then next component) reintroduces the very race the daemon-first ordering
    exists to prevent: a relaunched daemon runs `_health_check_listener` on its
    first cycle, sees `listener.pid` pointing at the listener we just killed,
    and calls `_restart_listener` itself. `start_listener` has no
    singleton guard — it kills whatever pid it reads and spawns regardless — so
    our launch and the daemon's collide and one of the two listeners ends up
    orphaned, alive but absent from the pid file and therefore unreachable by
    any later stop. That is the leak that once reached 63 listeners and burned
    the app-wide Feishu long-connection quota.

    So: kill everything requested (daemon FIRST, so its health-check is gone
    before the listener dies), then launch (daemon LAST, so it only starts
    supervising once the listener it would supervise is already up).
    See DESIGN §1.1.6.
    """
    results = {c: dict(inspect(c), action="none", ok=True, error=None,
                       new_pid=None)
               for c in components}

    if dry_run:
        for result in results.values():
            result["action"] = "would_restart"
        return [results[c] for c in components]

    # Hold the guard whenever the listener is in scope. Phase ordering alone
    # only protects the case where the daemon is ALSO being restarted (it is
    # dead across the window). `--components listener` — the documented move
    # for a wedged listener — leaves the daemon running the whole time, so
    # without the guard its health check respawns the listener between our
    # kill and our launch and one of the two ends up orphaned (§1.1.6).
    guarding = "listener" in results
    if guarding:
        subprocess_util.write_guard(LISTENER_GUARD)
    try:
        # Phase 1 — kill, daemon first.
        for component in [c for c in COMPONENTS if c in results]:
            _kill(component, results[component])

        # Phase 2 — launch, daemon last. A component whose kill failed is
        # skipped: launching on top of a survivor is how you get two daemons
        # on one topic.
        launch_order = [c for c in ("listener", "monitor", "daemon")
                        if c in results]
        for component in launch_order:
            result = results[component]
            if result["ok"]:
                _launch(component, result)
    finally:
        # Release even on failure: a held guard means nobody supervises the
        # listener. The TTL would expire it anyway, but not for 90 s.
        if guarding:
            subprocess_util.clear_guard(LISTENER_GUARD)

    return [results[c] for c in components]


def restart(component, dry_run=False):
    """Kill + relaunch a single component. Returns its result dict."""
    return restart_components([component], dry_run=dry_run)[0]


def resolve_components(spec):
    """Turn the CLI argument into an ordered component list."""
    spec = (spec or "stale").strip().lower()
    if spec == "all":
        return list(COMPONENTS), "all"
    if spec == "stale":
        return [c for c in COMPONENTS if inspect(c)["stale"]], "stale"

    wanted, unknown = [], []
    for token in spec.replace(" ", ",").split(","):
        token = token.strip()
        if not token:
            continue
        if token in COMPONENTS:
            if token not in wanted:
                wanted.append(token)
        else:
            unknown.append(token)
    if unknown:
        raise ValueError("unknown component(s): %s (expected %s | all | stale)"
                         % (", ".join(unknown), " | ".join(COMPONENTS)))
    # Canonical order regardless of how they were typed; `restart_components`
    # owns the kill/launch sequencing that the ordering exists for.
    return [c for c in COMPONENTS if c in wanted], "explicit"


def main():
    parser = argparse.ArgumentParser(
        description="Partially restart review-bot processes")
    parser.add_argument("--components", default="stale",
                        help="daemon | listener | monitor | all | stale "
                             "(comma-separated; default: stale)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would restart, change nothing")
    args = parser.parse_args()

    try:
        components, mode = resolve_components(args.components)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2

    results = restart_components(components, dry_run=args.dry_run)
    failed = [r for r in results if not r["ok"]]
    out = {
        "mode": mode,
        "dry_run": args.dry_run,
        "restarted": [r["component"] for r in results
                      if r["action"] == "restarted"],
        "needs_session_action": [r["component"] for r in results
                                 if r.get("needs_session_action")],
        "failed": [{"component": r["component"], "error": r["error"]}
                   for r in failed],
        "results": results,
        "untouched": [inspect(c) for c in COMPONENTS if c not in components],
    }
    if mode == "stale" and not components:
        out["summary"] = ("Nothing stale — every component is already running "
                          "current code.")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
