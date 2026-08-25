# -*- coding: utf-8 -*-
"""Mechanical acknowledgement for `dev_reply` events triggering round N+1.

Symmetric to `ack_new_topic.py` but for the dev-reply path. When a
developer pushes a fix and posts `ok` in a `SIMPLE_REVISION` /
`FULL_REVISION` topic, the topic agent eventually picks it up and runs
an incremental review — but that's a 30s-15min wait (diff fetch + Opus
spawn + manual-issue verification + artifact write + dispatcher post).
Until the round-N artifact lands, the dev sees zero feedback and may
assume the trigger missed.

This handler races ahead of the agent and posts a brief Chinese ack
(`🤖 已收到 RAGE-XXXXX 第 N+1 轮修改 · <repo> <branch> · K 个文件变更 +X/-Y · 正在审查...`)
so the user sees a response within ~5s. Total work per ack: a couple
`git ls-remote` / `git fetch` / `git diff --numstat` calls — the topic
agent will redo the same fetches, so the additional cost is the
numstat parse + a single Lark post.

Scope:
    pending has a `dev_reply` event AND audit lacks `dev_reply_ack_sent`
    for that event_id AND ≥1 repo's branch_sha advanced past
    `review.last_review_commit`
      → fetch numstat, post `ack_dev_reply` template, audit `dev_reply_ack_sent`
      → the `dev_reply` event stays in pending (agent still needs it)

Skipped (intentional):
    No SHA change on any repo  — agent will post `no_new_commits`
    Topic state not in {SIMPLE_REVISION, FULL_REVISION}
    Event already acked for this event_id (idempotency)
    Withdrawn message (agent's defensive fallback handles it)

DESIGN §1.3.2.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import ack_new_topic
import incr_base
import subprocess_util
import topic_store


SKILL_DIR = SCRIPT_DIR.parent
RENDER_PY = SCRIPT_DIR / "templates" / "render.py"
_ACTIVITY_LOG = SKILL_DIR / "cfg" / "activity.log"

_REVISION_STATES = {"SIMPLE_REVISION", "FULL_REVISION"}


def _log(level, msg):
    try:
        with open(_ACTIVITY_LOG, "a", encoding="utf-8") as f:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{ts}] [{level}] ack_dev_reply: {msg}\n")
    except OSError:
        pass


def _already_acked(topic, event_id):
    """Idempotency: skip if we already posted an ack for this event_id."""
    for entry in reversed(topic.get("audit") or []):
        if entry.get("triggered_by_event_id") == event_id:
            if entry.get("event") == "dev_reply_ack_sent":
                return True
    return False


def _find_dev_reply_event(topic):
    """Return the first `dev_reply` event in pending, or None."""
    for ev in (topic.get("events") or {}).get("pending") or []:
        if ev.get("intent") == "dev_reply" and ev.get("role") == "developer":
            return ev
    return None


def _resolve_branch_sha(repo, branch, rage_root, chaos_root):
    """`git ls-remote origin <branch>` to learn the current head SHA.

    Falls back to None on any error — the caller treats that as
    "skip this repo" (the agent's path is resilient to missing data).
    Chinese branch names work fine through git ls-remote; we never
    pass the branch through a shell here, only as a single argv.
    """
    root = ack_new_topic._repo_root(repo, rage_root, chaos_root)
    if root is None or not os.path.isdir(root):
        return None
    stdout, _stderr, rc = ack_new_topic._run_git(
        ["ls-remote", "origin", branch], cwd=root, timeout_s=30)
    if rc != 0 or not stdout:
        return None
    # `ls-remote` output: `<sha>\trefs/heads/<branch>`. Take the first
    # whitespace token of the first non-empty line.
    for line in stdout.splitlines():
        sha = line.split(None, 1)[0].strip() if line.strip() else ""
        if sha:
            return sha
    return None


def _incremental_stats(repo, new_sha, expected_sha, target_branch,
                       rage_root, chaos_root):
    """`git diff --numstat` over the rebase-safe incremental range.

    Resolves the range via `incr_base.resolve_incr_range` (per-repo base +
    ancestry check → two-dot since base, or three-dot vs target after a
    rebase). Returns `{file_count, insertions, deletions}` or None on
    failure. 3rd-party repos return None (no local checkout).
    """
    root = ack_new_topic._repo_root(repo, rage_root, chaos_root)
    if root is None or not os.path.isdir(root):
        return None
    resolved = incr_base.resolve_incr_range(
        root, expected_sha, new_sha, target_branch)
    if not resolved:
        return None
    stdout, _stderr, rc = ack_new_topic._run_git(
        ["diff", "--numstat", resolved["diff_range"]],
        cwd=root, timeout_s=60)
    if rc != 0:
        return None
    file_count = 0
    total_ins = 0
    total_del = 0
    for line in (stdout or "").splitlines():
        parts = line.rstrip().split("\t")
        if len(parts) < 3:
            continue
        ins_s, del_s = parts[0], parts[1]
        try:
            ins = 0 if ins_s == "-" else int(ins_s)
            dels = 0 if del_s == "-" else int(del_s)
        except ValueError:
            continue
        file_count += 1
        total_ins += ins
        total_del += dels
    return {
        "file_count": file_count,
        "insertions": total_ins,
        "deletions":  total_del,
    }


def _repo_paragraphs(mrs, incr_stats):
    """Build REPO_PARAGRAPHS for the ack_dev_reply template.

    Skips repos where the SHA didn't advance (incr_stats entry missing
    or zero files) — listing them with `0 个文件变更` would be noisy.
    3rd-party entries still show their MR link + branch but omit stats
    (we don't have a local checkout to numstat).
    """
    paragraphs = []
    for repo, mr in mrs.items():
        stats = incr_stats.get(repo)
        if stats is None:
            # 3rd-party: render anyway (link + branch), no stats tail.
            if repo.startswith("3rd_party/"):
                tail = f" {mr.get('branch') or ''}"
                paragraphs.append([
                    {"tag": "text",
                     "text": f"{ack_new_topic._label_for(repo)} 仓库 ",
                     "style": ["bold"]},
                    {"tag": "a",
                     "text": ack_new_topic._link_text(repo, mr.get('mr_iid')),
                     "href": mr.get("web_url") or ""},
                    {"tag": "text", "text": tail},
                ])
            continue
        if stats["file_count"] == 0:
            # No new commits on this repo — skip entirely. The dev may
            # have only pushed to one repo of a cross-repo MR pair.
            continue
        branch = mr.get("branch") or ""
        tail = (f" {branch} · {stats['file_count']} 个文件变更 "
                f"+{stats['insertions']} / -{stats['deletions']}")
        paragraphs.append([
            {"tag": "text",
             "text": f"{ack_new_topic._label_for(repo)} 仓库 ",
             "style": ["bold"]},
            {"tag": "a",
             "text": ack_new_topic._link_text(repo, mr.get('mr_iid')),
             "href": mr.get("web_url") or ""},
            {"tag": "text", "text": tail},
        ])
    return paragraphs


def _ack_one_topic(topic, topic_path, rage_root, chaos_root):
    """Process one topic; return result bucket string or None."""
    review = topic.get("review") or {}
    state = review.get("state")
    if state not in _REVISION_STATES:
        return None
    event = _find_dev_reply_event(topic)
    if event is None:
        return None
    event_id = event.get("event_id") or event.get("message_id") or ""
    if not event_id:
        return None
    if _already_acked(topic, event_id):
        return None
    last_review_commit = review.get("last_review_commit") or ""
    mrs = topic.get("mrs") or {}
    if not mrs or not last_review_commit:
        # Nothing to diff against — bail to the agent (rare; means the
        # topic skipped the round-1 flow or got corrupted).
        return None

    thread_id = topic.get("thread_id") or topic.get("root_message_id")
    ticket_id = (topic.get("identity") or {}).get("ticket_id") or ""
    if not thread_id or not ticket_id:
        return None

    # Resolve current SHA per repo + numstat against last_review_commit.
    # A repo that hasn't advanced returns 0-file stats and gets pruned
    # from REPO_PARAGRAPHS; a repo that hasn't advanced AT ALL across the
    # whole topic causes us to skip the ack (the agent will post the
    # `no_new_commits` template).
    incr_stats = {}
    any_advanced = False
    for repo, mr in mrs.items():
        branch = mr.get("branch") or ""
        if not branch:
            continue
        if repo.startswith("3rd_party/"):
            # We don't track 3rd-party SHA advances here — leave to the
            # agent. The repo still renders in REPO_PARAGRAPHS without
            # stats so the user sees the MR link.
            incr_stats[repo] = None
            continue
        new_sha = _resolve_branch_sha(repo, branch, rage_root, chaos_root)
        if not new_sha:
            continue
        expected_sha = incr_base.expected_sha_for(repo, last_review_commit)
        if expected_sha and new_sha == expected_sha:
            incr_stats[repo] = {"file_count": 0, "insertions": 0,
                                 "deletions": 0}
            continue
        target_branch = mr.get("target_branch") or (
            "rage/master" if repo == "chaos" else "master")
        stats = _incremental_stats(repo, new_sha, expected_sha, target_branch,
                                    rage_root, chaos_root)
        if stats is None:
            continue
        if stats["file_count"] > 0:
            any_advanced = True
        incr_stats[repo] = stats

    if not any_advanced:
        # No new commits anywhere — skip the ack. Agent will respond.
        return None

    round_now = int(review.get("review_round") or 1)
    variables = {
        "TICKET_ID":       ticket_id,
        "ROUND":           round_now + 1,
        "REPO_PARAGRAPHS": _repo_paragraphs(mrs, incr_stats),
    }
    posted_ok, resp = ack_new_topic._render_and_post(
        "ack_dev_reply", variables, thread_id)
    if not posted_ok:
        _log("WARN", f"{ticket_id}: ack_dev_reply post failed: {resp}")
        return "errors"

    topic_store.append_audit(topic,
        event="lark_reply_sent",
        reply_type="ack_dev_reply",
        triggered_by_event_id=event_id)
    topic_store.append_audit(topic,
        event="dev_reply_ack_sent",
        triggered_by_event_id=event_id,
        round=round_now + 1,
        repos=[r for r, s in incr_stats.items()
                if s and s.get("file_count")])
    topic_store.write_atomic(topic_path, topic)
    return "posted"


def drain_dev_reply_ack(topics_dir, index_path, cycle_id,
                         rage_root, chaos_root):
    """Run the dev-reply ack handler across all open topics."""
    topics_dir = Path(topics_dir)
    summary = {"checked": 0, "posted": 0, "errors": 0, "skipped_locked": 0,
               "topics_touched": []}
    if not topics_dir.is_dir():
        return summary

    for topic_path in sorted(topics_dir.glob("*.json")):
        if topic_path.name.startswith("."):
            continue
        # Skip the inbox sidecars produced by router.py.
        if topic_path.name.endswith(".inbox.json"):
            continue
        lock_file = topic_path.with_suffix(".lock")
        # Best-effort cooperative lock — same model as the other
        # mechanical handlers. We don't actually need the lock for the
        # ack itself (the topic_store.write_atomic is filesystem-safe),
        # but if a topic agent is mid-flight on this topic, leave the
        # acking to the next cycle to avoid double-writes racing on
        # `audit[]` / `events.pending`.
        if not topic_store.acquire_lock(str(lock_file),
                                         f"ack_dev_reply-{cycle_id}",
                                         cycle_id):
            summary["skipped_locked"] += 1
            continue
        try:
            try:
                topic = topic_store.read(str(topic_path))
            except (OSError, ValueError):
                continue
            summary["checked"] += 1
            result = _ack_one_topic(topic, str(topic_path),
                                     rage_root, chaos_root)
            if result == "posted":
                summary["posted"] += 1
                summary["topics_touched"].append(topic_path.stem)
            elif result == "errors":
                summary["errors"] += 1
        finally:
            topic_store.release_lock(str(lock_file))
    return summary
