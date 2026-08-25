"""Scan Claude Code subagent transcripts for review-bot topic-agent
spawns and emit spawn_tokens entries into the activity log.

Topic-agent spawns from `/review-bot poll` are Task subagents, which
Claude Code writes to:

    $HOME/.claude/projects/<project-slug>/<parent-session>/subagents/
        agent-<agent-id>.jsonl        ← per-turn transcript (usage here)
        agent-<agent-id>.meta.json    ← {"agentType":..., "description":"Process RAGE-XXXXX"}

Each assistant turn line in the .jsonl has `message.usage = {input_tokens,
cache_creation_input_tokens, cache_read_input_tokens, output_tokens, ...}`.
Summing gives the exact spend for that spawn.

Attribution:
  - ticket_id: from meta.description ("Process RAGE-XXXXX") or first user turn
  - thread_id: extracted from the TOPIC_FILE path in the first user turn
               (basename minus .json)
  - cycle_id:  from POLL_CYCLE_ID marker in the first user turn

State:
  cfg/token_ledger.json — {agent_id: logged_ts_ms}. Already-logged
  agents are skipped, so this script is safe to rerun every cycle.
  Agent IDs are globally unique within a project (Claude Code uses
  random 17-char hex), so no need to scope by parent session.

Usage (called from dispatcher.py at end of cycle):
  python collect_session_tokens.py --projects-dir <path> --skill-dir <path>

Exit 0 always — token logging is advisory, never fatal.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import event_utils


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR_DEFAULT = SCRIPT_DIR.parent
LEDGER_NAME = "token_ledger.json"
ACTIVITY_LOG_NAME = "activity.log"

# Reuse the shared pattern rather than rebuilding it from the key list: a
# local copy drifts (this one lacked the left boundary AND re.IGNORECASE, so
# `ENG` matched inside `ZHENG-12` and the token stats disagreed with the
# router). `ack_new_topic` deleted its own copy for exactly this reason.
TICKET_RE = event_utils.TICKET_RE
TOPIC_FILE_RE = re.compile(r"TOPIC_FILE[`:\s]*[`\"']?([^`\"'\n]*topics[\\/](om_[A-Za-z0-9]+)\.json)")
CYCLE_RE = re.compile(r"POLL_CYCLE_ID[`:\s]*[`\"']?([0-9TZ:_a-z\-]+)")


def _first_user_text(lines):
    for line in lines[:8]:
        try:
            datum = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if datum.get("type") != "user":
            continue
        msg = datum.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif isinstance(block, str):
                    texts.append(block)
            if texts:
                return "\n".join(texts)
    return ""


def _attribute(meta_path: Path, lines):
    """Return {ticket_id, thread_id, cycle_id} or None if not a topic-agent."""
    ticket_id = None
    thread_id = None
    cycle_id = None

    # 1. meta.description is the cleanest ticket source.
    if meta_path.exists():
        try:
            with open(meta_path, "r", encoding="utf-8") as file_handle:
                meta = json.load(file_handle)
            desc = str(meta.get("description") or "")
            match = TICKET_RE.search(desc)
            if match:
                ticket_id = match.group(0)
        except (OSError, json.JSONDecodeError):
            pass

    # 2. first user turn carries the rendered spawn_topic_agent.md template
    prompt_text = _first_user_text(lines)
    if prompt_text:
        topic_match = TOPIC_FILE_RE.search(prompt_text)
        if topic_match:
            thread_id = topic_match.group(2)
        cycle_match = CYCLE_RE.search(prompt_text)
        if cycle_match:
            cycle_id = cycle_match.group(1)
        if not ticket_id:
            ticket_match = TICKET_RE.search(prompt_text)
            if ticket_match:
                ticket_id = ticket_match.group(0)

    # A topic-agent spawn must at minimum have a thread_id (from TOPIC_FILE)
    # and a ticket_id. cycle_id is best-effort — replay/manual spawns
    # may omit it, so we fall back to "manual".
    if not thread_id or not ticket_id:
        return None
    return {
        "ticket_id": ticket_id,
        "thread_id": thread_id,
        "cycle_id": cycle_id or "manual",
    }


def _sum_usage(lines):
    """Return {input, output, cache_read, turns, first_ts, last_ts} or
    None if no assistant turns have usage."""
    inp = out = cache_read = turns = 0
    first_ts = None
    last_ts = None
    for line in lines:
        try:
            datum = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        usage = (datum.get("message") or {}).get("usage")
        if not isinstance(usage, dict):
            continue
        # Treat cache_creation_input_tokens as part of "input" since they're
        # the ingestion path; cache_read_input_tokens is the cheap path and
        # gets its own column.
        inp += int(usage.get("input_tokens") or 0) + \
            int(usage.get("cache_creation_input_tokens") or 0)
        out += int(usage.get("output_tokens") or 0)
        cache_read += int(usage.get("cache_read_input_tokens") or 0)
        turns += 1
        ts = datum.get("timestamp")
        if ts:
            if first_ts is None:
                first_ts = ts
            last_ts = ts
    if turns == 0:
        return None
    return {"input": inp, "output": out, "cache_read": cache_read,
            "turns": turns, "first_ts": first_ts, "last_ts": last_ts}


def _wall_seconds(first_ts, last_ts):
    if not first_ts or not last_ts:
        return None
    try:
        import datetime as dt_module
        begin = dt_module.datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
        end = dt_module.datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        return max(0.0, (end - begin).total_seconds())
    except (ValueError, AttributeError):
        return None


def _iter_subagent_files(projects_dir: Path):
    """Yield (jsonl_path, meta_path) for every subagent transcript."""
    for session_dir in projects_dir.iterdir():
        if not session_dir.is_dir():
            continue
        subagents = session_dir / "subagents"
        if not subagents.is_dir():
            continue
        for jsonl_path in subagents.glob("agent-*.jsonl"):
            meta_path = jsonl_path.with_suffix(".meta.json")
            yield jsonl_path, meta_path


def _load_ledger(ledger_path: Path):
    if not ledger_path.exists():
        return {}
    try:
        with open(ledger_path, "r", encoding="utf-8") as file_handle:
            data = json.load(file_handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_ledger(ledger_path: Path, ledger: dict):
    tmp_path = ledger_path.with_suffix(ledger_path.suffix + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as file_handle:
            json.dump(ledger, file_handle, indent=2, sort_keys=True)
        os.replace(tmp_path, ledger_path)
    except OSError:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def _emit_tokens(activity_log: Path, payload: dict):
    cmd = [
        sys.executable, str(SCRIPT_DIR / "activity_logger.py"),
        "--log-file", str(activity_log),
        "--tokens",
        "--thread-id", payload["thread_id"],
        "--event-type", payload.get("event_type", "topic_agent_spawn"),
        "--cycle-id", payload["cycle_id"],
        "--ticket-id", payload["ticket_id"],
        "--input-tokens", str(payload["input"]),
        "--output-tokens", str(payload["output"]),
        "--cache-read-tokens", str(payload["cache_read"]),
    ]
    if payload.get("wall_s") is not None:
        cmd += ["--wall-seconds", str(payload["wall_s"])]
    subprocess.run(cmd, check=False, capture_output=True, timeout=15)


def _resolve_projects_dir(override: str | None):
    if override:
        return Path(override)
    home = Path.home()
    candidates = list((home / ".claude" / "projects").glob("*rage-review*"))
    return candidates[0] if candidates else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--projects-dir",
                        help="Claude Code transcripts dir "
                             "(default: $HOME/.claude/projects/<auto>)")
    parser.add_argument("--skill-dir", default=str(SKILL_DIR_DEFAULT),
                        help="review-bot skill dir")
    parser.add_argument("--max-age-hours", type=float, default=48,
                        help="Ignore agent transcripts whose mtime is older")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be logged; do not append")
    args = parser.parse_args()

    skill_dir = Path(args.skill_dir).resolve()
    cfg_dir = skill_dir / "cfg"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    activity_log = cfg_dir / ACTIVITY_LOG_NAME
    ledger_path = cfg_dir / LEDGER_NAME

    projects_dir = _resolve_projects_dir(args.projects_dir)
    if projects_dir is None or not projects_dir.exists():
        print(json.dumps({"skipped": "no_projects_dir",
                          "path": str(projects_dir)}))
        return 0

    ledger = _load_ledger(ledger_path)
    cutoff = time.time() - args.max_age_hours * 3600
    emitted = skipped_logged = skipped_unattributed = skipped_old = 0
    errors = 0

    for jsonl_path, meta_path in _iter_subagent_files(projects_dir):
        agent_id = jsonl_path.stem  # e.g. "agent-a96abedbcbded6a57"
        if agent_id in ledger:
            skipped_logged += 1
            continue
        try:
            if jsonl_path.stat().st_mtime < cutoff:
                skipped_old += 1
                continue
        except OSError:
            errors += 1
            continue

        try:
            with open(jsonl_path, "r", encoding="utf-8", errors="replace") as file_handle:
                lines = file_handle.readlines()
        except OSError:
            errors += 1
            continue
        if not lines:
            skipped_unattributed += 1
            continue

        attribution = _attribute(meta_path, lines)
        if not attribution:
            skipped_unattributed += 1
            continue

        totals = _sum_usage(lines)
        if not totals:
            skipped_unattributed += 1
            continue

        payload = {
            "thread_id": attribution["thread_id"],
            "ticket_id": attribution["ticket_id"],
            "cycle_id":  attribution["cycle_id"],
            "event_type": "topic_agent_spawn",
            "input":      totals["input"],
            "output":     totals["output"],
            "cache_read": totals["cache_read"],
            "wall_s":     _wall_seconds(totals["first_ts"], totals["last_ts"]),
            "agent_id":   agent_id,
        }
        if args.dry_run:
            print(json.dumps({"would_emit": payload}))
        else:
            try:
                _emit_tokens(activity_log, payload)
            except (OSError, subprocess.TimeoutExpired):
                errors += 1
                continue
            ledger[agent_id] = int(time.time() * 1000)
        emitted += 1

    if not args.dry_run and emitted:
        _save_ledger(ledger_path, ledger)

    print(json.dumps({
        "emitted": emitted,
        "skipped_logged": skipped_logged,
        "skipped_unattributed": skipped_unattributed,
        "skipped_old": skipped_old,
        "errors": errors,
        "projects_dir": str(projects_dir),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
