# -*- coding: utf-8 -*-
"""Process the merge queue directly in Python — no Claude agent needed.

Called by poll_dispatch.py when dispatch_plan.json has a non-empty merge_queue.
Handles: rebase (skip_ci) -> merge -> update topic -> post Lark notification.

All operations are mechanical (API calls + template rendering), so this runs
inside the daemon with zero LLM token overhead.

Usage:
    python process_merge_queue.py <dispatch_plan.json>
    # Or imported: process_merge_queue.run(plan_path) -> dict
"""
import json
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

# scripts/ -> review-bot/ -> skills/ -> .claude/ -> <project root>. Derived from
# __file__, never cwd: this module runs inside the daemon, whose working
# directory is whatever the launcher happened to be started from.
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "..", ".."))

import agent_preflight
import merge_tracker
import terminal_notice
import topic_store
from templates.render import load_template, render, post_to_lark


def _log(level, msg):
    """Append to activity.log."""
    log_path = os.path.join(SCRIPT_DIR, "..", "cfg", "activity.log")
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [{level}] merge_queue: {msg}\n")
    except OSError:
        pass


def _mr_sort_key(repo_name):
    """Sort order: 3rd-party first, then chaos, then rage."""
    if repo_name.startswith("3rd_party/"):
        return (0, repo_name)
    if repo_name == "chaos":
        return (1, repo_name)
    return (2, repo_name)


def _post_conflict(root_message_id, ticket_id, developer_id, developer_name):
    """Post a rebase conflict notification via the rebase_conflict template."""
    template = load_template("rebase_conflict")
    rendered = render(template, {
        "TICKET_ID": ticket_id,
        "DEVELOPER_ID": developer_id,
        "DEVELOPER_NAME": developer_name or "\u5f00\u53d1\u8005",
    })
    return post_to_lark(rendered, root_message_id)


# How long the post-merge cherry-pick offer stays answerable. Long enough to
# span a night (the approver may merge late and answer next morning), short
# enough that an ignored offer cannot leave a topic open indefinitely — the
# dispatcher archives on expiry. See DESIGN §1.24.
CHERRYPICK_WINDOW_MS = 24 * 60 * 60 * 1000


def _repo_roots():
    """(rage_root, chaos_root) — env override, else derived from `__file__`.

    NEVER fall back to `os.getcwd()`. The daemon inherits its working directory
    from whatever launched it, and a `cwd`-relative fallback fails ASYMMETRICALLY:
    `rage_root=<scripts dir>` still works, because git walks up to the enclosing
    repo, while `chaos_root=<scripts dir>/chaos` does not exist at all. So the
    rage half of a cross-repo operation silently succeeds and the chaos half
    silently drops out — see §1.24.1 (RAGE-23816: chaos never cherry-picked).
    """
    rage_root = os.environ.get("RAGE_REPO_ROOT") or PROJECT_ROOT
    chaos_root = (os.environ.get("CHAOS_REPO_ROOT")
                  or os.path.join(rage_root, "chaos"))
    return rage_root, chaos_root


def _repo_root_for(repo):
    """Local checkout for a repo slug, for `git ls-remote`."""
    rage_root, chaos_root = _repo_roots()
    return chaos_root if repo == "chaos" else rage_root


def _offer_cherrypick(topic, ticket_id):
    """Post the cherry-pick offer and open the window. True if it opened.

    Returns False (topic archives normally) when no release branch is live,
    when no merged SHA was captured, when every merged repo is 3rd-party
    (§1.24.2), or when discovery fails — a missing offer is a degraded but safe
    outcome, whereas holding the topic open with nothing to answer would strand
    it (DESIGN §1.24).
    """
    import cherrypick

    shas = (topic.get("lifecycle") or {}).get("merge_shas") or {}
    if not shas:
        return False

    active_by_repo = {}
    failed_repos = []
    skipped_3p = []
    # Merged, but no landed commit was captured — cannot be cherry-picked, and
    # must not be dropped without a word (§1.24.4). 3rd-party repos are never
    # offered anyway, so they are not worth a warning line.
    no_sha_repos = [r for r in ((topic.get("lifecycle") or {})
                                .get("merged_without_sha") or [])
                    if not r.startswith("3rd_party/")]
    for repo in sorted(shas):
        # 3rd-party libs ship on their own release cadence — our weekly rc_*
        # branches say nothing about them, so they are never offered (§1.24.2).
        # This is also a correctness guard, not just policy: `_repo_root_for`
        # falls back to the rage checkout for any non-chaos slug, so a
        # 3rd_party/* repo would otherwise be credited with RAGE's release
        # branches and the offer would propose porting a lib commit onto them.
        if repo.startswith("3rd_party/"):
            skipped_3p.append(repo)
            continue
        try:
            active = cherrypick.discover_active_branches(_repo_root_for(repo))
        except Exception as exc:  # noqa: BLE001
            # A repo whose merged commit we hold but whose branches we could
            # not read is NOT the same as one that simply has no release
            # branch (normal for 3rd-party libs) — only the former is a gap
            # worth telling the approver about. See §1.24.1.
            _log("WARN", f"{ticket_id}: branch discovery failed for {repo}: {exc}")
            failed_repos.append(repo)
            continue
        if active:
            active_by_repo[repo] = active
    if not active_by_repo:
        return False

    mapping = cherrypick.build_prompt_mapping(active_by_repo)
    if not mapping:
        return False

    lines = []
    for token, info in mapping.items():
        if info.get("ambiguous"):
            continue
        lines.append([{"tag": "text",
                       "text": "  " + cherrypick.describe_token(token, info)}])
    if not lines:
        return False

    if failed_repos or no_sha_repos:
        # Never let a half-covered offer read as a complete one: the approver
        # answering `p2` would otherwise believe both repos landed on rc_p2.
        import mechanical_reply_handler  # lazy: only on the failure path
    if failed_repos:
        labels = "、".join(mechanical_reply_handler._label_for(r)
                          for r in failed_repos)
        lines.append([{"tag": "text",
                       "text": f"  ⚠️ {labels} 仓库的发布分支读取失败，本次"
                               f"不会自动 cherry-pick，请手动处理。"}])
    if no_sha_repos:
        # Same class of gap as failed_repos, different cause: the repo merged
        # but we never learned its commit, so there is nothing to pick from.
        labels = "、".join(mechanical_reply_handler._label_for(r)
                          for r in no_sha_repos)
        lines.append([{"tag": "text",
                       "text": f"  ⚠️ {labels} 仓库已合并但未取到落地 commit，"
                               f"本次不会自动 cherry-pick，请手动处理。"}])

    review = topic.setdefault("review", {})
    now_ms = int(time.time() * 1000)
    review["cherrypick_window_until"] = now_ms + CHERRYPICK_WINDOW_MS
    # Persist the mapping the approver is answering against. Re-deriving it
    # at reply time could resolve `p2` differently if an rc branch is cut in
    # between — the token must mean what the prompt said it meant.
    review["cherrypick_mapping"] = mapping
    review["cherrypick_active"] = active_by_repo
    topic_store.append_audit(topic, **{
        "ts": now_ms, "event": "cherrypick_offered",
        "tokens": sorted(mapping), "shas": shas,
        # Record what the offer actually COVERS, not just which repos merged —
        # conflating the two is what hid §1.24.1 for a full release cycle.
        "covered_repos": sorted(active_by_repo),
        "skipped_3rd_party": sorted(skipped_3p),
        "merged_without_sha": sorted(no_sha_repos),
    })
    if failed_repos:
        topic_store.append_audit(topic, **{
            "ts": now_ms, "event": "cherrypick_partial",
            "failed_repos": sorted(failed_repos),
        })

    template = load_template("cherrypick_prompt")
    rendered = render(template, {
        "TICKET_ID": ticket_id,
        "APPROVER_ID": _cherrypick_decider_id(),
        "BRANCH_LINES": lines,
    })
    post_to_lark(rendered, topic.get("root_message_id", ""))
    return True


def _primary_approver_id():
    """Primary approver open_id for the @-mention (env-resolved, never hardcoded)."""
    single = os.environ.get("REVIEW_BOT_APPROVER_OPEN_ID", "").strip()
    if single:
        return single
    multi = os.environ.get("REVIEW_BOT_APPROVER_OPEN_IDS", "").strip()
    return multi.split(",")[0].strip() if multi else ""


def _cherrypick_decider_id():
    """Open_id @-mentioned by the cherry-pick offer (DESIGN §1.24).

    The release-branch decision belongs to whoever owns the release, which
    is not necessarily the primary code approver. `REVIEW_BOT_CHERRYPICK_
    DECIDER_OPEN_ID` names that person; without it the primary approver is
    mentioned as before. Authorization is unchanged — any approver in
    `REVIEW_BOT_APPROVER_OPEN_IDS` may answer; this only picks the @-target.
    """
    decider = os.environ.get("REVIEW_BOT_CHERRYPICK_DECIDER_OPEN_ID",
                             "").strip()
    return decider or _primary_approver_id()


# Retry gate for merge failures. Errors containing these substrings are
# treated as non-retryable and trigger the manual-merge notification
# immediately; everything else notifies after MAX_MERGE_RETRIES attempts.
# 405 = "Method Not Allowed" (MR config rejects automated merge — e.g.
# required second approver, unresolved threads, protected-branch rule).
#   NOTE: an *already-merged* MR also returns 405, but that case is
#   resolved to success upstream in merge_tracker.merge_mr (live-state
#   recheck → already_merged), so a 405 reaching here is a genuine
#   config-reject, not a harmless double-merge. See DESIGN §1.6.8.
# 403 = "Forbidden" (token lacks merge scope).
_NON_RETRYABLE_MERGE_ERRORS = ("405", "403")
MAX_MERGE_RETRIES = 3


def _post_merge_failure(root_message_id, ticket_id, developer_id,
                        developer_name, approver_id, repo, error, mr_url):
    """Post manual-merge request via the merge_failed template."""
    template = load_template("merge_failed")
    rendered = render(template, {
        "TICKET_ID": ticket_id,
        "DEVELOPER_ID": developer_id,
        "DEVELOPER_NAME": developer_name or "\u5f00\u53d1\u8005",
        "APPROVER_ID": approver_id,
        "REPO": repo,
        "ERROR": (error or "")[:200],
        "MR_URL": mr_url or "",
    })
    return post_to_lark(rendered, root_message_id)


def _prior_merge_failure_count(topic):
    """Count consecutive `merge_failed` audits since the most recent
    APPROVED transition. Resets on approval so a topic that gets
    re-approved (rare edge case) doesn't inherit stale counts.
    """
    count = 0
    for entry in reversed(topic.get("audit") or []):
        event = entry.get("event")
        if event == "state_transition" and entry.get("to_state") == "APPROVED":
            break
        if event == "merge_failed":
            count += 1
    return count


def _has_recent_conflict_notification(topic, repo, sha):
    """True iff the most recent rebase_conflict_notified audit for this
    repo was at this same SHA. Re-notify only when the dev pushes new
    commits (sha advances); otherwise the second/third notification
    carries zero new information and just spams the developer."""
    for entry in reversed(topic.get("audit") or []):
        if entry.get("event") == "rebase_conflict_notified" and entry.get("repo") == repo:
            return entry.get("branch_sha") == sha
    return False


def _refresh_main_phase_heads(topic):
    """Re-resolve rage/chaos heads + diff stats before the main-phase review.

    `mrs[repo].branch_sha` and `review.ack_stats` are captured once, at ack
    time. The 3rd-party phase then runs for as long as it takes to review and
    merge the lib MR — hours, in practice — and the developer is typically
    still pushing to the consumer MRs during it, precisely because the lib API
    they are adapting to is what is under review. Carrying the ack-time SHA
    into the main phase reviews commits the developer replaced hours ago.

    Observed on RAGE-23816: ack captured chaos `227623d8` / rage `5b1a46be` at
    23:28-23:36; the 3rd-party phase ran until 01:49; the developer migrated
    both repos to the revised lib API at 00:58 / 01:00; the main-phase review
    at 02:37 still diffed the ack-time SHAs and raised two 严重 issues telling
    the developer to make a change they had already made. Both were invalid,
    and the topic had to be closed and refiled by hand.

    Returns a dict describing what changed (empty on no-op), or None when
    discovery failed — the caller must surface that rather than silently
    review stale SHAs.
    """
    import ack_new_topic  # lazy: keeps the daemon's import graph flat

    ticket_id = (topic.get("identity") or {}).get("ticket_id") or ""
    if not ticket_id:
        return None

    fresh, errors, _has_merged_3p, _probe_errored = ack_new_topic._discover_mrs(
        ticket_id)
    if errors or not fresh:
        return None

    rage_root, chaos_root = _repo_roots()

    mrs = topic.setdefault("mrs", {})
    review = topic.setdefault("review", {})
    ack_stats = review.setdefault("ack_stats", {})
    moved = {}

    for repo, fresh_mr in fresh.items():
        if repo.startswith("3rd_party/"):
            continue  # already merged; leave the merged-state entry alone
        old = mrs.get(repo) or {}
        old_sha = old.get("branch_sha")
        new_sha = fresh_mr.get("branch_sha")
        # Preserve runtime fields the merge phase owns; refresh discovery ones.
        for key in ("mr_iid", "branch", "target_branch", "branch_sha",
                    "web_url", "state"):
            if fresh_mr.get(key) is not None:
                old[key] = fresh_mr[key]
        mrs[repo] = old
        if new_sha and new_sha != old_sha:
            moved[repo] = {"from": old_sha, "to": new_sha}
        stats = ack_new_topic._fetch_stats(repo, old, rage_root, chaos_root)
        if stats is not None:
            ack_stats[repo] = stats

    if moved:
        # Stats drive triage (file/line thresholds), so a moved head can flip
        # simple <-> complex. Recompute rather than inherit the 3p-era value.
        review["triage"] = ack_new_topic._compute_triage(ack_stats)
    return moved


def _enqueue_phase_reset_event(topic, now_ms):
    """Queue a synthetic `events.pending[]` entry for the main-phase review.

    The 3rd-party -> main phase reset is the one state transition with no
    inbound Lark message behind it, and `dispatcher._find_work` keys entirely
    off a non-empty pending queue. Without this the topic sits in
    TRIAGING/main forever. Shaped like a router inbox record so the agent's
    drain treats it as an ordinary root/new_topic event; no `intent` is
    stamped, so `render_spawn_prompt._classify_event` buckets it as
    `new_topic` off the TRIAGING state.
    """
    identity = topic.get("identity") or {}
    thread_id = topic.get("thread_id", "")
    event_id = f"phase_reset:{thread_id}:{now_ms}"
    if topic_store.is_recent_event(topic, event_id):
        return
    pending = topic.setdefault("events", {}).setdefault("pending", [])
    if any(p.get("event_id") == event_id for p in pending):
        return
    pending.append({
        "event_id": event_id,
        "source": "3p_phase_reset",
        "received_at": now_ms,
        "sender_id": identity.get("creator_open_id", ""),
        "message_id": topic.get("root_message_id") or thread_id,
        "content": identity.get("ticket_id", ""),
        "mentions": [],
        "raw_path": "",
        "classified_as": None,
        "phase_at_arrival": "main",
        "state_at_arrival": "TRIAGING",
    })


def process_one_topic(entry):
    """Process a single merge-queue entry. Returns result dict."""
    topic_file = entry["topic_file"]
    ticket_id = entry["ticket_id"]
    root_msg_id = entry.get("root_message_id", "")
    developer_id = entry.get("developer_id", "")
    developer_name = entry.get("developer_name", "")
    mrs = entry.get("mrs", {})

    if not os.path.isfile(topic_file):
        return {"ticket_id": ticket_id, "status": "skipped", "reason": "file_missing"}

    # phase drift check. The dispatcher set entry["phase"]
    # when it built the plan; if the topic has since flipped phase (e.g.
    # 3p→main after 3p merged in a previous run, or approver rolled back),
    # skip this entry so we don't merge under a stale assumption.
    expected_phase = entry.get("phase") or "main"
    pre = agent_preflight.check_phase(topic_file, expected_phase=expected_phase)
    if pre.get("status") != "ok":
        _log("WARN",
             f"{ticket_id}: preflight {pre.get('status')} "
             f"expected_phase={expected_phase} "
             f"actual_phase={pre.get('actual_phase') or pre.get('phase')}")
        return {"ticket_id": ticket_id, "status": "skipped",
                "reason": pre.get("status")}

    topic = topic_store.read(topic_file)

    # Sort repos: 3rd-party first, chaos, rage
    sorted_repos = sorted(mrs.keys(), key=_mr_sort_key)

    # Filter out already-merged MRs (e.g., 3rd-party MRs merged in an earlier phase)
    active_repos = []
    for repo in sorted_repos:
        mr_obj = mrs[repo]
        if mr_obj.get("pipeline_status") == "merged" or mr_obj.get("state") == "merged":
            _log("INFO", f"{ticket_id}: skipping {repo} (already merged)")
            continue
        active_repos.append(repo)
    sorted_repos = active_repos

    # Phase 1: Rebase all MRs
    post_rebase_shas = {}
    pre_rebase_shas = {}
    for repo in sorted_repos:
        mr_obj = mrs[repo]
        _log("INFO", f"{ticket_id}: rebasing {repo} !{mr_obj.get('mr_iid') or mr_obj.get('iid')}")
        result = merge_tracker.rebase_mr(repo, mr_obj)
        if not result.get("success"):
            err = result.get("error", "unknown")
            _log("WARN", f"{ticket_id}: rebase failed {repo}: {err}")
            if "conflict" in err.lower():
                current_sha = mr_obj.get("branch_sha")
                now_ms = int(time.time() * 1000)
                if not _has_recent_conflict_notification(topic, repo, current_sha):
                    _post_conflict(root_msg_id, ticket_id, developer_id, developer_name)
                    topic_store.append_audit(topic, **{
                        "ts": now_ms, "event": "rebase_conflict_notified",
                        "repo": repo, "branch_sha": current_sha, "error": err[:200],
                    })
                # Park the topic: stop auto-retrying the rebase every cycle.
                # A content conflict never self-resolves, so re-attempting each
                # cycle is pure waste. _check_approved_topics skips topics with
                # review.rebase_conflict_blocked, so the merge queue won't re-enter
                # this topic until the developer rebases, pushes, and replies `ok`
                # — drain_rebase_conflict_ack SHA-verifies the push and clears the
                # flag. rebase_conflict_shas records the conflict-time head per repo
                # so the `ok` handler can detect whether the branch actually
                # advanced. See DESIGN §1.6.7.
                review = topic.setdefault("review", {})
                review["rebase_conflict_blocked"] = True
                review.setdefault("rebase_conflict_shas", {})[repo] = current_sha
                topic["lifecycle"]["updated_at"] = now_ms
                topic_store.write_atomic(topic_file, topic)
            return {"ticket_id": ticket_id, "status": "conflict", "repo": repo}
        post_rebase_shas[repo] = result.get("post_rebase_sha")
        pre_rebase_shas[repo] = result.get("pre_rebase_sha")

    # Phase 1.5: Wait for each rebased MR's detailed_merge_status to
    # resettle AND its head SHA to reflect the post-rebase commit.
    # GitLab recomputes merge-status asynchronously after a rebase; the
    # MR's cached sha + detailed_merge_status can transiently still read
    # the pre-rebase "mergeable" state for a few seconds. Firing PUT
    # /merge in that window returns 405 once GitLab then flips to the
    # transitional state. We therefore require BOTH sha == post_rebase_sha
    # AND detailed_merge_status == "mergeable" — mirroring what the UI's
    # "Checking if branch can be merged…" spinner waits for.
    for repo in sorted_repos:
        mr_obj = mrs[repo]
        settle = merge_tracker.wait_for_mergeable(
            repo, mr_obj,
            expected_sha=post_rebase_shas.get(repo),
            previous_sha=pre_rebase_shas.get(repo),
        )
        if not settle.get("settled"):
            _log("WARN", f"{ticket_id}: {repo} merge-status did not settle "
                         f"(last_status={settle.get('status')}, "
                         f"last_sha={(settle.get('sha') or '')[:8]}, "
                         f"expected_sha={(post_rebase_shas.get(repo) or '')[:8]}), "
                         f"attempting merge anyway")
        else:
            _log("INFO", f"{ticket_id}: {repo} settled in "
                         f"{settle.get('waited_s', 0):.1f}s")

    # Phase 2: Merge all MRs
    for repo in sorted_repos:
        mr_obj = mrs[repo]
        _log("INFO", f"{ticket_id}: merging {repo} !{merge_tracker._get_iid(mr_obj)}")
        result = merge_tracker.merge_mr(repo, mr_obj)
        if result.get("success") and result.get("sha"):
            # Per-repo landed commit — the cherry-pick source (DESIGN §1.24).
            topic.setdefault("lifecycle", {}).setdefault(
                "merge_shas", {})[repo] = result["sha"]
        elif result.get("success"):
            # Merged, but no sha came back (unreadable PUT body, or an
            # `already_merged` recheck that could not read one either). The
            # repo is absent from `merge_shas`, so the cherry-pick offer
            # cannot cover it — record it so the offer SAYS so instead of
            # silently shipping half a cross-repo change (DESIGN §1.24.4).
            merged_no_sha = topic.setdefault("lifecycle", {}).setdefault(
                "merged_without_sha", [])
            if repo not in merged_no_sha:
                merged_no_sha.append(repo)
        if not result.get("success"):
            err = result.get("error", "unknown")
            _log("ERROR", f"{ticket_id}: merge failed {repo}: {err}")
            now_ms = int(time.time() * 1000)
            topic_store.append_audit(topic, **{
                "ts": now_ms, "event": "merge_failed",
                "repo": repo, "error": err[:200],
            })
            topic["lifecycle"]["updated_at"] = now_ms

            # Counted after the append so this includes the failure we
            # just recorded — first failure → 1, third → 3.
            failure_count = _prior_merge_failure_count(topic)
            non_retryable = any(code in err for code in _NON_RETRYABLE_MERGE_ERRORS)
            if non_retryable or failure_count >= MAX_MERGE_RETRIES:
                approver_id = entry.get("approver_id", "")
                mr_url = mr_obj.get("web_url", "")
                try:
                    _post_merge_failure(root_msg_id, ticket_id,
                                        developer_id, developer_name,
                                        approver_id, repo, err, mr_url)
                    topic_store.append_audit(topic, **{
                        "ts": now_ms, "event": "merge_manual_required_notified",
                        "repo": repo, "non_retryable": non_retryable,
                        "failure_count": failure_count,
                    })
                except Exception as exc:  # noqa: BLE001
                    _log("WARN",
                         f"{ticket_id}: merge_failed notification post failed: {exc}")
                # Circuit breaker: gate re-entry into the merge queue.
                # _check_approved_topics inspects this flag and skips the
                # topic until an operator clears it (or the MR state
                # changes externally, e.g. manual merge → MR "merged" →
                # _check_approved_topics transitions the topic to MERGED).
                review = topic.setdefault("review", {})
                review["merge_manual_required"] = True
                review["merge_failure_reason"] = err[:200]

            topic_store.write_atomic(topic_file, topic)
            return {"ticket_id": ticket_id, "status": "error", "repo": repo, "error": err}

        if result.get("already_merged"):
            # merge_mr rescued a failed PUT by confirming the MR was already
            # merged (concurrent/duplicate pass, or retry after a lost MERGED
            # write). Treat as success but leave an audit trail so the
            # double-merge is visible. See DESIGN §1.6.8 / RAGE-15898.
            topic_store.append_audit(topic, **{
                "ts": int(time.time() * 1000),
                "event": "merge_idempotent_already_merged",
                "repo": repo,
                "mr_iid": merge_tracker._get_iid(mr_obj),
            })
            _log("INFO", f"{ticket_id}: {repo} already merged — idempotent "
                         f"merge, continuing to MERGED")

    # Phase 3: All MRs merged — post-merge handling depends on phase
    phase = entry.get("phase", "main")
    # A lib-only ticket (3rd-party MR, no rage/chaos consumer MR) has no main
    # phase to reset into: discovery would find nothing — the lib MR is merged
    # by now, and merged 3rd-party MRs are deliberately not re-added to `mrs`
    # (§1.3.4) — so ack would post `⚠️ 未找到 <ticket> 对应的 MR` into a thread
    # whose review just succeeded, and the synthetic phase-reset event would
    # re-dispatch the topic every cycle. Treat the lib merge as terminal.
    has_main_mr = any(not k.startswith("3rd_party/")
                      for k in (topic.get("mrs") or {}))
    if phase == "3rd_party" and not has_main_mr:
        _log("INFO", f"{ticket_id}: 3rd-party-only ticket (no main-repo MR) — "
                     f"treating lib merge as terminal, skipping phase reset")
        topic_store.append_audit(topic, event="phase_reset_skipped_no_main_mr")
        phase = "main"
    now_ms = int(time.time() * 1000)
    state_from = (topic.get("review") or {}).get("state")
    state_to = "TRIAGING" if phase == "3rd_party" else "MERGED"
    for repo in sorted_repos:
        mr_obj = mrs[repo]
        topic_store.append_audit(topic, **{
            "ts": now_ms, "event": "merge_queue_merged",
            "mr_iid": merge_tracker._get_iid(mr_obj), "repo": repo,
            "phase": phase,
            "state_from": state_from,
            "state_to": state_to,
        })
    topic["lifecycle"]["updated_at"] = now_ms

    if phase == "3rd_party":
        # Non-terminal: reset topic for main phase. phase
        # reset is a full re-triage, so clear anything the main-phase
        # flow will re-derive (pipeline_status, issues, flagged_issues).
        # Leaving stale "failed"/"passed" on rage/chaos MRs would let the
        # next _check_approved_topics skip a fresh recheck and either
        # mis-route or silently stall.
        for repo, mr_obj in (topic.get("mrs") or {}).items():
            if repo.startswith("3rd_party/"):
                mr_obj["pipeline_status"] = "merged"
                mr_obj["state"] = "merged"
            else:
                mr_obj.pop("pipeline_status", None)
        review = topic.setdefault("review", {})
        review["state"] = "TRIAGING"
        review["review_phase"] = "main"
        review["review_round"] = 0
        review.pop("flagged_issues", None)
        review.pop("issues", None)
        review.pop("pending_action", None)
        # The Lark doc + issue count belong to the 3rd-party review that just
        # merged; the main-phase round 1 must not inherit them.
        review.pop("issues_found", None)
        review.pop("lark_doc_token", None)
        review.pop("lark_doc_url", None)
        # Re-resolve the main-repo heads BEFORE handing the topic to the
        # reviewer: the ack-time SHAs are hours old by the time the 3rd-party
        # phase finishes, and the developer has usually pushed since.
        try:
            moved = _refresh_main_phase_heads(topic)
        except Exception as exc:  # noqa: BLE001 — never block the phase reset
            moved = None
            _log("WARN", f"{ticket_id}: head refresh raised: {exc}")
        if moved is None:
            # Do NOT pretend the SHAs are current. The topic still advances
            # (losing it would be worse), but the staleness is on the record
            # in both the log and the audit trail.
            _log("WARN", f"{ticket_id}: main-phase head refresh FAILED — "
                         f"review may run against ack-time SHAs")
            topic_store.append_audit(topic,
                event="phase_reset_head_refresh_failed")
        elif moved:
            _log("INFO", f"{ticket_id}: main-phase heads advanced since ack: "
                         f"{json.dumps(moved, ensure_ascii=False)}")
            topic_store.append_audit(topic,
                event="phase_reset_heads_refreshed", moved=moved)

        # Enqueue a synthetic work item. `dispatcher._find_work` only surfaces
        # topics with a non-empty `events.pending[]`, so a bare state reset
        # would leave the topic parked in TRIAGING forever — nothing else in
        # the pipeline re-triggers a phase-reset topic (RAGE-23816).
        _enqueue_phase_reset_event(topic, now_ms)
        topic_store.append_audit(topic,
            event="phase_reset",
            from_phase="3rd_party", to_phase="main",
            from_state=state_from, to_state="TRIAGING",
        )
        topic_store.write_atomic(topic_file, topic)

        # Post phase-transition notification
        try:
            content = {
                "zh_cn": {
                    "title": ticket_id,
                    "content": [[
                        {"tag": "text",
                         "text": "\u2705 \u7b2c\u4e09\u65b9\u5e93 MR \u5df2\u5408\u5e76\u3002"
                                 "\u6b63\u5728\u7ee7\u7eed\u5ba1\u67e5\u4e3b\u4ed3\u5e93\u4ee3\u7801\u2026"},
                    ]],
                }
            }
            post_to_lark(content, root_msg_id)
        except Exception as exc:
            _log("WARN", f"{ticket_id}: 3p phase-transition post failed: {exc}")

        _log("INFO", f"{ticket_id}: 3rd-party merged, topic reset to TRIAGING/main")
        return {"ticket_id": ticket_id, "status": "3p_merged"}

    # Main phase: terminal — MERGED + archive.
    # Mark each merged MR's state/pipeline_status so mrs[] stays consistent
    # with review.state. Without this, closed topics carry stale
    # pipeline_status="passed" and state="opened" even after merge.
    for repo in sorted_repos:
        mr_entry = (topic.get("mrs") or {}).get(repo)
        if isinstance(mr_entry, dict):
            mr_entry["state"] = "merged"
            mr_entry["pipeline_status"] = "merged"
    topic["lifecycle"]["merge_detected_at"] = now_ms
    review = topic.setdefault("review", {})
    review["state"] = "MERGED"
    # Clear any stale merge-failure circuit-breaker flags: if the topic was
    # parked by a prior failed/duplicate attempt and then merged (here or
    # externally), the manual-merge flag must not survive into the archived
    # record. See DESIGN §1.6.8.
    review.pop("merge_manual_required", None)
    review.pop("merge_failure_reason", None)
    topic["lifecycle"]["resolved_at"] = now_ms

    # Post merged notification via the shared helper. It sets
    # lifecycle.terminal_notice_posted so the passive dispatcher / recover
    # paths never double-post this topic. See DESIGN §1.6.11.
    try:
        result = terminal_notice.post_terminal_notice(topic, "MERGED")
        _log("INFO", f"{ticket_id}: merged notification posted: {result}")
    except Exception as exc:
        _log("WARN", f"{ticket_id}: merged post failed: {exc}")
    topic_store.write_atomic(topic_file, topic)

    # Post-merge cherry-pick offer (DESIGN §1.24). When release branches are
    # live, the topic must stay OUT of closed/ or router Gate 4a drops the
    # approver's answer. The window is bounded; the dispatcher archives on
    # expiry, so a never-answered offer cannot leak an open topic.
    opened_window = False
    try:
        opened_window = _offer_cherrypick(topic, ticket_id)
    except Exception as exc:  # noqa: BLE001
        _log("WARN", f"{ticket_id}: cherry-pick offer failed (archiving): {exc}")
    if opened_window:
        topic_store.write_atomic(topic_file, topic)
        _log("INFO", f"{ticket_id}: merged; cherry-pick window open")
        return {"ticket_id": ticket_id, "status": "merged_awaiting_cherrypick"}

    # Archive to closed/ (state already set to MERGED above)
    try:
        topics_dir = os.path.dirname(topic_file)
        index_path = os.path.join(topics_dir, "open_topic_index.json")
        thread_id = topic.get("thread_id", os.path.basename(topic_file).replace(".json", ""))
        topic_store.archive_topic(topics_dir, index_path, thread_id)
    except Exception as exc:
        _log("WARN", f"{ticket_id}: archive_topic failed (dispatcher will retry): {exc}")

    _log("INFO", f"{ticket_id}: merge queue complete")
    return {"ticket_id": ticket_id, "status": "merged"}


def run(plan_path):
    """Process all merge-queue entries from a dispatch plan file.

    Returns summary dict: {"processed": N, "merged": [...], "conflict": [...], "errors": [...]}
    """
    with open(plan_path, encoding="utf-8") as f:
        plan = json.load(f)

    queue = plan.get("merge_queue", []) + plan.get("3p_merge_queue", [])
    if not queue:
        return {"processed": 0, "merged": [], "conflict": [], "errors": []}

    merged = []
    conflict = []
    errors = []

    for entry in queue:
        try:
            result = process_one_topic(entry)
            status = result.get("status")
            tid = result.get("ticket_id", "?")
            if status == "merged":
                merged.append(tid)
            elif status == "conflict":
                conflict.append(tid)
            elif status == "error":
                errors.append(tid)
        except Exception as exc:
            _log("ERROR", f"merge_queue entry failed: {exc}")
            errors.append(entry.get("ticket_id", "?"))

    summary = {
        "processed": len(queue),
        "merged": merged,
        "conflict": conflict,
        "errors": errors,
    }
    _log("INFO", f"merge_queue done: {json.dumps(summary, ensure_ascii=False)}")
    return summary


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python process_merge_queue.py <dispatch_plan.json>")
        sys.exit(1)
    result = run(sys.argv[1])
    print(json.dumps(result, ensure_ascii=False))
