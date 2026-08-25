"""Shared event helpers for router, dispatcher, and per-topic agents.

These were previously embedded in poll_events.py. Extracted into a
shared module so all v2 components (router.py, dispatcher.py,
migrate_state_to_topics.py) can import the same implementation.
"""

import re


# Jira project keys the bot answers to. RAGE is the game project; CB2N /
# ENG / MARS tickets cover shared, engine and 3rd-party library work whose
# MRs are reviewed in the same group. Keep the list explicit — a generic
# `[A-Z]+-\d+` would match arbitrary prose in a thread reply and open
# phantom topics.
TICKET_PROJECT_KEYS = ("RAGE", "CB2N", "ENG", "MARS")
# The left guard matters now that short keys are in the list: without it
# `ENG-\d+` fires inside a longer word (`ZHENG-12`, `CHALLENG-3`). It must
# exclude `_` and `-` too, not just alphanumerics — those are the usual
# joiners in identifiers and compound words, so a guard without them still
# matches `FOO_ENG-12`, `NON-ENG-3`, `build_MARS-7` and opens phantom topics
# from ordinary chatter.
TICKET_RE = re.compile(r"(?<![A-Za-z0-9_-])(?:%s)-\d+"
                       % "|".join(TICKET_PROJECT_KEYS), re.IGNORECASE)
HTML_TAG_RE = re.compile(r"<[^>]+>")


def extract_ticket_id(text):
    """Return the first ticket id (e.g. RAGE-XXXXX) found in text, or None."""
    if not text:
        return None
    m = TICKET_RE.search(str(text))
    return m.group(0).upper() if m else None


def strip_html(content):
    """Strip inline HTML tags from Lark message content.

    Lark web/mobile clients wrap plain text replies in <p>...</p> (and
    sometimes other inline HTML). A naive keyword match against the
    tagged form fails. Callers should pass stripped content to the
    reply parser.
    """
    if content is None:
        return ""
    return HTML_TAG_RE.sub("", str(content)).strip()


def extract_text_content(event):
    """Return the plain-text content of a Lark event dict.

    Handles the compact and non-compact listener formats uniformly:
    content may be a string, a dict (with 'text' key), or a JSON
    string (wrapped by the post type). Tags are stripped.
    """
    content = event.get("content", "") if isinstance(event, dict) else ""
    if isinstance(content, dict):
        content = content.get("text", "") or ""
    return strip_html(content)


# The bot's Lark display name starts with 沐寒. Mentions via the Lark
# client expose this under the `mentions` array (non-compact listener
# payload) or as `@沐寒...` literal inside the text (fallback).
BOT_NAME_PREFIXES = ("沐寒", "Review")


def extract_assigned_dev(event, fallback_sender_id, fallback_sender_name=None,
                         contacts_path=None):
    """Resolve who the topic should be addressed to (the developer).

    Two input shapes are supported:

    1. **Lark @-mention via picker** (preferred). Lark fills
       ``event["mentions"][i]`` with structured fields. We pick the first
       non-bot mention and read its ``open_id`` from the nested
       ``id`` dict (Lark sometimes returns it as a flat ``open_id`` key
       — both are handled). This is the case the operator hits when
       typing ``RAGE-12345 @<dev>`` and selecting from the picker.

    2. **Literal text ``@<name>``**. When the operator types raw text
       without using the picker, ``mentions`` is empty. We extract the
       first name-like token after a ``@`` (Chinese/letters, no
       whitespace) and look it up in the org-contact cache. The cache
       only stores Lark ``user_id`` (not ``open_id``), so the resolved
       handle may render as text rather than a clickable @-mention —
       the user_id form still gets the dev a notification when Lark
       parses the at-tag, just less reliably than the picker path. If
       the cache lookup misses, we keep the literal name as the display
       label and fall back to the sender for the open_id (so templates
       that require ``DEVELOPER_ID`` still render).

    Args:
        event: Lark event dict (raw mention list and content).
        fallback_sender_id: open_id to use when no assigned dev is found.
        fallback_sender_name: display name to use as final fallback.
        contacts_path: optional override for the org-contacts cache file.

    Returns:
        ``(open_id, display_name, source)`` where ``source`` is one of
        ``"mention"``, ``"text_lookup"``, ``"text_unresolved"``,
        or ``"sender"``.
    """
    # 1. Structured mention.
    mentions = []
    if isinstance(event, dict):
        mentions = event.get("mentions") or []
    for m in mentions:
        if not isinstance(m, dict):
            continue
        name = (m.get("name") or "").strip()
        if not name:
            continue
        # Skip bot mentions (they target review bot, not the dev).
        if any(name.startswith(p) for p in BOT_NAME_PREFIXES):
            continue
        # Lark exposes open_id either as `m["id"]["open_id"]` or
        # flat `m["open_id"]`, depending on listener compact mode.
        open_id = ""
        ids = m.get("id")
        if isinstance(ids, dict):
            open_id = ids.get("open_id") or ""
        if not open_id:
            open_id = m.get("open_id") or ""
        if open_id:
            return open_id, name, "mention"

    # 2. Literal text @<name> after the ticket id.
    text = extract_text_content(event) if isinstance(event, dict) else str(event)
    if text:
        # Match `@姚沐青` / `@yao` after a ticket. `\S` lets us catch
        # CJK + Latin without committing to a charset boundary; we cap
        # at 32 chars so a runaway @ doesn't swallow the rest of the
        # message.
        m = re.search(r"@([^\s@]{1,32})", text)
        if m:
            literal_name = m.group(1).rstrip(".,;:，。；：")
            if literal_name and not any(
                literal_name.startswith(p) for p in BOT_NAME_PREFIXES
            ):
                user_id = _lookup_contact_user_id(
                    literal_name, contacts_path)
                if user_id:
                    return user_id, literal_name, "text_lookup"
                return fallback_sender_id, literal_name, "text_unresolved"

    return fallback_sender_id, fallback_sender_name or "", "sender"


def _lookup_contact_user_id(name, contacts_path=None):
    """Search the org-contacts cache for a row whose Name column equals
    ``name``. Returns the cached ``user_id`` or ``""`` if not found.

    The cache lives under the user-level ``lark-contact-cache`` skill at
    ``~/.claude/skills/lark-contact-cache/cfg/org_contacts.md`` (Markdown
    table, columns: ``Name | user_id | email_name``). We do an exact
    match on the trimmed Name column — partial / fuzzy matches risk
    pinging the wrong person, and the cost of a miss (graceful fallback
    to literal-text rendering) is much lower than a wrong @-target.
    """
    import os
    if contacts_path is None:
        contacts_path = os.path.expanduser(
            "~/.claude/skills/lark-contact-cache/cfg/org_contacts.md")
    try:
        with open(contacts_path, encoding="utf-8") as f:
            for line in f:
                if not line.startswith("|"):
                    continue
                cells = [c.strip() for c in line.split("|")[1:-1]]
                if len(cells) >= 2 and cells[0] == name:
                    return cells[1]
    except OSError:
        pass
    return ""


def has_bot_mention(event):
    """Detect whether a message event @mentions the review bot.

    Works for both compact (no mentions[]) and non-compact (mentions[]
    populated) listener payloads. Non-compact is preferred.
    """
    mentions = []
    if isinstance(event, dict):
        mentions = event.get("mentions") or []
    for m in mentions:
        name = (m.get("name") or "").strip() if isinstance(m, dict) else ""
        for prefix in BOT_NAME_PREFIXES:
            if name.startswith(prefix):
                return True
    # Literal substring fallback (compact mode, or non-structured mentions)
    text = extract_text_content(event) if isinstance(event, dict) else str(event)
    for prefix in BOT_NAME_PREFIXES:
        if f"@{prefix}" in text:
            return True
    return False


def derive_event_id(raw_filename, message_id):
    """Compute a stable event id for a raw event.

    Two files with different names but the same message_id should hash
    to the same event_id so the router's dedupe catches listener +
    reconcile double delivery. Use message_id when present; otherwise
    fall back to the filename stem.
    """
    if message_id:
        return f"msg:{message_id}"
    return f"file:{raw_filename}"


def normalize_listener_event(raw):
    """Flatten a raw Lark websocket event into the router's expected shape.

    The listener writes the full websocket payload which nests message
    fields under event.message and sender under event.sender. Reconcile
    already produces flat dicts. This function makes listener events
    match that same shape so the router handles both uniformly.

    For im.message.recalled_v1 events, returns a dict with
    type="im.message.recalled_v1" and "recalled_message_id" set.

    Returns the flat dict (mutates nothing).
    """
    if not isinstance(raw, dict):
        return raw

    # Already flat (reconcile output or previously normalized)?
    if "sender_id" in raw and "chat_id" in raw and raw.get("_source"):
        return raw

    # Check for recall event BEFORE trying to normalize as a message
    raw_type = raw.get("type", "")
    ev = raw.get("event", raw)
    if isinstance(ev, dict) and ("recalled" in raw_type or "recalled" in ev.get("type", "")):
        # im.message.recalled_v1: payload has message_id at event level
        recalled_mid = ev.get("message_id", "")
        recalled_chat = ev.get("chat_id", "")
        return {
            "type":                 "im.message.recalled_v1",
            "recalled_message_id":  recalled_mid,
            "chat_id":              recalled_chat,
            "message_id":           "",
            "sender_id":            "",
            "thread_id":            "",
            "content":              "",
            "_source":              "listener",
        }

    # Lark websocket payload: nested under 'event'
    ev = raw.get("event", raw)
    msg = ev.get("message", {}) if isinstance(ev, dict) else {}
    sender = ev.get("sender", {}) if isinstance(ev, dict) else {}

    # Extract sender open_id
    sender_id_obj = sender.get("sender_id", {}) if isinstance(sender, dict) else {}
    if isinstance(sender_id_obj, dict):
        sender_id = sender_id_obj.get("open_id", "")
    elif isinstance(sender_id_obj, str):
        sender_id = sender_id_obj
    else:
        sender_id = sender.get("open_id", "") if isinstance(sender, dict) else ""

    # Extract content — may be JSON string for post messages
    content = msg.get("content", "") if isinstance(msg, dict) else ""
    if isinstance(content, str):
        import json as _json
        try:
            parsed = _json.loads(content)
            if isinstance(parsed, dict):
                content = parsed.get("text", content)
        except (ValueError, TypeError):
            pass

    # Extract thread_id for routing.
    # Topic files are keyed by the root message's om_ id, NOT by Lark's
    # omt_ topic thread id. So we prefer root_id (om_ format) over
    # thread.thread_id (omt_ format). For topic-starting messages that
    # have no root_id, message_id is the thread key.
    root_id = msg.get("root_id", "") if isinstance(msg, dict) else ""
    thread_id = root_id  # om_ format — matches topic file keys

    msg_id = msg.get("message_id", "") if isinstance(msg, dict) else ""
    chat_id = msg.get("chat_id", "") if isinstance(msg, dict) else ""

    # Extract mentions
    mentions = msg.get("mentions") or [] if isinstance(msg, dict) else []
    if isinstance(mentions, str):
        import json as _json
        try:
            mentions = _json.loads(mentions)
        except (ValueError, TypeError):
            mentions = []

    return {
        "chat_id":      chat_id,
        "chat_type":    msg.get("chat_type", "group") if isinstance(msg, dict) else "group",
        "content":      content,
        "create_time":  msg.get("create_time", "") if isinstance(msg, dict) else "",
        "id":           msg_id,
        "message_id":   msg_id,
        "thread_id":    thread_id,
        "root_id":      msg.get("root_id", "") if isinstance(msg, dict) else "",
        "parent_id":    msg.get("parent_id", "") if isinstance(msg, dict) else "",
        "message_type": msg.get("message_type", "text") if isinstance(msg, dict) else "text",
        "mentions":     mentions if isinstance(mentions, list) else [],
        "sender_id":    sender_id,
        "timestamp":    raw.get("ts", ""),
        "type":         raw.get("type", ev.get("type", "im.message.receive_v1") if isinstance(ev, dict) else ""),
        "_source":      "listener",
    }


def get_thread_id_from_event(event):
    """Resolve the thread id for an event with graceful fallback.

    Topic files are keyed by the root message's om_ id. Lark's omt_
    topic thread ids must be skipped — they don't match any topic file.

    Precedence: root_id → thread_id (if om_) → parent_id → message_id.
    """
    if not isinstance(event, dict):
        return ""
    # Prefer root_id — always om_ format and matches topic file keys
    root_id = event.get("root_id") or ""
    if root_id:
        return root_id
    # thread_id only if it's om_ (not omt_ which is Lark's topic id)
    thread_id = event.get("thread_id") or ""
    if thread_id and not thread_id.startswith("omt_"):
        return thread_id
    return (
        event.get("parent_id")
        or event.get("message_id")
        or ""
    )
