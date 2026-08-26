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
SCHEMA_VERSION = 2  # v2 = directory layout (topics/<key>.json + manifest.json)
SCHEMA_VERSION_V1 = 1

# Ordered topic phases. FAILED is a terminal sink reachable from any non-DONE
# phase; phase order is enforced so we cannot accidentally jump backwards.
PHASE_ORDER = ["SCANNED", "PARSING", "REVIEWING", "NOTIFYING", "DONE"]
TERMINAL_PHASES = {"DONE", "FAILED", "CLOSED"}
REPO_STATUSES = {"PENDING", "RUNNING", "SKIPPED", "SUCCESS", "FAILED"}
VALID_REPOS = ("engine", "game")


# ── Time helpers ─────────────────────────────────────────────────────────────

def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ── Low-level load/save (atomic) ─────────────────────────────────────────────
#
# Two layouts are supported, selected automatically by whether `path` is a
# directory or a file:
#   - FILE mode (schema v1, legacy): single whole-file JSON document.
#   - DIR mode  (schema v2): `path/` is a directory holding:
#         manifest.json   {schema_version:2, updated_at,
#                          topics:{<key>:"topics/<key>.json"}}
#         topics/<key>.json    one topic record
#         index.json      {<key>: {phase,status,pending,updated_at,mr_url}}
#         archive/<key>.json   closed/archived topics
#         patches/<key>.p<n>.diff   sidecar diff blobs (refs stored in topic)
#   DIR mode gives per-topic atomic writes + true per-topic locking, so concurrent
#   writers (event server / ci-poll / run / cleanup) cannot clobber whole-file state.

def _atomic_write(path, data):
    """Write `data` (dict) to `path` atomically (unique temp + fsync + rename).
    Raises OSError on failure — never silently returns on a bad write."""
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _is_dir_mode(path):
    return os.path.isdir(path)


def load_state(path=DEFAULT_STATE_FILE):
    """Read the state store. In FILE mode returns the whole document
    ({"schema_version","updated_at","topics",...}) for legacy callers. In DIR mode
    returns a minimal envelope {"schema_version":2,"updated_at","topics":{}} whose
    "topics" values are RELATIVE PATHS — callers must use _load_topic() to read an
    actual topic record (never a whole-document reader on dir mode)."""
    if _is_dir_mode(path):
        m = _load_manifest(path)
        topics = {}
        for k in (m.get("topics") or {}):
            topics[k] = m["topics"][k]  # relative path string
        return {"schema_version": SCHEMA_VERSION, "updated_at": m.get("updated_at", ""),
                "topics": topics}
    default = {
        "schema_version": SCHEMA_VERSION_V1,
        "updated_at": _now_iso(),
        "scanner": {},
        "topics": {},
    }
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError):
        # Distinguish missing vs corrupt: NEVER silently return empty on a corrupt
        # legacy file, or the next save would erase it. Raise instead so ops see it.
        raise RuntimeError(
            f"state file corrupt (not JSON): {path} — refusing to overwrite. "
            "Inspect the file; recovery is manual or via pipeline_state.migrate()")
    if not isinstance(state, dict):
        raise RuntimeError(f"state file wrong type at {path}: {type(state)}")
    return state


def save_state(state, path=DEFAULT_STATE_FILE):
    """Persist a state store. In FILE mode writes the whole document atomically.
    In DIR mode this is only valid for one-topic stores produced by load_state on a
    single key; mutators should prefer _save_topic(). Raises OSError on failure."""
    if _is_dir_mode(path):
        # Dir mode supports per-topic saves only; refuse whole-doc writes that were
        # routed here with a full document (they would clobber unrelated topics).
        raise RuntimeError(
            f"save_state() in dir mode is unsupported; use _save_topic(path, key, topic): {path}")
    _atomic_write(path, state)


def _manifest_path(path):
    return os.path.join(path, "manifest.json")


def _index_path(path):
    return os.path.join(path, "index.json")


def _load_raw(path, default):
    """Low-level JSON read that distinguishes a missing file (returns default) from
    a corrupt file (raises). Never silently returns a fresh store on corruption."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError):
        raise RuntimeError(f"state file corrupt (not JSON): {path}")
    return data if isinstance(data, dict) else default


def _load_manifest(path):
    d = _load_raw(_manifest_path(path), {})
    d.setdefault("schema_version", SCHEMA_VERSION)
    d.setdefault("updated_at", _now_iso())
    d.setdefault("topics", {})
    return d


def _save_manifest(path, manifest):
    _atomic_write(_manifest_path(path), manifest)


def _load_index(path):
    return _load_raw(_index_path(path), {})


def _save_index(path, index):
    _atomic_write(_index_path(path), index)


def _safe_filename(key):
    """Map an arbitrary topic key (may be a Jira URL, message id with '/', etc.)
    to a filesystem-safe filename. We hash it so the on-disk name never depends on
    the key's content, avoiding path traversal / invalid filenames. The real key is
    recovered from manifest['topics'] (real_key -> filename)."""
    import hashlib
    return hashlib.sha1((key or "?").encode("utf-8")).hexdigest() + ".json"


def _topic_path(path, key):
    return os.path.join(path, "topics", _safe_filename(key))


def _topic_dir(path):
    return os.path.join(path, "topics")


def _load_topic(path, key):
    """Return the topic dict for `key`, or None. Works in both file and dir mode."""
    if _is_dir_mode(path):
        try:
            with open(_topic_path(path, key), encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, OSError):
            raise RuntimeError(f"topic file corrupt: {_topic_path(path, key)}")
    state = load_state(path)
    return state["topics"].get(key)


def import_topic(path, topic):
    """Import a fully-formed topic dict into the store. Dir mode writes it as a
    topic file; file mode inserts it into the document. Returns the topic."""
    key = topic.get("message_id") or ""
    if not key:
        return None
    _save_topic(path, key, topic)
    return topic


def archive_topic(path, key):
    """Archive a topic record: move it out of the live set. Dir mode: move
    topics/<key>.json to archive/<key>.json and drop from manifest/index. File
    mode: move into the topics_archive sub-dict. Returns True if archived, False
    if the topic didn't exist."""
    topic = _load_topic(path, key)
    if topic is None:
        return False
    if _is_dir_mode(path):
        arch_dir = os.path.join(path, "archive")
        os.makedirs(arch_dir, exist_ok=True)
        _atomic_write(os.path.join(arch_dir, f"{key}.json"), topic)
        _delete_topic(path, key)
    else:
        state = load_state(path)  # file mode: legacy single-doc
        state.setdefault("topics_archive", {})[key] = topic
        state["topics"].pop(key, None)
        _atomic_write(path, state)
    return True


def list_archived_keys(path):
    """Return the keys currently archived (dir mode: archive/ listing; file mode:
    the topics_archive key)."""
    if _is_dir_mode(path):
        try:
            return sorted(fn[:-5] for fn in os.listdir(os.path.join(path, "archive"))
                          if fn.endswith(".json"))
        except OSError:
            return []
    st = load_state(path)
    return list((st.get("topics_archive") or {}).keys())


def _save_topic(path, key, topic):
    """Atomically persist one topic. Dir mode: write topics/<safe>.json, update the
    manifest + hot index. File mode: legacy whole-doc write (for migration source).
    Returns the topic."""
    if _is_dir_mode(path):
        os.makedirs(_topic_dir(path), exist_ok=True)
        topic["updated_at"] = _now_iso()
        _atomic_write(_topic_path(path, key), topic)
        # manifest: register real_key -> safe path
        m = _load_manifest(path)
        m["topics"].setdefault(key, f"topics/{_safe_filename(key)}")
        m["updated_at"] = _now_iso()
        _save_manifest(path, m)
        # hot index: phase/status/pending/updated_at/mr_url for cheap scans
        idx = _load_index(path)
        idx[key] = {
            "phase": topic.get("phase", ""),
            "status": topic.get("status", ""),
            "pending": topic.get("pending"),
            "updated_at": topic.get("updated_at", ""),
            "mr_url": topic.get("mr_url", ""),
            "jira_url": topic.get("jira_url", ""),
        }
        _save_index(path, idx)
        return topic
    # file mode (only used during migration before the fan-out)
    state = load_state(path)
    state["topics"][key] = topic
    state["updated_at"] = _now_iso()
    _atomic_write(path, state)
    return topic


def _delete_topic(path, key):
    """Remove a topic record (used when archiving). Dir mode only."""
    if not _is_dir_mode(path):
        return
    try:
        os.unlink(_topic_path(path, key))
    except OSError:
        pass
    m = _load_manifest(path)
    m["topics"].pop(key, None)
    m["updated_at"] = _now_iso()
    _save_manifest(path, m)
    idx = _load_index(path)
    idx.pop(key, None)
    _save_index(path, idx)


def _set_index_field(path, key, field, value):
    """Update a single hot-index field cheaply (used by scanners that only need
    index values and don't want to read the whole topic)."""
    if not _is_dir_mode(path):
        return
    idx = _load_index(path)
    if key in idx:
        idx[key][field] = value
        if field == "updated_at":
            idx[key]["updated_at"] = _now_iso()
        _save_index(path, idx)


def list_topic_keys(path):
    """Return the topic keys in the store (dir: from the manifest real_key -> filename
    map, since on-disk filenames are hashed; file: dict keys)."""
    if _is_dir_mode(path):
        return sorted((_load_manifest(path).get("topics") or {}).keys())
    return list(load_state(path)["topics"].keys())


def get_topic(path, key):
    return _load_topic(path, key)


def list_topics(path):
    if _is_dir_mode(path):
        topics = [_load_topic(path, k) for k in list_topic_keys(path)]
        topics = [t for t in topics if t is not None]
    else:
        topics = list(load_state(path)["topics"].values())
    topics.sort(key=lambda t: (t.get("updated_at") or ""), reverse=True)
    return topics


def terminal_topic_keys(path):
    if _is_dir_mode(path):
        idx = _load_index(path)
        return [k for k, v in idx.items()
                if (v or {}).get("phase") in TERMINAL_PHASES]
    return [k for k, t in load_state(path)["topics"].items()
            if t.get("phase") in TERMINAL_PHASES]


def migrate(path=DEFAULT_STATE_FILE):
    """Fan out a legacy v1 single-file state into the v2 directory layout. Creates a
    backup at <path>.bak-v1 first; fails without destroying the source on error.
    Idempotent: already-dir or no-source is a no-op. Returns (migrated_count, err)."""
    if _is_dir_mode(path):
        return 0, None
    if not os.path.isfile(path):
        return 0, "no legacy state file to migrate"
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return 0, f"legacy file unreadable: {e}"
    # backup v1
    bak = path + ".bak-v1"
    if not os.path.exists(bak):
        try:
            with open(path, "rb") as src, open(bak, "wb") as dst:
                dst.write(src.read())
        except OSError as e:
            return 0, f"backup failed: {e}"
    # Remove the original FILE at `path` so we can create a DIRECTORY there. The
    # data is safe in the <path>.bak-v1 backup (migrate_rollback restores it).
    try:
        os.unlink(path)
    except OSError as e:
        return 0, f"could not move legacy file aside: {e}"
    os.makedirs(path, exist_ok=True)
    topics = (state.get("topics") or {})
    os.makedirs(_topic_dir(path), exist_ok=True)
    manifest = {"schema_version": SCHEMA_VERSION, "updated_at": state.get("updated_at", _now_iso()),
                "topics": {}}
    for k, t in topics.items():
        if not isinstance(t, dict):
            continue
        _atomic_write(_topic_path(path, k), t)
        manifest["topics"][k] = f"topics/{_safe_filename(k)}"
        idx = _load_index(path)
        idx[k] = {"phase": t.get("phase", ""), "status": t.get("status", ""),
                  "pending": t.get("pending"), "updated_at": t.get("updated_at", ""),
                  "mr_url": t.get("mr_url", ""), "jira_url": t.get("jira_url", "")}
        _save_index(path, idx)
    _save_manifest(path, manifest)
    return len(topics), None


def migrate_rollback(path=DEFAULT_STATE_FILE):
    """Restore the v1 backup, removing the dir layout. Returns (ok, msg)."""
    if not _is_dir_mode(path):
        return False, "not in dir mode; nothing to roll back"
    bak = path + ".bak-v1"
    if not os.path.isfile(bak):
        return False, "no v1 backup found"
    import shutil
    shutil.rmtree(path, ignore_errors=True)
    os.rename(bak, path)
    return True, "restored v1 backup"


# ── Topic record helpers ─────────────────────────────────────────────────────

def _repo_default():
    return {
        "status": "PENDING",
        "error": "",
        "skip_reason": "",
        "result_file": "",
        "repo_url": "",
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
        # Async work queue (arch-D): intent the interaction layer wrote for the
        # Jenkins executor to consume. None when idle; one of an action marker:
        #   {"action":"re_review"} / {"action":"apply","patch":{...}} /
        #   {"action":"push"} / {"action":"rollback"}
        "pending": None,
        # Closed lifecycle (admin/owner or auto-silence): terminal, ignores further replies.
        "closed_by": "",            # actor or "auto"
        "closed_at": "",            # ISO
        "closed_reason": "",        # human/auto note
        # Ownership ledger for bot-created fix-branch MRs (R2): the *iid*s this bot
        # actually created for this topic's fix branch. _close_topic_resources /
        # _create_or_get_mr match against these (authoritative), not just the
        # concatenated branch name, so a same-named MR owned by someone else is
        # never closed/its branch never deleted.
        "fix_mr_iids": [],
        # 交互增强(C3/更新结论): 可持久化的 review 结论覆盖。原始 findings 只存于
        # result_*.json(不可变底版); 这里是"人工修订叠加层", 渲染时合并到方案C 卡。
        # 元素形如 {"ref":"#3"或file,"action":"amend|reclassify|resolve|add",
        #            "severity"?,"issue"?,"suggestion"?,"note"?}
        "review_overrides": [],
    }


# ── CRUD ─────────────────────────────────────────────────────────────────────

def _get_or_create_stub(path, key):
    """Return an existing topic or create-return a fresh stub (unknown keys attach
    early so later probes can fill fields). Dir mode uses _load_topic/_save_topic."""
    topic = _load_topic(path, key)
    if topic is None:
        topic = _topic_default(key, key, "", "", "scan", None, "", "", "")
        _save_topic(path, key, topic)
    return topic


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
    existing = _load_topic(path, message_id)
    if existing is None:
        fresh = _topic_default(message_id, jira_key, project, jira_url, mode,
                               build_number, text_preview, sender_id, sender_name)
        _save_topic(path, message_id, fresh)
        return fresh
    elif mode == "manual" and existing.get("phase") in TERMINAL_PHASES:
        fresh = _topic_default(message_id, jira_key, project, jira_url, "manual",
                               build_number, text_preview, sender_id, sender_name)
        _save_topic(path, message_id, fresh)
        return fresh
    return existing


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
    topic = _load_topic(path, key)
    if topic is None:
        # Unknown key — create a minimal stub so later probes can attach.
        topic = _topic_default(key, key, "", "", "scan", build_number, "", "", "")
        _save_topic(path, key, topic)

    prev = topic.get("phase", "SCANNED")
    # CLOSED is a hard terminal state: nothing may transition out of it (no retry,
    # no re-open). FAILED stays retryable via reset_for_retry().
    if prev == "CLOSED":
        raise ValueError("cannot leave terminal phase CLOSED")
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

    _save_topic(path, key, topic)
    return topic


def set_topic_fields(path, key, **fields):
    """Atomically set one or more arbitrary fields on a topic (dir mode: single-file
    write). Used by callers that previously hand-rolled whole-document read-modify-
    writes (e.g. review_summary, ci_status), which would corrupt concurrent writes.
    Returns the updated topic, or None if the topic doesn't exist."""
    topic = _load_topic(path, key)
    if topic is None:
        return None
    for k, v in fields.items():
        topic[k] = v
    topic["updated_at"] = _now_iso()
    _save_topic(path, key, topic)
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
    topic = _load_topic(path, key)
    if topic is None:
        topic = _topic_default(key, key, "", "", "scan", None, "", "", "")
        _save_topic(path, key, topic)
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
    _save_topic(path, key, topic)
    return topic


def close_topic(path, key, *, closed_by="", reason=""):
    """
    Mark a topic CLOSED (hard terminal state). CLOSED topics ignore further
    replies and are skipped by scan/review. Bypasses transition() on purpose:
    mirrors record_failure by writing phase/status directly. Writes an audit
    entry to approval_log. Returns the topic dict.
    """
    topic = _load_topic(path, key)
    if topic is None:
        return None
    topic["phase"] = "CLOSED"
    topic["status"] = "CLOSED"
    topic["closed_by"] = closed_by or ""
    topic["closed_reason"] = reason or ""
    topic["closed_at"] = _now_iso()
    topic["updated_at"] = _now_iso()
    _save_topic(path, key, topic)
    # Audit trail (append after save so a failed save doesn't double-log).
    append_approval(path, key, closed_by or "auto", "close_topic",
                    target="", result="ok", note=reason or "topic closed")
    return topic


def is_closed(topic):
    """True if a topic record is in the hard-terminated CLOSED state."""
    return bool(topic) and (topic.get("phase") == "CLOSED")


def can_retry(path, key):
    """
    Return True if a failed topic may be retried now (backoff elapsed, not
    exhausted). Also returns True if the topic isn't in a failed state.
    """
    topic = _load_topic(path, key)
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
    now = time.time()
    out = []
    for t in list_topics(path):
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

    CLOSED is a hard terminal state (see transition()): it must never be re-opened
    by a retry/reset, so a CLOSED topic is returned unchanged. FAILED stays
    retryable via this function.
    """
    topic = _load_topic(path, key)
    if topic is None:
        return None
    if topic.get("phase") == "CLOSED":
        # Hard terminal — never reopen. Mirrors the transition() guard.
        return topic
    topic["phase"] = "SCANNED"
    topic["status"] = "RUNNING"
    topic["next_retry_at"] = ""
    # Keep retry_count as a historical counter; failed_exhausted stays until a
    # manual/intervention clears it — but resetting implies the operator opted in,
    # so clear the exhausted flag so it can actually retry.
    topic["failed_exhausted"] = False
    topic["updated_at"] = _now_iso()
    _save_topic(path, key, topic)
    return topic


# ── Per-repo status ──────────────────────────────────────────────────────────

def set_repo(path, key, repo, *, status, error="", skip_reason="",
             result_file="", severity_counts=None, stats="", changed_files=0,
             repo_url=""):
    """Update a repo's status + granular fields. Sets finished_at when the repo
    reaches a terminal state (SUCCESS/SKIPPED/FAILED). repo_url is persisted so
    the executor can later locate this repo's shared checkout."""
    if repo not in VALID_REPOS:
        raise ValueError(f"invalid repo: {repo}")
    if status not in REPO_STATUSES:
        raise ValueError(f"invalid repo status: {status}")

    topic = _load_topic(path, key)
    if topic is None:
        topic = _topic_default(key, key, "", "", "scan", None, "", "", "")
        _save_topic(path, key, topic)

    r = topic["repos"].setdefault(repo, _repo_default())
    r["status"] = status
    if repo_url:
        r["repo_url"] = repo_url
    # SUCCESS 是成功态, 必须清掉历史 SKIPPED/FAILED 残留的 skip_reason/error,
    # 否则重审后 status 变 SUCCESS 但 skip_reason 还带 "branch not remote"(ENG-34409)。
    if status == "SUCCESS":
        r["skip_reason"] = ""
        r["error"] = ""
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
    _save_topic(path, key, topic)
    return topic


# ── Multi-turn agent session (design-3) ──────────────────────────────────────

MAX_CHAT_HISTORY = 40   # cap stored message turns to bound context/cost


def append_chat(path, key, message):
    """
    Append one chat message {"role","content"} to a topic's chat_history.
    Trims to the newest MAX_CHAT_HISTORY entries.
    """
    topic = _load_topic(path, key)
    if topic is None:
        return None
    history = topic.setdefault("chat_history", [])
    if isinstance(history, list):
        history.append(message)
        if len(history) > MAX_CHAT_HISTORY:
            topic["chat_history"] = history[-MAX_CHAT_HISTORY:]
    topic["updated_at"] = _now_iso()
    _save_topic(path, key, topic)
    return topic


def set_pending_patch(path, key, patch):
    """
    Set (or clear) the topic's pending_patch. patch is a dict or None.
    """
    topic = _load_topic(path, key)
    if topic is None:
        return None
    topic["pending_patch"] = patch
    topic["updated_at"] = _now_iso()
    _save_topic(path, key, topic)
    return topic


# ── Async work queue (arch-D: interaction writes intent, Jenkins consumes) ────

def set_pending(path, key, action, patch=None):
    """Write an async action marker for the Jenkins executor to consume. `action`
    is one of: re_review | apply | push | rollback | agent_edit | agent_edit_confirm.
    `patch` is carried only for apply (the suggested diff the executor should
    git-apply) / agent actions (the real actor). Returns the topic."""
    topic = _load_topic(path, key)
    if topic is None:
        return None
    entry = {"action": action}
    if patch:
        entry["patch"] = patch
    topic["pending"] = entry
    topic["updated_at"] = _now_iso()
    _save_topic(path, key, topic)
    return topic


def get_pending(path, key):
    """Return the topic's pending action dict, or None."""
    t = get_topic(path, key)
    return (t or {}).get("pending")


def clear_pending(path, key):
    """Clear the topic's pending action (executor finished). Records nothing extra."""
    topic = _load_topic(path, key)
    if topic is None:
        return None
    if topic.get("pending") is not None:
        topic["pending"] = None
        topic["updated_at"] = _now_iso()
        _save_topic(path, key, topic)
    return topic


def list_pending_topics(path):
    """Return [(key, pending_dict), ...] for every topic with a non-empty pending
    action — this is the queue the Jenkins executor consumes. Uses the hot index so
    this (every tick) is cheap even with many topics."""
    if _is_dir_mode(path):
        idx = _load_index(path)
        out = []
        for k, v in idx.items():
            p = (v or {}).get("pending")
            if p:
                out.append((k, p))
        return out
    out = []
    for k, t in (load_state(path)["topics"] or {}).items():
        p = t.get("pending")
        if p:
            out.append((k, p))
    return out


def topic_lock_context(lock_dir, key):
    """Cross-process, per-topic advisory lock via flock on a shared lock file.

    Both the interaction layer (event server) and the executor (Jenkins) take this
    lock around state + checkout mutations for the SAME topic, so the multi-round
    'maybe apply, maybe not' state is serialized across processes. Yields a file
    object that releases the lock on close/exit.

    Usage:
        with pipeline_state.topic_lock_context(lock_dir, key):
            ... mutate topic state / checkout ...
    """
    import contextlib, fcntl, os as _os
    _os.makedirs(lock_dir, exist_ok=True)
    lock_path = _os.path.join(lock_dir, f"{key}.lock")
    lock_file = open(lock_path, "a+")

    @contextlib.contextmanager
    def _locked():
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            lock_file.close()

    return _locked()


def DEFAULT_LOCK_DIR():
    return "/var/lib/report-server/daily/cr-locks"


def record_fix_mr(path, key, mr_iid):
    """Record that this bot created (or definitively attributed) a fix-branch MR
    with the given GitLab iid for this topic (R2). Used as the authoritative
    ownership check when closing fix MRS / deleting the fix branch, so we never
    touch a same-named MR that belongs to someone else. Idempotent."""
    topic = _load_topic(path, key)
    if topic is None:
        return None
    iids = topic.setdefault("fix_mr_iids", [])
    if isinstance(iids, list) and mr_iid not in iids:
        iids.append(mr_iid)
    topic["updated_at"] = _now_iso()
    _save_topic(path, key, topic)
    return topic


def record_applied_patch(path, key, patch):
    """
    Move a confirmed/executed patch into applied_patches (append) and clear
    pending_patch. patch includes {"file","diff","repo","commit","applied_at"}.
    """
    topic = _load_topic(path, key)
    if topic is None:
        return None
    applied = topic.setdefault("applied_patches", [])
    if isinstance(applied, list):
        applied.append(patch)
    topic["pending_patch"] = None
    topic["updated_at"] = _now_iso()
    _save_topic(path, key, topic)
    return topic


def pop_last_applied_patch(path, key):
    """Remove and return the most recently applied patch (for @撤销)."""
    topic = _load_topic(path, key)
    if topic is None:
        return None
    applied = topic.get("applied_patches") or []
    patch = applied.pop() if applied else None
    topic["updated_at"] = _now_iso()
    _save_topic(path, key, topic)
    return patch


# Keep at most this many audit entries per topic.
MAX_APPROVAL_LOG = 100


def append_approval(path, key, actor, action, target="", result="ok", note=""):
    """
    Append one audit entry to the topic's approval_log.
    Fields: actor, action, target, time, result, note.
    """
    topic = _load_topic(path, key)
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
    _save_topic(path, key, topic)
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

    p = sub.add_parser("migrate", parents=[common],
                       help="Fan out a legacy v1 single-file state into the v2 dir layout")
    p = sub.add_parser("migrate-rollback", parents=[common],
                       help="Restore the v1 backup and remove the v2 dir layout")

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
    elif args.command == "migrate":
        n, err = migrate(path)
        if err:
            print(f"[migrate] {err}", file=sys.stderr)
        else:
            print(f"[migrate] moved {n} topics into dir layout at {path} (backup: {path}.bak-v1)")
    elif args.command == "migrate-rollback":
        ok, msg = migrate_rollback(path)
        print(f"[migrate-rollback] {'ok: ' if ok else 'ERR: '}{msg}")
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