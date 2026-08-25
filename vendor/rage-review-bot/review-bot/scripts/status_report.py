# -*- coding: utf-8 -*-
"""Generate review-bot status report.

Usage:
    python status_report.py --params-json '{"paths":{...}}'

Enumerates open topics, checks listener status, tails activity log,
and outputs a structured JSON report.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import subprocess_util
import topic_store
from activity_logger import SPAWN_TOKENS_PREFIX


def _check_listener(cfg_dir):
    """Check listener PID and log freshness."""
    pid_file = cfg_dir / "listener.pid"
    log_file = cfg_dir / "listener.log"
    err_file = cfg_dir / "listener.err"

    info = {"pid": None, "alive": False, "log_age_seconds": None}

    if not pid_file.exists():
        return info

    try:
        pid = int(pid_file.read_text().strip())
        info["pid"] = pid
    except (ValueError, OSError):
        return info

    # Liveness via subprocess_util (Win32 OpenProcess on Windows, os.kill
    # elsewhere). Deliberately NOT tasklist.exe: under desktop-heap
    # exhaustion tasklist fails with an empty stdout, which read as DEAD and
    # made this report contradict the daemon's own health check.
    info["alive"] = subprocess_util.is_process_alive(pid)

    # Log freshness — check both .log and .err
    freshest_age = float("inf")
    for lp in (log_file, err_file):
        if lp.exists():
            try:
                age = time.time() - lp.stat().st_mtime
                freshest_age = min(freshest_age, age)
            except OSError:
                pass
    if freshest_age < float("inf"):
        info["log_age_seconds"] = int(freshest_age)

    return info


def _parse_spawn_tokens_lines(log_path):
    """Yield payload dicts from spawn_tokens lines in the activity log."""
    if not log_path.exists():
        return
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        marker_idx = line.find(SPAWN_TOKENS_PREFIX)
        if marker_idx < 0:
            continue
        json_fragment = line[marker_idx + len(SPAWN_TOKENS_PREFIX):]
        try:
            yield json.loads(json_fragment)
        except json.JSONDecodeError:
            continue


def _percentile(sorted_values, pct):
    if not sorted_values:
        return 0
    rank = (len(sorted_values) - 1) * (pct / 100.0)
    low_idx = int(rank)
    high_idx = min(low_idx + 1, len(sorted_values) - 1)
    fraction = rank - low_idx
    return sorted_values[low_idx] * (1 - fraction) + sorted_values[high_idx] * fraction


def _token_summary(log_path, max_entries=None):
    """Group spawn_tokens entries by event_type and compute summary stats."""
    groups = {}
    total_count = 0
    entries = list(_parse_spawn_tokens_lines(log_path))
    if max_entries is not None:
        entries = entries[-max_entries:]
    for payload in entries:
        group_key = payload.get("event_type") or payload.get("state") or "unknown"
        bucket = groups.setdefault(group_key, {"input": [], "output": [], "cache_read": []})
        for field in ("input", "output", "cache_read"):
            value = payload.get(field)
            if isinstance(value, (int, float)):
                bucket[field].append(int(value))
        total_count += 1

    summary = {"event_types": {}, "spawn_count": total_count}
    for event_type, bucket in groups.items():
        input_sorted = sorted(bucket["input"])
        output_sorted = sorted(bucket["output"])
        cache_sorted = sorted(bucket["cache_read"])
        mean_input = (sum(input_sorted) / len(input_sorted)) if input_sorted else 0
        mean_output = (sum(output_sorted) / len(output_sorted)) if output_sorted else 0
        mean_cache = (sum(cache_sorted) / len(cache_sorted)) if cache_sorted else 0
        summary["event_types"][event_type] = {
            "spawns": len(input_sorted),
            "input_mean": round(mean_input, 1),
            "input_p50": round(_percentile(input_sorted, 50), 1),
            "input_p95": round(_percentile(input_sorted, 95), 1),
            "output_mean": round(mean_output, 1),
            "cache_read_mean": round(mean_cache, 1),
        }
    return summary


def main():
    parser = argparse.ArgumentParser(description="Generate review-bot status report")
    parser.add_argument("--params-json", required=True,
                        help="JSON string from parse_args.py output")
    parser.add_argument("--token-summary", action="store_true",
                        help="Emit token-spend summary grouped by event_type instead of the normal report")
    parser.add_argument("--max-entries", type=int, default=None,
                        help="Only consider the last N spawn_tokens entries (token-summary only)")
    args = parser.parse_args()

    try:
        params = json.loads(args.params_json)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"Invalid params JSON: {e}"}))
        return 1

    paths = params.get("paths", {})
    topics_dir = Path(paths.get("topics_dir", ""))
    cfg_dir = Path(paths.get("cfg", ""))
    log_path = Path(paths.get("activity_log", ""))

    if args.token_summary:
        print(json.dumps(_token_summary(log_path, args.max_entries),
                         indent=2, ensure_ascii=False))
        return 0

    # Enumerate open topics
    topics = []
    for fp in sorted(topic_store.iter_topic_files(topics_dir)):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            topics.append({
                "thread_id": data.get("thread_id", fp.stem),
                "ticket_id": data.get("identity", {}).get("ticket_id", "?"),
                "state": data.get("review", {}).get("state", "?"),
                "round": data.get("review", {}).get("review_round", 0),
                "mrs_summary": ", ".join(
                    f"{r}!{m.get('mr_iid', '?')}"
                    for r, m in (data.get("mrs") or {}).items()
                ) or "?",
                "pending_events": len(data.get("events", {}).get("pending", [])),
                "blocked": (
                    "rebase-conflict"
                    if data.get("review", {}).get("rebase_conflict_blocked")
                    else "manual-merge"
                    if data.get("review", {}).get("merge_manual_required")
                    else ""
                ),
            })
        except (json.JSONDecodeError, OSError):
            topics.append({"thread_id": fp.stem, "error": "failed to read"})

    # Listener status
    listener = _check_listener(cfg_dir)

    # Tail activity log
    log_tail = []
    if log_path.exists():
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            log_tail = lines[-20:]
        except OSError:
            pass

    # Format table for display
    table_lines = ["| Thread | Ticket | State | Round | MRs | Pending |",
                   "|--------|--------|-------|-------|-----|---------|"]
    for t in topics:
        if "error" in t:
            table_lines.append(f"| {t['thread_id'][:16]}... | ? | ERROR | - | - | - |")
        else:
            tid = t["thread_id"]
            short_tid = tid[:16] + "..." if len(tid) > 16 else tid
            state_cell = t["state"]
            if t.get("blocked"):
                state_cell = f"{state_cell} (⛔ {t['blocked']})"
            table_lines.append(
                f"| {short_tid} | {t['ticket_id']} | {state_cell} | {t['round']} | {t['mrs_summary']} | {t['pending_events']} |"
            )

    listener_status = "ALIVE" if listener["alive"] else "DEAD"
    if listener["log_age_seconds"] is not None:
        listener_status += f" (log {listener['log_age_seconds']}s old)"
    if listener["pid"]:
        listener_status += f" PID={listener['pid']}"

    result = {
        "topics": topics,
        "topic_count": len(topics),
        "listener": listener,
        "log_tail": log_tail,
        "formatted_table": "\n".join(table_lines),
        "listener_summary": listener_status,
        "summary": f"{len(topics)} open topic(s). Listener: {listener_status}.",
    }

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    # The report carries Chinese state labels and emoji markers; a GBK console
    # (CN-Windows default) raised UnicodeEncodeError and killed the whole run.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    sys.exit(main())
