# -*- coding: utf-8 -*-
"""Parse review-bot arguments and validate environment.

Usage:
    python parse_args.py <command>

Validates the command (start|status|stop|recover), checks required env vars,
resolves all paths, and outputs a single JSON object with everything the
LLM needs to proceed.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import topic_store


def _read_settings(project_root):
    """Read merged env vars from settings.local.json and settings.json."""
    env = {}
    for fname in ("settings.json", "settings.local.json"):
        path = os.path.join(project_root, ".claude", fname)
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                env.update(data.get("env", {}))
            except (json.JSONDecodeError, OSError):
                pass
    return env


def main():
    parser = argparse.ArgumentParser(description="Parse review-bot arguments")
    parser.add_argument("command", nargs="?", default="",
                        help="start | status | stop | restart | recover")
    # `restart` takes an optional component list: `/review-bot restart daemon`,
    # `restart daemon,monitor`, `restart all`. Other commands ignore it.
    parser.add_argument("components", nargs="?", default="",
                        help="restart only: daemon | listener | monitor | "
                             "all | stale (default: stale)")
    parser.add_argument("--silent", action="store_true",
                        help="Skip Lark group greeting/farewell "
                             "(start: skip greeting; stop: skip farewell)")
    args = parser.parse_args()

    cmd = (args.command or "").strip().lower()
    valid_commands = {"start", "status", "stop", "restart", "recover", "poll"}

    if cmd not in valid_commands:
        print(json.dumps({
            "error": f"Unknown command '{cmd}'. Expected: start | status | stop | restart | recover",
            "valid_commands": sorted(valid_commands),
        }))
        return 1

    # Resolve project root from this script's location
    script_dir = Path(__file__).resolve().parent
    # scripts/ -> review-bot/ -> skills/ -> .claude/ -> project_root
    project_root = script_dir.parent.parent.parent.parent
    skill_dir = script_dir.parent
    cfg_dir = skill_dir / "cfg"

    # Read settings env vars
    settings_env = _read_settings(str(project_root))

    # Required env vars
    chat_id = settings_env.get("REVIEW_BOT_CHAT_ID", "") or os.environ.get("REVIEW_BOT_CHAT_ID", "")
    approver_id = settings_env.get("REVIEW_BOT_APPROVER_ID", "") or os.environ.get("REVIEW_BOT_APPROVER_ID", "")
    approver_open_ids_csv = (settings_env.get("REVIEW_BOT_APPROVER_OPEN_IDS", "")
                              or os.environ.get("REVIEW_BOT_APPROVER_OPEN_IDS", ""))
    poll_mode = settings_env.get("REVIEW_BOT_POLL_MODE", "") or os.environ.get("REVIEW_BOT_POLL_MODE", "manual")
    base_token = settings_env.get("REVIEW_BOT_BASE_TOKEN", "") or os.environ.get("REVIEW_BOT_BASE_TOKEN", "")
    table_id = settings_env.get("REVIEW_BOT_TABLE_ID", "") or os.environ.get("REVIEW_BOT_TABLE_ID", "")
    bot_open_id = settings_env.get("REVIEW_BOT_OPEN_ID", "") or os.environ.get("REVIEW_BOT_OPEN_ID", "")

    # Build approver namelist: parse CSV, append legacy single-approver if not
    # already present, dedupe while preserving order. First entry is the primary
    # approver (used for `@`-mentions in templates) and is exposed as `approver_id`
    # for back-compat.
    approver_open_ids = [s.strip() for s in approver_open_ids_csv.split(",") if s.strip()]
    if approver_id and approver_id not in approver_open_ids:
        approver_open_ids.append(approver_id)
    if approver_open_ids and not approver_id:
        approver_id = approver_open_ids[0]

    missing = []
    if not chat_id:
        missing.append("REVIEW_BOT_CHAT_ID")
    if not approver_open_ids:
        missing.append("REVIEW_BOT_APPROVER_ID (or REVIEW_BOT_APPROVER_OPEN_IDS)")

    if missing:
        print(json.dumps({
            "error": f"Missing required env vars: {', '.join(missing)}. Set them in .claude/settings.local.json.",
            "missing_vars": missing,
        }))
        return 1

    # Resolve paths — use forward slashes so JSON is safe to pass through bash
    def _fwd(p):
        return str(p).replace("\\", "/")

    paths = {
        "project_root": _fwd(project_root),
        "skill_dir": _fwd(skill_dir),
        "scripts": _fwd(skill_dir / "scripts"),
        "cfg": _fwd(cfg_dir),
        "topics_dir": _fwd(cfg_dir / "topics"),
        "closed_dir": _fwd(cfg_dir / "topics" / "closed"),
        "topic_index": _fwd(cfg_dir / "open_topic_index.json"),
        "dispatch_plan": _fwd(cfg_dir / "dispatch_plan.json"),
        "activity_log": _fwd(cfg_dir / "activity.log"),
        "pid_file": _fwd(cfg_dir / "listener.pid"),
        "events_dir": _fwd(cfg_dir / "events"),
        "base_config": _fwd(cfg_dir / "base_config.json"),
        "listener_log": _fwd(cfg_dir / "listener.log"),
    }

    # Check listener status
    listener_alive = False
    listener_pid = None
    if (cfg_dir / "listener.pid").exists():
        try:
            listener_pid = int((cfg_dir / "listener.pid").read_text().strip())
            listener_alive = True  # approximate — full check in dispatcher.py
        except (ValueError, OSError):
            pass

    # Count open topics
    topics_dir = cfg_dir / "topics"
    open_topics = sum(1 for _ in topic_store.iter_topic_files(topics_dir))

    result = {
        "command": cmd,
        "silent": args.silent,
        "env": {
            "chat_id": chat_id,
            "approver_id": approver_id,
            "approver_open_ids": approver_open_ids,
            "poll_mode": poll_mode or "manual",
            "base_token": base_token,
            "table_id": table_id,
            "bot_open_id": bot_open_id,
        },
        "paths": paths,
        "components": (args.components or "").strip().lower(),
        "listener": {
            "pid": listener_pid,
            "alive": listener_alive,
        },
        "open_topics": open_topics,
        "summary": f"Command: {cmd} | Chat: {chat_id[:16]}... | Mode: {poll_mode or 'manual'} | Topics: {open_topics}",
    }

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
