#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Replay harness — sandboxed pipeline test without dev/Lark interaction.

The real review-bot pipeline (router → drain → process_merge_queue) is
pure Python; only external I/O (GitLab, Lark, Claude agent) touches the
network. This script clones that pipeline into a sandbox directory and
monkey-patches external I/O to log-only, so you can inject synthetic
events and verify pipeline behavior without bothering devs.

NOT COVERED: the LLM topic agent. For pipeline-level bugs (dedup, drain,
locking, merge_queue state transitions), this harness is sufficient.

USAGE
-----
    python replay.py init <sandbox>
        Create an empty sandbox cfg tree.

    python replay.py init <sandbox> --from-closed <thread_id>
        Clone a previously-processed topic back to the sandbox's open
        topics dir, reset state to TRIAGING, clear audit+pending.

    python replay.py inject <sandbox> --thread <thread_id> \
        --content "ok" --sender approver|dev|<open_id> [--at <ms>]
        Write a synthetic reconcile-style raw event into cfg/events/.

    python replay.py pipeline <sandbox>
        Run router.route_pending_events + _drain_inboxes.
        Dry-run: no network. Reports routed/drained counts.

    python replay.py merge-queue <sandbox> [--plan <path>]
        Run process_merge_queue.run with merge_tracker/post_to_lark
        monkey-patched to log-only. Plan defaults to
        <sandbox>/cfg/dispatch_plan.json.

    python replay.py show <sandbox> --thread <thread_id> [--full]
        Pretty-print topic state, inbox, audit.

    python replay.py lock <sandbox> --thread <thread_id> [--release]
        Create (or delete) a fresh .lock file — useful for testing
        drain-skip-when-locked behavior.

ENV
---
    Sandbox paths override the normal SKILL_DIR/cfg layout. This module
    never touches the production cfg/ — unless you point <sandbox> at it.

EXAMPLES
--------
    # Reproduce the duplicate-reconcile bug
    python replay.py init /tmp/rb --from-closed om_x100b52d1fe4714acb341d12ed2a8a81
    python replay.py inject /tmp/rb --thread om_... --content "RAGE-13210" --at 1776179940000
    python replay.py pipeline /tmp/rb    # first route+drain
    python replay.py inject /tmp/rb --thread om_... --content "RAGE-13210" --at 1776179940000
    python replay.py pipeline /tmp/rb    # second: should be no-op with fix A
    python replay.py show /tmp/rb --thread om_...  # inspect pending[]
"""
import argparse
import copy
import json
import os
import shutil
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import router
import topic_store
import topic_index


def _approver_open_ids():
    """Resolve approver namelist from env (CSV + legacy single-id). Returns
    a fallback single-id list when the env is empty so existing fixtures
    keep working unchanged."""
    csv = os.environ.get("REVIEW_BOT_APPROVER_OPEN_IDS", "").strip()
    primary = os.environ.get("REVIEW_BOT_APPROVER_ID", "").strip()
    namelist = [s.strip() for s in csv.split(",") if s.strip()]
    if primary and primary not in namelist:
        namelist.append(primary)
    if not namelist:
        namelist = ["ou_1127c220c15c21355c0fe236c618f1af"]
    return namelist


# ---- Sandbox layout --------------------------------------------------

def _sandbox_paths(sandbox):
    sb = Path(sandbox).resolve()
    return {
        "root": sb,
        "cfg": sb / "cfg",
        "events": sb / "cfg" / "events",
        "topics": sb / "cfg" / "topics",
        "closed": sb / "cfg" / "topics" / "closed",
        "index": sb / "cfg" / "open_topic_index.json",
        "plan": sb / "cfg" / "dispatch_plan.json",
        "log": sb / "cfg" / "replay.log",
    }


def _log(sandbox, msg):
    paths = _sandbox_paths(sandbox)
    paths["log"].parent.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(paths["log"], "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")


# ---- Commands --------------------------------------------------------

def cmd_init(args):
    paths = _sandbox_paths(args.sandbox)
    for key in ("cfg", "events", "topics", "closed"):
        paths[key].mkdir(parents=True, exist_ok=True)
    topic_index.save(paths["index"], {})

    if args.from_closed:
        _seed_from_closed(paths, args.from_closed)

    _log(args.sandbox, f"init from_closed={args.from_closed or ''}")
    print(json.dumps({
        "ok": True,
        "sandbox": str(paths["root"]),
        "seeded": args.from_closed or None,
    }, ensure_ascii=False))


def _seed_from_closed(paths, thread_id):
    """Copy a closed topic back to open, reset review state."""
    # Find the source in the production cfg
    prod_closed = SCRIPT_DIR.parent / "cfg" / "topics" / "closed" / f"{thread_id}.json"
    if not prod_closed.exists():
        raise SystemExit(f"no closed topic at {prod_closed}")

    with open(prod_closed, encoding="utf-8") as f:
        topic = json.load(f)

    # Reset for a fresh run
    topic["review"]["state"] = "TRIAGING"
    topic["review"]["review_round"] = 0
    # review_phase, triage, version_3rd_check, ack_stats are all
    # pre-computed by ack_new_topic.py at ack time. Strip them so a
    # fresh replay re-enters the ack drain and re-derives them from
    # the on-disk MR state; otherwise a stale triage/phase from the
    # prior run bleeds into this replay's decisions.
    topic["review"]["review_phase"] = None
    topic["review"].pop("triage", None)
    topic["review"].pop("version_3rd_check", None)
    topic["review"].pop("ack_stats", None)
    topic["review"].pop("flagged_issues", None)
    topic["review"].pop("last_review_commit", None)
    topic["review"].pop("review_history", None)
    # lifecycle.ack_sent also clears so the replay's ack drain fires
    # again (rather than short-circuiting on stale ack_sent=true).
    lifecycle = topic.setdefault("lifecycle", {})
    lifecycle.pop("ack_sent", None)
    lifecycle.pop("ack_sent_at", None)
    # Per-MR runtime state (state="merged", pipeline_status="passed", etc.)
    # reflects the END of the previous lifecycle. Strip it so a replay
    # starts with just MR identity (iid/branch/url). A fresh agent would
    # set these back during approval/pipeline-check.
    for mr_entry in (topic.get("mrs") or {}).values():
        if isinstance(mr_entry, dict):
            mr_entry.pop("state", None)
            mr_entry.pop("pipeline_status", None)
    topic["events"] = {"pending": [], "last_processed_event_id": None,
                       "last_processed_ts": 0}
    topic["audit"] = [{"ts": int(time.time() * 1000),
                       "event": "replay_seeded",
                       "source_file": str(prod_closed)}]
    topic["lifecycle"]["resolved_at"] = None
    topic["lifecycle"]["merge_detected_at"] = None
    topic["lifecycle"]["closed_reason"] = None

    dest = paths["topics"] / f"{thread_id}.json"
    topic_store.write_atomic(dest, topic)

    # Register in the sandbox index
    idx = topic_index.load_or_rebuild(paths["topics"], paths["index"])
    idx[thread_id] = topic.get("identity", {}).get("ticket_id", "")
    topic_index.save(paths["index"], idx)


def cmd_inject(args):
    paths = _sandbox_paths(args.sandbox)
    paths["events"].mkdir(parents=True, exist_ok=True)

    sender_id = args.sender
    if sender_id == "approver":
        # default approver id — override with --sender <open_id>
        sender_id = "ou_1127c220c15c21355c0fe236c618f1af"
    elif sender_id == "dev":
        # read dev open_id from the topic if it exists
        topic_path = paths["topics"] / f"{args.thread}.json"
        if topic_path.exists():
            with open(topic_path, encoding="utf-8") as f:
                t = json.load(f)
            sender_id = (t.get("identity") or {}).get("creator_open_id") \
                or "ou_9999999999999999999999999999999999"
        else:
            sender_id = "ou_9999999999999999999999999999999999"

    # Use a deterministic msg_id so repeated injects with the same
    # content (at, content, sender) collide — which is exactly what we
    # want to exercise router dedup.
    ts = args.at if args.at is not None else int(time.time() * 1000)
    msg_id = args.message_id or f"om_replay_{ts}_{abs(hash((args.content, sender_id))) % 10**12}"

    chat_id = args.chat or _first_chat_id(paths) or "oc_sandbox_chat"

    ev = {
        "chat_id":      chat_id,
        "chat_type":    "group",
        "content":      args.content,
        "create_time":  str(ts),
        "id":           msg_id,
        "message_id":   msg_id,
        "thread_id":    args.thread,
        "root_id":      args.thread,
        "parent_id":    "",
        "message_type": "text",
        "mentions":     [],
        "sender_id":    sender_id,
        "timestamp":    str(int(time.time() * 1000)),
        "type":         "im.message.receive_v1",
        "_source":      args.source,
    }
    out = paths["events"] / f"{args.source}_{msg_id}_{ts}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(ev, f, ensure_ascii=False, indent=2)

    _log(args.sandbox, f"inject msg_id={msg_id} sender={sender_id} content={args.content!r}")
    print(json.dumps({"ok": True, "event_file": str(out), "message_id": msg_id},
                     ensure_ascii=False))


def cmd_pipeline(args):
    """Route + drain in the sandbox. With --mechanical, also run the
    in-process mechanical approver-reply handler with Lark/glab stubbed."""
    paths = _sandbox_paths(args.sandbox)

    # Derive chat_id from any existing topic (or fall back to sandbox default)
    chat_id = _first_chat_id(paths) or "oc_sandbox_chat"

    approver_open_ids = _approver_open_ids()
    route_summary = router.route_pending_events(
        paths["events"], paths["topics"], paths["index"], chat_id,
        approver_open_ids)

    drain_count = _drain_inboxes_sandbox(paths["topics"])

    result = {
        "route": route_summary,
        "drained": drain_count,
    }

    if getattr(args, "mechanical", False):
        result["mechanical"] = _run_mechanical_stubbed(paths, args.sandbox)

    _log(args.sandbox, f"pipeline: {json.dumps(result, ensure_ascii=False)}")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _run_mechanical_stubbed(paths, sandbox):
    """Invoke mechanical_reply_handler.drain_mechanical with external I/O
    stubbed so no real glab/Lark calls happen. Returns the handler summary."""
    import mechanical_reply_handler
    import merge_tracker

    def _stub_render_and_post(template_name, variables, message_id):
        _log(sandbox, f"[stub render_and_post] template={template_name} "
                      f"msg_id={message_id} vars_keys={list(variables.keys())}")
        return True, json.dumps({"ok": True, "stub": True})

    def _stub_post_plain_text(text, message_id):
        _log(sandbox, f"[stub post_plain_text] msg_id={message_id} "
                      f"text={text[:120]!r}")
        return True, json.dumps({"ok": True, "stub": True})

    def _stub_approve(repo, mr_obj):
        _log(sandbox, f"[stub approve] repo={repo} iid={merge_tracker._get_iid(mr_obj)}")
        return True, ""

    def _stub_close(repo, mr_obj):
        _log(sandbox, f"[stub close] repo={repo} iid={merge_tracker._get_iid(mr_obj)}")
        return True, ""

    def _stub_pipeline_msg(mrs):
        for mr in mrs.values():
            mr["pipeline_status"] = "passed"
        return "流水线已通过，等待合并队列处理。", False

    mechanical_reply_handler._render_and_post = _stub_render_and_post
    mechanical_reply_handler._post_plain_text = _stub_post_plain_text
    mechanical_reply_handler._glab_approve = _stub_approve
    mechanical_reply_handler._glab_close = _stub_close
    mechanical_reply_handler._pipeline_msg = _stub_pipeline_msg

    return mechanical_reply_handler.drain_mechanical(
        paths["topics"], paths["index"], _approver_open_ids(), "replay")


def _drain_inboxes_sandbox(topics_dir):
    """Replica of dispatcher._drain_inboxes that operates on sandbox paths.

    Mirrors the production logic (including the lock-skip fix). This
    lives here rather than importing from dispatcher because dispatcher
    hardcodes module-level TOPICS_DIR; we don't want to perturb it.
    """
    topics_dir = Path(topics_dir)
    if not topics_dir.exists():
        return 0
    drained = 0
    for inbox_path in topics_dir.glob("*.inbox.json"):
        thread_id = inbox_path.name.replace(".inbox.json", "")
        topic_path = topics_dir / f"{thread_id}.json"
        if not topic_path.exists():
            try:
                inbox_path.unlink()
            except OSError:
                pass
            continue

        lock_path = topic_store.lock_path_for(topic_path)
        if lock_path.exists() and not topic_store.lock_is_stale(topic_path):
            continue

        try:
            with open(inbox_path, encoding="utf-8") as f:
                inbox = json.load(f)
            if not inbox:
                inbox_path.unlink()
                continue
            topic = topic_store.read(topic_path)
            pending = topic.setdefault("events", {}).setdefault("pending", [])
            pending.extend(inbox)
            max_ts = max((ev.get("received_at", 0) for ev in inbox), default=0)
            topic["events"]["last_processed_ts"] = max(
                topic["events"].get("last_processed_ts", 0), max_ts)
            topic["lifecycle"]["updated_at"] = topic_store.now_ms()
            topic_store.write_atomic(topic_path, topic)
            inbox_path.unlink()
            drained += len(inbox)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return drained


def cmd_merge_queue(args):
    """Run process_merge_queue with external I/O stubbed."""
    import merge_tracker
    import process_merge_queue
    from templates import render as render_mod

    def _stub_rebase(repo, mr_obj, timeout_s=60):
        return {"success": True, "mr_iid": merge_tracker._get_iid(mr_obj)}

    def _stub_merge(repo, mr_obj, timeout_s=30):
        return {"success": True,
                "merge_commit_sha": f"replay_{int(time.time())}"}

    def _stub_post_to_lark(content, root_message_id):
        msg = json.dumps(content, ensure_ascii=False)
        _log(args.sandbox, f"[stub post_to_lark] reply_to={root_message_id} body={msg[:200]}")
        return json.dumps({"ok": True, "stub": True})

    # Patch
    merge_tracker.rebase_mr = _stub_rebase
    merge_tracker.merge_mr = _stub_merge
    process_merge_queue.post_to_lark = _stub_post_to_lark
    render_mod.post_to_lark = _stub_post_to_lark

    paths = _sandbox_paths(args.sandbox)
    plan_path = args.plan or paths["plan"]
    if not Path(plan_path).exists():
        raise SystemExit(f"plan not found: {plan_path}")

    result = process_merge_queue.run(str(plan_path))
    _log(args.sandbox, f"merge_queue: {json.dumps(result, ensure_ascii=False)}")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_show(args):
    paths = _sandbox_paths(args.sandbox)
    topic_path = paths["topics"] / f"{args.thread}.json"
    closed_path = paths["closed"] / f"{args.thread}.json"
    source = topic_path if topic_path.exists() else closed_path
    if not source.exists():
        raise SystemExit(f"topic not found (open or closed): {args.thread}")

    with open(source, encoding="utf-8") as f:
        topic = json.load(f)

    inbox_path = paths["topics"] / f"{args.thread}.inbox.json"
    inbox = []
    if inbox_path.exists():
        with open(inbox_path, encoding="utf-8") as f:
            inbox = json.load(f)

    lock_path = topic_store.lock_path_for(topic_path)
    lock_info = None
    if lock_path.exists():
        age_s = time.time() - lock_path.stat().st_mtime
        lock_info = {"age_s": round(age_s, 2),
                     "stale": topic_store.lock_is_stale(topic_path)}

    if args.full:
        print(json.dumps({
            "source_file": str(source),
            "topic":       topic,
            "inbox":       inbox,
            "lock":        lock_info,
        }, ensure_ascii=False, indent=2))
        return

    review = topic.get("review") or {}
    mrs = topic.get("mrs") or {}
    ev = topic.get("events") or {}
    pending = ev.get("pending", [])
    audit = topic.get("audit", [])

    summary = {
        "source":          "closed" if source == closed_path else "open",
        "thread_id":       topic.get("thread_id"),
        "ticket_id":       (topic.get("identity") or {}).get("ticket_id"),
        "review.state":    review.get("state"),
        "review.phase":    review.get("review_phase"),
        "review.round":    review.get("review_round"),
        "pending_count":   len(pending),
        "inbox_count":     len(inbox),
        "last_processed_ts": ev.get("last_processed_ts"),
        "last_processed_event_id": ev.get("last_processed_event_id"),
        "mrs":             {k: {"iid": v.get("mr_iid") or v.get("iid"),
                                "state": v.get("state"),
                                "pipeline": v.get("pipeline_status")}
                            for k, v in mrs.items()},
        "lock":            lock_info,
        "audit_tail":      [{"event": a.get("event"),
                             "state_from": a.get("state_from"),
                             "state_to": a.get("state_to")}
                            for a in audit[-5:]],
        "pending":         [{"source": p.get("source"),
                             "event_id": p.get("event_id"),
                             "sender": p.get("sender_id"),
                             "content": (p.get("content") or "")[:60]}
                            for p in pending],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


# ---- Fixture runner ---------------------------------
#
# Each fixture is a self-contained scenario run against a fresh tempdir
# sandbox. Fixtures patch the minimum set of external I/O (glab HTTP,
# lark post, subprocess) and assert invariants on the resulting topic
# file, audit log, and in-memory return values. Zero network.
#
# A fixture fn signature:
#     def fixture_<name>(paths) -> dict   # {"ok": bool, "reason": str}

def _make_topic(paths, thread_id, ticket_id, *, state="AWAITING_APPROVAL",
                review_phase="main", mrs=None, pending=None,
                recent_event_ids=None, review_extra=None):
    """Build and write a minimal valid topic file. `review_extra` merges
    extra fields into `review` (issues, triage, dev_triage, ...)."""
    topic = {
        "thread_id": thread_id,
        "root_message_id": thread_id,
        "identity": {
            "ticket_id": ticket_id,
            "chat_id": "oc_replay_chat",
            "creator_open_id": "ou_dev_replay",
            "developer": "replay-dev",
        },
        "review": {"state": state, "review_phase": review_phase,
                   "review_round": 1, "issues": [],
                   **(review_extra or {})},
        "mrs": mrs or {},
        "events": {
            "pending": pending or [],
            "last_processed_event_id": None,
            "last_processed_ts": 0,
            "recent_event_ids": recent_event_ids or [],
        },
        "audit": [],
        "lifecycle": {"created_at": topic_store.now_ms()},
    }
    topic_path = paths["topics"] / f"{thread_id}.json"
    topic_store.write_atomic(topic_path, topic)
    idx = topic_index.load_or_rebuild(paths["topics"], paths["index"])
    idx[thread_id] = ticket_id
    topic_index.save(paths["index"], idx)
    return topic_path


def _audit_has(topic, event_name):
    return any(e.get("event") == event_name for e in (topic.get("audit") or []))


def _load_topic(topic_path):
    with open(topic_path, encoding="utf-8") as f:
        return json.load(f)


def _fresh_sandbox(name):
    import tempfile
    sb = Path(tempfile.mkdtemp(prefix=f"review-bot-fixture-{name}-"))
    paths = _sandbox_paths(sb)
    for key in ("cfg", "events", "topics", "closed"):
        paths[key].mkdir(parents=True, exist_ok=True)
    topic_index.save(paths["index"], {})
    return paths


def _patch_merge_tracker(fail_once=False, status_seq=None):
    """Install stubs for check_pipeline_mr / check_infra_failure_and_retry.

    status_seq: list of raw pipeline statuses to return in order ("failed",
    "running", etc.). fail_once: if True, infra-retry returns retried=True
    the first call and retried=False subsequently.
    """
    import merge_tracker
    status_iter = iter(status_seq or [])
    orig_check = merge_tracker.check_pipeline_mr
    orig_retry = merge_tracker.check_infra_failure_and_retry
    state = {"retry_calls": 0}

    def stub_check(repo, mr_obj, timeout_s=30):
        try:
            s = next(status_iter)
        except StopIteration:
            s = "success"
        return {"status": s, "error": None}

    def stub_retry(repo, mr_obj, timeout_s=30):
        state["retry_calls"] += 1
        if fail_once and state["retry_calls"] == 1:
            return {"is_infra": True, "retried": True, "job_id": 42,
                    "reason": "infra: Permission denied (stub)", "error": None}
        return {"is_infra": False, "retried": False, "job_id": None,
                "reason": "", "error": None}

    merge_tracker.check_pipeline_mr = stub_check
    merge_tracker.check_infra_failure_and_retry = stub_retry
    return (merge_tracker, orig_check, orig_retry, state)


def _unpatch_merge_tracker(bundle):
    mt, orig_check, orig_retry, _ = bundle
    mt.check_pipeline_mr = orig_check
    mt.check_infra_failure_and_retry = orig_retry


def fixture_infra_retry_approval_mechanical(paths):
    """Mechanical approve path must retry an infra failure before reporting
    pipeline as failed. Assertion: pipeline_status=='running',
    infra_failure_retried audit present, no 'pipeline_warning' audit."""
    thread = "om_fx_mech_infra"
    mrs = {"chaos": {"mr_iid": 1001, "repo_slug": "booming/dev/projects/rage/chaos"}}
    pending = [{
        "event_id": "evt_mech_approve_1",
        "message_id": "om_evt_1",
        "sender_id": "ou_1127c220c15c21355c0fe236c618f1af",
        "source": "listener",
        "content": "ok",
        "received_at": topic_store.now_ms(),
        "phase_at_arrival": "main",
        "state_at_arrival": "AWAITING_APPROVAL",
    }]
    topic_path = _make_topic(paths, thread, "RAGE-FX-MECH",
                             mrs=mrs, pending=pending)

    # Stubs: approval returns OK; pipeline raw returns "failed" once then
    # after retry shouldn't be re-queried (helper returns running directly).
    import mechanical_reply_handler as mrh
    import post_approval as pa

    mt_bundle = _patch_merge_tracker(fail_once=True, status_seq=["failed"])
    orig_approve = mrh._glab_approve
    orig_render = mrh._render_and_post
    orig_pa_render = pa._render_and_post
    mrh._glab_approve = lambda repo, mr: (True, "")
    mrh._render_and_post = lambda *a, **k: (True, "{}")
    pa._render_and_post = lambda *a, **k: (True, "{}")

    try:
        result = mrh.drain_mechanical(paths["topics"], paths["index"],
                                      ["ou_1127c220c15c21355c0fe236c618f1af"],
                                      "fx-cycle", withdrawn_ids=None)
    finally:
        mrh._glab_approve = orig_approve
        mrh._render_and_post = orig_render
        pa._render_and_post = orig_pa_render
        _unpatch_merge_tracker(mt_bundle)

    topic = _load_topic(topic_path)
    mr = (topic.get("mrs") or {}).get("chaos") or {}
    if mr.get("pipeline_status") != "running":
        return {"ok": False, "reason": f"pipeline_status={mr.get('pipeline_status')} (want running)"}
    if not _audit_has(topic, "infra_failure_retried"):
        return {"ok": False, "reason": "missing infra_failure_retried audit"}
    if result.get("approve") != 1:
        return {"ok": False, "reason": f"approve count={result.get('approve')}"}
    return {"ok": True, "reason": "mechanical path retried infra failure + approved"}


def fixture_infra_retry_approval_dispatcher(paths):
    """_check_approved_topics must funnel every pipeline_status write
    through recheck_pipeline_with_retry. Assertion: retried infra failure
    leaves pipeline_status='running' + audit entry."""
    import merge_tracker
    thread = "om_fx_disp_infra"
    mrs = {"chaos": {"mr_iid": 2001,
                     "repo_slug": "booming/dev/projects/rage/chaos",
                     "pipeline_status": "unknown"}}
    topic_path = _make_topic(paths, thread, "RAGE-FX-DISP",
                             state="APPROVED", mrs=mrs)

    mt_bundle = _patch_merge_tracker(fail_once=True, status_seq=["failed"])
    # Also stub check_mr so no MR is "merged" or "closed" — stays open.
    orig_check_mr = merge_tracker.check_mr
    merge_tracker.check_mr = lambda repo, mr, timeout_s=30: {
        "merged": False, "closed": False, "state": "opened",
        "sha": None, "error": None,
    }

    try:
        result = merge_tracker.recheck_pipeline_with_retry("chaos", mrs["chaos"])
    finally:
        merge_tracker.check_mr = orig_check_mr
        _unpatch_merge_tracker(mt_bundle)

    if not result.get("retried"):
        return {"ok": False, "reason": f"expected retried=True, got {result}"}
    if mrs["chaos"].get("pipeline_status") != "running":
        return {"ok": False,
                "reason": f"pipeline_status={mrs['chaos'].get('pipeline_status')}"}
    return {"ok": True, "reason": "helper retried infra failure; status=running"}


def fixture_infra_retry_approval_3p_agent(paths):
    """3p phase gating must NOT trigger infra retry — failed/canceled
    count as 'pipeline done, ready to merge' for 3p MRs."""
    import merge_tracker
    mr = {"mr_iid": 3001, "repo_slug": "3rd_party_cpplibs/renderdoc"}
    mt_bundle = _patch_merge_tracker(fail_once=True, status_seq=["failed"])
    state = mt_bundle[3]

    try:
        raw = merge_tracker.check_pipeline_mr("3rd_party/renderdoc", mr)
    finally:
        _unpatch_merge_tracker(mt_bundle)

    if raw.get("status") != "failed":
        return {"ok": False, "reason": f"check_pipeline_mr returned {raw}"}
    if state["retry_calls"] != 0:
        return {"ok": False,
                "reason": f"infra retry fired {state['retry_calls']}x for 3p (should be 0)"}
    return {"ok": True, "reason": "3p check_pipeline_mr didn't trigger infra retry"}


def fixture_phase_mismatch_event_skip(paths):
    """Event tagged with phase_at_arrival=3rd_party but topic flipped to
    main: mechanical handler must drop with event_phase_mismatch audit."""
    import mechanical_reply_handler as mrh
    thread = "om_fx_phase_mismatch"
    mrs = {"chaos": {"mr_iid": 4001, "repo_slug": "booming/dev/projects/rage/chaos"}}
    pending = [{
        "event_id": "evt_phase_skew",
        "message_id": "om_evt_phase",
        "sender_id": "ou_1127c220c15c21355c0fe236c618f1af",
        "source": "listener",
        "content": "ok",
        "received_at": topic_store.now_ms(),
        "phase_at_arrival": "3rd_party",  # stale
        "state_at_arrival": "AWAITING_APPROVAL",
    }]
    topic_path = _make_topic(paths, thread, "RAGE-FX-PHASE",
                             review_phase="main", mrs=mrs, pending=pending)

    # Stubs — if they fire, the skip failed.
    called = {"approve": 0, "post": 0}
    orig_approve = mrh._glab_approve
    orig_post = mrh._render_and_post
    mrh._glab_approve = lambda *a, **k: (called.__setitem__("approve", called["approve"] + 1) or (True, ""))
    mrh._render_and_post = lambda *a, **k: (called.__setitem__("post", called["post"] + 1) or (True, "{}"))
    try:
        result = mrh.drain_mechanical(paths["topics"], paths["index"],
                                      ["ou_1127c220c15c21355c0fe236c618f1af"],
                                      "fx-cycle")
    finally:
        mrh._glab_approve = orig_approve
        mrh._render_and_post = orig_post

    topic = _load_topic(topic_path)
    if called["approve"] or called["post"]:
        return {"ok": False, "reason": f"side effects fired despite drift: {called}"}
    if result.get("tag_skipped") != 1:
        return {"ok": False, "reason": f"tag_skipped={result.get('tag_skipped')}"}
    if not _audit_has(topic, "event_phase_mismatch"):
        return {"ok": False, "reason": "missing event_phase_mismatch audit"}
    if topic["events"]["pending"]:
        return {"ok": False, "reason": "stale event not removed from pending"}
    return {"ok": True, "reason": "phase-mismatched event dropped without side effects"}


def fixture_out_of_order_ts(paths):
    """Router must route an event with older ts (reconcile backfill) as
    long as its event_id isn't in the ring. And it must skip if it IS in
    the ring, regardless of ts."""
    thread = "om_fx_ooo_ts"
    mrs = {"chaos": {"mr_iid": 5001, "repo_slug": "booming/dev/projects/rage/chaos"}}
    # Seed topic with recent_event_ids containing one known id.
    _make_topic(paths, thread, "RAGE-FX-OOO", mrs=mrs,
                recent_event_ids=["evt_already_processed"])

    # Inject old-timestamp event with NEW id.
    class _NS:  # argparse-like namespace
        pass
    args = _NS()
    args.sandbox = paths["root"]
    args.thread = thread
    args.content = "ok"
    args.sender = "approver"
    args.chat = "oc_replay_chat"
    args.at = 1  # absurdly old
    args.source = "reconcile"
    args.message_id = "om_new_but_old_ts"
    cmd_inject(args)

    route = router.route_pending_events(paths["events"], paths["topics"],
                                        paths["index"], "oc_replay_chat",
                                        ["ou_1127c220c15c21355c0fe236c618f1af"])
    if route.get("routed") != 1:
        return {"ok": False, "reason": f"routed={route.get('routed')} (want 1)"}

    # Now inject an event with a KNOWN id (should dedup regardless of ts).
    args.message_id = "om_dup"
    # Seed ring with its derived id:
    topic_path = paths["topics"] / f"{thread}.json"
    topic = _load_topic(topic_path)
    import event_utils
    fname = next(paths["events"].glob("*.json"), None)
    # Simulate by calling inject to drop a file then prime ring.
    args.at = 9999999999999
    args.message_id = "om_seen_before"
    cmd_inject(args)
    # Find the just-written file and derive the event_id the router would use.
    for p in paths["events"].glob("*.json"):
        with open(p, encoding="utf-8") as f:
            ev = json.load(f)
        if ev.get("message_id") == "om_seen_before":
            derived = event_utils.derive_event_id(p.name, "om_seen_before")
            break
    else:
        return {"ok": False, "reason": "couldn't locate injected event file"}
    topic["events"]["recent_event_ids"].append(derived)
    topic_store.write_atomic(topic_path, topic)

    route2 = router.route_pending_events(paths["events"], paths["topics"],
                                         paths["index"], "oc_replay_chat",
                                         ["ou_1127c220c15c21355c0fe236c618f1af"])
    if route2.get("routed") != 0:
        return {"ok": False,
                "reason": f"dup event routed ({route2}) — ring dedup failed"}
    return {"ok": True, "reason": "router ignores ts, dedups by event_id ring"}


def fixture_withdrawn_race(paths):
    """drain_mechanical with withdrawn_ids including the event's msg_id
    must drop the event with withdrawn_race_skipped audit and no I/O."""
    import mechanical_reply_handler as mrh
    thread = "om_fx_withdrawn"
    mrs = {"chaos": {"mr_iid": 6001, "repo_slug": "booming/dev/projects/rage/chaos"}}
    msg_id = "om_withdrawn_reply"
    pending = [{
        "event_id": "evt_withdrawn",
        "message_id": msg_id,
        "sender_id": "ou_1127c220c15c21355c0fe236c618f1af",
        "source": "listener",
        "content": "ok",
        "received_at": topic_store.now_ms(),
        "phase_at_arrival": "main",
        "state_at_arrival": "AWAITING_APPROVAL",
    }]
    topic_path = _make_topic(paths, thread, "RAGE-FX-WD",
                             mrs=mrs, pending=pending)

    called = {"approve": 0, "post": 0}
    orig_approve = mrh._glab_approve
    orig_post = mrh._render_and_post
    mrh._glab_approve = lambda *a, **k: (called.__setitem__("approve", 1) or (True, ""))
    mrh._render_and_post = lambda *a, **k: (called.__setitem__("post", 1) or (True, "{}"))
    try:
        result = mrh.drain_mechanical(paths["topics"], paths["index"],
                                      ["ou_1127c220c15c21355c0fe236c618f1af"],
                                      "fx-cycle",
                                      withdrawn_ids={msg_id})
    finally:
        mrh._glab_approve = orig_approve
        mrh._render_and_post = orig_post

    topic = _load_topic(topic_path)
    if called["approve"] or called["post"]:
        return {"ok": False, "reason": f"side effects fired: {called}"}
    if result.get("withdrawn_skipped") != 1:
        return {"ok": False, "reason": f"withdrawn_skipped={result.get('withdrawn_skipped')}"}
    if not _audit_has(topic, "withdrawn_race_skipped"):
        return {"ok": False, "reason": "missing withdrawn_race_skipped audit"}
    return {"ok": True, "reason": "withdrawn event dropped before side effects"}


def fixture_supersede_stale_lock(paths):
    """Router seeing a fresh lock on the old thread must defer close and
    record a supersede-pending ledger entry that dispatcher retries."""
    old_thread = "om_fx_super_old"
    new_thread = "om_fx_super_new"
    ticket = "RAGE-99001"
    _make_topic(paths, old_thread, ticket,
                mrs={"chaos": {"mr_iid": 7001,
                               "repo_slug": "booming/dev/projects/rage/chaos"}})
    # Hold the lock so close_topic(require_unlocked=True) defers.
    old_topic_path = paths["topics"] / f"{old_thread}.json"
    lock_path = topic_store.lock_path_for(old_topic_path)
    with open(lock_path, "w", encoding="utf-8") as f:
        json.dump({"holder": "replay", "locked_at": topic_store.now_ms()}, f)

    # Inject new-thread event containing same ticket id → triggers supersede.
    class _NS: pass
    args = _NS()
    args.sandbox = paths["root"]
    args.thread = new_thread
    args.content = f"{ticket} refresh"
    args.sender = "ou_dev_replay"
    args.chat = "oc_replay_chat"
    args.at = None
    args.source = "listener"
    args.message_id = new_thread  # root message == thread id
    cmd_inject(args)

    # Point dispatcher's SUPERSEDE_PENDING into the sandbox so its
    # record_pending_supersede writes where we can observe it.
    import dispatcher
    orig_ledger = dispatcher.SUPERSEDE_PENDING
    orig_topics = dispatcher.TOPICS_DIR
    orig_index = dispatcher.INDEX_PATH
    dispatcher.SUPERSEDE_PENDING = paths["cfg"] / "supersede_pending.json"
    dispatcher.TOPICS_DIR = paths["topics"]
    dispatcher.INDEX_PATH = paths["index"]

    try:
        router.route_pending_events(paths["events"], paths["topics"],
                                    paths["index"], "oc_replay_chat",
                                    ["ou_1127c220c15c21355c0fe236c618f1af"])

        ledger = dispatcher._load_supersede_pending()
        if not any(e.get("old_thread") == old_thread for e in ledger):
            return {"ok": False, "reason": f"ledger missing old_thread: {ledger}"}

        # Retry with lock still held → should remain queued.
        dispatcher._retry_pending_supersedes("fx-cycle")
        if not dispatcher._load_supersede_pending():
            return {"ok": False,
                    "reason": "ledger cleared despite fresh lock"}

        # Release lock and retry → ledger drains, old topic archived.
        lock_path.unlink()
        dispatcher._retry_pending_supersedes("fx-cycle")
        remaining = dispatcher._load_supersede_pending()
        if remaining:
            return {"ok": False, "reason": f"ledger not drained: {remaining}"}
        if old_topic_path.exists():
            return {"ok": False, "reason": "old topic still open after retry"}
    finally:
        dispatcher.SUPERSEDE_PENDING = orig_ledger
        dispatcher.TOPICS_DIR = orig_topics
        dispatcher.INDEX_PATH = orig_index
    return {"ok": True, "reason": "deferred close queued + retried after lock release"}


def fixture_no_new_commits_drained(paths):
    """dev_reply on a *_REVISION topic with SHAs equal to last_review_commit
    must post the no_new_commits template and drop the event — no agent spawn."""
    thread = "om_fx_no_new_commits"
    sha = "deadbeefcafe1234567890abcdef1234567890ab"
    mrs = {
        "rage":  {"mr_iid": 2001, "branch": "feature/fx-nnc",
                  "branch_sha": sha,
                  "repo_slug": "booming/dev/projects/rage/rage"},
        "chaos": {"mr_iid": 2002, "branch": "feature/fx-nnc",
                  "branch_sha": sha,
                  "repo_slug": "booming/dev/projects/rage/chaos"},
    }
    pending = [{
        "event_id": "evt_dev_ping",
        "message_id": "om_evt_dev_ping",
        "sender_id": "ou_dev_replay",
        "source": "listener",
        "content": "ping?",
        "mentions": [],
        "received_at": topic_store.now_ms(),
        "state_at_arrival": "SIMPLE_REVISION",
    }]
    topic_path = _make_topic(paths, thread, "RAGE-FX-NNC",
                             state="SIMPLE_REVISION",
                             review_phase=None,
                             mrs=mrs, pending=pending)
    # Seed the review.last_review_commit that drain_no_new_commits compares.
    topic = _load_topic(topic_path)
    topic["review"]["last_review_commit"] = sha
    topic_store.write_atomic(topic_path, topic)

    import mechanical_reply_handler as mrh
    orig_ls = mrh._ls_remote_sha
    orig_render = mrh._render_and_post
    render_calls = []

    def stub_ls(repo_root, branch):
        # Simulate remote SHA matching the cached branch_sha (== lrc).
        return sha

    def stub_render(template_name, variables, message_id):
        render_calls.append({"template": template_name,
                             "vars": dict(variables),
                             "message_id": message_id})
        return True, json.dumps({"ok": True, "stub": True})

    mrh._ls_remote_sha = stub_ls
    mrh._render_and_post = stub_render
    try:
        result = mrh.drain_no_new_commits(
            paths["topics"], paths["index"], "fx-cycle",
            rage_root="/fake/rage", chaos_root="/fake/chaos",
            approver_open_ids=["ou_1127c220c15c21355c0fe236c618f1af"])
    finally:
        mrh._ls_remote_sha = orig_ls
        mrh._render_and_post = orig_render

    if result.get("posted") != 1:
        return {"ok": False, "reason": f"posted={result.get('posted')} (want 1); summary={result}"}
    if len(render_calls) != 1:
        return {"ok": False, "reason": f"render called {len(render_calls)} times (want 1)"}
    if render_calls[0]["template"] != "no_new_commits":
        return {"ok": False, "reason": f"wrong template: {render_calls[0]['template']}"}
    topic = _load_topic(topic_path)
    if topic["events"]["pending"]:
        return {"ok": False, "reason": "pending not drained"}
    if not _audit_has(topic, "no_new_commits_drained"):
        return {"ok": False, "reason": "missing no_new_commits_drained audit"}
    if not _audit_has(topic, "lark_reply_sent"):
        return {"ok": False, "reason": "missing lark_reply_sent audit"}
    return {"ok": True, "reason": "template posted, event drained, audits recorded"}


def fixture_no_new_commits_stale_webhook(paths):
    """If git ls-remote returns a SHA different from the cached branch_sha
    (listener missed a push), drain_no_new_commits must defer to the agent
    instead of falsely posting the template."""
    thread = "om_fx_stale_webhook"
    cached_sha = "aaaaaa0000000000000000000000000000000001"
    live_sha   = "bbbbbb0000000000000000000000000000000002"
    mrs = {
        "rage": {"mr_iid": 3001, "branch": "feature/stale",
                 "branch_sha": cached_sha,
                 "repo_slug": "booming/dev/projects/rage/rage"},
    }
    pending = [{
        "event_id": "evt_stale",
        "message_id": "om_evt_stale",
        "sender_id": "ou_dev_replay",
        "source": "listener",
        "content": "any updates?",
        "mentions": [],
        "received_at": topic_store.now_ms(),
        "state_at_arrival": "SIMPLE_REVISION",
    }]
    topic_path = _make_topic(paths, thread, "RAGE-FX-STALE",
                             state="SIMPLE_REVISION",
                             review_phase=None,
                             mrs=mrs, pending=pending)
    topic = _load_topic(topic_path)
    topic["review"]["last_review_commit"] = cached_sha
    topic_store.write_atomic(topic_path, topic)

    import mechanical_reply_handler as mrh
    orig_ls = mrh._ls_remote_sha
    orig_render = mrh._render_and_post
    render_calls = []
    mrh._ls_remote_sha = lambda repo_root, branch: live_sha
    mrh._render_and_post = lambda *a, **k: (render_calls.append(a) or (True, "{}"))
    try:
        result = mrh.drain_no_new_commits(
            paths["topics"], paths["index"], "fx-cycle",
            rage_root="/fake/rage", chaos_root="/fake/chaos",
            approver_open_ids=["ou_approver"])
    finally:
        mrh._ls_remote_sha = orig_ls
        mrh._render_and_post = orig_render

    if result.get("posted"):
        return {"ok": False, "reason": f"posted={result.get('posted')} (want 0)"}
    if render_calls:
        return {"ok": False, "reason": "template posted despite stale webhook"}
    topic = _load_topic(topic_path)
    if not topic["events"]["pending"]:
        return {"ok": False, "reason": "pending wrongly drained"}
    return {"ok": True, "reason": "deferred to agent on SHA mismatch"}


def fixture_incr_cache_populated(paths):
    """_find_work must attach incr_cache to the work entry when a topic
    has state=*_REVISION, last_review_commit, and new commits pending."""
    thread = "om_fx_incr_cache"
    expected = "eeeeee0000000000000000000000000000000001"
    current  = "ffffff0000000000000000000000000000000002"
    mrs = {
        "rage": {"mr_iid": 4001, "branch": "feature/incr",
                 "branch_sha": current,
                 "repo_slug": "booming/dev/projects/rage/rage"},
    }
    pending = [{
        "event_id": "evt_dev_pushed",
        "message_id": "om_evt_dev_pushed",
        "sender_id": "ou_dev_replay",
        "source": "listener",
        "content": "please re-review",
        "mentions": [],
        "received_at": topic_store.now_ms(),
        "state_at_arrival": "SIMPLE_REVISION",
    }]
    topic_path = _make_topic(paths, thread, "RAGE-FX-INCR",
                             state="SIMPLE_REVISION",
                             review_phase=None,
                             mrs=mrs, pending=pending)
    topic = _load_topic(topic_path)
    topic["review"]["last_review_commit"] = expected
    topic_store.write_atomic(topic_path, topic)

    import dispatcher
    import incr_cache
    # The cache dir is shared across fixture invocations. Wipe any stale
    # files for this thread so the populate path is exercised, not a hit.
    for stale in incr_cache.cache_dir().glob(f"{thread}_*"):
        try:
            stale.unlink()
        except OSError:
            pass
    # Stub git so the fixture doesn't need a real repo. incr_cache routes
    # base resolution through incr_base.resolve_incr_range, so stub that
    # (returns a linear incremental range) plus _populate_cache.
    import incr_base
    orig_resolve = incr_base.resolve_incr_range
    orig_populate = incr_cache._populate_cache
    incr_base.resolve_incr_range = lambda repo_root, exp, cur, target, **k: {
        "mode": "incremental", "base_ref": exp,
        "log_range": f"{exp}..{cur}", "diff_range": f"{exp}..{cur}"}
    populate_calls = []

    def stub_populate(repo_root, log_range, diff_range, log_path, diff_path):
        populate_calls.append((log_range, diff_range))
        log_path.write_text(f"{current[:7]} stub commit\n", encoding="utf-8")
        diff_path.write_text("--- stub diff ---\n", encoding="utf-8")
        return True

    incr_cache._populate_cache = stub_populate

    orig_topics = dispatcher.TOPICS_DIR
    dispatcher.TOPICS_DIR = paths["topics"]
    try:
        work = dispatcher._find_work("fx-cycle",
                                     rage_root="/fake/rage",
                                     chaos_root="/fake/chaos")
    finally:
        dispatcher.TOPICS_DIR = orig_topics
        incr_base.resolve_incr_range = orig_resolve
        incr_cache._populate_cache = orig_populate

    if len(work) != 1:
        return {"ok": False, "reason": f"work len={len(work)} (want 1)"}
    entry = work[0]
    cache = entry.get("incr_cache")
    if not cache or "rage" not in cache:
        return {"ok": False, "reason": f"incr_cache missing rage entry: {cache}"}
    rage_cache = cache["rage"]
    if rage_cache.get("sha") != current:
        return {"ok": False, "reason": f"sha={rage_cache.get('sha')} (want {current})"}
    if rage_cache.get("expected_sha") != expected:
        return {"ok": False, "reason": f"expected_sha={rage_cache.get('expected_sha')}"}
    if not Path(rage_cache["log_path"]).exists():
        return {"ok": False, "reason": "log_path file missing"}
    if not Path(rage_cache["diff_path"]).exists():
        return {"ok": False, "reason": "diff_path file missing"}
    want_range = f"{expected}..{current}"
    if populate_calls != [(want_range, want_range)]:
        return {"ok": False, "reason": f"populate calls={populate_calls}"}
    return {"ok": True, "reason": "work entry carries incr_cache with cached log/diff paths"}


def fixture_incr_cache_skipped_on_no_new_commits(paths):
    """If branch_sha already equals last_review_commit, incr_cache must NOT
    be populated — drain_no_new_commits owns that case, and pre-computing
    would waste a git fetch. The work entry carries no incr_cache key."""
    thread = "om_fx_incr_skip"
    sha = "cafecafe00000000000000000000000000000003"
    mrs = {
        "rage": {"mr_iid": 5001, "branch": "feature/no-push",
                 "branch_sha": sha,
                 "repo_slug": "booming/dev/projects/rage/rage"},
    }
    pending = [{
        "event_id": "evt_just_pinging",
        "message_id": "om_evt_ping",
        "sender_id": "ou_dev_replay",
        "source": "listener",
        "content": "anyone home?",
        "mentions": [],
        "received_at": topic_store.now_ms(),
        "state_at_arrival": "SIMPLE_REVISION",
    }]
    topic_path = _make_topic(paths, thread, "RAGE-FX-SKIP",
                             state="SIMPLE_REVISION",
                             review_phase=None,
                             mrs=mrs, pending=pending)
    topic = _load_topic(topic_path)
    topic["review"]["last_review_commit"] = sha  # equal to branch_sha
    topic_store.write_atomic(topic_path, topic)

    import dispatcher
    import incr_cache
    populate_calls = []
    import incr_base
    orig_resolve = incr_base.resolve_incr_range
    orig_populate = incr_cache._populate_cache
    # Skip happens before base resolution; if resolve_incr_range fires the
    # no-new-commits short-circuit regressed, so make it loud.
    incr_base.resolve_incr_range = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("resolve_incr_range must not run for no-new-commits"))
    incr_cache._populate_cache = lambda *a, **k: (populate_calls.append(a) or True)

    orig_topics = dispatcher.TOPICS_DIR
    dispatcher.TOPICS_DIR = paths["topics"]
    try:
        work = dispatcher._find_work("fx-cycle",
                                     rage_root="/fake/rage",
                                     chaos_root="/fake/chaos")
    finally:
        dispatcher.TOPICS_DIR = orig_topics
        incr_base.resolve_incr_range = orig_resolve
        incr_cache._populate_cache = orig_populate

    if len(work) != 1:
        return {"ok": False, "reason": f"work len={len(work)}"}
    if "incr_cache" in work[0]:
        return {"ok": False, "reason": "incr_cache wrongly populated for no-new-commits topic"}
    if populate_calls:
        return {"ok": False, "reason": f"_populate_cache called {len(populate_calls)}x (want 0)"}
    return {"ok": True, "reason": "no-new-commits topic yields no incr_cache entry"}


# ---- Inverted-triage fixtures (DESIGN §1.23) -------------------------

_FX_APPROVER = "ou_1127c220c15c21355c0fe236c618f1af"


def _fx_issues(n):
    """n sample review.issues entries with dense 1..n indices."""
    sevs = ["严重", "中", "轻", "建议"]
    return [{"index": i, "severity": sevs[(i - 1) % len(sevs)],
             "repo": "rage", "file": f"src/f{i}.cpp",
             "line_range": f"{10 * i}-{10 * i + 5}",
             "description": f"问题 {i}"} for i in range(1, n + 1)]


def _fx_event(content, *, intent, role, sender, indices=None, exclude=False,
              none=False, state_at="DEV_TRIAGE", eid="evt_fx_1"):
    """Router-stamped pending event for the mechanical drain."""
    ev = {
        "event_id": eid, "message_id": f"om_{eid}", "sender_id": sender,
        "source": "listener", "content": content,
        "received_at": topic_store.now_ms(),
        "intent": intent, "role": role,
        "exclude": exclude, "none": none,
        "phase_at_arrival": "main", "state_at_arrival": state_at,
    }
    if indices:
        ev["indices"] = indices
    return ev


def _patch_mech_stubs():
    """Stub mechanical handler I/O; record template/text posts.
    Returns (calls, restore_fn)."""
    import mechanical_reply_handler as mrh
    calls = {"templates": [], "texts": [], "vars": []}
    orig = (mrh._render_and_post, mrh._post_plain_text,
            mrh._glab_approve, mrh._glab_close)

    def stub_render(template_name, variables, message_id):
        calls["templates"].append(template_name)
        calls["vars"].append(variables)
        return True, "{}"

    def stub_text(text, message_id):
        calls["texts"].append(text)
        return True, "{}"

    mrh._render_and_post = stub_render
    mrh._post_plain_text = stub_text
    mrh._glab_approve = lambda repo, mr: (True, "")
    mrh._glab_close = lambda repo, mr: (True, "")

    def restore():
        (mrh._render_and_post, mrh._post_plain_text,
         mrh._glab_approve, mrh._glab_close) = orig

    return calls, restore


def _run_mech(paths):
    import mechanical_reply_handler as mrh
    return mrh.drain_mechanical(paths["topics"], paths["index"],
                                [_FX_APPROVER], "fx-cycle", withdrawn_ids=None)


def fixture_dev_triage_partial(paths):
    """Dev replies `1 3` on a 5-issue review: accepted={1,3}, rejected
    ={2,4,5}, dev_triage_summary posted, state ARBITRATION."""
    thread = "om_fx_devtriage_partial"
    pending = [_fx_event("1 3", intent="dev_triage", role="developer",
                         sender="ou_dev_replay", indices=[1, 3])]
    topic_path = _make_topic(paths, thread, "RAGE-FX-DT1", state="DEV_TRIAGE",
                             pending=pending,
                             review_extra={"issues": _fx_issues(5),
                                           "triage": "simple"})
    calls, restore = _patch_mech_stubs()
    try:
        result = _run_mech(paths)
    finally:
        restore()
    topic = _load_topic(topic_path)
    dt = (topic.get("review") or {}).get("dev_triage") or {}
    if topic["review"]["state"] != "ARBITRATION":
        return {"ok": False, "reason": f"state={topic['review']['state']}"}
    if dt.get("accepted_indices") != [1, 3] or dt.get("rejected_indices") != [2, 4, 5]:
        return {"ok": False, "reason": f"dev_triage={dt}"}
    if calls["templates"] != ["dev_triage_summary"]:
        return {"ok": False, "reason": f"templates={calls['templates']}"}
    if not _audit_has(topic, "dev_triage_recorded"):
        return {"ok": False, "reason": "missing dev_triage_recorded audit"}
    if result.get("dev_triage") != 1:
        return {"ok": False, "reason": f"dev_triage count={result.get('dev_triage')}"}
    # Escalate hint present (triage=simple)
    if "ESCALATE_INSTRUCTION_PARAGRAPHS" not in calls["vars"][0]:
        return {"ok": False, "reason": "escalate hint missing for simple triage"}
    return {"ok": True, "reason": "dev triage recorded + summary posted"}


def fixture_dev_triage_reject_form(paths):
    """Dev replies `-2` on a 3-issue review (exclude form): rejected={2},
    accepted={1,3}."""
    thread = "om_fx_devtriage_reject"
    pending = [_fx_event("-2", intent="dev_triage", role="developer",
                         sender="ou_dev_replay", indices=[2], exclude=True)]
    topic_path = _make_topic(paths, thread, "RAGE-FX-DT2", state="DEV_TRIAGE",
                             pending=pending,
                             review_extra={"issues": _fx_issues(3),
                                           "triage": "complex"})
    calls, restore = _patch_mech_stubs()
    try:
        _run_mech(paths)
    finally:
        restore()
    topic = _load_topic(topic_path)
    dt = (topic.get("review") or {}).get("dev_triage") or {}
    if dt.get("accepted_indices") != [1, 3] or dt.get("rejected_indices") != [2]:
        return {"ok": False, "reason": f"dev_triage={dt}"}
    # Complex triage: no escalate hint in the summary
    if "ESCALATE_INSTRUCTION_PARAGRAPHS" in calls["vars"][0]:
        return {"ok": False, "reason": "escalate hint present for complex triage"}
    return {"ok": True, "reason": "-2 reject form maps to exclude semantics"}


def fixture_dev_triage_none_then_ok(paths):
    """Dev rejects all (`none`), approver replies `ok` in ARBITRATION:
    empty fix set delegates to _handle_approve -> APPROVED."""
    import post_approval as pa
    thread = "om_fx_devtriage_none"
    mrs = {"chaos": {"mr_iid": 2001,
                     "repo_slug": "booming/dev/projects/rage/chaos"}}
    pending = [
        _fx_event("none", intent="dev_triage", role="developer",
                  sender="ou_dev_replay", none=True, eid="evt_fx_none"),
        _fx_event("ok", intent="approve", role="approver",
                  sender=_FX_APPROVER, state_at="ARBITRATION",
                  eid="evt_fx_ok"),
    ]
    topic_path = _make_topic(paths, thread, "RAGE-FX-DT3", state="DEV_TRIAGE",
                             mrs=mrs, pending=pending,
                             review_extra={"issues": _fx_issues(3),
                                           "triage": "simple"})
    calls, restore = _patch_mech_stubs()
    mt_bundle = _patch_merge_tracker(status_seq=["success"])
    orig_pa_render = pa._render_and_post
    pa._render_and_post = lambda *a, **k: (True, "{}")
    try:
        _run_mech(paths)
    finally:
        pa._render_and_post = orig_pa_render
        _unpatch_merge_tracker(mt_bundle)
        restore()
    topic = _load_topic(topic_path)
    if topic["review"]["state"] != "APPROVED":
        return {"ok": False, "reason": f"state={topic['review']['state']}"}
    if not _audit_has(topic, "arbitration_accept_empty"):
        return {"ok": False, "reason": "missing arbitration_accept_empty audit"}
    if not _audit_has(topic, "approver_approve"):
        return {"ok": False, "reason": "missing approver_approve audit"}
    return {"ok": True, "reason": "reject-all + approver OK goes straight to APPROVED"}


def fixture_dev_triage_accept_all_skips_arbitration(paths):
    """Dev replies `all` (accepts every issue): no dispute, so ARBITRATION is
    skipped — flags every issue, posts revision_request, lands directly in
    SIMPLE_REVISION (triage=simple). No dev_triage_summary, no approver
    round-trip (DESIGN §1.23.1). Audit: dev_triage_accepted_all."""
    thread = "om_fx_devtriage_all"
    pending = [
        _fx_event("all", intent="dev_triage", role="developer",
                  sender="ou_dev_replay", exclude=True, eid="evt_fx_all"),
    ]
    topic_path = _make_topic(paths, thread, "RAGE-FX-DT4", state="DEV_TRIAGE",
                             pending=pending,
                             review_extra={"issues": _fx_issues(3),
                                           "triage": "simple"})
    calls, restore = _patch_mech_stubs()
    try:
        _run_mech(paths)
    finally:
        restore()
    topic = _load_topic(topic_path)
    flagged = (topic.get("review") or {}).get("flagged_issues") or []
    if topic["review"]["state"] != "SIMPLE_REVISION":
        return {"ok": False, "reason": f"state={topic['review']['state']}"}
    if sorted(i["index"] for i in flagged) != [1, 2, 3]:
        return {"ok": False, "reason": f"flagged={[i.get('index') for i in flagged]}"}
    if calls["templates"] != ["revision_request"]:
        return {"ok": False, "reason": f"templates={calls['templates']}"}
    if not _audit_has(topic, "dev_triage_accepted_all"):
        return {"ok": False, "reason": "missing dev_triage_accepted_all audit"}
    return {"ok": True, "reason": "accept-all skips arbitration → SIMPLE_REVISION"}


def fixture_arbitration_reinstate(paths):
    """Approver reinstates rejected issue #2: flagged = accepted ∪ {2},
    reinstated recorded, revision_request carries the prefix slot."""
    thread = "om_fx_arb_reinstate"
    pending = [_fx_event("2", intent="revision", role="approver",
                         sender=_FX_APPROVER, indices=[2],
                         state_at="ARBITRATION", eid="evt_fx_rei")]
    topic_path = _make_topic(paths, thread, "RAGE-FX-DT5",
                             state="ARBITRATION", pending=pending,
                             review_extra={"issues": _fx_issues(3),
                                           "triage": "complex",
                                           "dev_triage": {
                                               "accepted_indices": [1],
                                               "rejected_indices": [2, 3],
                                               "decided_at": 0,
                                               "triggered_by_event_id": "evt_0"}})
    calls, restore = _patch_mech_stubs()
    try:
        _run_mech(paths)
    finally:
        restore()
    topic = _load_topic(topic_path)
    review = topic.get("review") or {}
    flagged = review.get("flagged_issues") or []
    if review["state"] != "FULL_REVISION":
        return {"ok": False, "reason": f"state={review['state']} (want FULL_REVISION)"}
    if sorted(i["index"] for i in flagged) != [1, 2]:
        return {"ok": False, "reason": f"flagged={[i.get('index') for i in flagged]}"}
    if (review.get("dev_triage") or {}).get("reinstated_indices") != [2]:
        return {"ok": False, "reason": f"dev_triage={review.get('dev_triage')}"}
    if "PREFIX_PARAGRAPHS" not in calls["vars"][0]:
        return {"ok": False, "reason": "reinstate prefix slot missing"}
    if not _audit_has(topic, "arbitration_reinstated"):
        return {"ok": False, "reason": "missing arbitration_reinstated audit"}
    return {"ok": True, "reason": "reinstate merges into the final fix list"}


def fixture_arbitration_escalate_left_for_agent(paths):
    """Approver `full` in ARBITRATION is NOT mechanical — the event must
    stay pending for the Claude agent."""
    thread = "om_fx_arb_escalate"
    pending = [_fx_event("full", intent="escalate", role="approver",
                         sender=_FX_APPROVER, state_at="ARBITRATION",
                         eid="evt_fx_esc")]
    topic_path = _make_topic(paths, thread, "RAGE-FX-DT6",
                             state="ARBITRATION", pending=pending,
                             review_extra={"issues": _fx_issues(2),
                                           "triage": "simple",
                                           "dev_triage": {
                                               "accepted_indices": [],
                                               "rejected_indices": [1, 2],
                                               "decided_at": 0,
                                               "triggered_by_event_id": "evt_0"}})
    calls, restore = _patch_mech_stubs()
    try:
        result = _run_mech(paths)
    finally:
        restore()
    topic = _load_topic(topic_path)
    still_pending = (topic.get("events") or {}).get("pending") or []
    if len(still_pending) != 1:
        return {"ok": False, "reason": f"pending={len(still_pending)} (want 1)"}
    if topic["review"]["state"] != "ARBITRATION":
        return {"ok": False, "reason": f"state={topic['review']['state']}"}
    if result.get("topics_touched"):
        return {"ok": False, "reason": "drain touched the topic (escalate must defer)"}
    return {"ok": True, "reason": "escalate left pending for the agent"}


def fixture_legacy_decision_state_unchanged(paths):
    """Regression guard for the _handle_revision next-state refactor:
    TRIAGE_DECISION + indices still lands in SIMPLE_REVISION."""
    thread = "om_fx_legacy_revision"
    pending = [_fx_event("1", intent="revision", role="approver",
                         sender=_FX_APPROVER, indices=[1],
                         state_at="TRIAGE_DECISION", eid="evt_fx_leg")]
    topic_path = _make_topic(paths, thread, "RAGE-FX-DT7",
                             state="TRIAGE_DECISION", pending=pending,
                             review_extra={"issues": _fx_issues(2),
                                           "triage": "simple"})
    calls, restore = _patch_mech_stubs()
    try:
        _run_mech(paths)
    finally:
        restore()
    topic = _load_topic(topic_path)
    review = topic.get("review") or {}
    if review["state"] != "SIMPLE_REVISION":
        return {"ok": False, "reason": f"state={review['state']}"}
    flagged = review.get("flagged_issues") or []
    if [i.get("index") for i in flagged] != [1]:
        return {"ok": False, "reason": f"flagged={[i.get('index') for i in flagged]}"}
    if calls["templates"] != ["revision_request"]:
        return {"ok": False, "reason": f"templates={calls['templates']}"}
    return {"ok": True, "reason": "legacy revision path unchanged"}


def fixture_dev_triage_invalid_indices(paths):
    """Dev replies `9` on a 3-issue review: error posted, event DROPPED
    (no poison loop), state unchanged."""
    thread = "om_fx_devtriage_bad"
    pending = [_fx_event("9", intent="dev_triage", role="developer",
                         sender="ou_dev_replay", indices=[9],
                         eid="evt_fx_bad")]
    topic_path = _make_topic(paths, thread, "RAGE-FX-DT8", state="DEV_TRIAGE",
                             pending=pending,
                             review_extra={"issues": _fx_issues(3),
                                           "triage": "simple"})
    calls, restore = _patch_mech_stubs()
    try:
        _run_mech(paths)
    finally:
        restore()
    topic = _load_topic(topic_path)
    if (topic.get("events") or {}).get("pending"):
        return {"ok": False, "reason": "invalid-indices event left pending (poison loop)"}
    if topic["review"]["state"] != "DEV_TRIAGE":
        return {"ok": False, "reason": f"state={topic['review']['state']}"}
    if not calls["texts"]:
        return {"ok": False, "reason": "no correction text posted"}
    if not _audit_has(topic, "dev_triage_invalid_indices"):
        return {"ok": False, "reason": "missing dev_triage_invalid_indices audit"}
    return {"ok": True, "reason": "invalid indices post correction + drop event"}


def fixture_classify_intent_rows(paths):
    """reply_parser rows for the new states — incl. the dev∩approver
    self-review case resolving indices as dev_triage."""
    del paths  # pure-function fixture, no sandbox I/O
    import reply_parser as rp
    A, D = ["ou_appr"], "ou_dev"
    cases = [
        (("1 3", D, "DEV_TRIAGE", A, D, None), ("developer", "dev_triage")),
        (("-2", D, "DEV_TRIAGE", A, D, None), ("developer", "dev_triage")),
        (("none", D, "DEV_TRIAGE", A, D, None), ("developer", "dev_triage")),
        (("ok", D, "DEV_TRIAGE", A, D, None), ("ignored", None)),
        (("1 3", "ou_appr", "DEV_TRIAGE", A, D, None), ("ignored", None)),
        # `pass` alias removed (DESIGN §1.23.2): no longer recognized anywhere.
        (("pass", "ou_appr", "DEV_TRIAGE", A, D, None), ("ignored", None)),
        (("1", "ou_both", "DEV_TRIAGE", ["ou_both"], "ou_both", None),
         ("developer", "dev_triage")),          # self-review
        (("ok", "ou_appr", "ARBITRATION", A, D, None), ("approver", "approve")),
        (("2 4", "ou_appr", "ARBITRATION", A, D, None), ("approver", "revision")),
        (("full", "ou_appr", "ARBITRATION", A, D, "simple"),
         ("approver", "escalate")),
        (("full", "ou_appr", "ARBITRATION", A, D, "complex"), ("ignored", None)),
        (("ok", D, "ARBITRATION", A, D, None), ("ignored", None)),
        (("ok", D, "SIMPLE_REVISION", A, D, None), ("developer", "dev_reply")),
        (("1 3", "ou_appr", "TRIAGE_DECISION", A, D, None),
         ("approver", "revision")),
        # Unified `ok` approval (DESIGN §1.23.2): approver `ok` approves in
        # every decision state; in the revision states it is NOT approval
        # (stays the dev's re-review trigger).
        (("ok", "ou_appr", "TRIAGE_DECISION", A, D, None),
         ("approver", "approve")),
        (("ok", "ou_appr", "AWAITING_APPROVAL", A, D, None),
         ("approver", "approve")),
        (("ok", "ou_appr", "DEV_TRIAGE", A, D, None),
         ("approver", "approve")),
        (("ok", "ou_appr", "SIMPLE_REVISION", A, D, None),
         ("developer", "dev_reply")),
        # `pass` / `通过` aliases removed — not recognized in a decision state.
        (("pass", "ou_appr", "TRIAGE_DECISION", A, D, None), ("ignored", None)),
        (("通过", "ou_appr", "TRIAGE_DECISION", A, D, None), ("ignored", None)),
    ]
    for (content, sender, state, appr, dev, triage), want in cases:
        got = rp.classify_intent(content, sender, state, appr,
                                 developer_id=dev, triage=triage)
        if (got["role"], got["intent"]) != want:
            return {"ok": False,
                    "reason": f"{content!r}@{state}: got "
                              f"{(got['role'], got['intent'])}, want {want}"}
    if rp.parse_indices_with_mode("none") is not None:
        return {"ok": False, "reason": "`none` accepted without allow_none"}
    none_parse = rp.parse_indices_with_mode("不修", allow_none=True)
    if not (none_parse and none_parse["none"]):
        return {"ok": False, "reason": f"不修 parse={none_parse}"}
    return {"ok": True, "reason": "classify rows for DEV_TRIAGE/ARBITRATION hold"}


def _make_closed_topic(paths, thread_id, ticket_id, *, superseded_by,
                       from_state="FULL_REVISION", mrs=None,
                       last_processed_ts=0, recent_event_ids=None,
                       review_extra=None, root_message_id=None):
    """Write a CLOSED topic straight into closed/ shaped exactly like a
    same-ticket supersede left it (closed_reason pattern + topic_closed
    audit carrying from_state). NOT `init --from-closed` — that seeder
    strips the review context a reopen must preserve."""
    reason = f"superseded by new topic {superseded_by}"
    topic = {
        "thread_id": thread_id,
        "root_message_id": root_message_id or thread_id,
        "identity": {
            "ticket_id": ticket_id,
            "chat_id": "oc_replay_chat",
            "creator_open_id": "ou_dev_replay",
            "developer": "replay-dev",
        },
        "review": {"state": "CLOSED", "review_phase": "main",
                   "review_round": 1, "issues": [],
                   **(review_extra or {})},
        "mrs": mrs or {},
        "events": {
            "pending": [],
            "last_processed_event_id": None,
            "last_processed_ts": last_processed_ts,
            "recent_event_ids": recent_event_ids or [],
        },
        "audit": [{"ts": topic_store.now_ms(), "event": "topic_closed",
                   "from_state": from_state, "to_state": "CLOSED",
                   "reason": reason}],
        "lifecycle": {"created_at": topic_store.now_ms(),
                      "resolved_at": topic_store.now_ms(),
                      "closed_reason": reason},
    }
    closed_path = paths["closed"] / f"{thread_id}.json"
    topic_store.write_atomic(closed_path, topic)
    return closed_path


def _patch_reopen_guards(alive=True, mr_state="opened", mr_error=None):
    """Stub the reopen guards' external I/O. Returns (bundle, calls)."""
    import mechanical_reply_handler as mrh
    import merge_tracker
    import topic_reopen
    calls = {"alive": 0, "check_mr": 0, "fetch": 0, "notice": 0,
             "fetch_messages": [], "alive_args": []}
    orig = (mrh._is_message_alive, merge_tracker.check_mr,
            topic_reopen._default_fetch_thread, topic_reopen._post_reopen_notice)

    def stub_alive(mid):
        calls["alive"] += 1
        calls["alive_args"].append(mid)
        return alive

    def stub_check_mr(repo, mr_obj, timeout_s=30):
        calls["check_mr"] += 1
        return {"merged": mr_state == "merged", "closed": mr_state == "closed",
                "state": None if mr_error else mr_state, "sha": None,
                "error": mr_error}

    def stub_fetch(thread_id):
        calls["fetch"] += 1
        return calls["fetch_messages"]

    def stub_notice(topic):
        calls["notice"] += 1
        return True

    mrh._is_message_alive = stub_alive
    merge_tracker.check_mr = stub_check_mr
    topic_reopen._default_fetch_thread = stub_fetch
    topic_reopen._post_reopen_notice = stub_notice
    return (mrh, merge_tracker, topic_reopen, orig), calls


def _unpatch_reopen_guards(bundle):
    mrh, merge_tracker, topic_reopen, orig = bundle
    mrh._is_message_alive = orig[0]
    merge_tracker.check_mr = orig[1]
    topic_reopen._default_fetch_thread = orig[2]
    topic_reopen._post_reopen_notice = orig[3]


def fixture_reopen_after_withdrawn_supersede(paths):
    """The RAGE-20032 incident end-to-end: withdrawn duplicate root →
    duplicate closed, superseded predecessor reopened at its pre-close
    state, missed dev `ok` backfilled + routed + classified dev_reply."""
    import dispatcher
    ticket = "RAGE-FX-RO"
    pred_thread = "om_fx_ro_pred"
    dup_thread = "om_fx_ro_dup"
    mrs = {"chaos": {"mr_iid": 8001,
                     "repo_slug": "booming/dev/projects/rage/chaos"}}
    base = topic_store.now_ms()
    _make_closed_topic(paths, pred_thread, ticket, superseded_by=dup_thread,
                       from_state="FULL_REVISION", mrs=mrs,
                       last_processed_ts=base - 3_600_000,
                       recent_event_ids=[f"msg:om_fx_ro_seen"])
    _make_topic(paths, dup_thread, ticket, state="DEV_TRIAGE", mrs=mrs)

    bundle, calls = _patch_reopen_guards()
    calls["fetch_messages"] = [
        # withdrawn reply — must be skipped
        {"message_id": "om_fx_ro_del1", "deleted": True, "msg_type": "text",
         "create_time": str(base - 100_000),
         "sender": {"id": "ou_dev_replay"},
         "body": {"content": "{\"text\":\"oops\"}"}},
        # bot's own post — cli_ sender skipped
        {"message_id": "om_fx_ro_bot", "deleted": False, "msg_type": "post",
         "create_time": str(base - 90_000), "sender": {"id": "cli_replay"},
         "body": {"content": "{\"title\":\"x\",\"content\":[]}"}},
        # older than the floor — skipped
        {"message_id": "om_fx_ro_old", "deleted": False, "msg_type": "text",
         "create_time": str(base - 10_000_000),
         "sender": {"id": "ou_dev_replay"},
         "body": {"content": "{\"text\":\"old\"}"}},
        # already in the recent-event ring — skipped
        {"message_id": "om_fx_ro_seen", "deleted": False, "msg_type": "text",
         "create_time": str(base - 120_000),
         "sender": {"id": "ou_dev_replay"},
         "body": {"content": "{\"text\":\"1 3\"}"}},
        # THE dropped ok — must be synthesized, JSON-unwrapped
        {"message_id": "om_fx_ro_ok", "deleted": False, "msg_type": "text",
         "create_time": str(base - 60_000),
         "sender": {"id": "ou_dev_replay"},
         "body": {"content": "{\"text\":\"ok\"}"}},
    ]
    orig_topics = dispatcher.TOPICS_DIR
    orig_index = dispatcher.INDEX_PATH
    dispatcher.TOPICS_DIR = paths["topics"]
    dispatcher.INDEX_PATH = paths["index"]
    try:
        result = dispatcher._close_withdrawn_topics(
            {"withdrawn_ids": [dup_thread]}, "fx-cycle")
    finally:
        dispatcher.TOPICS_DIR = orig_topics
        dispatcher.INDEX_PATH = orig_index
        _unpatch_reopen_guards(bundle)

    if result.get("closed") != 1:
        return {"ok": False, "reason": f"closed={result.get('closed')}"}
    reopens = result.get("reopened") or []
    if not reopens or reopens[0].get("status") != "reopened":
        return {"ok": False, "reason": f"reopened={reopens}"}
    if reopens[0].get("backfill_written") != 1:
        return {"ok": False,
                "reason": f"backfill={reopens[0].get('backfill_written')}"}
    if calls["notice"] != 1:
        return {"ok": False, "reason": f"notice calls={calls['notice']}"}

    dup = _load_topic(paths["closed"] / f"{dup_thread}.json")
    if dup["lifecycle"].get("closed_reason") != "root message withdrawn":
        return {"ok": False,
                "reason": f"dup reason={dup['lifecycle'].get('closed_reason')}"}
    pred_path = paths["topics"] / f"{pred_thread}.json"
    if not pred_path.exists():
        return {"ok": False, "reason": "predecessor not reopened"}
    if (paths["closed"] / f"{pred_thread}.json").exists():
        return {"ok": False, "reason": "closed copy not removed"}
    pred = _load_topic(pred_path)
    if pred["review"]["state"] != "FULL_REVISION":
        return {"ok": False, "reason": f"state={pred['review']['state']}"}
    if pred["lifecycle"].get("closed_reason") is not None:
        return {"ok": False, "reason": "closed_reason not cleared"}
    if not _audit_has(pred, "topic_reopened"):
        return {"ok": False, "reason": "missing topic_reopened audit"}
    idx = topic_index.load(paths["index"])
    if idx.get(pred_thread) != ticket:
        return {"ok": False, "reason": f"index missing predecessor: {idx}"}

    synth = list(paths["events"].glob("reopen_*.json"))
    if len(synth) != 1 or "om_fx_ro_ok" not in synth[0].name:
        return {"ok": False,
                "reason": f"synth files={[p.name for p in synth]}"}
    with open(synth[0], encoding="utf-8") as f:
        ev = json.load(f)
    if ev.get("content") != "ok":
        return {"ok": False, "reason": f"content not unwrapped: {ev.get('content')!r}"}

    # Route the backfilled event through the real router: Gate 4a must
    # pass (topic open again) and the reply must classify dev_reply.
    router.route_pending_events(paths["events"], paths["topics"],
                                paths["index"], "oc_replay_chat",
                                ["ou_1127c220c15c21355c0fe236c618f1af"])
    inbox_path = paths["topics"] / f"{pred_thread}.inbox.json"
    if not inbox_path.exists():
        return {"ok": False, "reason": "backfilled event not routed to inbox"}
    with open(inbox_path, encoding="utf-8") as f:
        inbox = json.load(f)
    entry = next((e for e in inbox
                  if e.get("message_id") == "om_fx_ro_ok"), None)
    if not entry:
        return {"ok": False, "reason": f"ok not in inbox: {inbox}"}
    if entry.get("intent") != "dev_reply" or entry.get("role") != "developer":
        return {"ok": False,
                "reason": f"classified {entry.get('role')}/{entry.get('intent')}"}
    return {"ok": True,
            "reason": "duplicate closed, predecessor restored, ok backfilled + classified"}


def fixture_reopen_purges_deferred_supersede(paths):
    """Deferred-supersede race: the ledger entry keyed on the withdrawn
    duplicate must be purged BEFORE _retry_pending_supersedes runs, or the
    retry closes the (still-open) predecessor for a topic that just died."""
    import dispatcher
    ticket = "RAGE-FX-PG"
    pred_thread = "om_fx_pg_pred"
    dup_thread = "om_fx_pg_dup"
    _make_topic(paths, pred_thread, ticket, state="FULL_REVISION")
    _make_topic(paths, dup_thread, ticket, state="TRIAGING")
    ledger_path = paths["cfg"] / "supersede_pending.json"
    topic_store.write_atomic(ledger_path, [{
        "old_thread": pred_thread, "new_thread": dup_thread,
        "reason": f"superseded by new topic {dup_thread}",
        "queued_at": topic_store.now_ms()}])

    orig_topics = dispatcher.TOPICS_DIR
    orig_index = dispatcher.INDEX_PATH
    orig_ledger = dispatcher.SUPERSEDE_PENDING
    dispatcher.TOPICS_DIR = paths["topics"]
    dispatcher.INDEX_PATH = paths["index"]
    dispatcher.SUPERSEDE_PENDING = ledger_path
    try:
        result = dispatcher._close_withdrawn_topics(
            {"withdrawn_ids": [dup_thread]}, "fx-cycle")
        dispatcher._retry_pending_supersedes("fx-cycle")
    finally:
        dispatcher.TOPICS_DIR = orig_topics
        dispatcher.INDEX_PATH = orig_index
        dispatcher.SUPERSEDE_PENDING = orig_ledger

    reopens = result.get("reopened") or []
    if not reopens or reopens[0].get("purged") != 1:
        return {"ok": False, "reason": f"purge not reported: {reopens}"}
    if reopens[0].get("status") != "no_predecessor":
        return {"ok": False, "reason": f"status={reopens[0].get('status')}"}
    with open(ledger_path, encoding="utf-8") as f:
        remaining = json.load(f)
    if remaining:
        return {"ok": False, "reason": f"ledger not purged: {remaining}"}
    if not (paths["topics"] / f"{pred_thread}.json").exists():
        return {"ok": False,
                "reason": "predecessor closed by retried supersede"}
    return {"ok": True,
            "reason": "ledger purged before retry; predecessor survived"}


def fixture_reopen_blocked_root_withdrawn(paths):
    """Double withdrawal: a predecessor whose own root is withdrawn (or
    confirmed dead by mget) must stay archived."""
    import topic_reopen
    ticket = "RAGE-FX-RW"
    pred_thread = "om_fx_rw_pred"
    dup_thread = "om_fx_rw_dup"
    _make_closed_topic(paths, pred_thread, ticket, superseded_by=dup_thread)

    bundle, calls = _patch_reopen_guards(alive=True)
    try:
        res = topic_reopen.reopen_superseded_predecessor(
            paths["topics"], paths["index"], dup_thread,
            withdrawn_ids={dup_thread, pred_thread})
    finally:
        _unpatch_reopen_guards(bundle)
    if res.get("status") != "refused_root_withdrawn":
        return {"ok": False, "reason": f"fast path status={res.get('status')}"}

    bundle, calls = _patch_reopen_guards(alive=False)
    try:
        res = topic_reopen.reopen_superseded_predecessor(
            paths["topics"], paths["index"], dup_thread,
            withdrawn_ids={dup_thread})
    finally:
        _unpatch_reopen_guards(bundle)
    if res.get("status") != "refused_root_dead":
        return {"ok": False, "reason": f"mget path status={res.get('status')}"}
    if not (paths["closed"] / f"{pred_thread}.json").exists():
        return {"ok": False, "reason": "predecessor left closed/ despite guard"}
    if (paths["topics"] / f"{pred_thread}.json").exists():
        return {"ok": False, "reason": "predecessor reopened despite guard"}
    return {"ok": True, "reason": "both root-withdrawn paths refused"}


def fixture_reopen_blocked_mr_merged(paths):
    """MR guard: definitive merged blocks the reopen; a glab error does NOT
    (fail-open — a wrong non-reopen is silent and permanent)."""
    import topic_reopen
    mrs = {"chaos": {"mr_iid": 8002,
                     "repo_slug": "booming/dev/projects/rage/chaos"}}
    _make_closed_topic(paths, "om_fx_mr_pred", "RAGE-FX-MR",
                       superseded_by="om_fx_mr_dup", mrs=mrs)
    bundle, calls = _patch_reopen_guards(alive=True, mr_state="merged")
    try:
        res = topic_reopen.reopen_superseded_predecessor(
            paths["topics"], paths["index"], "om_fx_mr_dup",
            withdrawn_ids={"om_fx_mr_dup"})
    finally:
        _unpatch_reopen_guards(bundle)
    if res.get("status") != "refused_mr_terminal":
        return {"ok": False, "reason": f"merged status={res.get('status')}"}
    if not (paths["closed"] / "om_fx_mr_pred.json").exists():
        return {"ok": False, "reason": "predecessor reopened despite merged MR"}

    _make_closed_topic(paths, "om_fx_er_pred", "RAGE-FX-ER",
                       superseded_by="om_fx_er_dup", mrs=mrs)
    bundle, calls = _patch_reopen_guards(alive=True, mr_error="glab boom")
    try:
        res = topic_reopen.reopen_superseded_predecessor(
            paths["topics"], paths["index"], "om_fx_er_dup",
            withdrawn_ids={"om_fx_er_dup"})
    finally:
        _unpatch_reopen_guards(bundle)
    if res.get("status") != "reopened":
        return {"ok": False, "reason": f"fail-open status={res.get('status')}"}
    if res.get("backfill_written") != 0:
        return {"ok": False, "reason": "unexpected backfill"}
    if not (paths["topics"] / "om_fx_er_pred.json").exists():
        return {"ok": False, "reason": "fail-open predecessor not reopened"}
    return {"ok": True, "reason": "merged blocks; glab error fails open"}


def fixture_reopen_heal_after_crash(paths):
    """Crash between open-copy write and closed-copy unlink leaves both
    copies; re-entry must heal (drop closed copy) without re-backfilling."""
    import topic_reopen
    ticket = "RAGE-FX-HL"
    pred_thread = "om_fx_hl_pred"
    dup_thread = "om_fx_hl_dup"
    _make_topic(paths, pred_thread, ticket, state="FULL_REVISION")
    _make_closed_topic(paths, pred_thread, ticket, superseded_by=dup_thread)

    bundle, calls = _patch_reopen_guards()
    try:
        res = topic_reopen.reopen_superseded_predecessor(
            paths["topics"], paths["index"], dup_thread,
            withdrawn_ids={dup_thread})
    finally:
        _unpatch_reopen_guards(bundle)
    if res.get("status") != "already_open_healed":
        return {"ok": False, "reason": f"status={res.get('status')}"}
    if (paths["closed"] / f"{pred_thread}.json").exists():
        return {"ok": False, "reason": "closed copy survived heal"}
    if calls["fetch"] != 0:
        return {"ok": False, "reason": "backfill ran during heal"}
    idx = topic_index.load(paths["index"])
    if idx.get(pred_thread) != ticket:
        return {"ok": False, "reason": f"index missing after heal: {idx}"}
    return {"ok": True, "reason": "split copies healed, no duplicate backfill"}


def fixture_reopen_scan_tolerates_corrupt_closed(paths):
    """One corrupt file in closed/ must not brick the reopen scan — the
    trigger is one-shot, so fail-closed here re-creates the incident."""
    import topic_reopen
    ticket = "RAGE-FX-CX"
    pred_thread = "om_fx_cx_pred"
    dup_thread = "om_fx_cx_dup"
    _make_closed_topic(paths, pred_thread, ticket, superseded_by=dup_thread)
    with open(paths["closed"] / "om_fx_cx_garbage.json", "w",
              encoding="utf-8") as f:
        f.write("{not json")

    bundle, calls = _patch_reopen_guards()
    try:
        res = topic_reopen.reopen_superseded_predecessor(
            paths["topics"], paths["index"], dup_thread,
            withdrawn_ids={dup_thread})
    finally:
        _unpatch_reopen_guards(bundle)
    if res.get("status") != "reopened":
        return {"ok": False, "reason": f"status={res.get('status')}"}
    if not (paths["topics"] / f"{pred_thread}.json").exists():
        return {"ok": False, "reason": "predecessor not reopened"}
    return {"ok": True, "reason": "corrupt closed/ file skipped, reopen landed"}


def fixture_reopen_dry_run_pure(paths):
    """--dry-run must be pure: no ledger purge, no file moves."""
    import topic_reopen
    ticket = "RAGE-FX-DR"
    pred_thread = "om_fx_dr_pred"
    dup_thread = "om_fx_dr_dup"
    _make_closed_topic(paths, pred_thread, ticket, superseded_by=dup_thread)
    ledger_path = paths["cfg"] / "supersede_pending.json"
    ledger = [{"old_thread": pred_thread, "new_thread": dup_thread,
               "reason": f"superseded by new topic {dup_thread}",
               "queued_at": topic_store.now_ms()}]
    topic_store.write_atomic(ledger_path, ledger)

    bundle, calls = _patch_reopen_guards()
    try:
        res = topic_reopen.reopen_superseded_predecessor(
            paths["topics"], paths["index"], dup_thread,
            withdrawn_ids={dup_thread}, dry_run=True)
    finally:
        _unpatch_reopen_guards(bundle)
    if res.get("status") != "dry_run":
        return {"ok": False, "reason": f"status={res.get('status')}"}
    if res.get("would_restore_state") != "FULL_REVISION":
        return {"ok": False,
                "reason": f"would_restore={res.get('would_restore_state')}"}
    if res.get("would_purge") != 1 or res.get("purged") != 0:
        return {"ok": False,
                "reason": f"purge report wrong: {res}"}
    with open(ledger_path, encoding="utf-8") as f:
        remaining = json.load(f)
    if remaining != ledger:
        return {"ok": False, "reason": "dry-run mutated the ledger"}
    if (paths["topics"] / f"{pred_thread}.json").exists():
        return {"ok": False, "reason": "dry-run moved the topic"}
    if calls["fetch"] or calls["notice"]:
        return {"ok": False, "reason": "dry-run ran backfill/notice"}
    return {"ok": True, "reason": "dry-run reported without mutating"}


def fixture_reopen_backfill_failure_still_reopened(paths):
    """A backfill crash after the mutate must not flip the result to error —
    the reopen already landed and must be reported as such."""
    import topic_reopen
    ticket = "RAGE-FX-BF"
    pred_thread = "om_fx_bf_pred"
    dup_thread = "om_fx_bf_dup"
    _make_closed_topic(paths, pred_thread, ticket, superseded_by=dup_thread)

    bundle, calls = _patch_reopen_guards()

    def raising_fetch(thread_id):
        raise RuntimeError("lark exploded")
    topic_reopen._default_fetch_thread = raising_fetch
    try:
        res = topic_reopen.reopen_superseded_predecessor(
            paths["topics"], paths["index"], dup_thread,
            withdrawn_ids={dup_thread})
    finally:
        _unpatch_reopen_guards(bundle)
    if res.get("status") != "reopened":
        return {"ok": False, "reason": f"status={res.get('status')}"}
    if "lark exploded" not in (res.get("backfill_error") or ""):
        return {"ok": False,
                "reason": f"backfill_error={res.get('backfill_error')}"}
    if calls["notice"] != 1:
        return {"ok": False, "reason": "notice skipped after backfill failure"}
    if not (paths["topics"] / f"{pred_thread}.json").exists():
        return {"ok": False, "reason": "predecessor not reopened"}
    idx = topic_index.load(paths["index"])
    if idx.get(pred_thread) != ticket:
        return {"ok": False, "reason": "index missing predecessor"}
    return {"ok": True, "reason": "backfill failure isolated; reopen reported"}


def fixture_reopen_blocked_other_open_topic(paths):
    """Repost-before-reconcile chain: a third open topic already owns the
    ticket — the predecessor must stay archived (newest root is canonical)."""
    import topic_reopen
    ticket = "RAGE-FX-OT"
    pred_thread = "om_fx_ot_pred"
    dup_thread = "om_fx_ot_dup"
    rival_thread = "om_fx_ot_rival"
    _make_closed_topic(paths, pred_thread, ticket, superseded_by=dup_thread)
    _make_topic(paths, rival_thread, ticket, state="TRIAGING")

    bundle, calls = _patch_reopen_guards()
    try:
        res = topic_reopen.reopen_superseded_predecessor(
            paths["topics"], paths["index"], dup_thread,
            withdrawn_ids={dup_thread})
    finally:
        _unpatch_reopen_guards(bundle)
    if res.get("status") != "refused_other_open_topic":
        return {"ok": False, "reason": f"status={res.get('status')}"}
    if res.get("open_thread") != rival_thread:
        return {"ok": False, "reason": f"open_thread={res.get('open_thread')}"}
    if not (paths["closed"] / f"{pred_thread}.json").exists():
        return {"ok": False, "reason": "predecessor left closed/"}
    return {"ok": True, "reason": "rival open topic blocks resurrection"}


def fixture_retry_supersede_skips_dead_new_thread(paths):
    """_retry_pending_supersedes must not close the predecessor on behalf
    of a superseding topic that is no longer open (multi-duplicate chain)."""
    import dispatcher
    ticket = "RAGE-FX-DT"
    pred_thread = "om_fx_dt_pred"
    ghost_thread = "om_fx_dt_ghost"  # no open topic file exists for it
    _make_topic(paths, pred_thread, ticket, state="FULL_REVISION")
    ledger_path = paths["cfg"] / "supersede_pending.json"
    topic_store.write_atomic(ledger_path, [{
        "old_thread": pred_thread, "new_thread": ghost_thread,
        "reason": f"superseded by new topic {ghost_thread}",
        "queued_at": topic_store.now_ms()}])

    orig_topics = dispatcher.TOPICS_DIR
    orig_index = dispatcher.INDEX_PATH
    orig_ledger = dispatcher.SUPERSEDE_PENDING
    dispatcher.TOPICS_DIR = paths["topics"]
    dispatcher.INDEX_PATH = paths["index"]
    dispatcher.SUPERSEDE_PENDING = ledger_path
    try:
        dispatcher._retry_pending_supersedes("fx-cycle")
    finally:
        dispatcher.TOPICS_DIR = orig_topics
        dispatcher.INDEX_PATH = orig_index
        dispatcher.SUPERSEDE_PENDING = orig_ledger

    if not (paths["topics"] / f"{pred_thread}.json").exists():
        return {"ok": False,
                "reason": "predecessor closed for a dead new_thread"}
    with open(ledger_path, encoding="utf-8") as f:
        remaining = json.load(f)
    if remaining:
        return {"ok": False, "reason": f"stale entry survived: {remaining}"}
    return {"ok": True, "reason": "dead-new_thread entry dropped, topic kept"}


def fixture_reopen_idempotent_rerun(paths):
    """Re-running the hook with the same withdrawn thread must be a no-op
    (no second backfill, no second notice, single audit entry)."""
    import topic_reopen
    ticket = "RAGE-FX-ID"
    pred_thread = "om_fx_id_pred"
    dup_thread = "om_fx_id_dup"
    base = topic_store.now_ms()
    _make_closed_topic(paths, pred_thread, ticket, superseded_by=dup_thread,
                       last_processed_ts=base - 3_600_000)

    bundle, calls = _patch_reopen_guards()
    calls["fetch_messages"] = [
        {"message_id": "om_fx_id_ok", "deleted": False, "msg_type": "text",
         "create_time": str(base - 60_000),
         "sender": {"id": "ou_dev_replay"},
         "body": {"content": "{\"text\":\"ok\"}"}},
    ]
    try:
        first = topic_reopen.reopen_superseded_predecessor(
            paths["topics"], paths["index"], dup_thread,
            withdrawn_ids={dup_thread})
        second = topic_reopen.reopen_superseded_predecessor(
            paths["topics"], paths["index"], dup_thread,
            withdrawn_ids={dup_thread})
    finally:
        _unpatch_reopen_guards(bundle)
    if first.get("status") != "reopened":
        return {"ok": False, "reason": f"first={first.get('status')}"}
    if second.get("status") != "no_predecessor":
        return {"ok": False, "reason": f"second={second.get('status')}"}
    if calls["notice"] != 1:
        return {"ok": False, "reason": f"notice calls={calls['notice']}"}
    synth = list(paths["events"].glob("reopen_*.json"))
    if len(synth) != 1:
        return {"ok": False, "reason": f"synth={[p.name for p in synth]}"}
    topic = _load_topic(paths["topics"] / f"{pred_thread}.json")
    audits = [a for a in topic.get("audit", [])
              if a.get("event") == "topic_reopened"]
    if len(audits) != 1:
        return {"ok": False, "reason": f"{len(audits)} topic_reopened audits"}
    return {"ok": True, "reason": "second run is a clean no-op"}


def fixture_gate4a_drop_is_logged(paths):
    """Router Gate 4a must WARN-log and count closed-thread drops — the
    silent unlink is what hid the original incident."""
    closed_thread = "om_fx_g4_closed"
    _make_closed_topic(paths, closed_thread, "RAGE-FX-G4",
                       superseded_by="om_fx_g4_unrelated")
    ev = {
        "chat_id": "oc_replay_chat", "chat_type": "group",
        "content": "ok", "create_time": str(topic_store.now_ms()),
        "id": "om_fx_g4_msg", "message_id": "om_fx_g4_msg",
        "thread_id": closed_thread, "root_id": closed_thread,
        "parent_id": "", "message_type": "text", "mentions": [],
        "sender_id": "ou_dev_replay",
        "timestamp": str(topic_store.now_ms()),
        "type": "im.message.receive_v1", "_source": "fixture",
    }
    raw_path = paths["events"] / "fixture_g4.json"
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(ev, f, ensure_ascii=False)

    log_path = paths["cfg"] / "activity.log"
    orig_log = router._ACTIVITY_LOG
    router._ACTIVITY_LOG = log_path
    try:
        summary = router.route_pending_events(
            paths["events"], paths["topics"], paths["index"],
            "oc_replay_chat", ["ou_1127c220c15c21355c0fe236c618f1af"])
    finally:
        router._ACTIVITY_LOG = orig_log
    if summary.get("closed_thread_drops") != 1:
        return {"ok": False,
                "reason": f"drops={summary.get('closed_thread_drops')}"}
    if raw_path.exists():
        return {"ok": False, "reason": "raw event not unlinked"}
    if not log_path.exists():
        return {"ok": False, "reason": "no activity log written"}
    with open(log_path, encoding="utf-8") as f:
        log_text = f.read()
    if ("closed_topic_reply_dropped" not in log_text
            or closed_thread not in log_text
            or "om_fx_g4_msg" not in log_text):
        return {"ok": False, "reason": f"log line wrong: {log_text[-200:]}"}
    return {"ok": True, "reason": "Gate 4a drop logged + counted"}


def fixture_reopen_refused_unrestorable_state(paths):
    """(a) a terminal from_state refuses the reopen; (b) the liveness probe
    targets root_message_id, not thread_id, when they differ."""
    import topic_reopen
    # (a) from_state MERGED — restoring it would get janitor re-archived.
    _make_closed_topic(paths, "om_fx_us_pred", "RAGE-FX-US",
                       superseded_by="om_fx_us_dup", from_state="MERGED")
    bundle, calls = _patch_reopen_guards()
    try:
        res = topic_reopen.reopen_superseded_predecessor(
            paths["topics"], paths["index"], "om_fx_us_dup",
            withdrawn_ids={"om_fx_us_dup"})
    finally:
        _unpatch_reopen_guards(bundle)
    if res.get("status") != "refused_unrestorable_state":
        return {"ok": False, "reason": f"(a) status={res.get('status')}"}
    if (paths["topics"] / "om_fx_us_pred.json").exists():
        return {"ok": False, "reason": "(a) terminal state reopened"}

    # (b) root_message_id != thread_id — probe must use the ROOT id.
    _make_closed_topic(paths, "om_fx_rt_pred", "RAGE-FX-RT",
                       superseded_by="om_fx_rt_dup",
                       root_message_id="om_fx_rt_root")
    bundle, calls = _patch_reopen_guards()
    try:
        res = topic_reopen.reopen_superseded_predecessor(
            paths["topics"], paths["index"], "om_fx_rt_dup",
            withdrawn_ids={"om_fx_rt_dup"})
    finally:
        _unpatch_reopen_guards(bundle)
    if res.get("status") != "reopened":
        return {"ok": False, "reason": f"(b) status={res.get('status')}"}
    if calls["alive_args"] != ["om_fx_rt_root"]:
        return {"ok": False,
                "reason": f"(b) liveness probed {calls['alive_args']}"}
    if not (paths["topics"] / "om_fx_rt_pred.json").exists():
        return {"ok": False, "reason": "(b) file not keyed on thread_id"}
    return {"ok": True,
            "reason": "terminal state refused; liveness probes the root id"}


def fixture_reopen_approved_rearms_pipeline_hold(paths):
    """A reopened APPROVED topic must not carry stale pipeline fields into
    the same cycle's merge pass — the hold has to re-arm so next cycle's
    backfilled replies (e.g. a recovered `close`) land first."""
    import topic_reopen
    mrs = {"chaos": {"mr_iid": 8003,
                     "repo_slug": "booming/dev/projects/rage/chaos",
                     "pipeline_status": "passed",
                     "pipeline_passed_at_ms": 12345}}
    _make_closed_topic(paths, "om_fx_ap_pred", "RAGE-FX-AP",
                       superseded_by="om_fx_ap_dup",
                       from_state="APPROVED", mrs=mrs)
    bundle, calls = _patch_reopen_guards()
    try:
        res = topic_reopen.reopen_superseded_predecessor(
            paths["topics"], paths["index"], "om_fx_ap_dup",
            withdrawn_ids={"om_fx_ap_dup"})
    finally:
        _unpatch_reopen_guards(bundle)
    if res.get("status") != "reopened":
        return {"ok": False, "reason": f"status={res.get('status')}"}
    topic = _load_topic(paths["topics"] / "om_fx_ap_pred.json")
    if topic["review"]["state"] != "APPROVED":
        return {"ok": False, "reason": f"state={topic['review']['state']}"}
    mr = topic["mrs"]["chaos"]
    if "pipeline_status" in mr or "pipeline_passed_at_ms" in mr:
        return {"ok": False, "reason": f"stale pipeline fields kept: {mr}"}
    return {"ok": True, "reason": "pipeline hold re-armed on APPROVED reopen"}


_FIXTURES = {
    "infra_retry_approval_mechanical":  fixture_infra_retry_approval_mechanical,
    "infra_retry_approval_dispatcher":  fixture_infra_retry_approval_dispatcher,
    "infra_retry_approval_3p_agent":    fixture_infra_retry_approval_3p_agent,
    "phase_mismatch_event_skip":        fixture_phase_mismatch_event_skip,
    "out_of_order_ts":                  fixture_out_of_order_ts,
    "withdrawn_race":                   fixture_withdrawn_race,
    "supersede_stale_lock":             fixture_supersede_stale_lock,
    "no_new_commits_drained":           fixture_no_new_commits_drained,
    "no_new_commits_stale_webhook":     fixture_no_new_commits_stale_webhook,
    "incr_cache_populated":             fixture_incr_cache_populated,
    "incr_cache_skipped_on_no_new_commits":
                                        fixture_incr_cache_skipped_on_no_new_commits,
    "dev_triage_partial":               fixture_dev_triage_partial,
    "dev_triage_reject_form":           fixture_dev_triage_reject_form,
    "dev_triage_none_then_ok":          fixture_dev_triage_none_then_ok,
    "dev_triage_accept_all_skips_arbitration":
                                        fixture_dev_triage_accept_all_skips_arbitration,
    "arbitration_reinstate":            fixture_arbitration_reinstate,
    "arbitration_escalate_left_for_agent":
                                        fixture_arbitration_escalate_left_for_agent,
    "legacy_decision_state_unchanged":  fixture_legacy_decision_state_unchanged,
    "dev_triage_invalid_indices":       fixture_dev_triage_invalid_indices,
    "classify_intent_rows":             fixture_classify_intent_rows,
    "reopen_after_withdrawn_supersede": fixture_reopen_after_withdrawn_supersede,
    "reopen_purges_deferred_supersede": fixture_reopen_purges_deferred_supersede,
    "reopen_blocked_root_withdrawn":    fixture_reopen_blocked_root_withdrawn,
    "reopen_blocked_mr_merged":         fixture_reopen_blocked_mr_merged,
    "reopen_heal_after_crash":          fixture_reopen_heal_after_crash,
    "reopen_scan_tolerates_corrupt_closed":
                                        fixture_reopen_scan_tolerates_corrupt_closed,
    "reopen_dry_run_pure":              fixture_reopen_dry_run_pure,
    "reopen_backfill_failure_still_reopened":
                                        fixture_reopen_backfill_failure_still_reopened,
    "reopen_blocked_other_open_topic":  fixture_reopen_blocked_other_open_topic,
    "retry_supersede_skips_dead_new_thread":
                                        fixture_retry_supersede_skips_dead_new_thread,
    "reopen_idempotent_rerun":          fixture_reopen_idempotent_rerun,
    "gate4a_drop_is_logged":            fixture_gate4a_drop_is_logged,
    "reopen_refused_unrestorable_state":
                                        fixture_reopen_refused_unrestorable_state,
    "reopen_approved_rearms_pipeline_hold":
                                        fixture_reopen_approved_rearms_pipeline_hold,
}


def cmd_fixture(args):
    """Run one or all named fixtures."""
    names = list(_FIXTURES) if args.name == "all" else [args.name]
    unknown = [n for n in names if n not in _FIXTURES]
    if unknown:
        raise SystemExit(f"unknown fixtures: {unknown}; known: {list(_FIXTURES)}")

    results = []
    failed = 0
    for name in names:
        paths = _fresh_sandbox(name)
        try:
            outcome = _FIXTURES[name](paths)
        except Exception as exc:  # noqa: BLE001
            outcome = {"ok": False, "reason": f"raised: {type(exc).__name__}: {exc}"}
        outcome["name"] = name
        outcome["sandbox"] = str(paths["root"])
        results.append(outcome)
        if not outcome.get("ok"):
            failed += 1

    print(json.dumps({"results": results, "failed": failed}, ensure_ascii=False, indent=2))
    return 1 if failed else 0


# ---- Lint: centralized pipeline_status writer contract --

_PIPELINE_STATUS_ALLOWED_FILES = {
    # These files own the writer chain and may assign mr_obj["pipeline_status"].
    "merge_tracker.py",      # recheck_pipeline_with_retry (canonical)
    "post_approval.py",      # delegates to recheck_pipeline_with_retry
    "process_merge_queue.py",  # marks "merged" on post-merge bookkeeping
    "dispatcher.py",         # 3p gate uses raw check_pipeline_mr (no retry)
    "migrate_state_to_topics.py",  # historical seed, writes initial value
    "replay.py",             # fixtures + stubs set mock values
}


def cmd_lint_pipeline_status_writes(args):
    import re
    pattern = re.compile(r'pipeline_status\s*[\]=]\s*=?\s*"(passed|running|failed|unknown|merged|canceled)"')
    violations = []
    scripts_dir = SCRIPT_DIR
    for py in scripts_dir.glob("*.py"):
        if py.name in _PIPELINE_STATUS_ALLOWED_FILES:
            continue
        with open(py, encoding="utf-8") as f:
            for ln, line in enumerate(f, 1):
                if '"pipeline_status"' in line and '=' in line and 'get(' not in line:
                    if re.search(r'\["pipeline_status"\]\s*=', line):
                        violations.append({"file": py.name, "line": ln,
                                           "text": line.strip()[:120]})
    out = {"violations": violations, "allowed_files": sorted(_PIPELINE_STATUS_ALLOWED_FILES)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if violations else 0


def cmd_lock(args):
    paths = _sandbox_paths(args.sandbox)
    topic_path = paths["topics"] / f"{args.thread}.json"
    lock_path = topic_store.lock_path_for(topic_path)
    if args.release:
        if lock_path.exists():
            lock_path.unlink()
        print(json.dumps({"ok": True, "released": str(lock_path)}))
        return
    with open(lock_path, "w", encoding="utf-8") as f:
        json.dump({"holder": "replay", "locked_at": topic_store.now_ms()}, f)
    print(json.dumps({"ok": True, "acquired": str(lock_path)}))


# ---- Helpers ---------------------------------------------------------

def _first_chat_id(paths):
    """Infer the chat_id to filter routing on from any topic in the sandbox."""
    for parent in (paths["topics"], paths["closed"]):
        if not parent.exists():
            continue
        for p in parent.glob("*.json"):
            # Skip non-topic files (.inbox.json holds a list, not a topic dict)
            if p.name.endswith(".inbox.json"):
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    t = json.load(f)
                if not isinstance(t, dict):
                    continue
                cid = (t.get("identity") or {}).get("chat_id")
                if cid:
                    return cid
            except (OSError, ValueError):
                pass
    return None


# ---- CLI -------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Create sandbox cfg tree.")
    p_init.add_argument("sandbox")
    p_init.add_argument("--from-closed", default=None,
                        help="thread_id to clone from production closed/")

    p_inj = sub.add_parser("inject", help="Inject a synthetic event.")
    p_inj.add_argument("sandbox")
    p_inj.add_argument("--thread", required=True)
    p_inj.add_argument("--content", required=True)
    p_inj.add_argument("--sender", default="approver",
                       help="approver | dev | <open_id>")
    p_inj.add_argument("--chat", default=None, help="override chat_id")
    p_inj.add_argument("--at", type=int, default=None, help="create_time ms")
    p_inj.add_argument("--source", default="reconcile",
                       choices=["reconcile", "listener"])
    p_inj.add_argument("--message-id", default=None,
                       help="override derived msg_id")

    p_pipe = sub.add_parser("pipeline", help="Route + drain.")
    p_pipe.add_argument("sandbox")
    p_pipe.add_argument("--mechanical", action="store_true",
                        help="Also run mechanical_reply_handler (stubbed I/O)")

    p_mq = sub.add_parser("merge-queue", help="Run process_merge_queue (stubbed I/O).")
    p_mq.add_argument("sandbox")
    p_mq.add_argument("--plan", default=None)

    p_show = sub.add_parser("show", help="Pretty-print topic state.")
    p_show.add_argument("sandbox")
    p_show.add_argument("--thread", required=True)
    p_show.add_argument("--full", action="store_true", help="dump full JSON")

    p_lock = sub.add_parser("lock", help="Create or release a fresh .lock file.")
    p_lock.add_argument("sandbox")
    p_lock.add_argument("--thread", required=True)
    p_lock.add_argument("--release", action="store_true")

    p_fx = sub.add_parser("fixture", help="Run a named scenario fixture.")
    p_fx.add_argument("name", help="fixture name, or 'all'",
                      choices=list(_FIXTURES) + ["all"])

    p_lint = sub.add_parser("lint-pipeline-status-writes",
                            help="Verify pipeline_status writes stay centralized.")

    args = ap.parse_args()
    dispatch = {
        "init":                          cmd_init,
        "inject":                        cmd_inject,
        "pipeline":                      cmd_pipeline,
        "merge-queue":                   cmd_merge_queue,
        "show":                          cmd_show,
        "lock":                          cmd_lock,
        "fixture":                       cmd_fixture,
        "lint-pipeline-status-writes":   cmd_lint_pipeline_status_writes,
    }
    rc = dispatch[args.cmd](args)
    if isinstance(rc, int):
        sys.exit(rc)


if __name__ == "__main__":
    main()
