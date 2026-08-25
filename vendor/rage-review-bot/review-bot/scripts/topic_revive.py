"""Revive a topic that was closed at ack time for a fixable prerequisite.

Three ack-time closes (`mr_not_found`, `3rd_party_mr_not_found`,
`missing_version_3rd_bump`) ask the developer to go create an MR or push a
version bump and come back. Archiving the topic immediately made router
Gate 4a drop that follow-up, so the bot asked a question it could not hear
(RAGE-23816). `try_revive` moves such a topic back to `topics/` and resets it
to `TRIAGING` so the next `ack_dispatch` re-runs discovery against reality.

Bounded by `MAX_REVIVES` — re-running ack re-validates and re-closes when the
prerequisite is still unmet, so an unbounded revive would let idle chatter
re-post the warning forever. See DESIGN §1.3.7.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import topic_index  # noqa: E402
import topic_store  # noqa: E402

MAX_REVIVES = 3


def try_revive(topics_dir, index_path, thread_id):
    """Reopen `thread_id` if it is a revivable ack-time close.

    Returns (revived: bool, reason: str). `reason` is a short machine tag for
    the caller's log line: "revived", "not_closed", "not_revivable",
    "revive_cap", "unreadable", "move_failed".
    """
    topics_dir = Path(topics_dir)
    closed_path = topics_dir / "closed" / f"{thread_id}.json"
    if not closed_path.exists():
        return False, "not_closed"

    topic = topic_store.read_or_none(closed_path)
    if topic is None:
        return False, "unreadable"

    lifecycle = topic.get("lifecycle") or {}
    if not lifecycle.get("revivable"):
        return False, "not_revivable"

    revive_count = int(lifecycle.get("revive_count") or 0)
    if revive_count >= MAX_REVIVES:
        return False, "revive_cap"

    ticket_id = (topic.get("identity") or {}).get("ticket_id") or ""
    closed_reason = lifecycle.get("closed_reason")
    now = topic_store.now_ms()

    review = topic.setdefault("review", {})
    review["state"] = "TRIAGING"
    lifecycle["closed_reason"] = None
    lifecycle["resolved_at"] = None
    lifecycle["ack_sent"] = False
    lifecycle["ack_sent_at"] = None
    lifecycle["revivable"] = False
    lifecycle["revive_count"] = revive_count + 1
    lifecycle["updated_at"] = now
    topic.setdefault("lifecycle", lifecycle)
    topic_store.append_audit(
        topic,
        event="topic_revived",
        from_state="CLOSED",
        to_state="TRIAGING",
        closed_reason=closed_reason,
        revive_count=revive_count + 1,
    )

    # Write the reopened copy first, then drop the archived one — a crash
    # between the two leaves a duplicate (harmless, janitor-visible) rather
    # than losing the topic entirely.
    open_path = topics_dir / f"{thread_id}.json"
    try:
        topic_store.write_atomic(open_path, topic)
        closed_path.unlink()
    except OSError:
        return False, "move_failed"

    topic_index.add(index_path, thread_id, ticket_id)
    return True, "revived"
