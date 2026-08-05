#!/usr/bin/env python3
"""
Topic-level pipeline state for the code-review system.

Owns the JSON state file that records each topic's lifecycle phase and per-repo
status. Used by the Jenkins pipeline (via CLI subcommands) and by ops (query).

Storage layout (`.pipeline-state.json`):

    {
      "schema_version": 1,
      "updated_at": "2026-08-04T03:00:00Z",
      "scanner": { "last_scan_time": 1720000000 },      # optional cursor
      "topics": {
        "<message_id>": { ...one topic record... }
      }
    }

A topic record:

    {
      "message_id": "om_...",
      "jira_key": "EV-123", "project": "EV", "jira_url": "https://...",
      "mode": "scan" | "manual",
      "text_preview": "", "sender_id": "", "sender_name": "",
      "review_branch": "", "base_branch": "", "mr_url": "",
      "created_at": "..", "updated_at": "..",
      "phase": "SCANNED|PARSING|REVIEWING|NOTIFYING|DONE|FAILED",
      "status": "SUCCESS|FAILED|RUNNING|SKIPPED",
      "last_error": "", "render_msg_id": "", "build_number": null,
      "repos": {
        "engine": { "status": "PENDING|RUNNING|SKIPPED|SUCCESS|FAILED",
                    "error": "", "skip_reason": "", "result_file": "",
                    "changed_files": 0, "stats": "",
                    "severity_counts": {}, "started_at": null, "finished_at": null },
        "game": { ...same... }
      }
    }

Threading/atomicity: the Jenkins job enables `disableConcurrentBuilds()`, so
within a single build the scan and post stages run sequentially (single writer).
Writers still use an atomic temp-file + os.replace() so a crash never leaves a
truncated file, and concurrent readers (query CLI) never see a partial document.

Stdlib only (no external deps).
"""
import argparse
import json
import os
import sys
import time

DEFAULT_STATE_FILE = ".pipeline-state.json"
SCHEMA_VERSION = 1

# Ordered topic phases. FAILED is a terminal sink reachable from any non-DONE
# phase; phase order is enforced so we cannot accidentally jump backwards.
PHASE_ORDER = ["SCANNED", "PARSING", "REVIEWING", "NOTIFYING", "DONE"]
TERMINAL_PHASES = {"DONE", "FAILED"}
REPO_STATUSES = {"PENDING", "RUNNING", "SKIPPED", "SUCCESS", "FAILED"}
VALID_REPOS = ("engine", "game")


# ── Time helpers ─────────────────────────────────────────────────────────────

def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ── Low-level load/save (atomic) ─────────────────────────────────────────────

def load_state(path=DEFAULT_STATE_FILE):
    """Read the state file. Missing/invalid JSON yields an empty store. Always
    returns a well-formed store: {"schema_version", "updated_at", "topics"}."""
    default = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "scanner": {},
        "topics": {},
    }
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default
    # Normalize / repair partial documents (e.g. hand-edited or legacy).
    if not isinstance(state, dict):
        return default
    state.setdefault("schema_version", SCHEMA_VERSION)
    state.setdefault("updated_at", _now_iso())
    state.setdefault("scanner", {})
    state.setdefault("topics", {})
    return state


def save_state(state, path=DEFAULT_STATE_FILE):
    """Atomically persist state: write to <path>.tmp then os.replace() over
    path. Raises OSError on failure (callers must not swallow silently)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ── Topic record helpers ─────────────────────────────────────────────────────

def _repo_default():
    return {
        "status": "PENDING",
        "error": "",
        "skip_reason": "",
        "result_file": "",
        "changed_files": 0,
        "stats": "",
        "severity_counts": {},
        "started_at": None,
        "finished_at": None,
    }


def _topic_default(message_id, jira_key, project, jira_url, mode, build_number,
                   text_preview, sender_id, sender_name):
    return {
        "message_id": message_id,
        "jira_key": jira_key,
        "project": project,
        "jira_url": jira_url,
        "mode": mode,
        "text_preview": (text_preview or "")[:500],
        "sender_id": sender_id or "",
        "sender_name": sender_name or "",
        "review_branch": "",
        "base_branch": "",
        "mr_url": "",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "phase": "SCANNED",
        "status": "RUNNING",
        "last_error": "",
        "render_msg_id": "",
        "build_number": build_number,
        "retry_count": 0,
        "next_retry_at": "",       # ISO — earliest time the scan may retry this topic
        "failed_exhausted": False,  # True once retries are exhausted (stop auto-retry)
        "repos": {"engine": _repo_default(), "game": _repo_default()},
        # Multi-turn agent session (design-3): cross-message dialog memory + patches.
        "chat_history": [],         # list of {"role":"user|assistant","content":"..."}
        "pending_patch": None,      # {"file","diff","repo","created_at", "push_pending": bool} awaiting @ok/@confirm push
        "applied_patches": [],      # list of {"file","diff","repo","commit","applied_at"}
        "approval_log": [],         # audit: {"actor","action","target","time","result"}
    }


# ── CRUD ─────────────────────────────────────────────────────────────────────

def add_topic(path, *, message_id, jira_key, project="", jira_url="",
              mode="scan", text_preview="", sender_id="", sender_name="",
              build_number=None):
    """
    Create a SCANNED/RUNNING topic record.

    - mode=scan: idempotent — an existing record (in-progress or DONE) is left
      unchanged so the overlap window can't clobber it. The dedup filter treats
      terminal records as already-processed.
    - mode=manual: an existing TERMINAL record (DONE/FAILED) is RESET to a fresh
      SCANNED/RUNNING state — a manual re-review of the same issue is an explicit
      restart and should re-run (and pass the phase-order guard on the first
      transition).
    """
    state = load_state(path)
    existing = state["topics"].get(message_id)
    if existing is None:
        state["topics"][message_id] = _topic_default(
            message_id, jira_key, project, jira_url, mode, build_number,
            text_preview, sender_id, sender_name)
        state["updated_at"] = _now_iso()
        save_state(state, path)
    elif mode == "manual" and existing.get("phase") in TERMINAL_PHASES:
        # Manual restart: reset a terminal record so the review re-runs.
        fresh = _topic_default(message_id, jira_key, project, jira_url, "manual",
                               build_number, text_preview, sender_id, sender_name)
        state["topics"][message_id] = fresh
        state["updated_at"] = _now_iso()
        save_state(state, path)
    return state["topics"][message_id]


def get_topic(path, key):
    state = load_state(path)
    return state["topics"].get(key)


def list_topics(path):
    state = load_state(path)
    topics = list(state["topics"].values())
    topics.sort(key=lambda t: (t.get("updated_at") or ""), reverse=True)
    return topics


def terminal_topic_keys(path):
    """Return keys of topics in a terminal phase (used as a dedup signal)."""
    state = load_state(path)
    return [k for k, t in state["topics"].items()
            if t.get("phase") in TERMINAL_PHASES]


# ── Phase transition ─────────────────────────────────────────────────────────

def transition(path, key, *, to, status, last_error="", render_msg_id=None,
               review_branch=None, base_branch=None, mr_url=None, build_number=None):
    """
    Transition a topic to phase `to` with a new `status`. Enforces a non-decreasing
    phase order (only FAILED may jump to a terminal sink from any non-DONE phase).
    Updates updated_at and, when `to` is terminal, sets finished fields.

    write_topic: controls whether to persist (True) or read-only simulation.
    Returns the updated topic record.
    """
    state = load_state(path)
    topic = state["topics"].get(key)
    if topic is None:
        # Unknown key — create a minimal stub so later probes can attach.
        topic = _topic_default(key, key, "", "", "scan", build_number, "", "", "")
        state["topics"][key] = topic

    prev = topic.get("phase", "SCANNED")
    # Enforce ordering: reject backward jumps (except FAILED as a sink).
    if to not in TERMINAL_PHASES:
        old_idx = PHASE_ORDER.index(prev) if prev in PHASE_ORDER else -1
        new_idx = PHASE_ORDER.index(to) if to in PHASE_ORDER else -1
        if new_idx < old_idx and to not in TERMINAL_PHASES:
            raise ValueError(f"illegal phase transition {prev} -> {to}")

    topic["phase"] = to
    topic["status"] = status
    topic["updated_at"] = _now_iso()
    if last_error:
        topic["last_error"] = last_error
    if render_msg_id is not None:
        topic["render_msg_id"] = render_msg_id
    if review_branch is not None and review_branch != "":
        topic["review_branch"] = review_branch
    if base_branch is not None and base_branch != "":
        topic["base_branch"] = base_branch
    if mr_url is not None:
        topic["mr_url"] = mr_url
    if build_number is not None:
        topic["build_number"] = build_number

    state["updated_at"] = _now_iso()
    save_state(state, path)
    return topic


# ── Retry with exponential backoff ───────────────────────────────────────────

# Backoff schedule (seconds) between automatic retries of a failed topic, by
# attempt index. After exhausting this list the topic is marked failed_exhausted
# (no more auto-retry until a human resets it or a new scan adds it fresh).
RETRY_BACKOFF_SECONDS = [60, 300, 900, 3600, 7200]  # 1m, 5m, 15m, 1h, 2h


def record_failure(path, key, error=""):
    """
    Record a topic failure: increment retry_count, compute the next allowed retry
    time (exponential backoff), and set failed_exhausted when the schedule runs
    out. Returns the topic dict.
    """
    state = load_state(path)
    topic = state["topics"].get(key)
    if topic is None:
        topic = _topic_default(key, key, "", "", "scan", None, "", "", "")
        state["topics"][key] = topic
    if error:
        topic["last_error"] = error

    attempt = int(topic.get("retry_count") or 0)
    topic["retry_count"] = attempt + 1
    attempted_at = time.time()

    idx = attempt  # index into schedule for the *next* retry's delay
    if idx < len(RETRY_BACKOFF_SECONDS):
        delay = RETRY_BACKOFF_SECONDS[idx]
        topic["next_retry_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime(attempted_at + delay))
        topic["failed_exhausted"] = False
    else:
        topic["next_retry_at"] = ""
        topic["failed_exhausted"] = True

    topic["phase"] = "FAILED"
    topic["status"] = "FAILED"
    topic["updated_at"] = _now_iso()
    state["updated_at"] = _now_iso()
    save_state(state, path)
    return topic


def can_retry(path, key):
    """
    Return True if a failed topic may be retried now (backoff elapsed, not
    exhausted). Also returns True if the topic isn't in a failed state.
    """
    state = load_state(path)
    topic = state["topics"].get(key)
    if topic is None:
        return True
    if topic.get("failed_exhausted"):
        return False
    nxt = topic.get("next_retry_at") or ""
    if not nxt:
        # No pending schedule -> either not failed or immediate retry allowed.
        return True
    try:
        nxt_ts = time.mktime(time.strptime(nxt, "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return True
    return time.time() >= nxt_ts


def get_retryable(path, limit=5):
    """
    Return FAILED topics that are currently eligible for automatic retry
    (backoff elapsed AND not exhausted). This decouples retries from the Feishu
    scan window — a message that has aged out of the scanner's cursor can still
    be retried because we drive from pipeline state, not from message time.
    Returns a list of topic dicts (most recently updated first), capped at
    `limit` to bound retry volume per build.
    """
    state = load_state(path)
    now = time.time()
    out = []
    for t in state["topics"].values():
        if t.get("phase") != "FAILED":
            continue
        if t.get("failed_exhausted"):
            continue
        # Need a resolvable jira_url to re-run review.
        if not t.get("jira_url"):
            continue
        nxt = t.get("next_retry_at") or ""
        if nxt:
            try:
                nxt_ts = time.mktime(time.strptime(nxt, "%Y-%m-%dT%H:%M:%S"))
            except (ValueError, TypeError):
                pass  # treat as immediately retryable
            else:
                if now < nxt_ts:
                    continue
        out.append(t)
    out.sort(key=lambda t: (t.get("updated_at") or ""), reverse=True)
    return out[:limit]


def reset_for_retry(path, key):
    """
    Reset a FAILED topic to a fresh SCANNED/RUNNING state so a retry can run
    (clears the terminal FAILED phase and the backoff schedule). Keeps the
    original jira_url/branch fields. Returns the topic dict.
    """
    state = load_state(path)
    topic = state["topics"].get(key)
    if topic is None:
        return None
    topic["phase"] = "SCANNED"
    topic["status"] = "RUNNING"
    topic["next_retry_at"] = ""
    # Keep retry_count as a historical counter; failed_exhausted stays until a
    # manual/intervention clears it — but resetting implies the operator opted in,
    # so clear the exhausted flag so it can actually retry.
    topic["failed_exhausted"] = False
    topic["updated_at"] = _now_iso()
    state["updated_at"] = _now_iso()
    save_state(state, path)
    return topic


# ── Per-repo status ──────────────────────────────────────────────────────────

def set_repo(path, key, repo, *, status, error="", skip_reason="",
             result_file="", severity_counts=None, stats="", changed_files=0):
    """Update a repo's status + granular fields. Sets finished_at when the repo
    reaches a terminal state (SUCCESS/SKIPPED/FAILED)."""
    if repo not in VALID_REPOS:
        raise ValueError(f"invalid repo: {repo}")
    if status not in REPO_STATUSES:
        raise ValueError(f"invalid repo status: {status}")

    state = load_state(path)
    topic = state["topics"].get(key)
    if topic is None:
        state["topics"][key] = _topic_default(key, key, "", "", "scan", None,
                                              "", "", "")
        topic = state["topics"][key]

    r = topic["repos"].setdefault(repo, _repo_default())
    r["status"] = status
    if error:
        r["error"] = error
    if skip_reason:
        r["skip_reason"] = skip_reason
    if result_file:
        r["result_file"] = result_file
    if severity_counts:
        r["severity_counts"] = severity_counts
    if stats:
        r["stats"] = stats
    if changed_files:
        r["changed_files"] = int(changed_files)
    if status in ("SUCCESS", "SKIPPED", "FAILED"):
        if not r.get("started_at"):
            r["started_at"] = _now_iso()
        r["finished_at"] = _now_iso()

    topic["updated_at"] = _now_iso()
    state["updated_at"] = _now_iso()
    save_state(state, path)
    return topic


# ── Multi-turn agent session (design-3) ──────────────────────────────────────

MAX_CHAT_HISTORY = 40   # cap stored message turns to bound context/cost


def append_chat(path, key, message):
    """
    Append one chat message {"role","content"} to a topic's chat_history.
    Trims to the newest MAX_CHAT_HISTORY entries.
    """
    state = load_state(path)
    topic = state["topics"].get(key)
    if topic is None:
        return None
    history = topic.setdefault("chat_history", [])
    if isinstance(history, list):
        history.append(message)
        if len(history) > MAX_CHAT_HISTORY:
            topic["chat_history"] = history[-MAX_CHAT_HISTORY:]
    topic["updated_at"] = _now_iso()
    state["updated_at"] = _now_iso()
    save_state(state, path)
    return topic


def set_pending_patch(path, key, patch):
    """
    Set (or clear) the topic's pending_patch. patch is a dict or None.
    """
    state = load_state(path)
    topic = state["topics"].get(key)
    if topic is None:
        return None
    topic["pending_patch"] = patch
    topic["updated_at"] = _now_iso()
    state["updated_at"] = _now_iso()
    save_state(state, path)
    return topic


def record_applied_patch(path, key, patch):
    """
    Move a confirmed/executed patch into applied_patches (append) and clear
    pending_patch. patch includes {"file","diff","repo","commit","applied_at"}.
    """
    state = load_state(path)
    topic = state["topics"].get(key)
    if topic is None:
        return None
    applied = topic.setdefault("applied_patches", [])
    if isinstance(applied, list):
        applied.append(patch)
    topic["pending_patch"] = None
    topic["updated_at"] = _now_iso()
    state["updated_at"] = _now_iso()
    save_state(state, path)
    return topic


def pop_last_applied_patch(path, key):
    """Remove and return the most recently applied patch (for @撤销)."""
    state = load_state(path)
    topic = state["topics"].get(key)
    if topic is None:
        return None
    applied = topic.get("applied_patches") or []
    patch = applied.pop() if applied else None
    topic["updated_at"] = _now_iso()
    state["updated_at"] = _now_iso()
    save_state(state, path)
    return patch


# Keep at most this many audit entries per topic.
MAX_APPROVAL_LOG = 100


def append_approval(path, key, actor, action, target="", result="ok", note=""):
    """
    Append one audit entry to the topic's approval_log.
    Fields: actor, action, target, time, result, note.
    """
    state = load_state(path)
    topic = state["topics"].get(key)
    if topic is None:
        return None
    log = topic.setdefault("approval_log", [])
    entry = {
        "actor": actor or "",
        "action": action or "",
        "target": target or "",
        "time": _now_iso(),
        "result": result or "ok",
        "note": note or "",
    }
    if isinstance(log, list):
        log.append(entry)
        if len(log) > MAX_APPROVAL_LOG:
            topic["approval_log"] = log[-MAX_APPROVAL_LOG:]
    topic["updated_at"] = _now_iso()
    state["updated_at"] = _now_iso()
    save_state(state, path)
    return topic


# ── Structured log line ──────────────────────────────────────────────────────

def log_line(*, phase, status, topic="", issue="", project="", repo="", detail=""):
    """Build one line of structured build-log output, greppable via
    `grep '[CODEREVIEW]'`."""
    parts = [phase or "", status or ""]
    fields = [
        ("topic", topic), ("issue", issue), ("project", project),
        ("repo", repo), ("detail", detail),
    ]
    pairs = " ".join(f"{k}={v}" for k, v in fields if v)
    return f"[CODEREVIEW] phase={parts[0]} status={parts[1]} {pairs}"


# ── Query UX ─────────────────────────────────────────────────────────────────

def _sev_badge(repo_status, sev_counts):
    """Compact per-repo badge for the query table."""
    if repo_status == "PENDING":
        return "-"
    if repo_status == "RUNNING":
        return "RUNNING"
    if repo_status == "SKIPPED":
        return "SKIPPED"
    s = sev_counts or {}
    crit = s.get("critical", 0)
    warn = s.get("warning", 0)
    sugg = s.get("suggestion", 0)
    if repo_status == "SUCCESS":
        return f"{crit}/{warn}/{sugg}" if (crit or warn or sugg) else "CLEAN"
    return "FAILED"


def query(path, key=None, as_json=False):
    """Print query output. Returns nothing."""
    if key:
        t = get_topic(path, key)
        if not t:
            print(json.dumps({"error": f"topic {key} not found"}, ensure_ascii=False))
            return
        print(json.dumps(t, ensure_ascii=False, indent=2))
        return
    topics = list_topics(path)
    if as_json:
        print(json.dumps(topics, ensure_ascii=False, indent=2))
        return
    # Aligned table
    header = ["TOPIC", "JIRA", "PHASE", "STATUS", "ENGINE", "GAME", "UPDATED_AT"]
    rows = []
    for t in topics:
        eng = t["repos"].get("engine", {})
        gam = t["repos"].get("game", {})
        rows.append([
            t.get("message_id", ""),
            t.get("jira_key", ""),
            t.get("phase", ""),
            t.get("status", ""),
            _sev_badge(eng.get("status"), eng.get("severity_counts")),
            _sev_badge(gam.get("status"), gam.get("severity_counts")),
            t.get("updated_at", ""),
        ])
    widths = [max(len(header[i]), max((len(r[i]) for r in rows), default=0))
              for i in range(len(header))]
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(header)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(r)))
    print()
    print(f"{len(topics)} topic(s)")


def status(path):
    """Print a compact ops overview: counts of topics by phase/status, plus the
    currently-running and exhausted-failed ones (for alerting/monitoring)."""
    topics = list_topics(path)
    running = [t for t in topics if t.get("status") == "RUNNING"]
    done = [t for t in topics if t.get("phase") == "DONE"]
    failed = [t for t in topics if t.get("phase") == "FAILED"]
    exhausted = [t for t in topics if t.get("failed_exhausted")]
    skipped = [t for t in topics if t.get("status") == "SKIPPED"]
    print(f"topics={len(topics)} running={len(running)} done={len(done)} "
          f"failed={len(failed)} skipped={len(skipped)} exhausted={len(exhausted)}")
    if running:
        print("RUNNING:")
        for t in running:
            print(f"  - {t.get('message_id','')} ({t.get('jira_key','')}) {t.get('phase','')}")
    if exhausted:
        print("EXHAUSTED (need attention / might alert):")
        for t in exhausted:
            print(f"  - {t.get('message_id','')} ({t.get('jira_key','')}) error={t.get('last_error','')[:80]}")
    # For scanning the state (non-interactive), return counts so callers can alert.
    return {"topics": len(topics), "running": len(running), "done": len(done),
            "failed": len(failed), "exhausted": len(exhausted)}


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(description="CodeReview pipeline state")
    # --state-file may appear before or after the subcommand.
    parser.add_argument("--state-file", default="",
                        help="Path to state file (env PIPELINE_STATE_FILE or default)")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state-file", default="",
                        help="Path to state file (overrides global)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add", help="Create (idempotently) a topic record", parents=[common])
    p.add_argument("--key", required=True)
    p.add_argument("--jira-key", default="",
                   help="Jira issue key; defaults to --key when omitted (e.g. manual mode)")
    p.add_argument("--project", default="")
    p.add_argument("--jira-url", default="")
    p.add_argument("--mode", default="scan", choices=["scan", "manual"])
    p.add_argument("--text", default="")
    p.add_argument("--sender-id", default="")
    p.add_argument("--sender-name", default="")
    p.add_argument("--build-number", default=None)

    p = sub.add_parser("transition", parents=[common],
                       help="Transition topic phase/status")
    p.add_argument("--key", required=True)
    p.add_argument("--to", required=True)
    p.add_argument("--status", required=True)
    p.add_argument("--error", default="")
    p.add_argument("--render-msg-id", default=None)
    p.add_argument("--review-branch", default=None)
    p.add_argument("--base-branch", default=None)
    p.add_argument("--mr-url", default=None)
    p.add_argument("--build-number", default=None)

    p = sub.add_parser("repo", parents=[common], help="Update a repo's sub-status")
    p.add_argument("--key", required=True)
    p.add_argument("--repo", required=True, choices=list(VALID_REPOS))
    p.add_argument("--status", required=True, choices=sorted(REPO_STATUSES))
    p.add_argument("--error", default="")
    p.add_argument("--skip-reason", default="")
    p.add_argument("--result-file", default="")
    p.add_argument("--severity-json", default="")
    p.add_argument("--stats", default="")
    p.add_argument("--changed-files", default=0)

    p = sub.add_parser("query", parents=[common], help="Query topic state")
    p.add_argument("--key", default="")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("status", parents=[common],
                       help="Ops overview: counts by phase/status + alerts")

    p = sub.add_parser("retryable", parents=[common],
                       help="List FAILED topics eligible for automatic retry (JSON list)")

    p = sub.add_parser("fail", parents=[common],
                       help="Record a topic failure with exponential-backoff retry set")
    p.add_argument("--key", required=True)
    p.add_argument("--error", default="")

    args = parser.parse_args(argv)
    path = (args.state_file or os.environ.get("PIPELINE_STATE_FILE")
            or DEFAULT_STATE_FILE)

    if args.command == "add":
        add_topic(path, message_id=args.key,
                  jira_key=(args.jira_key or args.key),
                  project=args.project, jira_url=args.jira_url, mode=args.mode,
                  text_preview=args.text, sender_id=args.sender_id,
                  sender_name=args.sender_name, build_number=args.build_number)
        print(f"[ok] topic {args.key} add (or already exists)")
    elif args.command == "fail":
        record_failure(path, args.key, args.error)
        print(f"[ok] topic {args.key} recorded failure; retry={can_retry(path, args.key)}")
    elif args.command == "transition":
        try:
            transition(path, args.key, to=args.to, status=args.status,
                       last_error=args.error, render_msg_id=args.render_msg_id,
                       review_branch=args.review_branch, base_branch=args.base_branch,
                       mr_url=args.mr_url, build_number=args.build_number)
        except ValueError as e:
            print(f"[warn] {e} (kept current state)", file=sys.stderr)
    elif args.command == "repo":
        sev = {}
        if args.severity_json:
            try:
                sev = json.loads(args.severity_json)
            except json.JSONDecodeError:
                sev = {}
        set_repo(path, args.key, args.repo, status=args.status,
                 error=args.error, skip_reason=args.skip_reason,
                 result_file=args.result_file, severity_counts=sev,
                 stats=args.stats, changed_files=args.changed_files)
    elif args.command == "query":
        query(path, key=args.key or None, as_json=args.json)
    elif args.command == "status":
        status(path)
    elif args.command == "retryable":
        topics = get_retryable(path)
        # Emit a JSON array of {message_id, jira_url, jira_key, retry_count}
        print(json.dumps([{
            "message_id": t.get("message_id", ""),
            "jira_key": t.get("jira_key", ""),
            "jira_url": t.get("jira_url", ""),
            "retry_count": int(t.get("retry_count") or 0),
        } for t in topics], ensure_ascii=False))


if __name__ == "__main__":
    main()