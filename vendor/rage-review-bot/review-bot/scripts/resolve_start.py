# -*- coding: utf-8 -*-
"""Resolve start-mode prerequisites: dirs, Base config, poll mode.

Usage:
    python resolve_start.py --params-json '{"env":{...},"paths":{...}}'

Checks/creates required directories, resolves Lark Base config (priority:
env vars > cached file > needs creation), and outputs what the LLM still
needs to do (post greeting, confirm Base creation, start polling).
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import session_registry


def main():
    parser = argparse.ArgumentParser(description="Resolve start-mode prerequisites")
    parser.add_argument("--params-json", required=True,
                        help="JSON string from parse_args.py output")
    args = parser.parse_args()

    try:
        params = json.loads(args.params_json)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"Invalid params JSON: {e}"}))
        return 1

    env = params.get("env", {})
    paths = params.get("paths", {})

    topics_dir = Path(paths.get("topics_dir", ""))
    closed_dir = Path(paths.get("closed_dir", ""))
    events_dir = Path(paths.get("events_dir", ""))
    topic_index = Path(paths.get("topic_index", ""))
    base_config_path = Path(paths.get("base_config", ""))

    # Step 1: Create directories
    dirs_created = []
    for d in (topics_dir, closed_dir, events_dir):
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            dirs_created.append(str(d))

    # Step 2: Initialize topic index
    index_created = False
    if not topic_index.exists():
        topic_index.write_text("{}", encoding="utf-8")
        index_created = True

    # Step 3: Resolve Lark Base config
    base_token = env.get("base_token", "")
    table_id = env.get("table_id", "")
    base_source = None
    need_create_base = False

    if base_token and table_id:
        # Priority 1: env vars
        base_source = "env_vars"
        # Write to base_config.json for downstream
        base_config_path.parent.mkdir(parents=True, exist_ok=True)
        base_config_path.write_text(
            json.dumps({"app_token": base_token, "table_id": table_id},
                       indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
    elif base_config_path.exists():
        # Priority 2: cached file
        try:
            cached = json.loads(base_config_path.read_text(encoding="utf-8"))
            if cached.get("app_token") and cached.get("table_id"):
                base_token = cached["app_token"]
                table_id = cached["table_id"]
                base_source = "cached_file"
        except (json.JSONDecodeError, OSError):
            pass

    if not base_source:
        # Priority 3: needs user confirmation to create
        need_create_base = True

    # Step 4: Register this parent session so the daily restart can end it.
    # Runs here, not in launch_review_bot.ps1, because every start path goes
    # through this script — a hand-started session used to be invisible to the
    # stop and outlived every subsequent restart (DESIGN §1.1.5).
    session_entry = session_registry.register(paths.get("cfg", ""))

    # Step 5: Determine poll mode actions
    poll_mode = env.get("poll_mode", "manual")

    result = {
        "dirs_created": dirs_created,
        "index_created": index_created,
        "session": session_entry or {"pid": None,
                                     "note": "parent claude.exe not resolved — "
                                             "this session will not be stopped "
                                             "by the daily restart"},
        "base": {
            "resolved": not need_create_base,
            "source": base_source,
            "app_token": base_token,
            "table_id": table_id,
            "need_create": need_create_base,
        },
        "poll_mode": poll_mode,
        "remaining_actions": [],
    }

    # What the LLM still needs to do
    result["remaining_actions"].append("start_listener")
    if need_create_base:
        result["remaining_actions"].append("confirm_and_create_base")
    if poll_mode == "loop":
        result["remaining_actions"].append("start_loop_polling")
    result["remaining_actions"].append("post_greeting")

    result["summary"] = (
        f"Dirs ready. Base: {'needs creation (confirm with user)' if need_create_base else base_source}. "
        f"Poll mode: {poll_mode}. "
        f"Remaining: {', '.join(result['remaining_actions'])}."
    )

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
