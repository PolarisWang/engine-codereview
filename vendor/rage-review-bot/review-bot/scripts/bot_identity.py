"""Resolve the deployed bot's own open_id and rewrite leading @-mentions.

Background
----------
`reply_parser.classify_intent` matches dev_question/manual_refresh by the
literal `@bot` prefix. Real Lark messages come with a structured
`mentions[]` array — the leading token in `content` is the bot's
*display name* (`@沐寒蒸馏版`, `@Review Bot`, etc.) not the literal
string `@bot`. To match across deployments without hard-coding bot names
(see DESIGN §1.2.2) we need the bot's actual `open_id` (`ou_...`).

The bot's open_id is configured via `REVIEW_BOT_OPEN_ID` in
`.claude/settings.json` / `.claude/settings.local.json`. When unset, this
module auto-learns it from the first P2P DM the operator sends to the
bot — `mentions[0].id` of any such message is unambiguously the bot.
Approver-only + chat_type=p2p gates the learn so we don't grab a random
user's open_id from a noisy group event.
"""

import json
import os
from pathlib import Path


def _mention_open_id(mention):
    """Extract the ``open_id`` from a single Lark mention entry.

    Lark's websocket payload nests ids as
    ``mention["id"] = {"open_id": "ou_...", "user_id": "u-...", "union_id": "on_..."}``;
    compact-mode listeners flatten ``mention["id"]`` to the bare open_id
    string and surface ``mention["open_id"]`` directly. We accept both
    shapes so callers don't have to. Returns ``""`` when no open_id is
    derivable.
    """
    if not isinstance(mention, dict):
        return ""
    ids = mention.get("id")
    if isinstance(ids, dict):
        oid = ids.get("open_id") or ""
        if oid:
            return oid
    elif isinstance(ids, str) and ids.startswith("ou_"):
        return ids
    flat = mention.get("open_id") or ""
    return flat if isinstance(flat, str) else ""


def normalize_bot_mention(content, mentions, bot_open_id):
    """If the message's leading @-mention picks THIS bot, rewrite that
    leading mention to the literal token `@bot` so the existing
    `reply_parser` regexes match across deployments.

    Returns the (possibly rewritten) content string. Safe to call
    unconditionally — when `bot_open_id` is empty, mentions is empty,
    or the leading mention isn't the bot, returns `content` unchanged.

    Narrowing rule: we only touch the SINGLE leading `@<token>` and only
    when the FIRST mention entry's `id` matches `bot_open_id`. This
    avoids the wide-regex footgun where `^@\\S+\\s*` would also strip a
    `@<another-user>` prefix and falsely trigger `dev_question`.
    """
    if not bot_open_id or not content or not isinstance(content, str):
        return content
    if not mentions or not isinstance(mentions, list):
        return content
    first = mentions[0]
    if not isinstance(first, dict):
        return content
    if _mention_open_id(first) != bot_open_id:
        return content
    stripped = content.lstrip()
    if not stripped.startswith("@"):
        return content
    # Replace exactly the leading `@<token>` (up to first whitespace) with `@bot`.
    parts = stripped.split(None, 1)
    rest = parts[1] if len(parts) == 2 else ""
    return f"@bot {rest}" if rest else "@bot"


def maybe_learn_bot_open_id(event, approver_open_ids, project_root,
                             current_bot_open_id):
    """Auto-detect the deployed bot's open_id from a P2P DM event.

    Returns the (possibly newly learned) bot_open_id. When already set,
    returns `current_bot_open_id` unchanged — this is the hot path.

    Trigger conditions (all required, so we never grab the wrong id):
      - `current_bot_open_id` is empty (one-time learn)
      - `event.chat_type == "p2p"` (DM; mentions[0] is unambiguously the bot)
      - `event.sender_id ∈ approver_open_ids` (only the operator can register)
      - `event.mentions` non-empty

    On a successful learn we write `REVIEW_BOT_OPEN_ID` into
    `.claude/settings.local.json` (preferring local over the committed
    template) so the value persists across daemon restarts. We do NOT
    write to the read-only `settings.json` template.
    """
    if current_bot_open_id:
        return current_bot_open_id
    if not isinstance(event, dict):
        return current_bot_open_id
    if event.get("chat_type") != "p2p":
        return current_bot_open_id
    sender_id = event.get("sender_id") or ""
    if not approver_open_ids or sender_id not in approver_open_ids:
        return current_bot_open_id
    mentions = event.get("mentions") or []
    if not mentions or not isinstance(mentions, list):
        return current_bot_open_id
    first = mentions[0]
    if not isinstance(first, dict):
        return current_bot_open_id
    learned = _mention_open_id(first)
    if not learned or not learned.startswith("ou_"):
        return current_bot_open_id

    # Persist into settings.local.json (git-ignored, machine-local).
    settings_path = Path(project_root) / ".claude" / "settings.local.json"
    try:
        if settings_path.is_file():
            with open(settings_path, encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {}
        env = data.setdefault("env", {})
        env["REVIEW_BOT_OPEN_ID"] = learned
        tmp = settings_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, settings_path)
    except (OSError, ValueError):
        # Persist failure shouldn't block runtime — caller can still use
        # the learned value for this cycle and try again next time.
        pass

    return learned
