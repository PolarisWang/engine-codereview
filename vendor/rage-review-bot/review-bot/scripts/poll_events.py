"""
Poll and classify Lark event files for the review bot.

Scans the events directory for new event files, filters by chat_id,
classifies each message into actions (new_topic, dev_reply, approver_reply),
and outputs a JSON array of actions to stdout.

Usage:
    python poll_events.py \
        --events-dir <path> \
        --state-file <path> \
        --chat-id <oc_xxx> \
        --approver-id <ou_xxx>
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import event_utils


def parse_args():
    p = argparse.ArgumentParser(description="Poll Lark events for review bot")
    p.add_argument("--events-dir", required=True, help="Directory containing event JSON files")
    p.add_argument("--state-file", required=True, help="Path to state.json")
    p.add_argument("--chat-id", required=True, help="Lark group chat_id to filter")
    p.add_argument("--approver-id", required=True, help="Approver's Lark open_id")
    return p.parse_args()


def load_state(state_file):
    """Load state from file, or create default state."""
    if os.path.exists(state_file):
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"topics": {}, "last_processed_timestamp": 0}


def save_state(state_file, state):
    """Save state to file."""
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def extract_ticket_id(text):
    """Extract a Jira ticket ID (e.g. RAGE-XXXXX) from message text."""
    match = event_utils.TICKET_RE.search(text or "")
    return match.group(0).upper() if match else None


def extract_text_content(event):
    """Extract plain text content from a compact event."""
    # Compact format from lark-cli event +subscribe --compact
    # The content field varies by message type
    content = event.get("content", "")
    if isinstance(content, dict):
        content = content.get("text", "") or str(content)
    content = str(content)

    # Lark web/mobile clients wrap plain replies in <p>...</p> (and may
    # include other inline HTML). Strip tags so keyword matching works
    # against the plain text payload.
    import re as _re
    content = _re.sub(r"<[^>]+>", "", content)
    return content.strip()


def has_bot_mention(event):
    """Detect whether the message @mentions the review bot.

    Lark events carry mentions in a `mentions` list with the user's
    id / id_type. The bot's own id starts with the app prefix `cli_`,
    but user-visible @ tags are resolved to the bot user's open_id
    (looked up via chat member list at start-up and cached in
    state.bot_open_id). To avoid depending on that cache here, we
    accept any mention whose key matches @_user_N in the content AND
    whose resolved id is either the configured bot open_id, OR whose
    name looks like the bot (begins with common bot display names).
    Falls back to a plain substring check for @<bot name> in content
    when structured mention info is missing.
    """
    message = event.get("message", event)
    mentions = message.get("mentions", []) or event.get("mentions", [])
    for m in mentions:
        name = (m.get("name") or "").strip()
        # The team's review bot display name starts with 沐寒 — widen
        # this if you rename the bot.
        if name.startswith("沐寒") or name.startswith("Review"):
            return True
    # Fallback: look for @<bot name> literal in content text
    content = event.get("content", "")
    if isinstance(content, dict):
        content = content.get("text", "")
    if "@沐寒" in str(content):
        return True
    return False


def get_sender_id(event):
    """Extract sender's open_id from event."""
    # Compact format puts sender_id at top level
    flat_sender = event.get("sender_id", "")
    if flat_sender:
        return flat_sender

    # Full format nests sender info
    sender = event.get("sender", {})
    if isinstance(sender, dict):
        sender_id = sender.get("sender_id", {})
        if isinstance(sender_id, dict):
            return sender_id.get("open_id", "")
        return sender.get("open_id", "")
    return ""


def get_chat_id(event):
    """Extract chat_id from event."""
    # Compact format may flatten this
    message = event.get("message", event)
    return message.get("chat_id", "")


def get_thread_id(event):
    """Extract thread_id (topic ID) from event.

    In compact format, topic-starting messages have no thread_id — use
    message_id as the thread identifier instead, since the root message
    IS the thread in Lark topic groups.
    """
    message = event.get("message", event)
    thread = message.get("thread", {})
    if isinstance(thread, dict):
        tid = thread.get("thread_id", "")
        if tid:
            return tid
    # Some events put thread_id at top level
    tid = message.get("thread_id", "")
    if tid:
        return tid
    # Compact format for topic-starting messages: no thread_id.
    # Use root_id if present, otherwise this is a top-level message
    # and we use message_id as the thread identifier.
    root_id = message.get("root_id", "")
    if root_id:
        return root_id
    return ""


def get_message_id(event):
    """Extract message_id from event."""
    message = event.get("message", event)
    return message.get("message_id", "")


def get_event_timestamp(event):
    """Extract timestamp from event file or event data."""
    # Try event create_time first
    ts = event.get("create_time", "")
    if ts:
        try:
            return int(ts)
        except (ValueError, TypeError):
            pass

    # Try send_time
    message = event.get("message", event)
    ts = message.get("create_time", "")
    if ts:
        try:
            return int(ts)
        except (ValueError, TypeError):
            pass

    return 0


def is_topic_start(event):
    """Check if this message starts a new topic (is the first message in a thread)."""
    message = event.get("message", event)
    # In Lark, a topic-starting message has root_id == message_id
    # or the thread is newly created
    root_id = message.get("root_id", "")
    msg_id = get_message_id(event)

    # If root_id equals message_id, this is the topic starter
    if root_id and msg_id and root_id == msg_id:
        return True

    # If no root_id, check if parent_id is empty (top-level message)
    parent_id = message.get("parent_id", "")
    if not parent_id and not root_id:
        return True

    return False


# States where we expect developer replies
REVISION_STATES = {"SIMPLE_REVISION", "FULL_REVISION"}

# States where we expect approver replies
APPROVAL_STATES = {"TRIAGE_DECISION", "AWAITING_APPROVAL"}

# States that mean the topic is actively being processed (race lock)
PROCESSING_STATES = {"TRIAGING", "INLINE_REVIEW", "FULL_REVIEW"}

# Terminal states
TERMINAL_STATES = {"APPROVED", "CLOSED"}


def classify_events(events, state, chat_id, approver_id):
    """Classify events into (actions, deferred_min_ts).

    Returns:
        actions: list of action dicts to dispatch this poll.
        deferred_min_ts: earliest timestamp of an event that could NOT be
            resolved this poll (topic still in a PROCESSING_STATES lock,
            waiting for the dispatcher to finish the previous transition).
            The caller must NOT advance `last_processed_timestamp` past
            this value, otherwise the pending reply is silently lost on
            the next poll. `None` means nothing was deferred.
    """
    actions = []
    topics = state.get("topics", {})
    deferred_min_ts = None

    def defer(ts):
        nonlocal deferred_min_ts
        if ts and (deferred_min_ts is None or ts < deferred_min_ts):
            deferred_min_ts = ts

    for event in events:
        # Filter by chat_id
        event_chat_id = get_chat_id(event)
        if event_chat_id != chat_id:
            continue

        thread_id = get_thread_id(event)
        sender_id = get_sender_id(event)
        message_id = get_message_id(event)
        content = extract_text_content(event)
        event_ts = event.get("_timestamp", 0)

        # Ignore the bot's own messages — chat history reconcile pulls them
        # back in and the bot's review post contains the ticket ID, which
        # would otherwise spawn a phantom new_topic action every poll.
        # Lark bot sender_id is the app ID (cli_xxx) for app-sent messages.
        if sender_id and sender_id.startswith("cli_"):
            continue

        if not thread_id:
            # Compact format: topic-starting messages have no thread_id.
            # Use message_id as the thread identifier — the root message
            # IS the thread in Lark topic groups.
            if message_id:
                thread_id = message_id
            else:
                continue

        topic = topics.get(thread_id)

        if topic is None:
            # Unknown thread — check if it's a new topic with a ticket ID
            if not is_topic_start(event):
                # Reply in an untracked thread — might be a topic we missed
                # Try to extract ticket ID anyway
                ticket_id = extract_ticket_id(content)
                if not ticket_id:
                    continue
            else:
                ticket_id = extract_ticket_id(content)
                if not ticket_id:
                    # No ticket ID — not a review topic, skip
                    continue

            actions.append({
                "action": "new_topic",
                "thread_id": thread_id,
                "ticket_id": ticket_id,
                "creator_open_id": sender_id,
                "message_id": message_id,
                "content": content,
            })

        elif sender_id == topic.get("creator_open_id") and has_bot_mention(event):
            # Developer @mentioned the bot — fires in any state
            # (including CLOSED), because the dev might question a
            # finding at any point. The dispatcher is expected to
            # answer against the current code state, not mechanically
            # re-classify.
            actions.append({
                "action": "dev_question",
                "thread_id": thread_id,
                "message_id": message_id,
                "content": content,
            })

        elif topic.get("state") in TERMINAL_STATES:
            # Topic is done — ignore further messages (non-question)
            continue

        elif topic.get("state") in PROCESSING_STATES:
            # Topic is mid-transition (the dispatcher holds a race lock
            # and hasn't written the resolved state yet). A reply arriving
            # right now must NOT be consumed: the rule for it depends on
            # the state we're about to move into. Defer so the next poll
            # re-reads this event after the dispatcher has updated state.
            defer(event_ts)
            continue

        elif sender_id == approver_id and topic.get("state") in APPROVAL_STATES:
            # Approver replied in a state expecting their input
            actions.append({
                "action": "approver_reply",
                "thread_id": thread_id,
                "content": content,
                "message_id": message_id,
            })

        elif sender_id == topic.get("creator_open_id") and topic.get("state") in REVISION_STATES:
            # Developer replied in a revision-waiting state
            actions.append({
                "action": "dev_reply",
                "thread_id": thread_id,
                "message_id": message_id,
                "content": content,
            })

        # Other messages (e.g., random discussion in thread) are ignored
        # intentionally — no need to defer; they'll never match any rule.

    return actions, deferred_min_ts


def scan_event_files(events_dir, last_timestamp):
    """Scan events directory for new event files."""
    events = []
    events_path = Path(events_dir)

    if not events_path.exists():
        return events

    for f in sorted(events_path.iterdir()):
        if not f.suffix == ".json":
            continue

        # Check file modification time as a rough filter
        file_mtime_ms = int(f.stat().st_mtime * 1000)
        if file_mtime_ms <= last_timestamp:
            continue

        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, IOError):
            continue

        event_ts = get_event_timestamp(data)
        if event_ts <= last_timestamp:
            continue

        # Attach file path and timestamp for tracking
        data["_file"] = str(f)
        data["_timestamp"] = event_ts if event_ts > 0 else file_mtime_ms
        events.append(data)

    # Sort by timestamp
    events.sort(key=lambda e: e.get("_timestamp", 0))
    return events


def main():
    args = parse_args()
    state = load_state(args.state_file)
    last_ts = state.get("last_processed_timestamp", 0)

    # Scan for new events
    events = scan_event_files(args.events_dir, last_ts)

    if not events:
        print(json.dumps([]))
        return

    # Classify into actions
    actions, deferred_min_ts = classify_events(
        events, state, args.chat_id, args.approver_id
    )

    # Advance last_processed_timestamp, but NEVER past a deferred event.
    # A deferred event is a reply that arrived while its topic was mid-
    # transition (PROCESSING_STATES). The dispatcher hasn't written the
    # resolved state yet, so the reply's correct action is unknowable
    # this poll — we must re-scan it next poll.
    event_max_ts = max(e.get("_timestamp", 0) for e in events)
    if deferred_min_ts is not None:
        new_ts = min(event_max_ts, deferred_min_ts - 1)
    else:
        new_ts = event_max_ts
    if new_ts > last_ts:
        state["last_processed_timestamp"] = new_ts
        save_state(args.state_file, state)

    # Output actions
    print(json.dumps(actions, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()