# -*- coding: utf-8 -*-
"""Thin wrapper that runs parse_args + dispatcher in one process.

Avoids spawning visible python.exe windows on each poll cycle.
Singleton: only one daemon instance runs at a time (PID file guard).

Usage:
    python poll_dispatch.py              # single run
    pythonw poll_dispatch.py --loop 120  # daemon: run every 120s, no console
    pythonw poll_dispatch.py --watch     # event-driven daemon (singleton)
"""
import json
import os
import sys
import subprocess
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import subprocess_util
SKILL_DIR = os.path.dirname(SCRIPT_DIR)  # review-bot/
PID_FILE = os.path.join(SKILL_DIR, "cfg", "daemon.pid")
HEARTBEAT_FILE = os.path.join(SKILL_DIR, "cfg", "daemon.heartbeat")
ACTIVITY_LOG = os.path.join(SKILL_DIR, "cfg", "activity.log")


def _log(level, msg):
    """Append a timestamped line to activity.log."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}\n"
    try:
        os.makedirs(os.path.dirname(ACTIVITY_LOG), exist_ok=True)
        with open(ACTIVITY_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _acquire_singleton():
    """Try to become the singleton daemon. Returns True if acquired.

    If an existing daemon is alive but its heartbeat is stale (>60s),
    kill it and take over — it's stuck. The daemon touches the heartbeat
    file every 5s loop iteration, so 60s stale = 12 missed beats.
    """
    STALE_THRESHOLD = 60  # seconds — 12 missed heartbeats

    old_pid = subprocess_util.read_pid_file(PID_FILE)
    if old_pid is not None and subprocess_util.is_process_alive(old_pid):
        # Check heartbeat — touched every 5s by the daemon loop
        stale = True
        if os.path.isfile(HEARTBEAT_FILE):
            age = time.time() - os.path.getmtime(HEARTBEAT_FILE)
            stale = age > STALE_THRESHOLD
        if not stale:
            return False  # healthy daemon running
        _log("WARN", f"killing stale daemon pid={old_pid} "
             f"(heartbeat age={age:.0f}s)")
        subprocess_util.kill_process(old_pid)

    subprocess_util.write_pid_file(PID_FILE)
    return True


def _release_singleton():
    """Remove the PID file on exit."""
    subprocess_util.release_pid_file(PID_FILE)


def run_one_cycle(out_file, reconcile=False):
    """Execute one poll cycle: parse_args + dispatcher. Returns exit code.

    `reconcile=True` runs the Lark history backfill this cycle (cold start,
    listener restart, or one-off run); steady-state daemon cycles pass False
    and stay listener-only. See DESIGN §1.2.7."""
    lines = []

    # Step 1: parse_args
    parse_script = os.path.join(SCRIPT_DIR, "parse_args.py")
    run_kwargs = dict(capture_output=True, text=True, timeout=30, encoding="utf-8")
    try:
        r = subprocess_util.hidden_run(["python", parse_script, "poll"], **run_kwargs)
        if r.returncode != 0:
            lines.append(json.dumps({"error": r.stderr.strip()[:200]}))
            with open(out_file, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            return 1
        params = json.loads(r.stdout)
        lines.append(params.get("summary", ""))
    except Exception as e:
        lines.append(json.dumps({"error": str(e)}))
        with open(out_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return 1

    # Step 2: dispatcher. `reconcile=True` (cold start / one-off) tells the
    # dispatcher to run the Lark history backfill this cycle; steady-state
    # daemon cycles omit it and stay listener-only. See DESIGN §1.2.7.
    dispatcher_script = os.path.join(SCRIPT_DIR, "dispatcher.py")
    dispatcher_argv = ["python", dispatcher_script]
    if reconcile:
        dispatcher_argv.append("--reconcile")
    try:
        r2 = subprocess_util.hidden_run(dispatcher_argv, **run_kwargs)
        lines.append(r2.stdout.strip() if r2.stdout else "{}")
    except Exception as e:
        lines.append(json.dumps({"error": str(e)}))

    # Write to file and also stdout (for direct python calls)
    output = "\n".join(lines)
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(output)
    try:
        print(output)
    except Exception:
        pass  # pythonw has no stdout
    return 0


def main():
    out_file = os.path.join(
        os.environ.get("WRITE_TMP_DIR", os.path.join(SKILL_DIR, "cfg")),
        "poll_result.txt",
    )

    # Parse --loop <seconds>
    loop_interval = 0
    if "--loop" in sys.argv:
        idx = sys.argv.index("--loop")
        if idx + 1 < len(sys.argv):
            try:
                loop_interval = int(sys.argv[idx + 1])
            except ValueError:
                loop_interval = 120
        else:
            loop_interval = 120

    if loop_interval > 0:
        # Daemon mode: singleton guard
        if not _acquire_singleton():
            try:
                print("Another daemon is already running. Exiting.")
            except Exception:
                pass
            return 0
        import atexit
        atexit.register(_release_singleton)

        loop_first = True
        while True:
            try:
                run_one_cycle(out_file, reconcile=loop_first)
                loop_first = False
            except Exception as e:
                _log("ERROR", f"loop cycle failed: {type(e).__name__}: {e}")
            time.sleep(loop_interval)
    elif "--watch" in sys.argv:
        # Event-driven mode: singleton guard
        if not _acquire_singleton():
            try:
                print("Another daemon is already running. Exiting.")
            except Exception:
                pass
            return 0
        import atexit
        atexit.register(_release_singleton)

        # Only run dispatcher when new events arrive
        # or merge-tracker periodic check is due.
        events_dir = os.path.join(SKILL_DIR, "cfg", "events")
        trigger_file = os.path.join(
            os.environ.get("WRITE_TMP_DIR", os.path.join(SKILL_DIR, "cfg")),
            "poll_trigger.txt",
        )
        last_merge_check = 0
        last_trigger_time = 0
        MERGE_CHECK_INTERVAL = 120  # seconds
        TRIGGER_COOLDOWN = 30       # seconds — give Claude time to spawn agents
        first_cycle = True          # cold start → reconcile once (DESIGN §1.2.7)

        while True:
            try:
                # Check for new event files
                has_events = False
                event_files = []
                if os.path.isdir(events_dir):
                    event_files = [f for f in os.listdir(events_dir)
                                   if f.endswith(".json")]
                    has_events = len(event_files) > 0

                # Periodic merge-tracker check for APPROVED topics
                now = time.time()
                merge_check_due = (now - last_merge_check) >= MERGE_CHECK_INTERVAL

                if has_events or merge_check_due:
                    _log("INFO", f"daemon dispatch: events={len(event_files)} "
                         f"merge_due={merge_check_due}")
                    run_one_cycle(out_file, reconcile=first_cycle)
                    first_cycle = False
                    if merge_check_due:
                        last_merge_check = now

                    # Parse result — process merge queue locally, trigger Claude only for pending events
                    try:
                        with open(out_file, encoding="utf-8") as f:
                            lines = f.read().strip().split("\n")
                        if len(lines) >= 2:
                            result = json.loads(lines[1])
                            work = result.get("work_count", 0) > 0
                            plan_file = result.get("plan_file")

                            # Process merge queue directly in daemon (zero LLM tokens)
                            if plan_file and os.path.isfile(plan_file):
                                try:
                                    import process_merge_queue
                                    with open(plan_file, encoding="utf-8") as pf:
                                        plan = json.loads(pf.read())
                                    if plan.get("merge_queue") or plan.get("3p_merge_queue"):
                                        mq_count = len(plan.get('merge_queue', []))
                                        p3_count = len(plan.get('3p_merge_queue', []))
                                        _log("INFO", f"daemon: processing merge queue "
                                             f"(main={mq_count}, 3p={p3_count})")
                                        mq_result = process_merge_queue.run(plan_file)
                                        _log("INFO", f"daemon: merge queue done: "
                                             f"{json.dumps(mq_result, ensure_ascii=False)}")
                                except Exception as mq_err:
                                    _log("ERROR", f"daemon: merge queue failed: "
                                         f"{type(mq_err).__name__}: {mq_err}")

                            # Only trigger Claude when there are pending events needing reasoning
                            if work:
                                # Cooldown: don't re-trigger if we just triggered
                                if (now - last_trigger_time) >= TRIGGER_COOLDOWN:
                                    with open(trigger_file, "a", encoding="utf-8") as tf:
                                        tf.write(f"{result.get('cycle_id', '')}\n")
                                    last_trigger_time = now
                    except Exception:
                        pass
            except Exception as e:
                _log("ERROR", f"watch cycle failed: {type(e).__name__}: {e}")
            # Touch heartbeat so singleton check knows we're alive
            try:
                with open(HEARTBEAT_FILE, "w") as hb:
                    hb.write(str(time.time()))
            except OSError:
                pass
            time.sleep(5)  # check every 5 seconds
    else:
        # Single run (manual / recover) — always catch up via reconcile.
        return run_one_cycle(out_file, reconcile=True)


if __name__ == "__main__":
    sys.exit(main() or 0)
