# -*- coding: utf-8 -*-
"""Single canonical path for posting the `approval` template.

Three consumers must go through this script:
    1. mechanical_reply_handler._handle_approve    (Phase B2 wires it)
    2. dispatcher approval post (if/when added)    (no current caller; reserved)
    3. spawn_topic_agent.md §2 3rd-party flow      (Phase B3 wires it)

Behavior:
    1. Load topic from --topic.
    2. For each MR, call merge_tracker.recheck_pipeline_with_retry (helper #1).
       Append an `infra_failure_retried` audit entry per retried MR.
    3. Render the approval template with the right PIPELINE_MSG.
    4. Post via lark-cli using render.py --post --message-id <thread_id>.
    5. Append `lark_reply_sent` audit (with triggered_by_event_id=--event-id).
    6. Transition review.state -> APPROVED (if not already), clear pending_action.
    7. write_atomic the topic.

Idempotency: if a prior run already posted for this event_id (audit entry
`lark_reply_sent` with matching triggered_by_event_id), skip step 4 but
still ensure state=APPROVED and write.

Usage:
    python post_approval.py --topic FILE --event-id ID [--from-state STATE]

Exit codes:
    0  posted (or duplicate suppressed) and state is APPROVED
    1  error (network / render / lark); state NOT changed
    2  topic not found / unreadable
"""

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import merge_tracker
import subprocess_util
import topic_store


RENDER_PY = SCRIPT_DIR / "templates" / "render.py"


def _already_posted(topic, event_id):
    """True if a prior cycle already posted approval for this event_id.

    Mirrors mechanical_reply_handler._already_posted — shared dedup key
    across the three approval paths.
    """
    for entry in reversed(topic.get("audit") or []):
        if entry.get("triggered_by_event_id") == event_id:
            if entry.get("event") in ("lark_reply_sent",
                                      "lark_reply_duplicate_suppressed"):
                return True
    return False


def _recheck_all_pipelines(topic):
    """Run recheck_pipeline_with_retry on every MR; append audits; return msg.

    Returns (pipeline_msg, any_failed). Mutates topic["mrs"] in place and
    appends one `infra_failure_retried` audit entry per MR that triggered
    a retry.
    """
    mrs = topic.get("mrs") or {}
    all_passed = True
    any_failed = False
    any_running = False
    for repo, mr_obj in mrs.items():
        if not (mr_obj.get("mr_iid") or mr_obj.get("iid")):
            continue
        result = merge_tracker.recheck_pipeline_with_retry(repo, mr_obj)
        if result.get("retried"):
            topic_store.append_audit(topic,
                event="infra_failure_retried",
                repo=repo,
                reason=result.get("reason", ""),
                job_id=result.get("job_id"),
            )
        status = result.get("status")
        if status == "passed":
            pass
        elif status == "running":
            any_running = True
            all_passed = False
        elif status == "failed":
            any_failed = True
            all_passed = False
        else:
            all_passed = False

    if all_passed:
        msg = "流水线已通过，等待合并队列处理。"
    elif any_failed:
        msg = "流水线失败，请处理后再合并。"  # caller still posts a separate warning
    elif any_running:
        msg = "流水线运行中，完成后将自动合并。"
    else:
        msg = "已批准，等待合并队列处理。"
    return msg, any_failed


def _render_and_post(template_name, variables, message_id):
    """Invoke templates/render.py as subprocess with --post --message-id.

    Returns (ok, response_text).
    """
    vars_blob = json.dumps(variables, ensure_ascii=False)
    cmd = ["python", str(RENDER_PY), template_name,
           "--vars", vars_blob, "--post", "--message-id", message_id]
    try:
        proc = subprocess_util.hidden_run(
            cmd, capture_output=True, text=True,
            timeout=30, encoding="utf-8", errors="replace")
    except (OSError, Exception) as exc:  # noqa: BLE001
        return False, str(exc)[:200]
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()[:300]
    return True, (proc.stdout or "").strip()


def post_approval_on_topic(topic, topic_path, event_id, from_state=None):
    """In-memory canonical approval-post. Mutates `topic` in place and writes.

    Use this form when the caller already holds the topic dict (and its lock) —
    e.g. mechanical_reply_handler which owns the lock for the whole drain
    cycle. The caller passes its live `topic` so both views stay in sync;
    we still write_atomic so a crash between handlers doesn't lose audits.

    Result keys: ok (bool), reason (str), posted (bool), pipeline_failed (bool).
    On ok=False, review.state is NOT changed (caller can retry next cycle);
    pipeline_status + infra_failure_retried audits are still persisted.
    """
    topic_path = Path(topic_path)
    identity = topic.get("identity") or {}
    ticket_id = identity.get("ticket_id", "")
    thread_id = topic.get("thread_id") or topic.get("root_message_id") or ""

    pipeline_msg, any_failed = _recheck_all_pipelines(topic)

    posted = False
    if _already_posted(topic, event_id):
        # Prior cycle posted; ensure state and return.
        pass
    else:
        ok, resp = _render_and_post("approval", {
            "TICKET_ID": ticket_id,
            "PIPELINE_MSG": pipeline_msg or "",
        }, thread_id)
        if not ok:
            # Persist any mutated pipeline_status + infra_failure_retried audits
            # so we don't repeatedly retry the same job on the next cycle.
            topic_store.write_atomic(topic_path, topic)
            return {"ok": False, "reason": f"post_failed: {resp}",
                    "posted": False, "pipeline_failed": any_failed}
        posted = True
        topic_store.append_audit(topic,
            event="lark_reply_sent",
            reply_type="approval",
            triggered_by_event_id=event_id,
            pipeline_failed=any_failed,
        )

    review = topic.setdefault("review", {})
    prior_state = review.get("state")
    if prior_state != "APPROVED":
        review["state"] = "APPROVED"
        review.pop("pending_action", None)
        topic_store.append_audit(topic,
            event="state_transition",
            from_state=from_state or prior_state,
            to_state="APPROVED",
            triggered_by_event_id=event_id,
        )
    topic_store.write_atomic(topic_path, topic)

    return {"ok": True, "reason": "posted" if posted else "duplicate_suppressed",
            "posted": posted, "pipeline_failed": any_failed}


def post_approval(topic_path, event_id, from_state=None):
    """File-path variant: read topic from disk then delegate. CLI uses this."""
    topic_path = Path(topic_path)
    topic = topic_store.read_or_none(topic_path)
    if topic is None:
        return {"ok": False, "reason": "topic_missing", "posted": False,
                "pipeline_failed": False}
    return post_approval_on_topic(topic, topic_path, event_id,
                                  from_state=from_state)


def _cli():
    ap = argparse.ArgumentParser(description="Post the approval Lark template")
    ap.add_argument("--topic", required=True, help="Path to topic JSON file")
    ap.add_argument("--event-id", required=True,
                    help="Approver event_id (for dedup + audit trail)")
    ap.add_argument("--from-state", default=None,
                    help="Prior state for state_transition audit (optional)")
    args = ap.parse_args()

    result = post_approval(args.topic, args.event_id, from_state=args.from_state)
    print(json.dumps(result, ensure_ascii=False))
    if result.get("ok"):
        return 0
    if result.get("reason") == "topic_missing":
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(_cli())
