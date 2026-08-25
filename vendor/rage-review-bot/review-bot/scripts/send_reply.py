# -*- coding: utf-8 -*-
"""Send a reply to a Lark thread, as the BOT.

Usage:
    python send_reply.py --message-id MSG_ID --text "plain text"
    python send_reply.py --message-id MSG_ID --post '{"zh_cn":{...}}'

NOT a way to drive a topic. This posts with `--as bot`, and
`router._is_self_message` drops every message sent by the bot's own open_id
before classification — a `1 3 5` or `done` sent through here never becomes an
event and the topic does not move. An outside agent replying on a developer's
behalf must post as a *user*; see `reference/agent-topic-reply.md` (DESIGN §1.25).
"""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import subprocess_util


def main():
    # Parse arguments
    msg_id = None
    msg_type = None
    content = None

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--message-id" and i + 1 < len(args):
            msg_id = args[i + 1]
            i += 2
        elif args[i] == "--text" and i + 1 < len(args):
            msg_type = "text"
            content = args[i + 1]
            i += 2
        elif args[i] == "--post" and i + 1 < len(args):
            msg_type = "post"
            content = args[i + 1]
            i += 2
        else:
            print(f"Unknown arg: {args[i]}", file=sys.stderr)
            return 1

    if not msg_id or not msg_type:
        print(
            "Usage: send_reply.py --message-id ID (--text TEXT | --post JSON)",
            file=sys.stderr,
        )
        return 1

    cmd = subprocess_util.lark_cli_argv_prefix() + [
        "im", "+messages-reply",
        "--message-id", msg_id,
        "--reply-in-thread",
        "--as", "bot",
        "--msg-type", msg_type,
    ]
    if msg_type == "text":
        cmd += ["--text", content]
    else:
        cmd += ["--content", content]

    return subprocess_util.hidden_run(cmd).returncode


if __name__ == "__main__":
    sys.exit(main())
