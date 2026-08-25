# -*- coding: utf-8 -*-
"""Mechanical acknowledgement for `dev_question` events (`@bot <question>`).

Symmetric to `ack_dev_reply.py` but for the question path. When a
developer posts `@bot <question>`, the topic agent eventually answers —
but that's a multi-minute wait (spawn + code reading + freeform reply).
Until the answer lands the thread looks unresponsive, the same failure
mode `ack_dev_reply` fixed for the round N+1 trigger.

This handler races ahead of the agent and posts a brief Chinese ack
(`🤖 已收到 RAGE-XXXXX 的提问，正在查证，稍后回复…`) within ~5s. Unlike
`ack_dev_reply` there is no git work at all — a question implies no new
commits, so the ack is a single template render + Lark post.

Scope:
    pending has a `dev_question` event AND audit lacks
    `dev_question_ack_sent` for that event_id
      → post `ack_dev_question` template, audit `dev_question_ack_sent`
      → the `dev_question` event stays in pending (agent owns the answer)

No state filter: the topic agent answers `dev_question` in any state
(spawn_topic_agent.md "Any state" row), so the ack is valid in any state
an open topic can be in.

DESIGN §1.3.3.
"""
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import ack_new_topic
import topic_store


SKILL_DIR = SCRIPT_DIR.parent
_ACTIVITY_LOG = SKILL_DIR / "cfg" / "activity.log"


def _log(level, msg):
    try:
        with open(_ACTIVITY_LOG, "a", encoding="utf-8") as f:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{ts}] [{level}] ack_dev_question: {msg}\n")
    except OSError:
        pass


def _already_acked(topic, event_id):
    """Idempotency: skip if we already posted an ack for this event_id."""
    for entry in reversed(topic.get("audit") or []):
        if entry.get("triggered_by_event_id") == event_id:
            if entry.get("event") == "dev_question_ack_sent":
                return True
    return False


def _find_dev_question_event(topic):
    """Return the first `dev_question` event in pending, or None."""
    for ev in (topic.get("events") or {}).get("pending") or []:
        if ev.get("intent") == "dev_question" and ev.get("role") == "developer":
            return ev
    return None


def _ack_one_topic(topic, topic_path):
    """Process one topic; return result bucket string or None."""
    event = _find_dev_question_event(topic)
    if event is None:
        return None
    event_id = event.get("event_id") or event.get("message_id") or ""
    if not event_id:
        return None
    if _already_acked(topic, event_id):
        return None

    thread_id = topic.get("thread_id") or topic.get("root_message_id")
    ticket_id = (topic.get("identity") or {}).get("ticket_id") or ""
    if not thread_id or not ticket_id:
        return None

    posted_ok, resp = ack_new_topic._render_and_post(
        "ack_dev_question", {"TICKET_ID": ticket_id}, thread_id)
    if not posted_ok:
        _log("WARN", f"{ticket_id}: ack_dev_question post failed: {resp}")
        return "errors"

    topic_store.append_audit(topic,
        event="lark_reply_sent",
        reply_type="ack_dev_question",
        triggered_by_event_id=event_id)
    topic_store.append_audit(topic,
        event="dev_question_ack_sent",
        triggered_by_event_id=event_id)
    topic_store.write_atomic(topic_path, topic)
    return "posted"


def drain_dev_question_ack(topics_dir, index_path, cycle_id):  # noqa: ARG001
    """Run the dev-question ack handler across all open topics."""
    topics_dir = Path(topics_dir)
    summary = {"checked": 0, "posted": 0, "errors": 0, "skipped_locked": 0,
               "topics_touched": []}
    if not topics_dir.is_dir():
        return summary

    for topic_path in sorted(topics_dir.glob("*.json")):
        if topic_path.name.startswith("."):
            continue
        # Skip the inbox sidecars produced by router.py.
        if topic_path.name.endswith(".inbox.json"):
            continue
        lock_file = topic_path.with_suffix(".lock")
        # Best-effort cooperative lock — same model as ack_dev_reply. If a
        # topic agent is mid-flight on this topic, leave the acking to the
        # next cycle to avoid double-writes racing on `audit[]`.
        if not topic_store.acquire_lock(str(lock_file),
                                         f"ack_dev_question-{cycle_id}",
                                         cycle_id):
            summary["skipped_locked"] += 1
            continue
        try:
            try:
                topic = topic_store.read(str(topic_path))
            except (OSError, ValueError):
                continue
            summary["checked"] += 1
            result = _ack_one_topic(topic, str(topic_path))
            if result == "posted":
                summary["posted"] += 1
                summary["topics_touched"].append(topic_path.stem)
            elif result == "errors":
                summary["errors"] += 1
        finally:
            topic_store.release_lock(str(lock_file))
    return summary
