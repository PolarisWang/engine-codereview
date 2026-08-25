# -*- coding: utf-8 -*-
"""Finalize one topic-agent review pass — the deterministic writeback glue.

A topic agent's only *judgment* output is the review content: the issue
list, the summary, the verdicts. Everything that happens AFTER that is a
FIXED procedure that every agent used to re-author by hand each spawn as a
bespoke `writeback.py` / `finalize.py` (2-7 KB of identical scaffolding,
sometimes written twice when the first attempt had a bug). This CLI owns
that procedure so the agent never writes glue again:

  1. Schema-validate the reply artifact (the SAME check `reply_dispatcher`
     runs, so we never write an artifact it would reject).
  2. Render-validate the artifact's vars via `render.py --check-only` — the
     mandatory poison-loop guard (a missing required var would make
     `reply_dispatcher` retry forever, silent on the user side).
  3. Atomic-write the artifact to `cfg/replies/<thread>__<event>.json`.
  4. Topic-side writeback: drain the triggering event from `events.pending`,
     stamp `last_processed_event_id` + the recent-event ring, apply the
     agent's `topic_updates` (per-repo `branch_sha`, `review.*` fields,
     flagged-issue verifications, a `review_history` append), and append the
     one audit entry. One `write_atomic`.
  5. Release the lock.

State transitions and `last_review_commit` / `review_round` are NOT applied
here — those live in the artifact's `post_actions` and are applied by
`reply_dispatcher` only AFTER the Lark post lands (so a topic never advances
on a post that never happened). `finalize_review` only persists what must be
durable regardless of post success: the review work product + event drain.

Order is load-bearing: artifact FIRST, then the topic writeback. A crash
between the two leaves the artifact on disk (the post still happens; the
dispatcher's `_already_posted` de-dups a re-spawn) rather than draining the
event with no artifact (which would silently drop the reply). Both writes
happen microseconds apart in this one process — far tighter than the agent's
old multi-tool-call bespoke script.

Input: a single result JSON (``--result-file PATH`` or ``--result-stdin``):

    {
      "topic_file": "...json",            // required
      "lock_file": "...lock",             // optional (default: sibling .lock)
      "skill_dir": "...",                 // optional (default: parent of scripts/)
      "thread_id": "om_...",              // required
      "ticket_id": "RAGE-XXXXX",          // required
      "triggered_by_event_id": "msg:...", // required — drained from pending
      "artifact": {                       // required — the agent's reply
        "template": "review_round1_dev_triage",
        "vars": { ... },
        "preflight": {"expected_state": "TRIAGING", "expected_phase": null},
        "post_actions": [ ... ],          // optional; applied post-publish
        "orphan_doc_token": null          // optional
      },
      "topic_updates": {                  // optional — all keys optional
        "mrs_branch_sha": {"chaos": "<sha>"},
        "review_set": {"issues": [...], "issues_found": 3,
                       "flagged_issues": [...], "triage": "complex",
                       "lark_doc_token": "..."},
        "flagged_issue_verifications": [
          {"index": 1, "verification": "addressed",
           "verification_rationale": "...", "verified_at_sha": "<sha>"}
        ],
        "review_history_append": { ... }, // ts auto-stamped
        "audit": {"event": "review_round_completed", "round": 2, ...}
      },
      "drain_event_ids": ["msg:..."]      // optional; default [triggered_by_event_id]
    }

Output: one JSON line. ``status`` is ``ok`` on success, ``error`` on a
validation failure (artifact NOT written; topic gets an
``artifact_validation_failed`` audit; lock released; exit 1).
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import topic_store
import reply_dispatcher  # reuse the exact artifact schema validator

RENDER_PY = SCRIPT_DIR / "templates" / "render.py"


# ---- helpers --------------------------------------------------------------

def _artifact_filename(thread_id, event_id):
    """Match reply_dispatcher's donation-rewrite convention exactly."""
    return f"{thread_id}__{event_id.replace(':', '__')}.json"


def _check_only(template, variables):
    """Run render.py --check-only on (template, vars). Returns (ok, detail)."""
    with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8") as vf:
        json.dump(variables, vf, ensure_ascii=False)
        vars_file = vf.name
    try:
        proc = subprocess.run(
            [sys.executable, str(RENDER_PY), template,
             "--vars-file", vars_file, "--check-only"],
            capture_output=True, text=True, encoding="utf-8")
        if proc.returncode != 0:
            return False, (proc.stderr or proc.stdout or "")[:500]
        return True, ""
    finally:
        try:
            os.unlink(vars_file)
        except OSError:
            pass


def _build_artifact(result):
    """Compose the full artifact dict the dispatcher will consume."""
    a = result["artifact"]
    return {
        "version": 1,
        "thread_id": result["thread_id"],
        "ticket_id": result["ticket_id"],
        "triggered_by_event_id": result["triggered_by_event_id"],
        "template": a["template"],
        "vars": a.get("vars") or {},
        "preflight": a.get("preflight") or {},
        "post_actions": a.get("post_actions") or [],
        "orphan_doc_token": a.get("orphan_doc_token"),
    }


def _apply_topic_updates(topic, updates):
    """Apply the agent's review work-product to the topic dict (in place)."""
    review = topic.setdefault("review", {})

    # Per-repo branch_sha refresh.
    for repo, sha in (updates.get("mrs_branch_sha") or {}).items():
        mr = (topic.get("mrs") or {}).get(repo)
        if isinstance(mr, dict) and sha:
            mr["branch_sha"] = sha

    # Direct review.* field sets (issues, issues_found, flagged_issues, ...).
    for key, value in (updates.get("review_set") or {}).items():
        review[key] = value

    # Merge per-issue verification verdicts into flagged_issues dicts by index.
    verifs = updates.get("flagged_issue_verifications") or []
    if verifs:
        flagged = review.get("flagged_issues") or []
        by_index = {}
        for entry in flagged:
            if isinstance(entry, dict) and entry.get("index") is not None:
                by_index[entry["index"]] = entry
        for v in verifs:
            idx = v.get("index")
            target = by_index.get(idx)
            if target is None:
                continue
            for k in ("verification", "verification_rationale",
                      "verified_at_sha", "marked_resolved_at"):
                if k in v:
                    target[k] = v[k]

    # Optional review_history append (ts auto-stamped).
    hist = updates.get("review_history_append")
    if isinstance(hist, dict):
        entry = dict(hist)
        entry.setdefault("ts", topic_store.now_ms())
        review.setdefault("review_history", []).append(entry)


def _drain_events(topic, event_ids):
    """Remove drained events from pending; stamp last_processed + ring.

    Returns the count remaining in pending after the drain.
    """
    events = topic.setdefault("events", {})
    pending = events.get("pending") or []
    drained = set(e for e in event_ids if e)
    kept = [e for e in pending if e.get("event_id") not in drained]
    events["pending"] = kept
    if drained:
        # Last drained id wins as the scalar watermark; ring records all.
        for ev_id in event_ids:
            if ev_id:
                topic_store.push_recent_event(topic, ev_id)
        events["last_processed_event_id"] = event_ids[-1]
        # NOTE: do NOT touch events.last_processed_ts — that field is the
        # dispatcher's inbox-drain watermark (spawn_topic_agent.md §4). The
        # event_id ring + last_processed_event_id are the agent-side dedup.
    return len(kept)


# ---- main -----------------------------------------------------------------

def finalize(result):
    """Run the full finalize sequence for one review pass. Returns a result
    dict (also the JSON the CLI prints). Never raises on the normal failure
    paths — it persists an audit + releases the lock and returns status.
    """
    topic_file = result["topic_file"]
    thread_id = result["thread_id"]
    event_id = result["triggered_by_event_id"]
    lock_file = result.get("lock_file") or str(
        topic_store.lock_path_for(topic_file))
    skill_dir = result.get("skill_dir") or str(SCRIPT_DIR.parent)
    updates = result.get("topic_updates") or {}

    artifact = _build_artifact(result)

    # 1. Schema validation — the SAME check the dispatcher runs at drain time.
    schema_ok, schema_reason = reply_dispatcher._validate_artifact(artifact)

    # 2. Render validation (poison-loop guard) — only if schema is sane.
    render_ok, render_detail = (True, "")
    if schema_ok:
        render_ok, render_detail = _check_only(
            artifact["template"], artifact["vars"])

    if not (schema_ok and render_ok):
        reason = schema_reason if not schema_ok else "render_check_failed"
        detail = schema_reason if not schema_ok else render_detail
        # Topic-side audit + lock release. Do NOT write the artifact —
        # that creates the poison-loop bug.
        try:
            topic = topic_store.read(topic_file)
            topic_store.append_audit(
                topic,
                event="artifact_validation_failed",
                template=artifact.get("template"),
                reason=reason,
                error=detail[:500],
                triggered_by_event_id=event_id)
            topic_store.write_atomic(topic_file, topic)
        except (OSError, ValueError):
            pass
        topic_store.release_lock(lock_file)
        return {
            "status": "error",
            "topic_file": topic_file,
            "error": "artifact_validation_failed",
            "reason": reason,
            "detail": detail[:300],
        }

    # 3. Atomic-write the artifact.
    replies_dir = Path(skill_dir) / "cfg" / "replies"
    replies_dir.mkdir(parents=True, exist_ok=True)
    art_path = replies_dir / _artifact_filename(thread_id, event_id)
    tmp = art_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
    tmp.replace(art_path)

    # 4. Topic-side writeback (one read + one write_atomic). Wrapped so the
    #    lock is ALWAYS released even if the topic file vanished/corrupted
    #    after the artifact landed — otherwise we'd leak the lock until the
    #    10-min stale expiry. The artifact is already on disk, so the
    #    dispatcher will still post; only the topic-side persistence is lost.
    drain_ids = result.get("drain_event_ids") or [event_id]
    try:
        topic = topic_store.read(topic_file)
        remaining = _drain_events(topic, drain_ids)
        _apply_topic_updates(topic, updates)

        audit = dict(updates.get("audit") or {"event": "review_finalized"})
        audit.setdefault("triggered_by_event_id", event_id)
        topic_store.append_audit(topic, **audit)

        topic_store.write_atomic(topic_file, topic)
    except (OSError, ValueError) as exc:
        if not result.get("keep_lock"):
            topic_store.release_lock(lock_file)
        return {
            "status": "error",
            "topic_file": topic_file,
            "artifact": str(art_path),
            "error": "topic_writeback_failed",
            "detail": str(exc)[:300],
        }

    # 5. Release the lock (the agent's contract ends here).
    if not result.get("keep_lock"):
        topic_store.release_lock(lock_file)

    return {
        "status": "ok",
        "topic_file": topic_file,
        "artifact": str(art_path),
        "drained_event_ids": drain_ids,
        "events_remaining": remaining,
        "lock_released": not result.get("keep_lock"),
    }


def _load_result(args):
    if args.result_stdin:
        return json.loads(sys.stdin.read())
    with open(args.result_file, encoding="utf-8") as f:
        return json.load(f)


def _cli():
    ap = argparse.ArgumentParser(
        description="Finalize one topic-agent review pass (artifact write + "
                    "topic writeback + lock release).")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--result-file", help="Path to the result JSON.")
    src.add_argument("--result-stdin", action="store_true",
                     help="Read the result JSON from stdin.")
    ap.add_argument("--keep-lock", action="store_true",
                    help="Do not release the lock (testing / chained calls).")
    args = ap.parse_args()

    result = _load_result(args)
    if args.keep_lock:
        result["keep_lock"] = True

    for key in ("topic_file", "thread_id", "ticket_id",
                "triggered_by_event_id", "artifact"):
        if key not in result:
            print(json.dumps({"status": "error",
                              "error": f"missing_input_field: {key}"},
                             ensure_ascii=False))
            return 2

    out = finalize(result)
    # Force UTF-8 so Chinese audit values survive a GBK stdout.
    import io
    w = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    w.write(json.dumps(out, ensure_ascii=False))
    w.write("\n")
    w.flush()
    return 0 if out.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(_cli())
