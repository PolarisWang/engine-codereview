"""Topic file I/O and lock management for the review bot v2 architecture.

Each topic lives in its own JSON file at cfg/topics/<thread_id>.json.
Locks live in sibling files cfg/topics/<thread_id>.lock (tiny JSON
content, present only while a poll cycle's topic agent is processing).

All topic file writes go through write_atomic (write-tmp + rename)
so readers never see a partial file.

Lock acquisition uses O_CREAT|O_EXCL which is atomic on NTFS and
POSIX — the first caller wins, losers get FileExistsError. No flock.
"""

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path


# ---- Constants ------------------------------------------------------

STALE_LOCK_SECONDS = 10 * 60          # 10 minutes — agents that outlive
                                      # this are assumed crashed; next
                                      # cycle may steal.
KEEPALIVE_INTERVAL_SECONDS = 60       # how often LockKeepalive refreshes
                                      # the lock file's mtime.
KEEPALIVE_MAX_SECONDS = 30 * 60       # hard ceiling — beyond this the
                                      # keepalive stops refreshing so
                                      # the existing 10-min stale rule
                                      # re-engages as a safety valve for
                                      # genuinely hung agents.
SCHEMA_VERSION = 3


# ---- Time helper ----------------------------------------------------

def now_ms():
    return int(time.time() * 1000)


# ---- Atomic file I/O -------------------------------------------------

def _migrate_v2_to_v3(topic):
    """Convert a v2 topic (single 'mr' object) to v3 ('mrs' dict).

    Moves topic["mr"] into topic["mrs"][repo] (keyed by repo slug),
    stripping the redundant 'repo' field from the MR object itself.
    Returns the mutated topic dict.
    """
    mr = topic.pop("mr", None) or {}
    repo = mr.pop("repo", None)
    if repo and mr.get("mr_iid"):
        topic["mrs"] = {repo: mr}
    else:
        topic["mrs"] = {}
    topic["schema_version"] = SCHEMA_VERSION
    return topic


def read(path):
    """Read a topic (or plan / index / lock) JSON file.

    Returns the parsed dict, or raises FileNotFoundError if missing.
    UTF-8 always — state contains Chinese branch names and titles.

    Auto-migrates v2 topics to v3 on read (writes back atomically).
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and data.get("schema_version") == 2:
        data = _migrate_v2_to_v3(data)
        write_atomic(path, data)
    return data


def read_or_none(path):
    """Same as read() but returns None when the file doesn't exist."""
    try:
        return read(path)
    except FileNotFoundError:
        return None


def _normalize_review_issues(data):
    """Auto-rename non-canonical issue keys in `review.issues[]` and
    `review.flagged_issues[]`.

    Topic agents have written `{id, text}` instead of the canonical
    `{index, description}` more than once, which makes
    `mechanical_reply_handler._handle_revision` fail to look up
    flagged indices and silently leave revision events un-drained.
    Normalizing on every write means a buggy agent's first writeback
    self-heals before the next mechanical cycle.
    """
    if not isinstance(data, dict):
        return
    review = data.get("review") or {}
    for field in ("issues", "flagged_issues"):
        items = review.get(field)
        if not isinstance(items, list):
            continue
        for issue in items:
            if not isinstance(issue, dict):
                continue
            if "id" in issue and "index" not in issue:
                issue["index"] = issue.pop("id")
            if "text" in issue and "description" not in issue:
                issue["description"] = issue.pop("text")


def write_atomic(path, data):
    """Write a dict as JSON to `path` atomically.

    Uses a unique .tmp suffix and os.replace (atomic on NTFS + POSIX).
    The unique suffix prevents collision if two callers race on the
    same topic — whichever loses the rename race just overwrites its
    tmp file; no partial read is possible.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Normalize iid → mr_iid so downstream code can always use mr_iid
    if isinstance(data, dict):
        for _repo, mr in (data.get("mrs") or {}).items():
            if isinstance(mr, dict) and mr.get("iid") and not mr.get("mr_iid"):
                mr["mr_iid"] = mr["iid"]
    _normalize_review_issues(data)
    tmp = path.with_suffix(path.suffix + f".tmp-{uuid.uuid4().hex[:8]}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass  # fsync not supported on some filesystems
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# ---- Topic file discovery --------------------------------------------
#
# `cfg/topics/` holds four shapes that all match `*.json` globbing:
#   - `om_X.json`           topic state (dict)
#   - `om_X.inbox.json`     router inbox for that topic (list)
#   - `om_X.lock`           lock file (dict, but `.lock` suffix)
#   - `om_X.json.tmp-HEX8`  atomic-write tmp file (transient)
# Every scanner in the pipeline must skip inbox/lock/tmp files or it will
# read a list and crash on `.get()` (or read partial JSON and misreport
# state). Centralizing the filter here keeps that invariant in one place.

def iter_topic_files(topics_dir):
    """Yield Path objects for real topic JSONs only.

    Filters out: subdirectories, non-.json suffixes, `.inbox.json`
    files, atomic-write tmp files (`.tmp-...`), and anything whose
    name doesn't start with `om_`. Safe to call when topics_dir is
    missing (yields nothing).
    """
    topics_dir = Path(topics_dir)
    if not topics_dir.exists():
        return
    for p in topics_dir.iterdir():
        if p.is_dir() or p.suffix != ".json":
            continue
        if p.name.endswith(".inbox.json"):
            continue
        if ".tmp-" in p.name:
            continue
        if not p.name.startswith("om_"):
            continue
        yield p


def inbox_path_for(topics_dir, thread_id):
    """Return the sibling inbox file path for a topic thread_id."""
    return Path(topics_dir) / f"{thread_id}.inbox.json"


# ---- Topic creation --------------------------------------------------

def empty_topic(thread_id, root_message_id, ticket_id, creator_open_id,
                chat_id, initial_state="TRIAGING"):
    """Return a freshly-initialized v2 topic dict."""
    t = now_ms()
    return {
        "schema_version": SCHEMA_VERSION,
        "thread_id": thread_id,
        "root_message_id": root_message_id,
        "identity": {
            "ticket_id": ticket_id,
            "creator_open_id": creator_open_id,
            "developer": None,
            "chat_id": chat_id,
        },
        "mrs": {},
        "review": {
            "state": initial_state,
            "triage": None,
            "review_round": 0,
            "last_review_commit": None,
            "issues_found": 0,
            "flagged_issues": [],
            "lark_doc_token": None,
            "review_history": [],
        },
        "events": {
            "pending": [],
            "last_processed_event_id": None,
            "last_processed_ts": 0,
        },
        "lifecycle": {
            "created_at": t,
            "updated_at": t,
            "resolved_at": None,
            "merge_sha": None,
            "merge_detected_at": None,
            "closed_reason": None,
        },
        "audit": [
            {"ts": t, "event": "topic_created", "state": initial_state},
        ],
    }


def create_topic(topics_dir, thread_id, root_message_id, ticket_id,
                 creator_open_id, chat_id, initial_state="TRIAGING"):
    """Create a new topic file. Returns its Path.

    Idempotent: if a file already exists at the target path, it is
    returned unchanged.
    """
    topics_dir = Path(topics_dir)
    topics_dir.mkdir(parents=True, exist_ok=True)
    topic_path = topics_dir / f"{thread_id}.json"
    if topic_path.exists():
        return topic_path
    data = empty_topic(thread_id, root_message_id, ticket_id,
                       creator_open_id, chat_id, initial_state)
    write_atomic(topic_path, data)
    return topic_path


# ---- Lock file API ---------------------------------------------------
#
# Convention: lock file is sibling to topic file, same basename with
# .lock extension instead of .json. The file's CONTENT is a JSON blob
# describing the holder; the file's EXISTENCE is the lock itself.

def lock_path_for(topic_path):
    """Return the sibling lock file path for a topic."""
    topic_path = Path(topic_path)
    return topic_path.with_suffix(".lock")


def acquire_lock(topic_path, holder_id, poll_cycle_id):
    """Try to atomically claim the lock for this topic.

    Returns True on success, False if another holder is still active
    (lock exists and mtime is within STALE_LOCK_SECONDS). Steals a
    stale lock once and retries.
    """
    lp = lock_path_for(topic_path)
    lp.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "locked_by": holder_id,
        "locked_at": now_ms(),
        "poll_cycle_id": poll_cycle_id,
    }
    try:
        fd = os.open(str(lp), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        pass

    # Lock exists — check staleness
    try:
        age_s = time.time() - lp.stat().st_mtime
    except FileNotFoundError:
        # Race: other holder released between the EXCL attempt and the stat.
        return acquire_lock(topic_path, holder_id, poll_cycle_id)
    if age_s > STALE_LOCK_SECONDS:
        try:
            lp.unlink()
        except FileNotFoundError:
            pass
        # Retry exactly once; if it races again, caller can retry later.
        try:
            fd = os.open(str(lp), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            return False
    return False


def release_lock(topic_path):
    """Release the lock for this topic (delete the lock file).

    Safe to call on an already-released topic.
    """
    lp = lock_path_for(topic_path)
    try:
        lp.unlink()
    except FileNotFoundError:
        pass


def read_lock(topic_path):
    """Return the lock file contents as a dict, or None if not locked."""
    lp = lock_path_for(topic_path)
    if not lp.exists():
        return None
    try:
        return read(lp)
    except (OSError, ValueError):
        return None


def lock_is_stale(topic_path, threshold_seconds=STALE_LOCK_SECONDS):
    """Return True if the lock exists and its mtime exceeds the threshold."""
    lp = lock_path_for(topic_path)
    if not lp.exists():
        return False
    try:
        age = time.time() - lp.stat().st_mtime
    except FileNotFoundError:
        return False
    return age > threshold_seconds


def _run_keepalive_loop(lock_path, *, started_at, interval, max_seconds,
                         sleep_fn=None, log_fn=None):
    """Touch `lock_path`'s mtime every `interval` seconds for up to
    `max_seconds` from `started_at`. Single source of truth for both
    in-process callers (the `LockKeepalive` daemon thread) and the
    `keepalive-run` CLI worker that runs in a detached subprocess.

    Exits cleanly when:
        - `sleep_fn(interval)` returns truthy (caller-requested stop —
          the thread wraps a `threading.Event` here; the CLI worker
          passes `None` and falls back to plain `time.sleep`).
        - The lock file disappears (agent called `release_lock`, or
          another cycle stole the lock — either way nothing to keep
          alive).

    Stops refreshing past `max_seconds` but the function does NOT
    return — it keeps polling so an in-process caller's
    `__exit__`/`stop` signal still gets observed. The CLI worker
    survives this branch the same way; the dispatcher's 30-min
    janitor (`STALE_LOCK_MINUTES`) is the hard force-clear path.

    Logging: `log_fn(level, msg)` is called ONCE when the cap is
    first reached. Optional — `None` is silent.
    """
    lock_path = Path(lock_path)
    capped_logged = False
    while True:
        # Sleep first so the very first refresh is at +interval, not
        # at +0 — the agent's acquire_lock already touched the file.
        if sleep_fn is not None:
            if sleep_fn(interval):
                return
        else:
            time.sleep(interval)
        if not lock_path.exists():
            # Lock was released or stolen externally — nothing to refresh.
            return
        elapsed = time.time() - started_at
        if elapsed > max_seconds:
            if not capped_logged and log_fn:
                try:
                    log_fn(
                        "WARN",
                        f"lock_keepalive_capped path={lock_path.name} "
                        f"elapsed={int(elapsed)}s "
                        f"cap={max_seconds}s — refreshes stopped, "
                        f"stale-lock safety valve re-engaged")
                except Exception:  # noqa: BLE001
                    pass  # logger failure must not abort the agent
            capped_logged = True
            # Keep polling for the caller's stop signal / lock removal,
            # but stop refreshing — the existing 10-min stale rule will
            # now fire as designed.
            continue
        try:
            os.utime(lock_path, None)
        except FileNotFoundError:
            return  # lock disappeared — stop trying
        except OSError:
            # Transient FS error; ignore and try again next interval.
            pass


class LockKeepalive:
    """Refresh a lock file's mtime on a background daemon thread so a
    long-running topic agent does not trip the 10-min stale-lock rule
    mid-flight.

    Usage:
        with LockKeepalive(lock_path, log_fn=log) as keepalive:
            ... long-running work ...
        # thread joined automatically on context exit (success / drift / crash)

    Hard ceiling: KEEPALIVE_MAX_SECONDS (30 min). Past the cap, refreshes
    stop and the existing stale-lock safety valve re-engages — for
    genuinely hung agents we still want supersede / lock-stealing to
    eventually succeed. The cap is logged once via `log_fn` (a callable
    accepting `level: str, msg: str` — typically the dispatcher's `_log`)
    if provided, else silent.

    **Topic agents on the Claude Code harness should NOT use this
    class** — the agent's work spans many separate Bash/Task tool
    invocations and the daemon thread doesn't survive between
    subprocesses. Use the `keepalive-spawn` CLI subcommand instead.
    See DESIGN §1.8.4.
    """

    def __init__(self, lock_path, log_fn=None,
                 interval=KEEPALIVE_INTERVAL_SECONDS,
                 max_seconds=KEEPALIVE_MAX_SECONDS):
        self._lock_path = Path(lock_path)
        self._interval = interval
        self._max_seconds = max_seconds
        self._stop = threading.Event()
        self._thread = None
        self._started_at = None
        self._log_fn = log_fn

    def __enter__(self):
        self._started_at = time.time()
        self._thread = threading.Thread(
            target=self._run, name="LockKeepalive", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        if self._thread is not None:
            # Daemon thread; small join timeout is enough — we don't
            # want exit() to hang if the thread is mid-utime.
            self._thread.join(timeout=2.0)
        return False  # never suppress exceptions

    def _run(self):
        _run_keepalive_loop(
            self._lock_path,
            started_at=self._started_at or time.time(),
            interval=self._interval,
            max_seconds=self._max_seconds,
            sleep_fn=self._stop.wait,
            log_fn=self._log_fn,
        )


# ---- Close helper ----------------------------------------------------

def archive_topic(topics_dir, index_path, thread_id, require_unlocked=False):
    """Move a terminal topic to closed/ and remove from index.

    Does NOT change the topic's review state — caller must set the desired
    terminal state (MERGED, CLOSED, etc.) before calling this.

    When `require_unlocked=True` (opt-in): if a fresh (<10 min)
    lock file exists next to the topic, skip the rename and return False
    with reason "skipped_locked" via a sentinel — caller must defer to the
    next cycle. Default is False to preserve existing caller behavior; B2
    wiring flips callers to True site-by-site.

    Returns True if archived, False if topic file was already gone OR
    (require_unlocked=True) the topic's lock is still fresh.
    """
    topics_dir = Path(topics_dir)
    topic_path = topics_dir / f"{thread_id}.json"
    closed_dir = topics_dir / "closed"
    closed_dir.mkdir(parents=True, exist_ok=True)

    if not topic_path.exists():
        return False

    if require_unlocked:
        lp = lock_path_for(topic_path)
        if lp.exists() and not lock_is_stale(topic_path):
            return False

    dest = closed_dir / topic_path.name
    try:
        if dest.exists():
            dest.unlink()
        topic_path.rename(dest)
    except OSError:
        return False

    # Drop the sibling inbox too. Left behind, it would be picked up by
    # the next poll's scanners and — if a late reply races in before the
    # janitor sweeps it — resurrected into a phantom topic.
    inbox = inbox_path_for(topics_dir, thread_id)
    try:
        inbox.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass

    import topic_index
    topic_index.remove(Path(index_path), thread_id)
    return True


def close_topic(topics_dir, index_path, thread_id, reason, from_state=None,
                require_unlocked=False):
    """Close a topic: set state=CLOSED, archive to closed/, drop from index.

    When `require_unlocked=True` (opt-in): if the topic's lock
    is still fresh, skip both the state write AND the archive; return False
    with the topic left untouched so the next cycle can retry. Default is
    False to preserve existing caller behavior (most existing callers hold
    the lock themselves and pass it through).

    Returns True if closed, False if topic was already gone OR
    (require_unlocked=True) the topic's lock is still fresh.
    """
    topics_dir = Path(topics_dir)
    topic_path = topics_dir / f"{thread_id}.json"

    if not topic_path.exists():
        return False

    if require_unlocked:
        lp = lock_path_for(topic_path)
        if lp.exists() and not lock_is_stale(topic_path):
            return False

    try:
        topic = read(topic_path)
    except (OSError, ValueError):
        return False

    old_state = (topic.get("review") or {}).get("state", "UNKNOWN")
    topic.setdefault("review", {})["state"] = "CLOSED"
    ls = topic.setdefault("lifecycle", {})
    ls["resolved_at"] = now_ms()
    ls["closed_reason"] = reason
    append_audit(topic,
        event="topic_closed",
        from_state=from_state or old_state,
        to_state="CLOSED",
        reason=reason)
    write_atomic(topic_path, topic)

    return archive_topic(topics_dir, index_path, thread_id,
                         require_unlocked=require_unlocked)


# ---- Work donation (superseded-topic handoff) -----------------------
#
# When a same-ticket supersede archives a topic mid-agent (or right
# after the agent finished but before the artifact got drained), we'd
# like the canonical topic to inherit the completed Lark doc + issues
# rather than throw the work away and re-do it. That's `donate_review_to_topic`.
#
# Donation is attempted from two sites: (a) the topic agent itself when
# its preflight detects drift AND it has already done expensive work,
# (b) `reply_dispatcher` when an artifact's thread_id no longer
# resolves to an open topic and the closed topic's lifecycle.closed_reason
# is `"superseded by new topic <X>"`. Both callers extract a
# src_payload dict and call the shared transfer below.

DONATABLE_REVIEW_KEYS = (
    "issues", "flagged_issues",
    "lark_doc_token", "lark_doc_url",
    "last_review_commit", "review_round", "triage",
)


def find_canonical_thread_for_ticket(topics_dir, ticket_id, index_path=None):
    """Return the open thread_id mapped to `ticket_id`, or None.

    Reads `topic_index.load_or_rebuild` if `index_path` is provided
    (cheap — usually cached). Falls back to a flat scan of topics_dir
    when no index path is given. Skips topics under `closed/`.
    """
    if not ticket_id:
        return None
    if index_path is not None:
        import topic_index
        idx = topic_index.load_or_rebuild(Path(topics_dir), Path(index_path))
        for tid, tkt in idx.items():
            if tkt == ticket_id:
                return tid
        return None
    # Fallback: walk topic files directly.
    for p in iter_topic_files(topics_dir):
        try:
            t = read(p)
        except (OSError, ValueError):
            continue
        if (t.get("identity") or {}).get("ticket_id") == ticket_id:
            return t.get("thread_id")
    return None


def _shas_match(src_sha, dst_mrs):
    """True iff `src_sha` equals every repo's branch_sha in `dst_mrs`.

    Empty `dst_mrs` (canonical topic was created but ack hasn't yet
    populated its MRs) returns False — donation should defer until ack
    runs and the SHAs are knowable. Empty `src_sha` also returns False —
    we won't donate work that the source agent never anchored to a SHA.
    """
    if not src_sha or not dst_mrs:
        return False
    for mr in dst_mrs.values():
        if (mr or {}).get("branch_sha") != src_sha:
            return False
    return True


def donate_review_to_topic(topics_dir, dst_thread_id, src_payload, *,
                           src_thread_id, cycle_id, log_fn=None):
    """Transfer completed review fields into the canonical topic for the
    same ticket. Returns a status dict — caller is responsible for any
    artifact-side rewrite (filename, thread_id, triggered_by_event_id).

    Statuses (always JSON-friendly):
      - donated:       fields written, dst lock released
      - missing_dst:   dst topic file not found (canonical may have been
                       superseded too, or index is stale)
      - locked:        dst lock held by a fresh agent — caller should
                       leave the artifact in place for next cycle
      - sha_mismatch:  src.last_review_commit != dst.mrs[*].branch_sha
                       (donation would replay stale findings)
      - error:         I/O or JSON failure; details in 'reason'
    """
    topics_dir = Path(topics_dir)
    dst_topic_path = topics_dir / f"{dst_thread_id}.json"
    if not dst_topic_path.exists():
        return {"status": "missing_dst",
                "reason": f"no open topic file for {dst_thread_id}"}

    holder = f"donor-{cycle_id}"
    if not acquire_lock(dst_topic_path, holder, cycle_id):
        return {"status": "locked",
                "reason": "canonical topic lock held by another agent"}

    try:
        try:
            dst = read(dst_topic_path)
        except (OSError, ValueError) as exc:
            return {"status": "error",
                    "reason": f"read dst failed: {exc}"}

        src_sha = (src_payload or {}).get("last_review_commit")
        dst_mrs = dst.get("mrs") or {}
        if not _shas_match(src_sha, dst_mrs):
            return {
                "status": "sha_mismatch",
                "reason": "src last_review_commit does not match dst "
                          "branch_sha for all repos",
                "src_sha": src_sha,
                "dst_shas": {r: (m or {}).get("branch_sha")
                             for r, m in dst_mrs.items()},
            }

        review = dst.setdefault("review", {})
        donated_keys = []
        for key in DONATABLE_REVIEW_KEYS:
            if key in (src_payload or {}) and src_payload[key] is not None:
                review[key] = src_payload[key]
                donated_keys.append(key)

        # Drain the canonical topic's pending root `new_topic` event
        # so ack_new_topic doesn't double-process. The root's event_id
        # is `msg:<dst_thread_id>` by convention; match defensively.
        events = dst.setdefault("events", {})
        pending = events.setdefault("pending", [])
        root_event_id = f"msg:{dst_thread_id}"
        new_pending = []
        drained_event_ids = []
        for ev in pending:
            ev_id = ev.get("event_id") or ""
            ev_msg = ev.get("message_id") or ""
            is_root = (ev_id == root_event_id
                       or ev_msg == dst_thread_id
                       or ev.get("classified_as") in (None, "new_topic")
                       and ev.get("state_at_arrival") == "TRIAGING")
            if is_root:
                drained_event_ids.append(ev_id or ev_msg)
            else:
                new_pending.append(ev)
        events["pending"] = new_pending
        if drained_event_ids:
            events["last_processed_event_id"] = drained_event_ids[-1]
            for ev_id in drained_event_ids:
                push_recent_event(dst, ev_id)

        append_audit(dst,
            event="inherited_review_from_superseded_topic",
            src_thread_id=src_thread_id,
            cycle_id=cycle_id,
            donated_keys=donated_keys,
            drained_event_ids=drained_event_ids,
        )
        write_atomic(dst_topic_path, dst)

        if log_fn:
            try:
                log_fn("INFO",
                    f"donate_review src={src_thread_id} dst={dst_thread_id} "
                    f"keys={donated_keys} drained={drained_event_ids}")
            except Exception:  # noqa: BLE001
                pass

        return {
            "status": "donated",
            "donated_keys": donated_keys,
            "drained_event_ids": drained_event_ids,
        }
    finally:
        release_lock(dst_topic_path)


def extract_donation_payload(topic):
    """Pull the donatable fields out of a topic dict (src side).

    Returns a payload dict suitable for `donate_review_to_topic`. None /
    missing fields are omitted; the donor function only writes keys
    actually present.
    """
    review = (topic or {}).get("review") or {}
    payload = {}
    for key in DONATABLE_REVIEW_KEYS:
        if review.get(key) is not None:
            payload[key] = review[key]
    return payload


# Writers (router.py supersede) must use this format so the regex below —
# the reopen path's entire reverse lookup (DESIGN §1.2.9) — keeps matching.
SUPERSEDE_REASON_FMT = "superseded by new topic {thread_id}"
_SUPERSEDE_REASON_RE = re.compile(r"superseded by new topic (om_\S+)")


def parse_superseded_by(topic):
    """If `topic.lifecycle.closed_reason` matches the supersede pattern,
    return the new canonical thread_id; otherwise None.

    Used by both `agent_preflight` and `reply_dispatcher` to route
    work from a closed src topic to its canonical successor.
    """
    reason = ((topic or {}).get("lifecycle") or {}).get("closed_reason") or ""
    m = _SUPERSEDE_REASON_RE.search(reason)
    return m.group(1) if m else None


# ---- Audit helper ----------------------------------------------------

AUDIT_MAX = 500  # cap per-topic audit entries in memory.


def append_audit(topic, **entry):
    """Append a timestamped audit entry to a topic dict (in place).

    caps the in-memory audit at AUDIT_MAX (500) entries by
    dropping the oldest. We keep the most recent N because debugging a
    stuck topic usually needs the last few transitions, not transitions
    from weeks ago. A dedicated sidecar spill is intentionally out of
    scope: operator can grep cfg/activity.log for pre-rotation history.
    Caller is responsible for writing back via write_atomic.
    """
    if "ts" not in entry:
        entry["ts"] = now_ms()
    audit = topic.setdefault("audit", [])
    audit.append(entry)
    overflow = len(audit) - AUDIT_MAX
    if overflow > 0:
        del audit[:overflow]
    topic.setdefault("lifecycle", {})["updated_at"] = entry["ts"]


# ---- Event-id ring buffer ------------------------------
#
# Router and inbox-drain populate this ring so per-topic dedup works
# even when events arrive out of ts order. The ring replaces the strict
# `<=` last_processed_ts gate that previously dropped any event whose
# message create_time was older than the topic's watermark — a problem
# when a reconcile pass backfills messages from before the last tick.

RECENT_EVENT_CAP = 50


def push_recent_event(topic, event_id, cap=RECENT_EVENT_CAP):
    """Record event_id in the topic's recent-event ring (FIFO, deduped).

    Callers: router.py drain-path (when inbox items land in pending),
    dispatcher._drain_inboxes, mechanical_reply_handler when popping.
    No-op for falsy event_id (defensive).
    """
    if not event_id:
        return
    evs = topic.setdefault("events", {})
    ring = evs.setdefault("recent_event_ids", [])
    if event_id in ring:
        return
    ring.append(event_id)
    overflow = len(ring) - cap
    if overflow > 0:
        del ring[:overflow]


def is_recent_event(topic, event_id):
    """True if event_id appears in the topic's ring OR matches the
    legacy scalar last_processed_event_id (back-compat for topics
    written before the ring existed)."""
    if not event_id:
        return False
    evs = topic.get("events") or {}
    if event_id in (evs.get("recent_event_ids") or ()):
        return True
    return evs.get("last_processed_event_id") == event_id


# ---- Active lock listing (recover precondition) ----------

def active_lock_holders(topics_dir):
    """Return a list of dicts describing currently-held (non-stale) locks.

    Used by `/review-bot recover` to refuse running when agents are
    mid-flight — forcibly reconciling state under them would clobber
    their in-progress writes. Each entry: topic_file, holder, cycle,
    age_seconds.
    """
    topics_dir = Path(topics_dir)
    if not topics_dir.exists():
        return []
    holders = []
    for topic_path in iter_topic_files(topics_dir):
        lp = lock_path_for(topic_path)
        if not lp.exists():
            continue
        if lock_is_stale(topic_path):
            continue
        try:
            info = read(lp)
        except (OSError, ValueError):
            info = {}
        try:
            age = time.time() - lp.stat().st_mtime
        except FileNotFoundError:
            continue
        holders.append({
            "topic_file": str(topic_path),
            "holder":     info.get("holder"),
            "cycle":      info.get("cycle"),
            "age_seconds": round(age, 1),
        })
    return holders


# ---- CLI entrypoint --------------------------------------------------
#
# Used by shell code inside the per-topic agent prompt. Supports:
#   python topic_store.py acquire-lock --topic FILE --holder ID --cycle ID
#   python topic_store.py release-lock --topic FILE
#   python topic_store.py read-lock    --topic FILE
#
# Exit code 0 = lock acquired / released. Exit code 1 = lock busy
# (another cycle) — agent should exit with status=skipped_locked.

def _activity_log_writer():
    """Return a log_fn(level, msg) that appends to cfg/activity.log.

    Used by the `keepalive-run` worker since it runs in a detached
    subprocess with no console. Mirrors the format the dispatcher's
    `_log` uses so a grep on `activity.log` surfaces both sources
    uniformly.
    """
    activity_log = Path(__file__).resolve().parent.parent / "cfg" / "activity.log"

    def _log(level, msg):
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(activity_log, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] [{level}] keepalive_worker: {msg}\n")
        except OSError:
            pass

    return _log


def _keepalive_run(topic_path):
    """Body of the `keepalive-run` CLI worker.

    Detached subprocess entry point: refresh `lock_path_for(topic_path)`
    every KEEPALIVE_INTERVAL_SECONDS until the lock file disappears
    (agent released it) or KEEPALIVE_MAX_SECONDS elapses (then keeps
    polling without refreshing — the janitor's 30-min hard-clear
    handles genuinely-hung agents).

    No stdout output — the parent has already detached us. Errors go
    to activity.log via `_activity_log_writer()`.
    """
    lock_path = lock_path_for(topic_path)
    log = _activity_log_writer()
    try:
        _run_keepalive_loop(
            lock_path,
            started_at=time.time(),
            interval=KEEPALIVE_INTERVAL_SECONDS,
            max_seconds=KEEPALIVE_MAX_SECONDS,
            sleep_fn=None,           # plain time.sleep — no early-stop signal
            log_fn=log,
        )
    except KeyboardInterrupt:
        # Killed by signal — exit cleanly, leave the lock as-is for
        # the agent's `release_lock` to clean up.
        return 0
    except Exception as exc:  # noqa: BLE001
        log("ERROR", f"keepalive crashed: {exc!r}")
        return 1
    return 0


def _keepalive_spawn(topic_path):
    """Launch a `keepalive-run` worker as a fully detached subprocess.

    Uses `subprocess_util.detached_popen` (same machinery as
    `start_listener.py`) so the worker survives /clear and any
    parent process exit. Lifetime is bounded by KEEPALIVE_MAX_SECONDS
    (worker self-exits on the cap) and by the lock file's existence
    (worker exits when `release_lock` removes the file).

    Returns the spawned PID. No stdin/stdout/stderr — silent
    background process.
    """
    import sys as _sys
    import subprocess as _subprocess
    # Lazy import to avoid pulling subprocess_util when topic_store is
    # imported by tests / other consumers that don't spawn processes.
    sys_path_added = False
    try:
        import subprocess_util  # type: ignore[import-not-found]
    except ImportError:
        _sys.path.insert(0, str(Path(__file__).resolve().parent))
        import subprocess_util  # type: ignore[import-not-found]
        sys_path_added = True
    cmd = [_sys.executable, str(Path(__file__).resolve()),
           "keepalive-run", "--topic", str(topic_path)]
    proc = subprocess_util.detached_popen(
        cmd,
        stdin=_subprocess.DEVNULL,
        stdout=_subprocess.DEVNULL,
        stderr=_subprocess.DEVNULL,
    )
    if sys_path_added:
        _sys.path.pop(0)
    return proc.pid


def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="topic_store CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("acquire-lock")
    a.add_argument("--topic", required=True)
    a.add_argument("--holder", required=True)
    a.add_argument("--cycle", required=True)

    r = sub.add_parser("release-lock")
    r.add_argument("--topic", required=True)

    p = sub.add_parser("read-lock")
    p.add_argument("--topic", required=True)

    ks = sub.add_parser("keepalive-spawn",
                         help="Launch detached keepalive worker for a topic's lock")
    ks.add_argument("--topic", required=True)

    kr = sub.add_parser("keepalive-run",
                         help="Worker entry point — invoked by keepalive-spawn, "
                              "not normally called by hand")
    kr.add_argument("--topic", required=True)

    args = ap.parse_args()

    if args.cmd == "acquire-lock":
        ok = acquire_lock(args.topic, args.holder, args.cycle)
        print(json.dumps({"acquired": ok}))
        return 0 if ok else 1
    if args.cmd == "release-lock":
        release_lock(args.topic)
        print(json.dumps({"released": True}))
        return 0
    if args.cmd == "read-lock":
        info = read_lock(args.topic)
        print(json.dumps({"lock": info}, ensure_ascii=False))
        return 0
    if args.cmd == "keepalive-spawn":
        pid = _keepalive_spawn(args.topic)
        print(json.dumps({"keepalive_pid": pid}))
        return 0
    if args.cmd == "keepalive-run":
        return _keepalive_run(args.topic)
    return 2


if __name__ == "__main__":
    import sys
    sys.exit(_cli())
