# -*- coding: utf-8 -*-
"""Drain agent-written reply artifacts into Lark posts.

Topic agents write reply intents as JSON artifacts in cfg/replies/, the
dispatcher consumes them in-process every cycle. This decouples agent
reasoning from the post side-effect: the agent decides *what* to say, the
script decides *where* to post (always the artifact's `thread_id`, full
stop, no ticket-id reverse lookups).

Why: closes the bug class where a long-running agent, mid-reasoning,
conflates multiple topics for the same ticket and posts to the wrong
thread (see DESIGN.md §1.19 / RAGE-13296 incident).

Artifact schema (v1):

    {
      "version": 1,
      "thread_id": "om_...",                  // post target — script
                                              //   trusts this verbatim
                                              //   and only checks the
                                              //   topic file exists
      "ticket_id": "RAGE-12345",
      "triggered_by_event_id": "msg:...",     // dedup + audit
      "template": "review_round1",            // must be in allowlist
      "vars": { ... },                        // template placeholders
      "preflight": {
        "expected_state": "TRIAGING",
        "expected_phase": null                // null = main; "3rd_party"
                                              //   forces phase match
      },
      "post_actions": [
        {"type": "set_state", "to": "AWAITING_APPROVAL"},
        {"type": "set_review_field", "key": "last_review_commit",
         "value": "abc123"}
      ],
      "orphan_doc_token": null                // when 230011 fires, the
                                              //   script deletes this
                                              //   Lark doc to keep the
                                              //   workspace tidy
    }

Allowed `template` values: see ``_ALLOWED_TEMPLATES``. Mechanical
templates (approval, revision_request, dev_triage_summary,
handoff_summary, close-notice, merged, no_new_commits) keep their
direct-post path through
``mechanical_reply_handler`` / ``post_approval`` / ``process_merge_queue``;
this dispatcher only handles agent-generated replies.

Allowed ``post_actions[*].type`` values: see ``_ALLOWED_ACTION_TYPES``.

CLI:
    python reply_dispatcher.py --topics-dir ... --replies-dir ...
                               --cycle-id manual
"""
import argparse
import datetime
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import subprocess_util
import topic_store


SKILL_DIR = SCRIPT_DIR.parent
RENDER_PY = SCRIPT_DIR / "templates" / "render.py"
RETRY_COUNTS_FILE = SKILL_DIR / "cfg" / "replies" / ".retry_counts.json"
QUARANTINE_DIR = SKILL_DIR / "cfg" / "replies" / "quarantine"
ORPHAN_DOC_QUARANTINE_FILE = SKILL_DIR / "cfg" / "orphan_doc_quarantine.jsonl"
RETRY_MAX = 3  # quarantine after this many failures on the same artifact

# Strict allowlist. Anything else in the artifact's `template` field is
# rejected without rendering, preventing an agent from coercing the script
# into posting an arbitrary template payload.
_ALLOWED_TEMPLATES = {
    "review_round1",
    "review_round1_dev_triage",
    "review_roundN",
    "freeform_reply",
}

# Strict allowlist for post_actions[*].type. New action types must be
# explicitly added here AND have a corresponding _apply_post_action arm.
_ALLOWED_ACTION_TYPES = {
    "set_state",
    "set_review_field",
}


# ---- Artifact validation -------------------------------------------------

def _validate_artifact(data):
    """Return (ok, reason). Strict schema check before any side effect."""
    if not isinstance(data, dict):
        return False, "artifact_not_object"
    if data.get("version") != 1:
        return False, f"unsupported_version: {data.get('version')!r}"
    for key in ("thread_id", "ticket_id", "triggered_by_event_id",
                "template", "vars"):
        if key not in data:
            return False, f"missing_field: {key}"
    if data["template"] not in _ALLOWED_TEMPLATES:
        return False, f"template_not_allowed: {data['template']}"
    if not isinstance(data.get("vars"), dict):
        return False, "vars_not_object"
    actions = data.get("post_actions") or []
    if not isinstance(actions, list):
        return False, "post_actions_not_list"
    for action in actions:
        if not isinstance(action, dict):
            return False, "post_action_not_object"
        atype = action.get("type")
        if atype not in _ALLOWED_ACTION_TYPES:
            return False, f"action_type_not_allowed: {atype}"
    preflight = data.get("preflight") or {}
    if not isinstance(preflight, dict):
        return False, "preflight_not_object"
    return True, ""


def _preflight_ok(topic, preflight):
    """True if the topic's current phase/state matches what the agent saw
    when writing the artifact. Drift means the artifact is stale."""
    if not preflight:
        return True, ""
    review = topic.get("review") or {}
    expected_state = preflight.get("expected_state")
    if expected_state is not None and review.get("state") != expected_state:
        return False, (f"state_drift: expected={expected_state} "
                       f"actual={review.get('state')}")
    expected_phase = preflight.get("expected_phase")
    # Treat null/absent as "main" since legacy topics don't always
    # populate review_phase.
    actual_phase = review.get("review_phase") or "main"
    expected_phase = expected_phase or "main"
    if expected_phase != actual_phase:
        return False, (f"phase_drift: expected={expected_phase} "
                       f"actual={actual_phase}")
    return True, ""


# ---- Lark posting --------------------------------------------------------

def _summarize_subprocess_error(stderr, stdout):
    """Extract the actionable line from a subprocess failure.

    Python tracebacks bury the actual error several lines deep; a flat
    `[:300]` truncation typically captured `Traceback (most recent call
    last):\n  File "..."\n  File "..."` and stopped before the
    `ValueError: …` line that names the missing var. This walks the
    stderr in reverse for the first non-blank line that looks like
    `Type: message`, which is what Python prints last. Falls back to
    a 600-char prefix of stderr+stdout when no such line exists.
    """
    text = (stderr or "") + ("\n" if stderr and stdout else "") + (stdout or "")
    # Reverse-walk for the "ErrorType: message" line. Line must contain
    # ": " and start with an alpha-token followed by "Error" / "Exception" /
    # "Warning" so we don't return a path-with-colons line by accident.
    candidates = []
    for line in (text.splitlines() if text else []):
        s = line.strip()
        if not s:
            continue
        if ": " not in s:
            continue
        head = s.split(": ", 1)[0].strip()
        if not head or " " in head:
            continue
        if "Error" in head or "Exception" in head or "Warning" in head:
            candidates.append(s)
    if candidates:
        # Last actionable line — Python prints the most-derived exception last.
        return candidates[-1][:500]
    return text.strip()[:600]


def _render_and_post(template_name, variables, message_id):
    """Render via render.py --post --message-id; returns (status, response).

    status:
        "ok"            — posted successfully
        "withdrawn"     — root message gone (Lark code 230011)
        "error"         — transient or permanent failure; retry next cycle
    """
    vars_blob = json.dumps(variables, ensure_ascii=False)
    cmd = ["python", str(RENDER_PY), template_name,
           "--vars", vars_blob, "--post", "--message-id", message_id]
    try:
        proc = subprocess_util.hidden_run(
            cmd, capture_output=True, text=True,
            timeout=30, encoding="utf-8", errors="replace")
    except (OSError, Exception) as exc:  # noqa: BLE001
        return "error", str(exc)[:200]
    combined = (proc.stdout or "") + (proc.stderr or "")
    if "230011" in combined or "message was withdrawn" in combined.lower():
        return "withdrawn", combined.strip()[:300]
    if proc.returncode != 0:
        return "error", _summarize_subprocess_error(proc.stderr, proc.stdout)
    return "ok", (proc.stdout or "").strip()


def _delete_orphan_doc(doc_token):
    """Delete a Lark doc by token via the raw OpenAPI. Best-effort.

    Returns (ok, err). The previous version called
    ``lark-cli drive files delete`` — that subcommand DOES NOT EXIST in
    the lark-cli build (`drive files` only exposes `copy`), so every
    invocation exited 1 with a help-text dump and the doc was never
    deleted.

    Switching to the raw ``api DELETE /open-apis/drive/v1/files/<token>?type=docx``
    is the documented endpoint, but in practice the lark-cli user
    identity in this workspace lacks the ``drive:drive`` scope, so this
    path will ALSO fail silently (exit 1, empty stderr) until the
    operator grants the scope. ``_quarantine_orphan_doc`` writes a
    JSONL line on every failure so an operator can clean up manually
    even before the scope is granted. See DESIGN.md §3.x (orphan-doc
    cleanup) for the scope grant procedure.
    """
    if not doc_token:
        return True, ""
    cmd = subprocess_util.lark_cli_argv_prefix() + [
        "api", "DELETE",
        f"/open-apis/drive/v1/files/{doc_token}",
        "--params", json.dumps({"type": "docx"}),
        "--as", "user",  # bot lacks drive:drive; user is the doc owner
    ]
    try:
        proc = subprocess_util.hidden_run(
            cmd, capture_output=True, text=True,
            timeout=20, encoding="utf-8", errors="replace")
    except (OSError, Exception) as exc:  # noqa: BLE001
        return False, str(exc)[:200]
    if proc.returncode != 0:
        # CLI swallows stderr on this endpoint when the scope is missing —
        # treat any non-zero as failure and let the caller quarantine.
        msg = (proc.stderr or proc.stdout or "").strip()[:200]
        return False, msg or "exit_nonzero_no_stderr"
    return True, ""


def _quarantine_orphan_doc(doc_token, ticket_id, thread_id, reason):
    """Append one JSONL line per failed orphan-doc delete attempt.

    Operators can grep this file for tokens to clean up manually (or run
    a sweeper once `drive:drive` scope is granted). Idempotent on
    re-call: each attempt adds a fresh line so retry history is visible.
    """
    if not doc_token:
        return
    try:
        ORPHAN_DOC_QUARANTINE_FILE.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "doc_token": doc_token,
            "ticket_id": ticket_id,
            "thread_id": thread_id,
            "reason": reason,
            "ts": topic_store.now_ms(),
        }
        with open(ORPHAN_DOC_QUARANTINE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # quarantine logging is observability, never load-bearing


def _load_retry_counts():
    if not RETRY_COUNTS_FILE.exists():
        return {}
    try:
        with open(RETRY_COUNTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_retry_counts(counts):
    try:
        topic_store.write_atomic(RETRY_COUNTS_FILE, counts)
    except OSError:
        pass  # transient — counts re-derive next cycle


def _quarantine_artifact(art_path, reason, retries):
    """Move a poison artifact to cfg/replies/quarantine/ so it stops
    retrying every cycle. Idempotent — destination collision overwrites."""
    try:
        QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
        dest = QUARANTINE_DIR / art_path.name
        if dest.exists():
            try:
                dest.unlink()
            except OSError:
                pass
        art_path.rename(dest)
        _log("WARN", f"reply_artifact_quarantined {art_path.name} "
                     f"retries={retries} reason={reason[:200]}")
        return dest
    except OSError as exc:
        _log("WARN", f"reply_artifact_quarantine_failed {art_path.name}: {exc}")
        return None


# ---- Post-action application --------------------------------------------

def _apply_post_action(topic, action):
    """Mutate `topic` per a single post_action. Returns (ok, audit_kv).

    Each arm covers one entry in _ALLOWED_ACTION_TYPES. Adding a new
    action type means: (a) add it to the allowlist, (b) add an arm
    here, (c) document it in the schema docstring at the top of this
    module.
    """
    atype = action.get("type")
    if atype == "set_state":
        new_state = action.get("to")
        if not new_state or not isinstance(new_state, str):
            return False, {"error": "set_state_missing_to"}
        review = topic.setdefault("review", {})
        from_state = review.get("state")
        review["state"] = new_state
        return True, {"action": "set_state",
                      "from_state": from_state, "to_state": new_state}
    if atype == "set_review_field":
        key = action.get("key")
        if not key or not isinstance(key, str):
            return False, {"error": "set_review_field_missing_key"}
        value = action.get("value")
        topic.setdefault("review", {})[key] = value
        return True, {"action": "set_review_field", "key": key}
    return False, {"error": f"unknown_action_type: {atype}"}


# ---- Drain --------------------------------------------------------------

def _attempt_artifact_donation(topics_dir, replies_dir, index_path,
                               art_path, data, cycle_id, summary):
    """Best-effort: when an artifact's `thread_id` no longer resolves
    to an open topic, route it to the canonical-for-ticket topic if
    there is one and the closed src topic was superseded.

    Returns True iff the artifact has been HANDLED (donation succeeded
    and the artifact was rewritten/removed, OR donation was deferred
    because the canonical topic is locked — in which case we leave
    the artifact in place for next cycle and report `lock_skipped`).
    Returns False iff the caller should fall through to the legacy
    `thread_missing` path (delete + log).

    Updates `summary` counters in place.
    """
    src_thread_id = data["thread_id"]
    ticket_id = data["ticket_id"]
    template = data["template"]
    closed_path = topics_dir / "closed" / f"{src_thread_id}.json"

    # Need the closed src topic to (a) parse `superseded_by` and
    # (b) extract the donation payload (issues, lark_doc_token, etc.).
    src_topic = topic_store.read_or_none(closed_path)
    if src_topic is None:
        return False
    canonical = topic_store.parse_superseded_by(src_topic)
    if not canonical:
        return False

    # Confirm the canonical thread is actually open before donating.
    canonical_path = topics_dir / f"{canonical}.json"
    if not canonical_path.exists():
        # Canonical was itself superseded or withdrawn — give up,
        # fall through to legacy thread_missing.
        return False

    src_payload = topic_store.extract_donation_payload(src_topic)
    result = topic_store.donate_review_to_topic(
        topics_dir, canonical, src_payload,
        src_thread_id=src_thread_id,
        cycle_id=cycle_id,
        log_fn=_log,
    )
    status = result.get("status")
    if status == "donated":
        # Rewrite the artifact for the canonical topic. The dst's root
        # event_id is `msg:<canonical>` (router convention); use it as
        # the new triggered_by_event_id so dedup works.
        new_event_id = f"msg:{canonical}"
        new_filename = (
            f"{canonical}__{new_event_id.replace(':', '__')}.json")
        new_path = replies_dir / new_filename
        new_data = dict(data)
        new_data["thread_id"] = canonical
        new_data["triggered_by_event_id"] = new_event_id
        # The donation already drained the canonical's pending root
        # event AND wrote review.* fields, so the dispatcher can post
        # next cycle without further preflight surprises. Reset
        # preflight expectations to match the donated state.
        canonical_topic = topic_store.read_or_none(canonical_path) or {}
        canonical_review = canonical_topic.get("review") or {}
        new_data["preflight"] = {
            "expected_state": canonical_review.get("state"),
            "expected_phase": canonical_review.get("review_phase"),
        }
        try:
            topic_store.write_atomic(new_path, new_data)
        except OSError as exc:
            _log("WARN", f"donation_artifact_write_failed "
                         f"{art_path.name}->{new_filename}: {exc}")
            return False  # let caller fall back to thread_missing
        # Remove the original artifact only after the new one lands.
        _safe_unlink(art_path)
        summary["donated"] = summary.get("donated", 0) + 1
        _log("INFO", f"reply_donated {ticket_id} "
                     f"src={src_thread_id} dst={canonical} "
                     f"template={template} -> {new_filename}")
        return True
    if status == "locked":
        # Canonical lock held by another agent — leave the original
        # artifact in place for next cycle, count as lock_skipped (NOT
        # thread_missing, since we DO want to retry).
        summary["lock_skipped"] += 1
        _log("INFO", f"reply_donation_locked {ticket_id} "
                     f"src={src_thread_id} dst={canonical} "
                     f"— retry next cycle")
        return True
    if status == "sha_mismatch":
        # Findings would be against a stale SHA — donating them would
        # mislead the approver. Fall through; legacy path deletes the
        # artifact and we orphan-doc-quarantine the Lark doc.
        doc_token = data.get("orphan_doc_token") or ""
        if doc_token:
            _quarantine_orphan_doc(
                doc_token, ticket_id, src_thread_id,
                f"sha_mismatch_donation: src={result.get('src_sha')} "
                f"dst={result.get('dst_shas')}")
        _log("INFO", f"reply_donation_sha_mismatch {ticket_id} "
                     f"src={src_thread_id} dst={canonical} "
                     f"src_sha={result.get('src_sha')} "
                     f"dst_shas={result.get('dst_shas')}")
        return False
    # missing_dst / error — fall through to legacy thread_missing.
    _log("WARN", f"reply_donation_failed {ticket_id} "
                 f"src={src_thread_id} dst={canonical} "
                 f"status={status} reason={result.get('reason')}")
    return False


def drain_replies(topics_dir, replies_dir, index_path, cycle_id):
    """Walk replies_dir, post each artifact, mutate the matching topic.

    Each artifact is processed at most once per call; on transient error
    it stays on disk for the next cycle. On withdrawn-root or schema
    rejection the artifact is deleted (no infinite retry on poison input).

    Returns a summary dict.
    """
    topics_dir = Path(topics_dir)
    replies_dir = Path(replies_dir)
    summary = {"posted": 0, "withdrawn": 0, "drift_skipped": 0,
               "schema_rejected": 0, "thread_missing": 0,
               "lock_skipped": 0, "errors": 0,
               "donated": 0, "quarantined": 0,
               "topics_touched": 0,
               "failed_artifacts": []}
    if not replies_dir.exists():
        return summary

    artifacts = sorted(
        [p for p in replies_dir.iterdir()
         if p.is_file() and p.name.endswith(".json")
         and not p.name.startswith(".")],
        key=lambda p: p.stat().st_mtime,
    )
    if not artifacts:
        return summary

    retry_counts = _load_retry_counts()
    retry_counts_dirty = False

    holder = f"reply-{cycle_id}"
    for art_path in artifacts:
        # 1. Load + schema check.
        try:
            with open(art_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            summary["schema_rejected"] += 1
            _log("WARN", f"reply_artifact_unreadable {art_path.name}: {exc}")
            _safe_unlink(art_path)
            retry_counts.pop(art_path.name, None)
            retry_counts_dirty = True
            continue

        ok, reason = _validate_artifact(data)
        if not ok:
            summary["schema_rejected"] += 1
            _log("WARN", f"reply_artifact_invalid {art_path.name}: {reason}")
            _safe_unlink(art_path)
            retry_counts.pop(art_path.name, None)
            retry_counts_dirty = True
            continue

        thread_id = data["thread_id"]
        ticket_id = data["ticket_id"]
        event_id = data["triggered_by_event_id"]
        template = data["template"]
        topic_path = topics_dir / f"{thread_id}.json"

        # 2. Topic existence — refuse to post to a nonexistent thread.
        if not topic_path.exists():
            # Try work-donation first: if the closed src topic was
            # superseded, route the artifact to the canonical successor
            # for this ticket instead of throwing the work away.
            handled = _attempt_artifact_donation(
                topics_dir, replies_dir, Path(index_path),
                art_path, data, cycle_id, summary)
            if not handled:
                summary["thread_missing"] += 1
                _log("WARN", f"reply_thread_missing {art_path.name} "
                             f"thread={thread_id} ticket={ticket_id}")
                _safe_unlink(art_path)
                retry_counts.pop(art_path.name, None)
                retry_counts_dirty = True
            else:
                # Donation either rewrote the artifact in-place under a
                # new filename (success) or left it in place (locked
                # canonical, retry next cycle). The helper updates
                # summary counters; nothing else to do here.
                retry_counts.pop(art_path.name, None)
                retry_counts_dirty = True
            continue

        # 3. Lock acquisition.
        if not topic_store.acquire_lock(topic_path, holder, cycle_id):
            summary["lock_skipped"] += 1
            continue
        summary["topics_touched"] += 1

        try:
            topic = topic_store.read(topic_path)

            # 4. Preflight check — skip stale artifacts.
            ok, reason = _preflight_ok(topic, data.get("preflight") or {})
            if not ok:
                topic_store.append_audit(topic,
                    event="reply_drift_skipped",
                    reason=reason,
                    template=template,
                    triggered_by_event_id=event_id,
                )
                topic_store.write_atomic(topic_path, topic)
                summary["drift_skipped"] += 1
                _safe_unlink(art_path)
                retry_counts.pop(art_path.name, None)
                retry_counts_dirty = True
                _log("INFO", f"reply_drift_skipped {ticket_id} "
                             f"thread={thread_id}: {reason}")
                continue

            # 4b. Dedup — if a prior cycle already posted the *same template*
            # for this event, delete the artifact silently. Match on
            # template too: ack_new_topic and review_round1 share the
            # root new-topic event_id, but they are distinct posts.
            if _already_posted(topic, event_id, template):
                topic_store.append_audit(topic,
                    event="reply_artifact_duplicate_suppressed",
                    template=template,
                    triggered_by_event_id=event_id,
                )
                topic_store.write_atomic(topic_path, topic)
                _safe_unlink(art_path)
                retry_counts.pop(art_path.name, None)
                retry_counts_dirty = True
                continue

            # 5. Render + post.
            status, resp = _render_and_post(
                template, data["vars"], thread_id)

            if status == "withdrawn":
                # Root message gone. Close the topic, delete the orphan
                # doc (if any), and drop the artifact.
                doc_token = data.get("orphan_doc_token") or ""
                if doc_token:
                    ok_doc, err_doc = _delete_orphan_doc(doc_token)
                    if not ok_doc:
                        _log("WARN", f"orphan_doc_delete_failed "
                                     f"{ticket_id} doc={doc_token}: {err_doc}")
                        _quarantine_orphan_doc(
                            doc_token, ticket_id, thread_id,
                            f"withdrawn_root: {err_doc}")
                topic_store.append_audit(topic,
                    event="reply_root_withdrawn",
                    template=template,
                    triggered_by_event_id=event_id,
                    orphan_doc_token=doc_token or None,
                )
                from_state = (topic.get("review") or {}).get("state")
                topic.setdefault("review", {})["state"] = "CLOSED"
                ls = topic.setdefault("lifecycle", {})
                ls["resolved_at"] = topic_store.now_ms()
                ls["closed_reason"] = "root_message_withdrawn"
                topic_store.append_audit(topic,
                    event="topic_closed",
                    from_state=from_state,
                    to_state="CLOSED",
                    reason="root_message_withdrawn",
                    triggered_by_event_id=event_id,
                )
                topic_store.write_atomic(topic_path, topic)
                # archive_topic moves topic_path under closed/ and
                # drops it from the open index.
                topic_store.archive_topic(
                    topics_dir, index_path, thread_id)
                _safe_unlink(art_path)
                retry_counts.pop(art_path.name, None)
                retry_counts_dirty = True
                summary["withdrawn"] += 1
                _log("INFO", f"reply_root_withdrawn {ticket_id} "
                             f"thread={thread_id}")
                continue

            if status != "ok":
                summary["errors"] += 1
                summary["failed_artifacts"].append({
                    "filename": art_path.name,
                    "ticket": ticket_id,
                    "template": template,
                    "thread_id": thread_id,
                    "error": (resp or "")[:300],
                })
                _log("WARN", f"reply_post_error {ticket_id} "
                             f"thread={thread_id} template={template}: {resp}")
                # Bump retry count; quarantine if we've hit RETRY_MAX so
                # the same poison artifact doesn't loop forever (Bug 3
                # SUMMARY-missing case retried for ~16 min before manual
                # patch).
                count = retry_counts.get(art_path.name, 0) + 1
                retry_counts[art_path.name] = count
                retry_counts_dirty = True
                if count >= RETRY_MAX:
                    moved_to = _quarantine_artifact(
                        art_path,
                        f"{template}_render_or_post_failed: {resp}",
                        count)
                    if moved_to is not None:
                        summary["quarantined"] += 1
                        retry_counts.pop(art_path.name, None)
                        topic_store.append_audit(topic,
                            event="reply_artifact_quarantined",
                            template=template,
                            triggered_by_event_id=event_id,
                            retries=count,
                            last_error=(resp or "")[:300],
                            moved_to=str(moved_to),
                        )
                        topic_store.write_atomic(topic_path, topic)
                # Leave artifact on disk for next cycle (unless quarantined).
                continue

            # 6. Successful post — write audit, apply post_actions,
            # then delete the artifact.
            topic_store.append_audit(topic,
                event="lark_reply_sent",
                reply_type=template,
                triggered_by_event_id=event_id,
            )
            applied = []
            for action in (data.get("post_actions") or []):
                action_ok, audit_kv = _apply_post_action(topic, action)
                if not action_ok:
                    summary["errors"] += 1
                    _log("WARN", f"post_action_failed {ticket_id}: "
                                 f"{audit_kv}")
                    # Action failed but post landed — keep the audit
                    # truthful and bail on further actions for this
                    # artifact (operator can re-trigger if needed).
                    topic_store.append_audit(topic,
                        event="post_action_error",
                        triggered_by_event_id=event_id,
                        **{k: v for k, v in audit_kv.items()
                           if k != "error"},
                        error=audit_kv.get("error", ""),
                    )
                    break
                applied.append(audit_kv)
            for kv in applied:
                topic_store.append_audit(topic,
                    event="reply_post_action_applied",
                    triggered_by_event_id=event_id,
                    **kv,
                )
            topic.setdefault("lifecycle", {})["updated_at"] = topic_store.now_ms()
            topic_store.write_atomic(topic_path, topic)
            _safe_unlink(art_path)
            retry_counts.pop(art_path.name, None)
            retry_counts_dirty = True
            summary["posted"] += 1
            _log("INFO", f"reply_posted {ticket_id} thread={thread_id} "
                         f"template={template}")
        finally:
            topic_store.release_lock(topic_path)

    if retry_counts_dirty:
        _save_retry_counts(retry_counts)
    return summary


def _already_posted(topic, event_id, template):
    """True if a prior cycle already posted *the same template* for this
    event_id. Match on template too — multiple distinct posts can legitimately
    share an event_id (e.g. ack_new_topic and review_round1 both keyed off
    the root new-topic message). Only an actual `lark_reply_sent` counts as
    evidence; `lark_reply_duplicate_suppressed` does not (suppression is
    informational, never a substitute for a successful post — counting it
    would re-suppress on retry if a prior suppression was itself buggy)."""
    for entry in reversed(topic.get("audit") or []):
        if entry.get("triggered_by_event_id") != event_id:
            continue
        if entry.get("event") == "lark_reply_sent" \
                and entry.get("reply_type") == template:
            return True
    return False


def _safe_unlink(path):
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _log(level, message):
    try:
        log_path = SKILL_DIR / "cfg" / "activity.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [{level}] {message}\n")
    except OSError:
        pass


# ---- CLI -----------------------------------------------------------------

def _cli():
    ap = argparse.ArgumentParser(description="Drain reply artifacts.")
    ap.add_argument("--topics-dir", required=True)
    ap.add_argument("--replies-dir", required=True)
    ap.add_argument("--index", required=True)
    ap.add_argument("--cycle-id", default="manual")
    args = ap.parse_args()
    result = drain_replies(args.topics_dir, args.replies_dir,
                           args.index, args.cycle_id)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
