# -*- coding: utf-8 -*-
"""Monitor wrapper for the review-bot dispatch loop.

Watches poll_trigger.txt for new lines (skipping backlog), reads the
dispatch plan on each trigger, and emits a self-contained JSON line to
stdout ONLY when there's actionable work (topic agents or merge queue).

Usage (as a Monitor command):
    python "$SKILL_DIR/scripts/monitor_dispatch.py"

Each stdout line is one JSON object:
    {"cycle":"...","plan_file":"...","work":[...],"merge_queue":[...],"3p_merge_queue":[...]}

The Monitor notification contains everything Claude needs to spawn agents
without additional Read calls.
"""
import atexit
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import subprocess_util

TRIGGER_FILE = os.path.join(
    os.environ.get("WRITE_TMP_DIR", "D:/tmp"), "poll_trigger.txt"
)
RESULT_FILE = os.path.join(
    os.environ.get("WRITE_TMP_DIR", "D:/tmp"), "poll_result.txt"
)
PID_FILE = os.path.join(
    Path(__file__).resolve().parent.parent, "cfg", "monitor.pid"
)


def _read_plan(plan_path):
    """Read dispatch_plan.json and extract actionable items."""
    try:
        with open(plan_path, encoding="utf-8") as f:
            plan = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    work = plan.get("work", [])
    merge_q = plan.get("merge_queue", [])
    merge_3p = plan.get("3p_merge_queue", [])

    if not work and not merge_q and not merge_3p:
        return None

    return {
        "work": [
            {
                "ticket_id": t.get("ticket_id", ""),
                "state": t.get("state", ""),
                "topic_file": t.get("topic_file", ""),
                "pending_count": t.get("pending_count", 0),
            }
            for t in work
        ],
        "merge_queue": [
            {
                "ticket_id": t.get("ticket_id", ""),
                "topic_file": t.get("topic_file", ""),
            }
            for t in merge_q
        ],
        "3p_merge_queue": [
            {
                "ticket_id": t.get("ticket_id", ""),
                "topic_file": t.get("topic_file", ""),
            }
            for t in merge_3p
        ],
    }


def main():
    # Singleton: kill any previous monitor instance, then take over
    old_pid = subprocess_util.read_pid_file(PID_FILE)
    if old_pid is not None and old_pid != os.getpid():
        subprocess_util.kill_process(old_pid)
    subprocess_util.write_pid_file(PID_FILE)
    atexit.register(subprocess_util.release_pid_file, PID_FILE)

    # Skip existing content — only watch new appends
    try:
        pos = os.path.getsize(TRIGGER_FILE)
    except OSError:
        pos = 0

    while True:
        try:
            with open(TRIGGER_FILE, "r", encoding="utf-8") as f:
                f.seek(pos)
                while True:
                    line = f.readline()
                    if not line:
                        # No new data — sleep and retry
                        pos = f.tell()
                        break
                    line = line.strip()
                    if not line:
                        continue

                    # New trigger — read poll_result.txt for plan_file
                    plan_file = None
                    try:
                        with open(RESULT_FILE, "r", encoding="utf-8") as rf:
                            lines = rf.readlines()
                            if len(lines) >= 2:
                                result = json.loads(lines[1].strip())
                                plan_file = result.get("plan_file")
                    except (OSError, json.JSONDecodeError, IndexError):
                        pass

                    if not plan_file:
                        continue

                    items = _read_plan(plan_file)
                    if items is None:
                        continue

                    # Emit actionable dispatch notification
                    out = {"cycle": line, "plan_file": plan_file}
                    out.update(items)
                    print(json.dumps(out, ensure_ascii=False), flush=True)

        except OSError:
            pass

        time.sleep(2)


if __name__ == "__main__":
    main()
