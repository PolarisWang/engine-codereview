#!/usr/bin/env python
"""Review-bot v2 poll-cycle dispatcher.

This is the orchestrator for a single poll cycle. It does NOT invoke
Claude — it's pure Python. It writes the per-cycle work plan to
cfg/dispatch_plan.json; the parent Claude agent running /review-bot
poll reads that file and spawns Task calls for each work item.

Flow per cycle:
  0. Janitor sweep (terminal-file relocation, 30-day closed retention,
     stale-lock cleanup)
  1. Listener health check
  2. Reconcile (safety net — pulls recent chat history and writes
     missed events into cfg/events/)
  3. Load or rebuild open topic index
  4. Router: drain cfg/events/ into per-topic pending queues
  5. Merge tracker: check APPROVED topics for train landings
  6. Find topics with pending events (skip those locked by other cycles)
  7. Write cfg/dispatch_plan.json for the parent agent
  8. Emit a one-line JSON summary to stdout
"""

import json
import os
import random
import string
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import router
import subprocess_util
import topic_index
import topic_store
import state_machine
import merge_tracker
import mechanical_reply_handler
import reply_dispatcher
import ack_dev_reply
import ack_dev_question
import ack_new_topic
import incr_cache
import terminal_notice
import topic_reopen


# ---- Paths ----------------------------------------------------------

SKILL_DIR = SCRIPT_DIR.parent
CFG_DIR = SKILL_DIR / "cfg"
EVENTS_DIR = CFG_DIR / "events"
REPLIES_DIR = CFG_DIR / "replies"
TOPICS_DIR = CFG_DIR / "topics"
CLOSED_DIR = TOPICS_DIR / "closed"
INDEX_PATH = CFG_DIR / "open_topic_index.json"
DISPATCH_PLAN = CFG_DIR / "dispatch_plan.json"
SUPERSEDE_PENDING = CFG_DIR / "supersede_pending.json"
DISPATCHER_STATE = CFG_DIR / "dispatcher.state.json"
ACTIVITY_LOG = CFG_DIR / "activity.log"
PID_FILE = CFG_DIR / "listener.pid"
LISTENER_LOG = CFG_DIR / "listener.log"
# How long to wait for a killed listener to actually disappear.
# TerminateProcess returns before teardown completes, so liveness must
# be polled, not sampled once (DESIGN §1.1.4).
LISTENER_RESTART_GUARD = CFG_DIR / subprocess_util.LISTENER_RESTART_GUARD_NAME
_KILL_CONFIRM_TIMEOUT_S = 8.0
_KILL_CONFIRM_POLL_S = 0.2
PROMPT_TEMPLATE = SCRIPT_DIR / "spawn_topic_agent.md"


# ---- Tunables -------------------------------------------------------

PARALLEL_LIMIT = 4
CLOSED_RETENTION_DAYS = 30
RECONCILE_FLOOR_CAP_MS = 7 * 24 * 60 * 60 * 1000   # never scan back >7 days
RECONCILE_COLD_START_MS = 60 * 60 * 1000           # fresh install: look back 1h
STALE_LOCK_MINUTES = 30      # beyond this, janitor forcibly deletes
SPAWN_SPEC_RETENTION_DAYS = 2  # cfg/spawn/*.json — read once, kept for forensics
LISTENER_STALE_SECONDS = 45 * 60   # only restart if truly silent —
                                   # lark-cli reconnect chatter keeps
                                   # the log mtime fresh during idle
                                   # periods, so a short threshold
                                   # caused needless restarts and new
                                   # detached-node spawns.
LISTENER_RECONNECT_LOOP_LINES = 30 # if the last N log lines are all
                                   # reconnect chatter without a real
                                   # "→ events/*.json" line, the
                                   # WebSocket is zombied even though
                                   # Node is alive.


# ---- Helpers --------------------------------------------------------

def _cycle_id():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{now}_{suffix}"


def _log(level, message):
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ACTIVITY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(ACTIVITY_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [{level}] {message}\n")
    except OSError:
        pass


def _load_env():
    """Load env vars from settings files (for daemon context) with os.environ fallback.

    Returns (chat_id, approver_id, approver_open_ids, bot_open_id).
    `approver_open_ids` is the canonical authorized-approver namelist; the
    legacy single-approver `REVIEW_BOT_APPROVER_ID` is merged into the list
    for back-compat. `approver_id` is preserved as the primary @-mention
    target (first entry of the list). `bot_open_id` is the deployed bot's
    own user-form open_id (`ou_...`) — used by `router.py` to detect when
    a Lark @-mention picks THIS bot vs another person. Empty string when
    not yet auto-learned (see `bot_identity.maybe_learn`)."""
    chat_id = os.environ.get("REVIEW_BOT_CHAT_ID", "").strip()
    approver_id = os.environ.get("REVIEW_BOT_APPROVER_ID", "").strip()
    approver_csv = os.environ.get("REVIEW_BOT_APPROVER_OPEN_IDS", "").strip()
    bot_open_id = os.environ.get("REVIEW_BOT_OPEN_ID", "").strip()
    if not chat_id or (not approver_id and not approver_csv) or not bot_open_id:
        # Daemon may not inherit Claude's env — read settings files directly
        project_root = SKILL_DIR.parent.parent.parent
        for fname in ("settings.json", "settings.local.json"):
            path = project_root / ".claude" / fname
            if path.is_file():
                try:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                    env = data.get("env", {})
                    if not chat_id:
                        chat_id = env.get("REVIEW_BOT_CHAT_ID", "").strip()
                    if not approver_id:
                        approver_id = env.get("REVIEW_BOT_APPROVER_ID", "").strip()
                    if not approver_csv:
                        approver_csv = env.get("REVIEW_BOT_APPROVER_OPEN_IDS", "").strip()
                    if not bot_open_id:
                        bot_open_id = env.get("REVIEW_BOT_OPEN_ID", "").strip()
                except (json.JSONDecodeError, OSError):
                    pass

    approver_open_ids = [s.strip() for s in approver_csv.split(",") if s.strip()]
    if approver_id and approver_id not in approver_open_ids:
        approver_open_ids.append(approver_id)
    if approver_open_ids and not approver_id:
        approver_id = approver_open_ids[0]

    if not chat_id or not approver_open_ids:
        print(json.dumps({
            "error": "missing REVIEW_BOT_CHAT_ID or REVIEW_BOT_APPROVER_ID/REVIEW_BOT_APPROVER_OPEN_IDS in env and settings",
        }))
        sys.exit(2)
    return chat_id, approver_id, approver_open_ids, bot_open_id


# ---- Step 0: Janitor ------------------------------------------------

def _cherrypick_window_open(topic):
    """True while a MERGED topic may still receive a cherry-pick answer.

    Single source of truth for both archive paths (the generic terminal
    sweep in _janitor and _archive_expired_cherrypick_windows) — they must
    not diverge, or one archives a topic the other is still holding open.
    """
    review = topic.get("review") or {}
    if review.get("state") != "MERGED":
        return False
    return (review.get("cherrypick_window_until") or 0) > int(time.time() * 1000)


def _janitor(cycle_id):
    """Housekeeping sweep:
      (a) move any open-dir topic whose state is terminal into closed/
      (b) delete closed/*.json whose resolved_at is older than retention
      (c) delete *.lock files whose mtime is older than stale threshold
    """
    TOPICS_DIR.mkdir(parents=True, exist_ok=True)
    CLOSED_DIR.mkdir(parents=True, exist_ok=True)

    # (a) Relocate terminal topics that are still in topics/
    for p in topic_store.iter_topic_files(TOPICS_DIR):
        try:
            t = topic_store.read(p)
        except (OSError, ValueError):
            continue
        state = (t.get("review") or {}).get("state")
        if state in state_machine.TERMINAL_STATES:
            # MERGED is terminal but stays addressable while a cherry-pick
            # window is open (DESIGN §1.24). Archiving here would let router
            # Gate 4a drop the approver's answer — the exact failure the
            # deferred archive exists to prevent. Expiry is owned by
            # _archive_expired_cherrypick_windows.
            if _cherrypick_window_open(t):
                continue
            dest = CLOSED_DIR / p.name
            try:
                if dest.exists():
                    dest.unlink()
                p.rename(dest)
                topic_index.remove(INDEX_PATH, t.get("thread_id", ""))
                _log("INFO", f"janitor relocated terminal topic "
                              f"{t.get('identity',{}).get('ticket_id')} -> closed/")
            except OSError:
                pass

    # (b) Prune old closed topics (retention: CLOSED_RETENTION_DAYS)
    cutoff_ms = topic_store.now_ms() - CLOSED_RETENTION_DAYS * 24 * 60 * 60 * 1000
    pruned = 0
    for p in CLOSED_DIR.iterdir():
        if p.is_dir() or not p.name.endswith(".json"):
            continue
        try:
            t = topic_store.read(p)
        except (OSError, ValueError):
            continue
        resolved = (t.get("lifecycle") or {}).get("resolved_at")
        if not resolved:
            # Fall back to file mtime if the v1-migrated entry lacked it
            try:
                resolved = int(p.stat().st_mtime * 1000)
            except OSError:
                resolved = topic_store.now_ms()
        if resolved < cutoff_ms:
            try:
                p.unlink()
                pruned += 1
            except OSError:
                pass
    if pruned:
        _log("INFO", f"janitor pruned {pruned} closed topics older than "
                     f"{CLOSED_RETENTION_DAYS}d")

    # (c) Stale lock cleanup
    stale_cutoff = time.time() - STALE_LOCK_MINUTES * 60
    cleared = 0
    for p in TOPICS_DIR.iterdir():
        if p.is_dir() or not p.name.endswith(".lock"):
            continue
        try:
            if p.stat().st_mtime < stale_cutoff:
                p.unlink()
                cleared += 1
        except OSError:
            pass
    if cleared:
        _log("WARN", f"janitor cleared {cleared} stale lock files")

    # (d) Prune spent spawn specs. `render_spawn_prompt --mode ref` writes one
    # per spawn (DESIGN §1.4.6); they are read once, seconds later, by the
    # agent. Keep a couple of days so a post-mortem can still see exactly what
    # an agent was handed, then drop them.
    spawn_dir = CFG_DIR / "spawn"
    if spawn_dir.is_dir():
        spec_cutoff = time.time() - SPAWN_SPEC_RETENTION_DAYS * 86400
        dropped = 0
        for p in spawn_dir.iterdir():
            if p.is_dir() or p.suffix != ".json":
                continue
            try:
                if p.stat().st_mtime < spec_cutoff:
                    p.unlink()
                    dropped += 1
            except OSError:
                pass
        if dropped:
            _log("INFO", f"janitor pruned {dropped} spawn specs older than "
                         f"{SPAWN_SPEC_RETENTION_DAYS}d")


# ---- Step 1: Listener health ----------------------------------------

def _listener_alive():
    """Return True if the listener PID is running. Best-effort,
    platform-aware.

    Delegates to ``subprocess_util.is_process_alive``, which queries the
    kernel via the Win32 API instead of shelling out to ``tasklist.exe``.
    ``tasklist`` fails with ``ERROR_COMMITMENT_LIMIT`` once the interactive
    desktop heap is exhausted; the old inline implementation treated that
    failure as "listener dead" and restarted the listener every cycle,
    piling up zombie processes that deepened the exhaustion (see
    ``subprocess_util.is_process_alive`` for the full rationale)."""
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return False
    return subprocess_util.is_process_alive(pid)


def _health_check_listener():
    """Try to verify the listener is healthy. If not, restart it via
    start_listener.py. Logged but non-fatal."""
    alive = _listener_alive()
    stale_log = False
    zombied = False
    if alive:
        # Check both listener.log and listener.err — lark-cli writes
        # all output to stderr, so listener.err may be the only file
        # with a fresh mtime (before the 2>&1 fix was deployed).
        log_err = LISTENER_LOG.with_suffix(".err")
        freshest_age = float("inf")
        freshest_path = None
        for log_path in (LISTENER_LOG, log_err):
            if log_path.exists():
                try:
                    age = time.time() - log_path.stat().st_mtime
                    if age < freshest_age:
                        freshest_age = age
                        freshest_path = log_path
                except OSError:
                    pass
        if freshest_age > LISTENER_STALE_SECONDS:
            stale_log = True
        # Zombie detector: the Node process stays alive and keeps writing
        # "Connecting..." / "Connected" / "trying to reconnect" even after
        # the WebSocket stopped delivering events. If the last N log
        # lines are 100% reconnect chatter, force a restart.
        elif freshest_path is not None and freshest_age < LISTENER_STALE_SECONDS:
            try:
                tail = freshest_path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()[-LISTENER_RECONNECT_LOOP_LINES:]
                if tail and not any("→" in line or "events\\" in line
                                     or "events/" in line for line in tail):
                    markers = ("Connecting", "Connected", "reconnect",
                               "disconnected", "SDK Info", "SDK Error")
                    if all(any(m in line for m in markers) for line in tail):
                        zombied = True
            except OSError:
                pass
    if alive and not stale_log and not zombied:
        return {"alive": True, "restarted": False}
    # Someone is restarting the listener right now (`/review-bot restart
    # listener`, which deliberately leaves this daemon running). Respawning
    # here would race their launch and orphan one of the two listeners, so
    # stand down and let the next cycle re-check (DESIGN §1.1.6).
    if subprocess_util.guard_active(LISTENER_RESTART_GUARD):
        _log("INFO", "listener unhealthy but a restart is in progress "
                     "(guard held) — skipping health-check respawn")
        return {"alive": alive, "restarted": False, "suppressed": True}
    _log("WARN", f"listener unhealthy (alive={alive}, stale_log={stale_log}, "
                 f"zombied={zombied}); restarting")
    try:
        _restart_listener()
    except Exception as e:
        _log("ERROR", f"listener restart failed: {e}")
        return {"alive": False, "restarted": False, "error": str(e)}
    return {"alive": True, "restarted": True}


def _restart_listener():
    """Restart the lark-cli event listener. Pure Python — no bash dependency."""
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)

    # Rotate logs >5MB
    for log_path in (LISTENER_LOG, LISTENER_LOG.with_suffix(".err")):
        if log_path.exists():
            try:
                if log_path.stat().st_size > 5_242_880:
                    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
                    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            except OSError:
                pass

    # Kill the existing listener FIRST — a restart that leaves the old one
    # running leaks a listener on every health-check trip, and the check can
    # fire every ~45 min forever (63 leaked listeners by 2026-07-31).
    #
    # This used `taskkill.exe`, which fails on desktop-heap exhaustion with
    # "The paging file is too small for this operation to complete" and was
    # swallowed by the except below — so the old listener survived and the
    # spawn beneath added another. `subprocess_util.kill_process` goes
    # straight to Win32 TerminateProcess, no console helper. Same root cause
    # as the stop-path failure in DESIGN §1.1.4.
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            subprocess_util.kill_process(old_pid)
            # TerminateProcess is ASYNCHRONOUS — the kernel returns before the
            # process is torn down, so checking liveness immediately reads
            # STILL_ACTIVE on a process that is already dying. That made this
            # log a false "survived kill" and return without a replacement,
            # leaving no listener at all until the next health check ~45 min
            # later. Poll instead, same as restart_bot._await_death.
            deadline = time.time() + _KILL_CONFIRM_TIMEOUT_S
            while (subprocess_util.is_process_alive(old_pid)
                   and time.time() < deadline):
                time.sleep(_KILL_CONFIRM_POLL_S)
            if subprocess_util.is_process_alive(old_pid):
                _log("WARN", f"listener {old_pid} survived kill after "
                             f"{_KILL_CONFIRM_TIMEOUT_S:.0f}s — not "
                             f"spawning a replacement (would leak)")
                return
        except (OSError, ValueError):
            pass
        PID_FILE.unlink(missing_ok=True)

    # Launch listener as a detached process that survives parent exit.
    # Going through lark-cli.exe directly (not `node run.js`) keeps the
    # whole process tree windowless — see subprocess_util.lark_cli_argv_prefix.
    log_file = str(LISTENER_LOG)
    cmd_args = subprocess_util.lark_cli_argv_prefix() + [
                "event", "+subscribe",
                "--event-types", "im.message.receive_v1,im.message.recalled_v1",
                "--as", "bot", "--force",
                "--output-dir", "events"]

    with open(log_file, "ab") as lf:
        popen_kwargs = dict(stdout=lf, stderr=subprocess.STDOUT, cwd=str(CFG_DIR))
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True
        proc = subprocess_util.detached_popen(cmd_args, **popen_kwargs)
    new_pid = proc.pid

    PID_FILE.write_text(str(new_pid), encoding="utf-8")


# ---- Step 2: Reconcile ----------------------------------------------

def _read_dispatcher_state():
    try:
        with open(DISPATCHER_STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_dispatcher_state(updates):
    state = _read_dispatcher_state()
    state.update(updates)
    topic_store.write_atomic(DISPATCHER_STATE, state)


def _compute_reconcile_floor(now_ms):
    """Lower bound (epoch ms) for the reconcile scan.

    Prefer the persisted high-water mark `last_reconcile_ts` so the window
    auto-widens to cover the entire downtime since the last successful
    reconcile — the fix for missed topics / mechanical replies that arrived
    while the bot was down (a fixed 1h window dropped anything older). On a
    cold start with no state file, seed from the newest
    `events.last_processed_ts` across open topics (the newest message the
    bot is known to have handled); if there are no topics either, fall back
    to a 1h look-back. Clamp so we never scan back more than the 7-day cap.
    """
    floor = _read_dispatcher_state().get("last_reconcile_ts")
    if not floor:
        seed = 0
        for tp in topic_store.iter_topic_files(TOPICS_DIR):
            try:
                t = topic_store.read(tp)
            except (OSError, ValueError):
                continue
            seed = max(seed, (t.get("events") or {}).get("last_processed_ts", 0) or 0)
        floor = seed if seed else now_ms - RECONCILE_COLD_START_MS
    return max(int(floor), now_ms - RECONCILE_FLOOR_CAP_MS)


def _compute_thread_page_floor(now_ms, floor):
    """Pagination target (epoch ms) for the reconcile chat scan.

    The bulk chat list orders by ROOT create_time and shows replies only
    nested under their root (DESIGN §1.2.8), so the scan must page back to
    the oldest OPEN topic's root or a fresh reply to it is invisible. 2 min
    of slack covers the list's minute-precision create_time; clamp to the
    same 7-day cap as the event floor. No open topics → the event floor is
    deep enough.
    """
    oldest = None
    for tp in topic_store.iter_topic_files(TOPICS_DIR):
        try:
            t = topic_store.read(tp)
        except (OSError, ValueError):
            continue
        created = (t.get("lifecycle") or {}).get("created_at", 0) or 0
        if created:
            oldest = created if oldest is None else min(oldest, created)
    if oldest is None:
        return int(floor)
    return max(int(oldest) - 120_000, now_ms - RECONCILE_FLOOR_CAP_MS)


def _reconcile(chat_id):
    now = topic_store.now_ms()
    floor = _compute_reconcile_floor(now)
    page_floor = _compute_thread_page_floor(now, floor)
    try:
        out = subprocess_util.hidden_run(
            ["python", str(SCRIPT_DIR / "reconcile.py"),
             "--chat-id", chat_id,
             "--events-dir", str(EVENTS_DIR),
             "--since-ts", str(floor),
             "--page-floor-ts", str(page_floor)],
            capture_output=True, text=True, timeout=120,
        )
        if out.returncode != 0:
            _log("ERROR", f"reconcile failed: {out.stderr[:200]}")
            return {"error": out.stderr[:200]}
        try:
            result = json.loads(out.stdout.strip())
        except json.JSONDecodeError:
            return {"error": "bad reconcile output"}
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        _log("ERROR", f"reconcile error: {e}")
        return {"error": str(e)}

    # Advance the persisted floor to the newest message we captured — but
    # never past one we failed to resolve this pass (skipped_unresolved), so
    # a transient failure gets retried next cycle instead of being dropped.
    max_ts = result.get("max_ts", 0) or 0
    candidate = max_ts if max_ts else floor
    min_unresolved = result.get("min_unresolved_ts")
    if min_unresolved:
        candidate = min(candidate, int(min_unresolved) - 1)
    _write_dispatcher_state({"last_reconcile_ts": max(floor, candidate)})
    if result.get("capped"):
        _log("WARN", "reconcile hit max_pages — chat history older than the "
                     f"scanned window was not pulled this cycle (floor={floor})")
    return result


# ---- Step 4b: Close topics with withdrawn root messages --------------

def _archive_expired_cherrypick_windows(cycle_id):
    """Archive MERGED topics whose cherry-pick window has closed.

    Covers both exits: the approver answered (handler zeroes the deadline)
    and nobody answered (deadline simply passes). A MERGED topic without a
    window field at all is a pre-feature or offer-less merge and archives
    immediately. See DESIGN §1.24.
    """
    archived = []
    for path in list(topic_store.iter_topic_files(TOPICS_DIR)):
        try:
            topic = topic_store.read(path)
        except Exception:  # noqa: BLE001
            continue
        if (topic.get("review") or {}).get("state") != "MERGED":
            continue
        if _cherrypick_window_open(topic):
            continue  # still answerable
        thread_id = topic.get("thread_id") or os.path.basename(path)[:-5]
        try:
            topic_store.archive_topic(TOPICS_DIR, INDEX_PATH, thread_id)
            archived.append(thread_id)
        except Exception as exc:  # noqa: BLE001
            _log("WARN", f"cherrypick window archive failed {thread_id}: {exc}")
    if archived:
        _log("INFO", f"cycle {cycle_id}: archived {len(archived)} topic(s) "
                     f"after cherry-pick window: {archived}")
    return archived


def _close_withdrawn_topics(reconcile_result, cycle_id):
    """Cross-reference reconcile's withdrawn_ids against open topics.
    If an open topic's root_message_id appears in the withdrawn set,
    close it — the developer deleted the topic and may repost.

    After each close, undo any supersede the dead topic performed:
    `topic_reopen.reopen_superseded_predecessor` purges the deferred-
    supersede ledger (must happen before `_retry_pending_supersedes`
    later this same cycle) and reopens + backfills the predecessor the
    withdrawn duplicate had closed. See DESIGN §1.2.9."""
    if not isinstance(reconcile_result, dict):
        return {"closed": 0, "reopened": []}
    withdrawn = set(reconcile_result.get("withdrawn_ids") or [])
    if not withdrawn or not TOPICS_DIR.exists():
        return {"closed": 0, "reopened": []}

    closed = 0
    reopened = []
    for p in list(topic_store.iter_topic_files(TOPICS_DIR)):
        try:
            t = topic_store.read(p)
        except (OSError, ValueError):
            continue
        root_mid = t.get("root_message_id", "")
        if root_mid in withdrawn:
            ticket = (t.get("identity") or {}).get("ticket_id", "?")
            thread_id = t.get("thread_id", p.stem)
            topic_store.close_topic(
                TOPICS_DIR, INDEX_PATH, thread_id,
                reason="root message withdrawn")
            _log("WARN", f"withdrawn_root_closed: {ticket} "
                         f"thread={t.get('thread_id','')}")
            closed += 1
            # Pass the closed duplicate's thread_id, NOT the withdrawn
            # message id — the supersede reason embeds the thread_id and
            # create_topic can record root_message_id != thread_id.
            try:
                res = topic_reopen.reopen_superseded_predecessor(
                    TOPICS_DIR, INDEX_PATH, thread_id,
                    withdrawn_ids=withdrawn, log_fn=_log)
            except Exception as exc:  # noqa: BLE001 — never break the cycle
                res = {"status": "error", "error": str(exc)[:200]}
                _log("WARN", f"reopen_superseded_predecessor raised: {exc}")
            if res.get("status") != "no_predecessor" or res.get("purged"):
                reopened.append({"withdrawn_thread": thread_id, **res})
    return {"closed": closed, "reopened": reopened}


# ---- Step 3b: Drain inboxes -----------------------------------------

def _load_supersede_pending():
    """Load the supersede retry ledger."""
    if not SUPERSEDE_PENDING.exists():
        return []
    try:
        with open(SUPERSEDE_PENDING, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_supersede_pending(entries):
    topic_store.write_atomic(SUPERSEDE_PENDING, entries)


def record_pending_supersede(old_thread, new_thread, reason):
    """Append a retry marker for a supersede that was deferred because
    the old topic's lock was still fresh. Called from router.py.
    """
    entries = _load_supersede_pending()
    if any(e.get("old_thread") == old_thread for e in entries):
        return  # already queued
    entries.append({
        "old_thread": old_thread,
        "new_thread": new_thread,
        "reason": reason,
        "queued_at": topic_store.now_ms(),
    })
    _save_supersede_pending(entries)


def _retry_pending_supersedes(cycle_id):
    """Retry any deferred supersede closes whose old topic's lock has
    been released since the previous cycle.
    """
    entries = _load_supersede_pending()
    if not entries:
        return
    remaining = []
    for entry in entries:
        old_thread = entry.get("old_thread")
        if not old_thread:
            continue
        topic_path = TOPICS_DIR / f"{old_thread}.json"
        if not topic_path.exists():
            # Already archived by some other path — drop the marker.
            continue
        new_thread = entry.get("new_thread", "")
        if new_thread and not (TOPICS_DIR / f"{new_thread}.json").exists():
            # The superseding topic itself is gone (withdrawn / archived
            # before this retry) — closing the predecessor on its behalf
            # would orphan the ticket (DESIGN §1.2.9). Drop the marker.
            _log("INFO", f"supersede retry dropped: new_thread {new_thread} "
                         f"no longer open (old={old_thread})")
            continue
        closed = topic_store.close_topic(
            TOPICS_DIR, INDEX_PATH, old_thread,
            reason=entry.get("reason") or "supersede",
            require_unlocked=True)
        if closed:
            _log("INFO", f"supersede retry closed {old_thread} "
                         f"(queued {entry.get('queued_at')})")
        else:
            remaining.append(entry)
    if len(remaining) != len(entries):
        _save_supersede_pending(remaining)


def _drain_inboxes():
    """Move events from per-topic .inbox.json files into topic events.pending[].

    This is the SINGLE WRITER for events.pending[] — router writes to inbox,
    this function moves them into the topic file.

    If an agent currently holds the topic lock, skip draining this cycle:
    writing the topic file while an agent is processing causes the agent's
    later write (from its stale snapshot) to overwrite the appended events.
    The inbox persists across cycles, so events are not lost — they drain
    on the next cycle after the agent releases the lock.
    """
    if not TOPICS_DIR.exists():
        return 0
    drained = 0
    for inbox_path in TOPICS_DIR.glob("*.inbox.json"):
        thread_id = inbox_path.name.replace(".inbox.json", "")
        topic_path = TOPICS_DIR / f"{thread_id}.json"
        if not topic_path.exists():
            # Orphaned inbox — topic was closed/moved. Clean up.
            try:
                inbox_path.unlink()
            except OSError:
                pass
            continue

        # Skip if an agent holds a fresh lock on this topic. Per-topic
        # work is serial (one agent per topic at a time), so the only
        # possible lock holder is a prior cycle's agent still running.
        lock_path = topic_store.lock_path_for(topic_path)
        if lock_path.exists() and not topic_store.lock_is_stale(topic_path):
            continue

        try:
            with open(inbox_path, encoding="utf-8") as f:
                inbox = json.load(f)
            if not inbox:
                inbox_path.unlink()
                continue
            topic = topic_store.read(topic_path)
            pending = topic.setdefault("events", {}).setdefault("pending", [])
            pending.extend(inbox)
            # once inbox items are queued, stamp them in the
            # recent-event ring so the router's next pass dedups them
            # even if the raw-event file re-appears (reconcile backfill).
            for ev_item in inbox:
                topic_store.push_recent_event(
                    topic, ev_item.get("event_id"))
            # Keep last_processed_ts for observability (downstream tools
            # still read it), but it is no longer a dedup gate.
            max_ts = max((ev.get("received_at", 0) for ev in inbox), default=0)
            topic["events"]["last_processed_ts"] = max(
                topic["events"].get("last_processed_ts", 0), max_ts)
            topic["lifecycle"]["updated_at"] = topic_store.now_ms()
            topic_store.write_atomic(topic_path, topic)
            inbox_path.unlink()
            drained += len(inbox)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            _log("WARN", f"drain_inbox failed for {thread_id}: {exc}")
    return drained


# ---- Step 5: Merge tracker ------------------------------------------

def _active_mrs(topic):
    """Return mrs dict filtered by current review_phase.

    - review_phase == "3rd_party": only 3rd-party MRs
    - review_phase == "main" or None: only rage + chaos MRs
    """
    mrs = (topic.get("mrs") or {})
    phase = (topic.get("review") or {}).get("review_phase")
    if phase == "3rd_party":
        return {k: v for k, v in mrs.items() if k.startswith("3rd_party/")}
    else:
        return {k: v for k, v in mrs.items() if not k.startswith("3rd_party/")}


# Pipeline-warning helpers ────────────────────────────────────────────
#
# When an APPROVED topic's MR pipeline transitions into "failed" we want
# to notify the developer once, then post a one-shot follow-up the next
# time we observe a "running" status on a NEW SHA (i.e. their fix push
# kicked off a fresh pipeline). Idempotency is keyed on the failed SHA
# so we don't re-warn for the same failure across cycles.

_PIPELINE_WARN_REPO_LABEL = {"rage": "Game", "chaos": "Chaos"}


def _pipeline_warn_repo_label(repo):
    if repo.startswith("3rd_party/"):
        return "3rd-party"
    return _PIPELINE_WARN_REPO_LABEL.get(repo, repo)


def _write_pipeline_warn_artifact(topic, repo, mr_obj, *, kind,
                                   pipeline_id, pipeline_url):
    """Write a freeform_reply artifact under cfg/replies/. Filename
    encodes kind+sha so re-running the same cycle overwrites in place
    instead of producing duplicates."""
    identity = topic.get("identity") or {}
    ticket_id = identity.get("ticket_id", "")
    dev_id = identity.get("creator_open_id", "")
    thread_id = topic.get("thread_id", "")
    if not (thread_id and dev_id and ticket_id):
        return False
    branch_sha = mr_obj.get("branch_sha") or ""
    short_sha = branch_sha[:8] if branch_sha else ""
    repo_label = _pipeline_warn_repo_label(repo)

    if kind == "failed":
        title = f"{ticket_id} 流水线警告"
        warn_paragraph = [
            {"tag": "text", "text": "⚠️ MR 已批准，但 ", "style": ["bold"]},
            {"tag": "text", "text": f"{repo_label} 仓库 CI "},
        ]
        if pipeline_id and pipeline_url:
            warn_paragraph.append({"tag": "a", "text": f"#{pipeline_id}",
                                    "href": pipeline_url})
            warn_paragraph.append({"tag": "text", "text": " 流水线失败"})
        elif pipeline_id:
            warn_paragraph.append(
                {"tag": "text", "text": f"#{pipeline_id} 流水线失败"})
        else:
            warn_paragraph.append({"tag": "text", "text": "流水线失败"})
        if short_sha:
            warn_paragraph.append(
                {"tag": "text", "text": f"（commit {short_sha}）"})
        warn_paragraph.append({"tag": "text", "text": "。请修复后推送新提交。"})
        body = [
            warn_paragraph,
            [{"tag": "at", "user_id": dev_id, "user_name": "开发者"}],
        ]
    elif kind == "running_after_fail":
        title = f"{ticket_id} 流水线状态"
        msg = [{"tag": "text",
                "text": "修复已提交，流水线运行中，完成后将自动合并。"}]
        if pipeline_url:
            msg.append({"tag": "text", "text": " "})
            msg.append({"tag": "a", "text": "查看流水线", "href": pipeline_url})
        body = [msg]
    else:
        return False

    triggered = f"pipeline_warn:{repo}:{kind}:{branch_sha or 'unknown'}"
    artifact = {
        "version":               1,
        "thread_id":             thread_id,
        "ticket_id":             ticket_id,
        "triggered_by_event_id": triggered,
        "template":              "freeform_reply",
        "vars":                  {"TITLE": title, "BODY_PARAGRAPHS": body},
        "preflight":             {"expected_state":  "APPROVED",
                                  "expected_phase": None},
        "post_actions":          [],
        "orphan_doc_token":      None,
    }
    REPLIES_DIR.mkdir(parents=True, exist_ok=True)
    safe = triggered.replace(":", "__")
    path = REPLIES_DIR / f"{thread_id}__{safe}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(artifact, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)
    return True


def _maybe_post_pipeline_warning(topic, repo, mr_obj, new_status,
                                  recheck_result, cycle_id):
    """One-shot pipeline failure notification + one-shot recovery follow-up.

    State lives on `mr_obj["pipeline_warn_state"]`:
        warned_failed_sha     — branch_sha at warn time (idempotency key)
        warned_pipeline_id    — for audit only
        warned_pipeline_url   — for audit only
        warned_at_ms          — wall clock
        warn_followup_pending — True until we post the recovery follow-up
        follow_up_running_sha — branch_sha at follow-up post time
        follow_up_at_ms       — wall clock
    """
    branch_sha = mr_obj.get("branch_sha") or ""
    warn = mr_obj.get("pipeline_warn_state") or {}
    warned_sha = warn.get("warned_failed_sha")
    follow_up_pending = bool(warn.get("warn_followup_pending"))
    pipeline_id = (recheck_result or {}).get("pipeline_id")
    pipeline_url = (recheck_result or {}).get("pipeline_web_url")

    if new_status == "failed" and warned_sha != branch_sha:
        if _write_pipeline_warn_artifact(
                topic, repo, mr_obj, kind="failed",
                pipeline_id=pipeline_id, pipeline_url=pipeline_url):
            now_ms = int(time.time() * 1000)
            mr_obj["pipeline_warn_state"] = {
                "warned_failed_sha":     branch_sha,
                "warned_pipeline_id":    pipeline_id,
                "warned_pipeline_url":   pipeline_url,
                "warned_at_ms":          now_ms,
                "warn_followup_pending": True,
            }
            topic.setdefault("audit", []).append({
                "ts":          now_ms,
                "event":       "pipeline_warn_posted",
                "repo":        repo,
                "kind":        "failed",
                "pipeline_id": pipeline_id,
                "branch_sha":  branch_sha,
                "cycle_id":    cycle_id,
            })
        return

    if (new_status == "running" and follow_up_pending
            and warned_sha and branch_sha and branch_sha != warned_sha):
        if _write_pipeline_warn_artifact(
                topic, repo, mr_obj, kind="running_after_fail",
                pipeline_id=pipeline_id, pipeline_url=pipeline_url):
            now_ms = int(time.time() * 1000)
            warn["warn_followup_pending"] = False
            warn["follow_up_running_sha"] = branch_sha
            warn["follow_up_at_ms"] = now_ms
            mr_obj["pipeline_warn_state"] = warn
            topic.setdefault("audit", []).append({
                "ts":          now_ms,
                "event":       "pipeline_warn_posted",
                "repo":        repo,
                "kind":        "running_after_fail",
                "branch_sha":  branch_sha,
                "cycle_id":    cycle_id,
            })
        return

    if new_status == "passed" and follow_up_pending:
        # Merge worker will post the canonical 'merged' template; clear
        # the dangling follow-up flag so we don't post stale text later.
        warn["warn_followup_pending"] = False
        mr_obj["pipeline_warn_state"] = warn


def _check_approved_topics(cycle_id, approver_id=""):
    """For each APPROVED topic:
    1. Check MR state (merged/closed/opened)
    2. If opened, recheck pipeline status
    3. Collect topics with passed pipeline for merge queue

    `approver_id` is threaded into each queue entry so
    `process_merge_queue._post_merge_failure` can @-mention the approver
    alongside the developer in the manual-merge request.

    Returns dict with checked/transitioned/errors/merge_queue."""
    transitioned = 0
    errors = 0
    merge_queue = []
    tp_merge_queue = []
    skipped_manual = 0
    skipped_rebase_conflict = 0
    if not TOPICS_DIR.exists():
        return {"checked": 0, "transitioned": 0, "errors": 0,
                "merge_queue": [], "3p_merge_queue": [],
                "skipped_manual_required": 0,
                "skipped_rebase_conflict": 0}
    checked = 0
    for p in topic_store.iter_topic_files(TOPICS_DIR):
        try:
            t = topic_store.read(p)
        except (OSError, ValueError):
            continue
        review = t.get("review") or {}
        if review.get("state") != "APPROVED":
            continue
        # Circuit breaker: process_merge_queue set this after merge
        # failures exceeded MAX_MERGE_RETRIES (or on a non-retryable
        # error code). Skip re-queuing until either the MR moves to
        # "merged" externally (next cycle's check_mr() catches it and
        # transitions to MERGED) or an operator clears the flag.
        if review.get("merge_manual_required"):
            skipped_manual += 1
            continue
        # Circuit breaker: process_merge_queue parked this topic on a rebase
        # conflict. Skip re-queuing (no more per-cycle rebase attempts) until
        # drain_rebase_conflict_ack clears the flag — i.e. the developer
        # rebased, pushed, and replied `ok`. See DESIGN §1.6.7.
        if review.get("rebase_conflict_blocked"):
            skipped_rebase_conflict += 1
            continue
        checked += 1

        # --- Per-MR state check across all repos in this topic ---
        mrs = _active_mrs(t)
        if not mrs:
            continue

        any_error = False
        any_closed = False
        all_merged = True   # assume true, falsify if any MR is NOT merged
        merge_sha = None
        mr_count = 0

        for repo, mr_obj in mrs.items():
            if not (mr_obj.get("mr_iid") or mr_obj.get("iid")):
                continue
            mr_count += 1
            result = merge_tracker.check_mr(repo, mr_obj)
            if result["state"] is None:
                any_error = True
                all_merged = False
                _log("WARN", f"merge_tracker error for "
                             f"{t['identity']['ticket_id']} repo={repo}: "
                             f"{result.get('error')}")
                continue
            if result["merged"]:
                merge_sha = merge_sha or result.get("sha")
            elif result["closed"]:
                any_closed = True
                all_merged = False
            else:
                # MR still open
                all_merged = False

        if mr_count == 0:
            continue
        if any_error and not any_closed:
            errors += 1
            continue

        # Terminal state logic:
        # 1. ANY closed → CLOSED (takes priority)
        # 2. ALL merged → MERGED (only if none closed, none open)
        # 3. Mixed (some merged, some open) → stay APPROVED, wait
        next_state = None
        action = None
        if any_closed:
            next_state = "CLOSED"
            action = "mr_closed"
        elif all_merged:
            next_state = "MERGED"
            action = "merge_detected"

        if next_state:
            # Transition to terminal
            t["review"]["state"] = next_state
            ls = t.setdefault("lifecycle", {})
            ls["resolved_at"] = topic_store.now_ms()
            if next_state == "MERGED":
                ls["merge_sha"] = merge_sha
                ls["merge_detected_at"] = topic_store.now_ms()
            topic_store.append_audit(t,
                event=action,
                from_state="APPROVED",
                to_state=next_state,
                cycle_id=cycle_id,
                sha=merge_sha)

            # Reflect the terminal transition in the thread. This is a
            # *passive* detection path (manual merge, GitLab-side close,
            # downtime catch-up) — process_merge_queue is what posts on the
            # active path. Without this the thread froze at the last reply
            # while the real state moved on. Idempotent + sets the flag on t
            # before we persist. See DESIGN §1.6.11.
            try:
                terminal_notice.post_terminal_notice(t, next_state)
            except Exception as e:  # noqa: BLE001 — never block archiving on a post
                _log("WARN", f"terminal notice post failed for "
                             f"{t['identity']['ticket_id']}: {e}")
            topic_store.write_atomic(p, t)

            # Archive via the canonical helper so the index entry AND the
            # sibling inbox are removed (a bare rename orphaned the inbox —
            # DESIGN §1.1.3).
            try:
                archived = topic_store.archive_topic(
                    p.parent, INDEX_PATH, t.get("thread_id", p.stem))
            except Exception as e:  # noqa: BLE001
                _log("ERROR", f"archive_topic failed for {p.name}: {e}")
                continue
            if not archived:
                _log("ERROR", f"archive_topic returned False for {p.name}")
                continue
            transitioned += 1
            _log("INFO", f"merge_tracker {action}: "
                         f"{t['identity']['ticket_id']} -> {next_state}")
            continue

        # MR(s) still opened — recheck pipeline status per repo via the
        # canonical helper. All pipeline_status writes in
        # this module go through merge_tracker.recheck_pipeline_with_retry;
        # do not inline the mapping or ad-hoc retry logic here.
        #
        # Gating rules differ by phase:
        #   main phase      → every MR must end with pipeline_status="passed"
        #   3rd_party phase → every MR must be pipeline-complete, i.e.
        #                     pipeline_status not in {"running"}. success,
        #                     failed, canceled, and no-pipeline all qualify
        #                     because many 3rd-party repos ship with broken
        #                     or absent CI; we still merge regardless.
        #                     Infra-retry is a main-phase rule; skip the
        #                     retry helper for 3p MRs so a failed infra job
        #                     doesn't flip them back to "running".
        topic_phase = (t.get("review") or {}).get("review_phase") or "main"
        queue_ready = True
        for repo, mr_obj in mrs.items():
            if not (mr_obj.get("mr_iid") or mr_obj.get("iid")):
                continue
            is_3p = repo.startswith("3rd_party/") or topic_phase == "3rd_party"
            if is_3p:
                # 3p rule: just refresh raw pipeline status; no retry.
                pl_result = merge_tracker.check_pipeline_mr(repo, mr_obj)
                raw = pl_result.get("status")
                if raw in ("running", "pending", "created"):
                    mr_obj["pipeline_status"] = "running"
                elif raw is None or raw in ("success", "skipped"):
                    # skipped = no jobs ran; same gate semantic as None->passed
                    mr_obj["pipeline_status"] = "passed"
                else:
                    mr_obj["pipeline_status"] = raw  # failed/canceled/other
                if mr_obj.get("pipeline_status") == "running":
                    queue_ready = False
            else:
                was_passed = mr_obj.get("pipeline_status") == "passed"
                recheck_result = None
                if not was_passed:
                    recheck_result = merge_tracker.recheck_pipeline_with_retry(repo, mr_obj)
                    if recheck_result.get("retried"):
                        t.setdefault("audit", []).append({
                            "ts": int(time.time() * 1000),
                            "event": "infra_failure_retried",
                            "repo": repo,
                            "reason": recheck_result.get("reason", ""),
                            "job_id": recheck_result.get("job_id"),
                        })
                # Pipeline-warning state machine: notify dev once per
                # failed SHA; follow up once when the next pipeline starts
                # running on a new SHA. No-op when status hasn't changed.
                _maybe_post_pipeline_warning(
                    t, repo, mr_obj,
                    new_status=mr_obj.get("pipeline_status"),
                    recheck_result=recheck_result,
                    cycle_id=cycle_id,
                )
                if mr_obj.get("pipeline_status") != "passed":
                    queue_ready = False
                    mr_obj.pop("pipeline_passed_at_ms", None)
                else:
                    # Stamp the moment we first observed "passed" for this MR.
                    # GitLab's detailed_merge_status lags pipeline success by
                    # up to ~60s; merging inside that window returns 405.
                    # Hold the topic off the merge queue for 30s to let
                    # detailed_merge_status settle to "mergeable".
                    now_ms = int(time.time() * 1000)
                    if not mr_obj.get("pipeline_passed_at_ms"):
                        mr_obj["pipeline_passed_at_ms"] = now_ms
                    if now_ms - mr_obj["pipeline_passed_at_ms"] < 30_000:
                        queue_ready = False
        topic_store.write_atomic(p, t)

        # Topic is merge-ready per the phase rule above.
        if queue_ready:
            approval_ts = 0
            for a in reversed(t.get("audit") or []):
                if a.get("to_state") == "APPROVED":
                    approval_ts = a.get("ts", 0)
                    break
            phase = (t.get("review") or {}).get("review_phase") or "main"
            identity = t.get("identity") or {}
            entry = {
                "topic_file":      str(p),
                "thread_id":       t.get("thread_id", ""),
                "ticket_id":       identity.get("ticket_id", ""),
                "mrs":             mrs,
                "root_message_id": t.get("root_message_id", ""),
                "developer_id":    identity.get("creator_open_id", ""),
                "developer_name":  identity.get("developer", ""),
                "approver_id":     approver_id,
                "approval_ts":     approval_ts,
                "phase":           phase,
            }
            if phase == "3rd_party":
                tp_merge_queue.append(entry)
            else:
                merge_queue.append(entry)

    # Sort both merge queues by approval timestamp (earliest first)
    merge_queue.sort(key=lambda x: x.get("approval_ts", 0))
    tp_merge_queue.sort(key=lambda x: x.get("approval_ts", 0))

    return {"checked": checked, "transitioned": transitioned,
            "errors": errors, "merge_queue": merge_queue,
            "3p_merge_queue": tp_merge_queue,
            "skipped_manual_required": skipped_manual,
            "skipped_rebase_conflict": skipped_rebase_conflict}


# ---- Step 6: Find work ----------------------------------------------

def _find_work(cycle_id, rage_root, chaos_root):
    """Scan topics/ for entries with pending events. Skip topics
    currently locked by another cycle (lock file exists and mtime is
    within 10 minutes).

    For topics whose next spawn will be an incremental dev_reply
    review, pre-compute per-repo git log/diff cache files and attach
    their paths under `incr_cache` so the agent can skip those tool
    rounds."""
    work = []
    if not TOPICS_DIR.exists():
        return work
    for p in topic_store.iter_topic_files(TOPICS_DIR):
        try:
            t = topic_store.read(p)
        except (OSError, ValueError):
            continue
        pending = (t.get("events") or {}).get("pending") or []
        if not pending:
            continue
        lock_path = p.with_suffix(".lock")
        if lock_path.exists():
            try:
                age_s = time.time() - lock_path.stat().st_mtime
            except OSError:
                age_s = 0
            if age_s < topic_store.STALE_LOCK_SECONDS:
                continue
        entry = {
            "topic_file":    str(p),
            "lock_file":     str(lock_path),
            "thread_id":     t.get("thread_id", ""),
            "ticket_id":     (t.get("identity") or {}).get("ticket_id"),
            "state":         (t.get("review") or {}).get("state"),
            "pending_count": len(pending),
        }
        try:
            precomputed = incr_cache.precompute_for_topic(
                t, rage_root, chaos_root)
        except Exception as exc:  # noqa: BLE001 — pre-compute must never abort dispatch
            _log("WARN", f"incr_cache precompute failed for {entry['thread_id']}: {exc}")
            precomputed = None
        if precomputed:
            entry["incr_cache"] = precomputed
        work.append(entry)
    return work


# ---- Main -----------------------------------------------------------

def main():
    cycle_id = _cycle_id()
    chat_id, approver_id, approver_open_ids, bot_open_id = _load_env()
    chaos_root = os.environ.get("CHAOS_REPO_ROOT",
                                 str(SKILL_DIR.parent.parent.parent / "chaos"))
    rage_root = os.environ.get("RAGE_REPO_ROOT",
                                 str(SKILL_DIR.parent.parent.parent))

    _log("INFO", f"cycle {cycle_id} starting")

    # Step 0
    _janitor(cycle_id)

    # Step 1
    listener = _health_check_listener()

    # Step 2: reconcile (Lark history backfill) runs ONLY on cold start (the
    # daemon's first cycle invokes dispatcher.py with --reconcile) or right
    # after a listener restart (mid-day gap). Steady-state cycles are
    # listener-only. This removes the per-cycle re-scan AND the dual-ingest
    # churn where reconcile re-processed live messages the listener had already
    # handled (creating a second topic that superseded the first). Withdrawn
    # ROOT detection is re-sourced to the listener recall path
    # (router._evict_recalled_message). See DESIGN §1.2.7.
    if ("--reconcile" in sys.argv) or listener.get("restarted"):
        reconcile_result = _reconcile(chat_id)
        if isinstance(reconcile_result, dict) and reconcile_result.get("reconciled", 0) > 0:
            _log("INFO", f"reconciled {reconcile_result['reconciled']} missed events")
    else:
        reconcile_result = {"reconciled": 0, "withdrawn_ids": [],
                            "skipped": "steady_state (listener-only)"}

    # Steps 3 + 4: router handles index load internally
    route_result = router.route_pending_events(
        EVENTS_DIR, TOPICS_DIR, INDEX_PATH, chat_id, approver_open_ids,
        bot_open_id=bot_open_id)
    if route_result["routed"] > 0 or route_result["new_topics"] > 0:
        _log("INFO", f"router: {route_result}")

    # Step 3b: drain inbox files into topic pending queues
    drained = _drain_inboxes()
    if drained:
        _log("INFO", f"drained {drained} events from inbox files")

    # Step 3c0: drop pending events whose source Lark message was withdrawn.
    # Runs before the mechanical/ack drains so those don't waste work (and
    # especially before topic agents spawn — a withdrawn reply surviving to
    # an opus agent costs ~100K tokens to drain a single useless event).
    withdrawn_drain_result = mechanical_reply_handler.drain_withdrawn(
        TOPICS_DIR, INDEX_PATH, cycle_id)
    if withdrawn_drain_result.get("withdrawn"):
        _log("INFO", f"withdrawn_drain: {withdrawn_drain_result}")

    # Step 3c-: no-new-commits short-circuit. Runs before the mechanical
    # and ack drains so a developer ping that carried no code changes
    # gets an instant template reply instead of burning an Opus spawn.
    # ls-remote failures defer the topic to the agent (safe default).
    no_new_commits_result = mechanical_reply_handler.drain_no_new_commits(
        TOPICS_DIR, INDEX_PATH, cycle_id, rage_root, chaos_root,
        approver_open_ids=approver_open_ids)
    if (no_new_commits_result.get("posted")
            or no_new_commits_result.get("errors")):
        _log("INFO", f"no_new_commits_drain: {no_new_commits_result}")

    # Step 3c-rebase: rebase-conflict `ok` acknowledgment. For topics parked
    # by process_merge_queue on a rebase conflict (review.rebase_conflict_blocked),
    # a developer `ok` reply is consumed here mechanically: if the branch SHA
    # advanced (they pushed a resolution) the flag is cleared so the topic
    # re-enters the normal APPROVED merge flow; otherwise they're told no new
    # commits were detected and the topic stays parked. Runs before _find_work
    # so the parent never sees the `ok` (it would otherwise be a §1.18.2 drop).
    # See DESIGN §1.6.7.
    rebase_ack_result = mechanical_reply_handler.drain_rebase_conflict_ack(
        TOPICS_DIR, INDEX_PATH, cycle_id, rage_root, chaos_root,
        approver_open_ids=approver_open_ids)
    if (rebase_ack_result.get("resumed")
            or rebase_ack_result.get("no_push")
            or rebase_ack_result.get("errors")):
        _log("INFO", f"rebase_conflict_ack_drain: {rebase_ack_result}")

    # Step 3c: mechanical approver-reply dispatch (in-process, no Claude).
    # Handles approve/revision/close for main-phase topics so the Claude
    # budget is spent only on cases that need reasoning.
    mech_result = mechanical_reply_handler.drain_mechanical(
        TOPICS_DIR, INDEX_PATH, approver_open_ids, cycle_id,
        withdrawn_ids=reconcile_result.get("withdrawn_ids") or [])
    if any(mech_result.get(k, 0) for k in ("approve", "revision", "close")):
        _log("INFO", f"mechanical_dispatch: {mech_result}")

    # Step 3c+: drain agent-written reply artifacts. Topic agents write
    # cfg/replies/<thread_id>__<event_id>.json instead of posting to Lark
    # directly; this script consumes them and posts to the artifact's
    # own thread_id (no ticket-id reverse lookups, see DESIGN.md §1.19).
    REPLIES_DIR.mkdir(parents=True, exist_ok=True)
    reply_result = reply_dispatcher.drain_replies(
        TOPICS_DIR, REPLIES_DIR, INDEX_PATH, cycle_id)
    if any(reply_result.get(k, 0) for k in
           ("posted", "withdrawn", "drift_skipped",
            "schema_rejected", "thread_missing", "errors")):
        _log("INFO", f"reply_dispatch: {reply_result}")

    # Step 3d: mechanical ack for new_topic events (in-process, no Claude).
    # Races ahead of the topic agent so the user sees a response within
    # ~5s instead of minutes. Populates `mrs` + `review.ack_stats` so the
    # subsequent agent run can skip MR discovery. Idempotent via
    # `lifecycle.ack_sent`.
    ack_result = ack_new_topic.drain_ack(
        TOPICS_DIR, INDEX_PATH, cycle_id, rage_root, chaos_root,
        approver_open_ids=approver_open_ids)
    if ack_result.get("posted") or ack_result.get("closed_no_mr") \
            or ack_result.get("closed_already_merged") \
            or ack_result.get("closed_already_closed") \
            or ack_result.get("fast_tracked"):
        _log("INFO", f"ack_dispatch: {ack_result}")

    # Step 3e: mechanical ack for dev_reply events (round N+1 trigger).
    # Mirrors step 3d: races ahead of the topic agent's round-N review so
    # the dev sees an immediate `已收到 RAGE-XXXXX 第 N+1 轮修改 ...` reply
    # within ~5s instead of the multi-minute wait for the actual review
    # to land. Idempotent via audit `dev_reply_ack_sent`. See DESIGN §1.3.2.
    dev_ack_result = ack_dev_reply.drain_dev_reply_ack(
        TOPICS_DIR, INDEX_PATH, cycle_id, rage_root, chaos_root)
    if dev_ack_result.get("posted") or dev_ack_result.get("errors"):
        _log("INFO", f"ack_dev_reply_dispatch: {dev_ack_result}")

    # Step 3f: mechanical ack for dev_question events (`@bot <question>`).
    # Mirrors step 3e: the agent's answer takes minutes; the ack lands in
    # ~5s so the dev knows the question was heard. Idempotent via audit
    # `dev_question_ack_sent`. See DESIGN §1.3.3.
    question_ack_result = ack_dev_question.drain_dev_question_ack(
        TOPICS_DIR, INDEX_PATH, cycle_id)
    if question_ack_result.get("posted") or question_ack_result.get("errors"):
        _log("INFO", f"ack_dev_question_dispatch: {question_ack_result}")

    # Step 4b: close topics whose root message was withdrawn
    withdrawn_closed = _close_withdrawn_topics(reconcile_result, cycle_id)

    # Step 4c: archive MERGED topics whose cherry-pick window has lapsed.
    # process_merge_queue deliberately leaves these out of closed/ so the
    # approver's branch answer isn't dropped by Gate 4a; this is the other
    # half of that bargain, so an unanswered offer can't leak an open topic
    # forever (DESIGN §1.24).
    _archive_expired_cherrypick_windows(cycle_id)

    # Step 5
    merge_result = _check_approved_topics(cycle_id, approver_id)

    # Step 6
    work = _find_work(cycle_id, rage_root, chaos_root)

    # Step 6b: retry any deferred supersede-closes.
    _retry_pending_supersedes(cycle_id)

    # Step 7: write dispatch_plan.json. topic_store.write_atomic
    # is the canonical writer — unique .tmp suffix + os.replace makes the
    # plan file a consistent snapshot for the parent agent. Do NOT inline a
    # bare open()+json.dump here; a partial read would make the agent
    # spawn stale work.
    plan = {
        "cycle_id":        cycle_id,
        "generated_at":    topic_store.now_ms(),
        "work":            work,
        "merge_queue":     merge_result.get("merge_queue", []),
        "3p_merge_queue":  merge_result.get("3p_merge_queue", []),
        "parallel_limit":  PARALLEL_LIMIT,
        "prompt_template": str(PROMPT_TEMPLATE),
        "context": {
            "approver_open_id": approver_id,
            "chat_id":          chat_id,
            "chaos_root":       chaos_root,
            "rage_root":        rage_root,
            "skill_dir":        str(SKILL_DIR),
        },
        "diagnostics": {
            "listener":    listener,
            "reconcile":   reconcile_result,
            "route":       route_result,
            "mechanical":  mech_result,
            "withdrawn_drain": withdrawn_drain_result,
            "ack":         ack_result,
            "withdrawn":   withdrawn_closed,
            "merge_track": merge_result,
            "reply_dispatcher": reply_result,
        },
    }
    topic_store.write_atomic(DISPATCH_PLAN, plan)

    # Step 7b: harvest token usage from any subagent transcripts that
    # have completed since the last cycle. Advisory — never fatal.
    try:
        subprocess_util.hidden_run(
            ["python", str(SCRIPT_DIR / "collect_session_tokens.py"),
             "--skill-dir", str(SKILL_DIR)],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        _log("WARN", f"collect_session_tokens failed: {exc}")

    # Step 8: one-line summary to stdout for shell consumers
    print(json.dumps({
        "cycle_id":   cycle_id,
        "work_count": len(work),
        "plan_file":  str(DISPATCH_PLAN),
    }))
    _log("INFO", f"cycle {cycle_id} complete: work={len(work)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
