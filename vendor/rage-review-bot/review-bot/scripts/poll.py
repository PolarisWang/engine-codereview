# -*- coding: utf-8 -*-
"""Single poll cycle wrapper — delegates to dispatcher.py.

The dispatcher does everything: listener health-check, reconcile,
route raw events into per-topic queues, poll merge-train status for
APPROVED topics, and write cfg/dispatch_plan.json with the list of
topics whose events.pending is non-empty.

Stdout is a single JSON line with cycle_id, work_count, plan_file.
The parent agent reads cfg/dispatch_plan.json and spawns per-topic
Task calls (up to parallel_limit=4).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import subprocess_util


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return subprocess_util.hidden_run(
        [sys.executable, os.path.join(script_dir, "dispatcher.py")] + sys.argv[1:]
    ).returncode


if __name__ == "__main__":
    sys.exit(main())
