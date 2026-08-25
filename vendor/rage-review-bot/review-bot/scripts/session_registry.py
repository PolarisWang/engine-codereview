# -*- coding: utf-8 -*-
"""Registry of live review-bot parent sessions (`cfg/sessions.json`).

Why this exists: the daily 08:00 restart kills the previous session by PID,
and `cfg/session.pid` — written only by `launch_review_bot.ps1` — records the
*scheduled* session and nothing else. Every hand-started session (how the bot
comes back after any incident recovery) was therefore invisible to the stop and
survived every subsequent restart: sessions `5ce29e73` (2026-08-04 02:01, alive
through three restarts) and `096edf7d` (2026-08-06 15:04) both did. Two live
parent sessions means two Monitor claimants racing over `monitor.pid` and a
dead-weight session holding context nobody reads. See DESIGN §1.1.5.

The fix is to register at *start* rather than at *launch*: `resolve_start.py`
runs on every `/review-bot start`, whatever launched it, so the PID it records
covers the scheduled and the manual path alike.

Registered PID = the nearest `claude.exe` ancestor of this python process
(claude → shell → python), resolved via `subprocess_util.find_ancestor_pid`.

The file is a list, not a scalar: overwriting one slot is exactly the bug
above — a second start would erase the first session's only record and orphan
it permanently.
"""
import json
import os
import time
from pathlib import Path

import subprocess_util

FILENAME = "sessions.json"
_IMAGE = "claude"


def _path(cfg_dir):
    return Path(cfg_dir) / FILENAME


def load(cfg_dir):
    """Return the recorded entries. Unreadable/corrupt file → empty list.

    `utf-8-sig`, not `utf-8`: `stop_review_bot.ps1` rewrites this file with
    `Set-Content -Encoding UTF8`, and PowerShell 5.1 emits a BOM there. Plain
    `utf-8` would leave the BOM in the first token and every read after a stop
    would silently return "no sessions recorded".
    """
    try:
        with open(_path(cfg_dir), "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    entries = data.get("sessions") if isinstance(data, dict) else data
    return [e for e in (entries or []) if isinstance(e, dict) and e.get("pid")]


def _write(cfg_dir, entries):
    path = _path(cfg_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    payload = {"sessions": entries, "updated_at": _now()}
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def prune(cfg_dir):
    """Drop entries whose process is gone. Returns the surviving entries.

    Only liveness is checked here — the image guard lives at the kill site
    (`stop_review_bot.ps1`), which must re-verify anyway: a PID recorded days
    ago can belong to an unrelated process by the time a stop reads it.
    """
    live = [e for e in load(cfg_dir)
            if subprocess_util.is_process_alive(e.get("pid"))]
    _write(cfg_dir, live)
    return live


def register(cfg_dir, pid=None, source="start"):
    """Record this session's parent `claude.exe` PID. Returns the entry or None.

    Best-effort by contract: a start must never fail because the ancestry walk
    came up empty (script run standalone, snapshot refused). The caller reports
    the miss so it is visible, and the legacy `session.pid` path still covers
    the scheduled session in that case.
    """
    if pid is None:
        pid = subprocess_util.find_ancestor_pid(_IMAGE)
    if not pid:
        return None
    pid = int(pid)
    entries = [e for e in prune(cfg_dir) if int(e.get("pid", 0)) != pid]
    entry = {"pid": pid, "registered_at": _now(), "source": source}
    entries.append(entry)
    _write(cfg_dir, entries)
    return entry
