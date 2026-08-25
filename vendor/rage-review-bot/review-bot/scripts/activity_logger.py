"""
Activity logger for review bot — append timestamped entries with rotation.

Appends formatted log entries to the activity log. If the log exceeds 1MB,
keeps only the last 500 lines.

Usage:
    python activity_logger.py --log-file <path> --level INFO --message "new_topic: RAGE-12469"
    python activity_logger.py --log-file <path> --tokens \
        --thread-id <id> --event-type dev_reply --cycle-id <cid> \
        --input-tokens 12345 --output-tokens 678 --cache-read-tokens 9012
"""

import argparse
import datetime
import json
import os
import sys
from pathlib import Path


DEFAULT_MAX_BYTES = 1_000_000
DEFAULT_KEEP_LINES = 500

SPAWN_TOKENS_PREFIX = "spawn_tokens "


def rotate_if_needed(log_path, max_bytes=DEFAULT_MAX_BYTES, keep_lines=DEFAULT_KEEP_LINES):
    """Rotate log file if it exceeds max_bytes, keeping the last keep_lines."""
    log_file = Path(log_path)
    if not log_file.exists():
        return False
    if log_file.stat().st_size <= max_bytes:
        return False
    lines = log_file.read_text(encoding="utf-8").splitlines()
    log_file.write_text("\n".join(lines[-keep_lines:]) + "\n", encoding="utf-8")
    return True


def log_entry(log_path, level, message):
    """
    Append a timestamped log entry and rotate if needed.

    Format: [YYYY-MM-DD HH:MM:SS] [LEVEL] message
    """
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    rotate_if_needed(log_path)
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{ts}] [{level}] {message}\n"
    with open(log_path, "a", encoding="utf-8") as file_handle:
        file_handle.write(entry)
    return entry.rstrip("\n")


def log_spawn_tokens(log_path, payload):
    """
    Append a structured spawn_tokens line. payload is a dict; it is
    serialized as compact JSON and prefixed with SPAWN_TOKENS_PREFIX so
    `status_report.py --token-summary` can parse it deterministically.
    """
    line = SPAWN_TOKENS_PREFIX + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return log_entry(log_path, "INFO", line)


def main():
    parser = argparse.ArgumentParser(description="Review bot activity logger")
    parser.add_argument("--log-file", required=True, help="Path to activity log")
    parser.add_argument("--level", default="INFO", help="Log level (INFO, WARN, ERROR)")
    parser.add_argument("--message", help="Log message (required unless --tokens)")
    parser.add_argument("--tokens", action="store_true",
                        help="Emit a structured spawn_tokens entry instead of a plain message")
    parser.add_argument("--thread-id", help="Thread ID (spawn_tokens only)")
    parser.add_argument("--event-type", help="Event type (spawn_tokens only)")
    parser.add_argument("--cycle-id", help="Dispatcher cycle ID (spawn_tokens only)")
    parser.add_argument("--ticket-id", help="Ticket ID (spawn_tokens only)")
    parser.add_argument("--state", help="Topic state at spawn time (spawn_tokens only)")
    parser.add_argument("--input-tokens", type=int, help="Input token count")
    parser.add_argument("--output-tokens", type=int, help="Output token count")
    parser.add_argument("--cache-read-tokens", type=int, help="Cache-read token count")
    parser.add_argument("--wall-seconds", type=float, help="Wall-clock seconds for the spawn")
    args = parser.parse_args()

    if args.tokens:
        payload = {}
        for field, value in (
            ("thread_id", args.thread_id),
            ("event_type", args.event_type),
            ("cycle_id", args.cycle_id),
            ("ticket_id", args.ticket_id),
            ("state", args.state),
            ("input", args.input_tokens),
            ("output", args.output_tokens),
            ("cache_read", args.cache_read_tokens),
            ("wall_s", args.wall_seconds),
        ):
            if value is not None:
                payload[field] = value
        entry = log_spawn_tokens(args.log_file, payload)
    else:
        if not args.message:
            parser.error("--message is required unless --tokens is given")
        entry = log_entry(args.log_file, args.level, args.message)
    print(entry)


if __name__ == "__main__":
    main()