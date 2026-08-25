"""Honest terminal-state notice for the mechanical reconcile paths.

The *active* merge path (`process_merge_queue`) posts a `merged` notice inline
the moment it merges. But the *passive* reconcile paths historically went
silent: `dispatcher._check_approved_topics` (which catches merges / closes done
outside the bot's own queue — a manual merge, a GitLab-side MR close, or a
catch-up after downtime) and `recover.py` both transitioned a topic to
MERGED / CLOSED and archived it WITHOUT posting anything. A developer watching
only the Lark thread saw it freeze at the last reply while the real state had
moved on — see DESIGN §1.6.11.

This module is the single shared helper those paths call so every terminal
transition is reflected honestly in the thread.

Idempotent: gated on `lifecycle.terminal_notice_posted` so a retried cycle
(post succeeded but archive failed) — or a topic `process_merge_queue` already
notified — never double-posts.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from templates.render import load_template, render, post_to_lark  # noqa: E402


# Terminal review.state -> template name. Only these states get a notice;
# any other (non-terminal) state returns None without posting.
_TEMPLATE_BY_STATE = {"MERGED": "merged", "CLOSED": "mr_closed"}


def post_terminal_notice(topic, new_state):
    """Post a MERGED / CLOSED notice to the topic's thread, exactly once.

    Args:
        topic: the in-memory topic dict (mutated: lifecycle.terminal_notice_posted
               is set to True on a successful post so the caller's subsequent
               write_atomic persists the idempotency flag).
        new_state: the terminal state just decided ("MERGED" or "CLOSED").

    Returns:
        (ok, response) from the post attempt, or None when skipped (already
        posted, unknown/non-terminal state, or no root message id to reply to).
    """
    template_name = _TEMPLATE_BY_STATE.get(new_state)
    if template_name is None:
        return None

    lifecycle = topic.setdefault("lifecycle", {})
    if lifecycle.get("terminal_notice_posted"):
        return None

    root_message_id = topic.get("root_message_id") or topic.get("thread_id")
    if not root_message_id:
        return None

    ticket_id = (topic.get("identity") or {}).get("ticket_id", "")
    rendered = render(load_template(template_name), {"TICKET_ID": ticket_id})
    ok, response = post_to_lark(rendered, root_message_id)
    if ok:
        lifecycle["terminal_notice_posted"] = True
    return ok, response
