"""Execute mechanical approver replies in-process (no Claude agent spawn).

Approver replies like `pass`, `1,3,5`, or `close` are pure functions of
the topic file: parse the reply, filter review.issues by indices, call
glab (approve/close), render a Lark template, post it. The dispatcher
calls drain_mechanical() between inbox drain and work-finding; any event
handled here is removed from events.pending[] so the Claude agent never
sees it. Wall-clock drops from ~20-40s (poll + Claude spawn + Claude run)
to ~2-5s (next 5s tick + Python execution).

Scope (minimum viable change):
    - TRIAGE_DECISION + approve (both phases)           -> APPROVED
    - AWAITING_APPROVAL + approve                       -> APPROVED
    - TRIAGE_DECISION + revision(indices) (main phase)  -> SIMPLE_REVISION
    - AWAITING_APPROVAL + revision(indices)             -> FULL_REVISION
    - AWAITING_APPROVAL + close                         -> CLOSED
    - DEV_TRIAGE + dev_triage(some rejected) (§1.23)    -> ARBITRATION
    - DEV_TRIAGE + dev_triage(accept all)    (§1.23)    -> *_REVISION
    - DEV_TRIAGE + approve (approver override)          -> APPROVED
    - ARBITRATION + approve `ok` (fix set empty)        -> APPROVED
    - ARBITRATION + approve `ok` (fix set non-empty)    -> *_REVISION
    - ARBITRATION + revision(reinstate indices)         -> *_REVISION

Deferred to Claude (unchanged paths):
    - TRIAGE_DECISION + escalate   -> Claude runs full review inline
    - ARBITRATION + escalate       -> Claude (full review, simple triage only)
    - *_REVISION + dev_reply       -> incremental review
    - unknown intent               -> Claude clarifies
"""

import json
import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import event_utils
import gitlab_threads
import merge_tracker
import post_approval
import reply_parser
import review_rounds
import subprocess_util
import topic_store


SKILL_DIR = SCRIPT_DIR.parent
RENDER_PY = SCRIPT_DIR / "templates" / "render.py"

# Which (state, intent) pairs this module owns. Everything else goes
# back to Claude via events.pending[].
# MERGED is here only for the post-merge cherry-pick window (DESIGN §1.24).
# It stays terminal — the topic is finished, merely still addressable while
# `review.cherrypick_window_until` is in the future — so the intent gate
# below admits nothing else in that state.
_MECHANICAL_STATES = {"TRIAGE_DECISION", "AWAITING_APPROVAL",
                      "DEV_TRIAGE", "ARBITRATION",
                      "SIMPLE_REVISION", "FULL_REVISION",
                      "MERGED"}
_MECHANICAL_INTENTS = {"approve", "revision", "close", "dev_triage",
                       "dev_handoff", "cherrypick", "cherrypick_skip"}
_MERGED_STATE_INTENTS = {"cherrypick", "cherrypick_skip"}
# In revision states the approver can only override with approve/close.
# Indices in revision states are the developer's territory (reply_parser
# returns unknown there), and re-dispatching revision from a revision
# state would loop — forbid it defensively.
_REVISION_STATE_INTENTS = {"approve", "close", "dev_handoff"}

# Revision states whose dev_reply events become candidates for the
# no-new-commits short-circuit.
_REVISION_STATES = {"SIMPLE_REVISION", "FULL_REVISION"}

# States from which the developer may hand the topic to the approver
# (DESIGN §1.23.7). Mirrors `reply_parser._HANDOFF_STATES`.
_HANDOFF_STATES = {"DEV_TRIAGE", "SIMPLE_REVISION", "FULL_REVISION"}

# Longest issue summary echoed into the hand-off ledger before truncation.
_SUMMARY_CAP = 80

# REPO_LABEL for Lark posts (keep in sync with spawn_topic_agent.md §3).
_REPO_LABEL = {"rage": "Game", "chaos": "Chaos"}


def _label_for(repo):
    if repo.startswith("3rd_party/"):
        return "3rd-party"
    return _REPO_LABEL.get(repo, repo)


def _repo_slug(repo, mr_obj):
    return mr_obj.get("repo_slug") or merge_tracker.REPO_SLUGS.get(repo) or repo


def _already_posted(topic, event_id):
    """True if a prior cycle already posted for this event_id.

    Matches the Claude agent's dedup convention (audit entry
    `lark_reply_sent` with matching triggered_by_event_id).
    """
    for entry in reversed(topic.get("audit") or []):
        if entry.get("triggered_by_event_id") == event_id:
            if entry.get("event") in ("lark_reply_sent",
                                      "lark_reply_duplicate_suppressed"):
                return True
    return False


def _render_and_post(template_name, variables, message_id):
    """Invoke templates/render.py as subprocess with --post --message-id.

    Returns (ok, response_text). On non-zero exit, ok is False.
    """
    vars_blob = json.dumps(variables, ensure_ascii=False)
    cmd = ["python", str(RENDER_PY), template_name,
           "--vars", vars_blob, "--post", "--message-id", message_id]
    try:
        proc = subprocess_util.hidden_run(
            cmd, capture_output=True, text=True,
            timeout=30, encoding="utf-8", errors="replace")
    except (OSError, Exception) as exc:  # noqa: BLE001
        return False, str(exc)[:200]
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()[:300]
    return True, (proc.stdout or "").strip()


def _post_plain_text(text, message_id):
    """Post a plain-text reply via lark-cli (no template). Returns (ok, response)."""
    cmd = subprocess_util.lark_cli_argv_prefix() + [
           "im", "+messages-reply",
           "--message-id", message_id, "--reply-in-thread",
           "--as", "bot", "--msg-type", "text", "--text", text]
    try:
        proc = subprocess_util.hidden_run(
            cmd, capture_output=True, text=True,
            timeout=30, encoding="utf-8", errors="replace")
    except (OSError, Exception) as exc:  # noqa: BLE001
        return False, str(exc)[:200]
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()[:300]
    return True, (proc.stdout or "").strip()


def _glab_approve(repo, mr_obj):
    """glab mr approve. Returns (ok, err_str)."""
    iid = merge_tracker._get_iid(mr_obj)
    slug = _repo_slug(repo, mr_obj)
    if not iid or not slug:
        return False, "missing iid or slug"
    stdout, stderr, rc = merge_tracker._run_glab(
        ["glab", "api", "--method", "POST",
         f"projects/{merge_tracker._urlenc(slug)}"
         f"/merge_requests/{iid}/approve"],
        timeout_s=30,
    )
    if rc != 0:
        err = (stderr or stdout or "").strip()
        # Already-approved is fine.
        if "already approved" in err.lower() or "401" in err:
            return True, ""
        return False, err[:200]
    return True, ""


def _glab_close(repo, mr_obj):
    """PUT state_event=close. Returns (ok, err_str). Idempotent on already-closed."""
    iid = merge_tracker._get_iid(mr_obj)
    slug = _repo_slug(repo, mr_obj)
    if not iid or not slug:
        return False, "missing iid or slug"
    stdout, stderr, rc = merge_tracker._run_glab(
        ["glab", "api", "--method", "PUT",
         f"projects/{merge_tracker._urlenc(slug)}/merge_requests/{iid}",
         "-f", "state_event=close"],
        timeout_s=30,
    )
    if rc != 0:
        return False, (stderr or stdout or "").strip()[:200]
    return True, ""


def _render_mod():
    """Lazy-import templates/render.py (paragraph builders live there)."""
    import sys as _sys
    _sys.path.insert(0, str(SCRIPT_DIR / "templates"))
    import render as _render
    return _render


def _flagged_issue_paragraphs(issues):
    """Preserved-#N issue lines for the revision_request template.

    Thin wrapper around render.build_indexed_issue_paragraphs — same
    single-line shape as the round-1 review list, but keeping the ORIGINAL
    round-1 index (no renumber, no re-sort) so the developer sees exactly
    the line the approver flagged. See DESIGN §1.9.4.
    """
    return _render_mod().build_indexed_issue_paragraphs(issues)


def _primary_approver_id(approver_open_ids=None):
    """The primary @-mention approver (REVIEW_BOT_APPROVER_ID).

    Mirrors dispatcher._load_env's resolution order: os.environ first,
    then the env block of .claude/settings(.local).json, finally the
    first entry of the namelist the caller already holds. Returns ""
    when nothing resolves (callers must treat that as a post-blocking
    error — an empty user_id makes Lark reject the at-mention).
    """
    approver = os.environ.get("REVIEW_BOT_APPROVER_ID", "").strip()
    if approver:
        return approver
    project_root = SKILL_DIR.parent.parent.parent
    for fname in ("settings.json", "settings.local.json"):
        path = project_root / ".claude" / fname
        if path.is_file():
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                approver = (data.get("env", {})
                            .get("REVIEW_BOT_APPROVER_ID", "")).strip()
            except (json.JSONDecodeError, OSError):
                continue
            if approver:
                return approver
    if approver_open_ids:
        return approver_open_ids[0]
    return ""


# ── Per-intent handlers ───────────────────────────────────────────────

def _handle_approve(topic, topic_path, event, thread_id, ticket_id):
    """Approve every MR; for main phase delegate to post_approval helper,
    for 3rd-party phase post the approval template directly with the fixed
    "等待合并队列处理。" pipeline message.

    Main-phase contract (unchanged): post_approval_on_topic owns pipeline
    recheck (via merge_tracker.recheck_pipeline_with_retry), infra-retry
    audits, approval render/post, lark_reply_sent audit, state transition
    to APPROVED, and the atomic write.

    3rd-party contract: 3rd-party MRs may have no pipeline at all and the
    bot merges regardless of pipeline state once the queue processes them
    (see DESIGN.md §1.6.4). Running main-phase pipeline retry here would
    flag legitimate "no pipeline" cases as failures and block the merge,
    so we skip post_approval and post directly. process_merge_queue.py
    already drives 3rd-party MRs to merge + phase reset to main, so all
    we owe Lark is the approval notification.
    """
    mrs = topic.get("mrs") or {}
    if not mrs:
        return False, "no mrs to approve"
    review = topic.get("review") or {}
    is_third_party = review.get("review_phase") == "3rd_party"
    event_id = event.get("event_id") or event.get("message_id")
    state_at = event.get("_state_at_dispatch")

    # 1. Approve each MR on GitLab.
    for repo, mr_obj in mrs.items():
        ok, err = _glab_approve(repo, mr_obj)
        if not ok:
            return False, f"approve failed for {repo}: {err}"

    if is_third_party:
        # 2a. Direct post — no pipeline recheck.
        if not _already_posted(topic, event_id):
            ok, resp = _render_and_post("approval", {
                "TICKET_ID": ticket_id,
                "PIPELINE_MSG": "等待合并队列处理。",
            }, thread_id)
            if not ok:
                return False, f"3p approval post failed: {resp}"
            topic_store.append_audit(topic,
                event="lark_reply_sent",
                reply_type="approval",
                triggered_by_event_id=event_id,
                phase="3rd_party",
            )
        # 2b. State transition.
        review["state"] = "APPROVED"
        review.pop("pending_action", None)
        topic.setdefault("lifecycle", {})["updated_at"] = topic_store.now_ms()
        topic_store.append_audit(topic,
            event="state_transition",
            from_state=state_at,
            to_state="APPROVED",
            triggered_by_event_id=event_id,
        )
        topic_store.append_audit(topic,
            event="approver_approve",
            from_state=state_at,
            to_state="APPROVED",
            triggered_by_event_id=event_id,
            phase="3rd_party",
        )
        topic_store.write_atomic(topic_path, topic)
        return True, "3p_approved"

    # 2. Main phase — delegate pipeline recheck + approval post + state flip.
    result = post_approval.post_approval_on_topic(
        topic, topic_path, event_id, from_state=state_at)
    if not result.get("ok"):
        return False, result.get("reason") or "post_approval_failed"
    any_failed = result.get("pipeline_failed", False)
    # 3. Mechanical-specific audit. post_approval already appended
    #    lark_reply_sent + state_transition; this entry preserves the
    #    mechanical intent trail the merge queue agent reads.
    topic.setdefault("lifecycle", {})["updated_at"] = topic_store.now_ms()
    topic_store.append_audit(topic,
        event="approver_approve",
        from_state=state_at,
        to_state="APPROVED",
        triggered_by_event_id=event_id,
        pipeline_failed=any_failed,
    )
    topic_store.write_atomic(topic_path, topic)
    return True, result.get("reason") or "approved"


def _handle_revision(topic, event, indices, state, thread_id, ticket_id,
                     exclude=False):
    """Filter review.issues by indices, copy to flagged_issues, post revision_request.

    `exclude=False` (default): `indices` is the positive selection
    (typed `1 3 5`) — flag exactly those.

    `exclude=True`: `indices` lists exclusions (typed `-1 -3` or
    `all -1 -3`). Flag every issue in `review.issues` whose `index`
    is NOT in `indices`. Bare `all` is `exclude=True, indices=[]`,
    which flags every issue. Returning empty (e.g. the operator
    typed `-1 -2 -3` on a 3-issue review and effectively excluded
    everything) is rejected — the operator should use `pass` if they
    meant to approve.

    Legacy decision states ONLY. ARBITRATION revision events dispatch to
    _handle_arbitration_reinstate; the explicit map below fails loudly if
    a new state ever reaches here instead of silently mapping to
    FULL_REVISION via an else-branch.
    """
    next_state = {"TRIAGE_DECISION": "SIMPLE_REVISION",
                  "AWAITING_APPROVAL": "FULL_REVISION"}.get(state)
    if next_state is None:
        return False, f"revision transition not defined from state {state!r}"
    review = topic.setdefault("review", {})
    issues = review.get("issues") or []
    if exclude:
        exclude_set = set(indices)
        flagged = [i for i in issues if i.get("index") not in exclude_set]
        if not flagged:
            return False, (f"empty flagged set after excluding {indices} "
                           f"from {len(issues)} issues — use 'ok' to approve")
    else:
        flagged = [i for i in issues if i.get("index") in set(indices)]
        if not flagged:
            return False, f"no issues match indices {indices}"
    # The entries above are pristine copies out of `review.issues`, which never
    # carries a verdict — so a rebuild used to wipe every verification and make
    # round N+1 re-check issues round N had already confirmed fixed. Carry the
    # settled ones across (DESIGN §1.4.8).
    review["flagged_issues"] = review_rounds.carry_verification(
        flagged, review.get("flagged_issues"))

    identity = topic.get("identity") or {}
    dev_id = identity.get("creator_open_id") or ""
    # render.py's validate_required rejects empty DEVELOPER_NAME. ack_new_topic
    # doesn't always populate identity.developer (Lark open_id lookup may miss,
    # MR author can be a bot), so fall back to a generic label rather than
    # letting the mechanical drain thrash forever.
    dev_name = identity.get("developer") or "开发者"

    if _already_posted(topic, event.get("event_id") or event.get("message_id")):
        # Don't re-post but do transition state (in case prior cycle
        # posted then crashed before writing state).
        pass
    else:
        ok, resp = _render_and_post("revision_request", {
            "TICKET_ID": ticket_id,
            "DEVELOPER_ID": dev_id,
            "DEVELOPER_NAME": dev_name,
            "FLAGGED_ISSUE_PARAGRAPHS": _flagged_issue_paragraphs(flagged),
        }, thread_id)
        if not ok:
            return False, f"revision_request post failed: {resp}"
        topic_store.append_audit(topic,
            event="lark_reply_sent",
            reply_type="revision_request",
            triggered_by_event_id=event.get("event_id") or event.get("message_id"),
        )

    review["state"] = next_state
    review.pop("pending_action", None)
    topic.setdefault("lifecycle", {})["updated_at"] = topic_store.now_ms()
    topic_store.append_audit(topic,
        event="approver_revision_request",
        from_state=state,
        to_state=next_state,
        indices=indices,
        exclude=exclude,
        flagged_indices=sorted(i.get("index") for i in flagged
                                if i.get("index") is not None),
        triggered_by_event_id=event.get("event_id") or event.get("message_id"),
    )
    return True, "revision_requested"


def _refresh_manual_issues(topic, event_id):
    """In-process manual-issue refresh at arbitration time (DESIGN §1.23.3).

    Fetch + reconcile only — no verification (that needs Claude and happens
    at the next round-N spawn). Returns the list of NEWLY added manual
    issues (display-only 待验证 entries) so the caller can append them to
    the revision_request post.

    Non-fatal by design: ANY fetch error skips the reconcile entirely and
    returns [] — reconcile_manual_issues prunes entries absent from the
    fetch result, so reconciling a partial fetch would silently drop live
    manual issues (and their verification verdicts). Never block the fix
    list on a glab blip.
    """
    review = topic.setdefault("review", {})
    try:
        fetched, errors = gitlab_threads.fetch_for_topic(topic)
    except Exception as exc:  # noqa: BLE001 — glab/network must never block
        fetched, errors = [], [str(exc)[:200]]
    if errors:
        topic_store.append_audit(topic,
            event="manual_refresh_failed_nonfatal",
            triggered_by_event_id=event_id,
            errors=errors[:5],
        )
        return []
    existing = review.get("manual_issues") or []
    existing_ids = {e.get("discussion_id") for e in existing}
    merged, summary = gitlab_threads.reconcile_manual_issues(existing, fetched)
    review["manual_issues"] = merged
    if summary["added"] or summary["pruned"]:
        topic_store.append_audit(topic,
            event="manual_issues_synced",
            triggered_by_event_id=event_id,
            summary=summary,
        )
    return [m for m in merged
            if m.get("discussion_id") not in existing_ids]


def _arbitration_fix_state(review):
    """The *_REVISION state an arbitration outcome lands in.

    The DEV_TRIAGE/ARBITRATION states don't encode simple-vs-full;
    `review.triage` is authoritative (set at ack time; flipped to
    "complex" by the agent on escalate — see spawn_topic_agent.md).
    """
    return ("SIMPLE_REVISION" if review.get("triage") == "simple"
            else "FULL_REVISION")


def _open_triage_universe(review, all_indices):
    """Indices the developer may still decide on this round.

    Round 1 sees every issue. Later rounds must not re-ask about issues that
    are already done with: one the bot has confirmed fixed (a settled verdict
    on `flagged_issues`) or one the dev already rejected in an earlier round.
    Reinstated issues are the exception — the approver put them back, so they
    are open again even though they sit in `rejected_indices`.

    Without this narrowing, a round-2 reply of `1 3` ("I'll fix these") would
    be read as rejecting every other issue, silently discarding round-1 work.
    """
    dev_triage = review.get("dev_triage") or {}
    prev_rejected = set(dev_triage.get("rejected_indices") or [])
    reinstated = set(dev_triage.get("reinstated_indices") or [])
    settled = {e.get("index") for e in (review.get("flagged_issues") or [])
               if review_rounds.is_settled(e)}
    return [n for n in all_indices
            if n not in settled
            and (n not in prev_rejected or n in reinstated)]


def _handle_dev_triage(topic, event, parsed, thread_id, ticket_id,
                       approver_open_ids=None):
    """Record the developer's triage of this round's issues (DESIGN §1.23.6).

    Self-service loop: the dev's decision goes straight to the fix state, and
    their rejections accumulate on `review.dev_triage` for the approver to
    look at once, at hand-off. ARBITRATION is no longer entered.

    `parsed` modes (indices = "issues I will fix"), evaluated against the
    still-open subset for this round, not the whole round-1 list:
        none=True               -> accepted = []        (reject all)
        exclude=True            -> accepted = all open except indices
                                   (`-N` reject form; bare `all` = fix all)
        otherwise               -> accepted = indices

    Invalid indices, and any attempt to re-reject an issue the approver
    reinstated, post a plain-text error and DROP the event (ok=True, no
    state change) — leaving it pending would poison-loop the drain every
    cycle on the same typo. The developer just re-replies.
    """
    review = topic.setdefault("review", {})
    issues = review.get("issues") or []
    event_id = event.get("event_id") or event.get("message_id")
    indices = list(parsed.get("indices") or [])
    exclude = bool(parsed.get("exclude"))
    none = bool(parsed.get("none"))
    reason = (parsed.get("reason") or "").strip()

    all_indices = sorted(i.get("index") for i in issues
                         if i.get("index") is not None)
    if not all_indices:
        return False, "no issues on review — DEV_TRIAGE state is invalid"

    open_indices = _open_triage_universe(review, all_indices)
    if not open_indices:
        # Everything is settled or already rejected — nothing left to
        # decide, so the only sensible next move is the hand-off.
        ok, resp = _post_plain_text(
            f"{ticket_id} 本轮没有待确认的问题——回复 done 提交审查人终审。",
            thread_id)
        if not ok:
            return False, f"empty-universe nudge post failed: {resp}"
        topic_store.append_audit(topic,
            event="dev_triage_nothing_open",
            triggered_by_event_id=event_id,
        )
        return True, "nothing_open_dropped"

    # Naming an index you previously disputed retracts the dispute
    # (DESIGN §1.23.6). It is deliberately not in `open_indices`: `all` and
    # silence must not resurrect it, only an explicit mention.
    prior_triage = review.get("dev_triage") or {}
    retractable = [n for n in (prior_triage.get("rejected_indices") or [])
                   if n in set(all_indices)]
    known = set(open_indices) | set(retractable)
    unknown = [n for n in indices if n not in known]
    if unknown:
        label = " ".join(f"#{n}" for n in unknown)
        valid = " ".join(f"#{n}" for n in sorted(known))
        ok, resp = _post_plain_text(
            f"{ticket_id} 无效的问题序号：{label}（本轮待确认的问题为 {valid}），"
            f"请重新回复。", thread_id)
        if not ok:
            return False, f"invalid-indices error post failed: {resp}"
        topic_store.append_audit(topic,
            event="dev_triage_invalid_indices",
            triggered_by_event_id=event_id,
            indices=indices, exclude=exclude,
            valid_indices=open_indices,
        )
        return True, "invalid_indices_dropped"

    if none:
        accepted_now = []
    elif exclude:
        rejected_set = set(indices)
        accepted_now = [n for n in open_indices if n not in rejected_set]
    else:
        accepted_now = sorted(set(indices))

    # Silence only demotes an issue the dev has not already committed to.
    # In round 2+ the dev naming "1" means "this is the one I'm fixing
    # now", not "I retract everything I accepted last round" — flipping
    # those to disputes would push work at the approver that the dev never
    # meant to dispute, with no notice anywhere (review issue #4). An
    # explicit `-N` / `none` still demotes, which is the documented way.
    prior = review.get("dev_triage") or {}
    committed = set(prior.get("accepted_indices") or [])
    if none:
        # `none` / `0` / `不修` names no indices, so it must be expanded to
        # the whole open set — reusing `indices` here made the one verb that
        # exists to dispute everything demote nothing at all.
        demotable = set(open_indices)
    elif exclude:
        demotable = set(indices)
    else:
        demotable = set()
    rejected_now = [n for n in open_indices
                    if n not in set(accepted_now)
                    and (n not in committed or n in demotable)]
    # Anything committed and not demoted stays accepted.
    accepted_now = sorted(set(accepted_now)
                          | (committed - set(rejected_now)))

    dev_triage = dict(review.get("dev_triage") or {})
    reinstated = set(dev_triage.get("reinstated_indices") or [])

    # Once the approver reinstates an issue, the dev's dissent on it is
    # spent — otherwise the two could volley the same index forever.
    clash = sorted(reinstated & set(rejected_now))
    if clash:
        label = " ".join(f"#{n}" for n in clash)
        ok, resp = _post_plain_text(
            f"{ticket_id} {label} 已由审查人确认必须修复，不能再次标记异议，"
            f"请重新回复。", thread_id)
        if not ok:
            return False, f"reinstated-lock error post failed: {resp}"
        topic_store.append_audit(topic,
            event="dev_triage_reinstated_locked",
            triggered_by_event_id=event_id,
            locked_indices=clash,
        )
        return True, "reinstated_locked_dropped"

    prev_accepted = set(dev_triage.get("accepted_indices") or [])
    prev_rejected = set(dev_triage.get("rejected_indices") or [])
    # This round's decision wins over an earlier one for the same index:
    # accepting something previously rejected clears the rejection.
    rejected = sorted((prev_rejected | set(rejected_now)) - set(accepted_now))
    accepted = sorted((prev_accepted | set(accepted_now)) - set(rejected))

    dev_triage["accepted_indices"] = accepted
    dev_triage["rejected_indices"] = rejected
    dev_triage["decided_at"] = topic_store.now_ms()
    dev_triage["triggered_by_event_id"] = event_id
    if reason or rejected_now:
        # One entry per round that carried a dissent OR an explanation.
        # Recording it only alongside a rejection silently ate the note in
        # "all 这些我都会修，顺带说明一下上下文" (review issue #8), and the
        # point of §1.23.8 is to give the approver the why either way.
        reasons = list(dev_triage.get("reasons") or [])
        reasons.append({
            "round": review.get("review_round") or 0,
            "rejected_indices": sorted(rejected_now),
            "text": reason,
            "at": topic_store.now_ms(),
        })
        dev_triage["reasons"] = reasons
    review["dev_triage"] = dev_triage

    settled = {e.get("index") for e in (review.get("flagged_issues") or [])
               if review_rounds.is_settled(e)}
    to_fix = [n for n in accepted if n not in settled]
    if not to_fix:
        # Nothing outstanding to fix, so `revision_request` would be an
        # empty list. Hold in DEV_TRIAGE and point at the hand-off verb.
        ok, resp = _post_plain_text(
            f"{ticket_id} 已记录：本轮没有需要修复的问题。"
            f"回复 done 提交审查人终审，或推送新提交后回复 ok 重新审查。",
            thread_id)
        if not ok:
            return False, f"all-rejected nudge post failed: {resp}"
        topic_store.append_audit(topic,
            event="dev_triage_all_rejected",
            triggered_by_event_id=event_id,
            accepted_indices=accepted,
            rejected_indices=rejected,
        )
        return True, "dev_triage_all_rejected"

    by_index = {i.get("index"): i for i in issues}
    # `flagged` keeps every accepted issue so settled verdicts survive the
    # rebuild (§1.4.8); only the unsettled ones are worth printing.
    flagged = [by_index[n] for n in accepted if n in by_index]
    display = [by_index[n] for n in to_fix if n in by_index]
    return _post_fix_list_and_transition(
        topic, event, flagged, thread_id, ticket_id,
        prefix_paragraphs=None,
        audit_event="dev_triage_recorded",
        audit_extra={"accepted_indices": accepted,
                     "rejected_indices": rejected},
        display=display)


def _post_fix_list_and_transition(topic, event, flagged, thread_id, ticket_id,
                                  prefix_paragraphs, audit_event, audit_extra,
                                  display=None):
    """Shared triage tail: refresh manual issues, post the final fix
    list (revision_request + optional prefix/manual sections), transition
    to the triage-appropriate *_REVISION state. See DESIGN §1.23.2–.3.

    `flagged` is what the ledger records (every issue the dev owns, so
    settled verdicts survive the rebuild — §1.4.8). `display` is what the
    post prints; it defaults to `flagged` and is narrowed to the still-
    unfixed subset from round 2 on, so the dev isn't handed a fix list
    that re-lists work they already did (§1.23.6).
    """
    review = topic.setdefault("review", {})
    event_id = event.get("event_id") or event.get("message_id")
    # Same rebuild-wipes-verdicts hazard as `_handle_revision` (§1.4.8).
    review["flagged_issues"] = review_rounds.carry_verification(
        flagged, review.get("flagged_issues"))

    identity = topic.get("identity") or {}
    dev_id = identity.get("creator_open_id") or ""
    dev_name = identity.get("developer") or "开发者"

    if not _already_posted(topic, event_id):
        new_manual = _refresh_manual_issues(topic, event_id)
        render = _render_mod()
        variables = {
            "TICKET_ID": ticket_id,
            "DEVELOPER_ID": dev_id,
            "DEVELOPER_NAME": dev_name,
            "FLAGGED_ISSUE_PARAGRAPHS": _flagged_issue_paragraphs(
                flagged if display is None else display),
        }
        if prefix_paragraphs:
            variables["PREFIX_PARAGRAPHS"] = prefix_paragraphs
        if new_manual:
            variables["MANUAL_ISSUE_PARAGRAPHS"] = (
                render.build_manual_issue_paragraphs(new_manual))
        ok, resp = _render_and_post("revision_request", variables, thread_id)
        if not ok:
            return False, f"revision_request post failed: {resp}"
        topic_store.append_audit(topic,
            event="lark_reply_sent",
            reply_type="revision_request",
            triggered_by_event_id=event_id,
        )

    from_state = review.get("state")
    next_state = _arbitration_fix_state(review)
    review["state"] = next_state
    review.pop("pending_action", None)
    topic.setdefault("lifecycle", {})["updated_at"] = topic_store.now_ms()
    topic_store.append_audit(topic,
        event=audit_event,
        from_state=from_state,
        to_state=next_state,
        flagged_indices=sorted(i.get("index") for i in flagged
                               if i.get("index") is not None),
        triggered_by_event_id=event_id,
        **audit_extra,
    )
    return True, audit_event


def _handle_arbitration_accept(topic, topic_path, event, thread_id, ticket_id):
    """Approver accepts the dev's triage (`ok`/`pass` in ARBITRATION).

    Fix set empty (dev rejected all, approver agrees) — delegate verbatim
    to _handle_approve: the approver's OK doubles as approval, no pointless
    revision round (DESIGN §1.23.2). Otherwise post the agreed fix list and
    enter the triage-appropriate *_REVISION state.
    """
    review = topic.setdefault("review", {})
    event_id = event.get("event_id") or event.get("message_id")
    dev_triage = review.get("dev_triage") or {}
    accepted = list(dev_triage.get("accepted_indices") or [])

    if not accepted:
        topic_store.append_audit(topic,
            event="arbitration_accept_empty",
            triggered_by_event_id=event_id,
        )
        return _handle_approve(topic, topic_path, event, thread_id, ticket_id)

    issues = review.get("issues") or []
    flagged = [i for i in issues if i.get("index") in set(accepted)]
    return _post_fix_list_and_transition(
        topic, event, flagged, thread_id, ticket_id,
        prefix_paragraphs=None,
        audit_event="arbitration_accepted",
        audit_extra={"accepted_indices": accepted})


def _handle_arbitration_reinstate(topic, event, parsed, thread_id, ticket_id):
    """Approver reinstates dev-rejected issues (indices in ARBITRATION).

    Indices are interpreted against `dev_triage.rejected_indices`:
    positive = reinstate those; `all` (exclude=True, indices=[]) =
    reinstate every rejected issue; `-N` = reinstate all rejected except N.
    Indices already in the accepted set are tolerated no-ops; indices
    matching no issue post an error and drop the event (anti-poison-loop,
    same rule as _handle_dev_triage). A reply that reinstates nothing also
    posts an error + drops — the approver should reply `ok` to accept.
    """
    review = topic.setdefault("review", {})
    event_id = event.get("event_id") or event.get("message_id")
    dev_triage = review.get("dev_triage") or {}
    accepted = list(dev_triage.get("accepted_indices") or [])
    rejected = list(dev_triage.get("rejected_indices") or [])
    indices = list(parsed.get("indices") or [])
    exclude = bool(parsed.get("exclude"))

    known = set(accepted) | set(rejected)
    unknown = [n for n in indices if n not in known]
    if unknown:
        label = " ".join(f"#{n}" for n in unknown)
        valid = " ".join(f"#{n}" for n in sorted(rejected)) or "（无）"
        ok, resp = _post_plain_text(
            f"{ticket_id} 无效的问题序号：{label}（可恢复的异议问题为 {valid}），"
            f"请重新回复。", thread_id)
        if not ok:
            return False, f"invalid-indices error post failed: {resp}"
        topic_store.append_audit(topic,
            event="arbitration_invalid_indices",
            triggered_by_event_id=event_id,
            indices=indices, exclude=exclude,
            rejected_indices=rejected,
        )
        return True, "invalid_indices_dropped"

    if exclude:
        excl = set(indices)
        reinstated = [n for n in rejected if n not in excl]
        refixed = []
    else:
        rejected_set = set(rejected)
        reinstated = [n for n in indices if n in rejected_set]
        # Indices naming an issue the dev already accepted are a plain
        # re-flag ("this one still isn't fixed"), not a reinstatement.
        # Without this the approver could never send back an accepted-but-
        # unfixed issue once the dev had disputed anything, because the
        # whole reply routed here and intersected with `rejected` only —
        # the reply drained to "未恢复任何异议问题…请回复 ok", which points
        # at approval (review issue #1).
        refixed = [n for n in indices if n in set(accepted)]
    if not reinstated and not refixed:
        ok, resp = _post_plain_text(
            f"{ticket_id} 未恢复任何异议问题——如同意开发者的处理意见，"
            f"请回复 ok。", thread_id)
        if not ok:
            return False, f"empty-reinstate error post failed: {resp}"
        topic_store.append_audit(topic,
            event="arbitration_empty_reinstate",
            triggered_by_event_id=event_id,
            indices=indices, exclude=exclude,
        )
        return True, "empty_reinstate_dropped"

    reinstated_set = sorted(set(reinstated))
    # A reinstated issue stops being a dispute the moment the approver
    # overrules it: fold it into `accepted` and drop it from `rejected`, or
    # the next hand-off keeps listing it under 开发者有异议 with the dev's
    # stale reason attached (review issue #3).
    prior_reinstated = set(dev_triage.get("reinstated_indices") or [])
    dev_triage["reinstated_indices"] = sorted(prior_reinstated
                                              | set(reinstated_set))
    dev_triage["rejected_indices"] = [n for n in rejected
                                      if n not in set(reinstated_set)]
    dev_triage["accepted_indices"] = sorted(set(accepted)
                                            | set(reinstated_set))
    review["dev_triage"] = dev_triage
    final_set = set(dev_triage["accepted_indices"])
    issues = review.get("issues") or []
    flagged = [i for i in issues if i.get("index") in final_set]
    # Print only what is still outstanding, same rule as the triage tail.
    settled = {e.get("index") for e in (review.get("flagged_issues") or [])
               if review_rounds.is_settled(e)}
    display = [i for i in flagged
               if i.get("index") not in settled
               or i.get("index") in set(reinstated_set) | set(refixed)]

    lines = []
    if reinstated_set:
        lines.append("审查人恢复了以下异议问题：%s"
                     % " ".join(f"#{n}" for n in reinstated_set))
    if refixed:
        lines.append("审查人要求继续修复：%s"
                     % " ".join(f"#{n}" for n in sorted(set(refixed))))
    prefix = [[{"tag": "text", "text": "；".join(lines) + "，最终修复列表如下："}]]
    return _post_fix_list_and_transition(
        topic, event, flagged, thread_id, ticket_id,
        prefix_paragraphs=prefix,
        audit_event="arbitration_reinstated",
        audit_extra={"reinstated_indices": reinstated_set,
                     "refixed_indices": sorted(set(refixed))},
        display=display)


def _handoff_status_paragraphs(review):
    """The fix ledger for the hand-off post, built defensively.

    `render.build_issue_status_paragraphs` validates hard and raises on a
    missing field — right for an agent-authored artifact, wrong here: a
    malformed ledger entry must not be able to strand the hand-off with the
    event stuck in pending. Entries that can't be rendered in the rich shape
    fall back to the plain indexed list.
    """
    render = _render_mod()
    flagged = review.get("flagged_issues") or []
    if not flagged:
        return []
    entries, degraded = [], []
    for item in flagged:
        if not isinstance(item, dict):
            continue
        summary = (item.get("summary") or item.get("description") or "").strip()
        if len(summary) > _SUMMARY_CAP:
            # `description` is the full issue body — often hundreds of chars
            # on a complex review. Untrimmed it turns the hand-off post into
            # a wall of text and reads nothing like the one-line summaries in
            # review_roundN (review issue #7).
            summary = summary[:_SUMMARY_CAP].rstrip() + "…"
        entry = {
            "index": item.get("index"),
            "severity": item.get("severity"),
            "repo": item.get("repo"),
            "file": item.get("file"),
            "verdict": item.get("verification") or "pending",
            "summary": summary,
            "rationale": item.get("verification_rationale") or "",
        }
        # Degrade per entry, not all-or-nothing: the verdict markers are the
        # one thing the approver needs, and a single malformed entry used to
        # strip them from every other issue too (review issue #7).
        if any(entry[k] in ("", None)
               for k in ("index", "severity", "repo", "file")):
            degraded.append(item)
            continue
        entries.append(entry)

    out = []
    if entries:
        try:
            out.extend(render.build_issue_status_paragraphs(entries))
        except (ValueError, TypeError):
            degraded = [i for i in flagged if isinstance(i, dict)]
    if degraded:
        out.extend(render.build_indexed_issue_paragraphs(degraded))
    return out


def _handoff_rejection_paragraphs(review):
    """Dev-rejected issues plus the reason the dev gave, per round."""
    render = _render_mod()
    dev_triage = review.get("dev_triage") or {}
    rejected = set(dev_triage.get("rejected_indices") or [])
    if not rejected:
        return []
    issues = review.get("issues") or []
    rejected_issues = [i for i in issues if i.get("index") in rejected]
    out = render.build_triage_section_paragraphs(
        "开发者有异议", rejected_issues)
    for entry in (dev_triage.get("reasons") or []):
        text = (entry.get("text") or "").strip()
        if not text:
            continue
        label = " ".join(f"#{n}" for n in (entry.get("rejected_indices") or []))
        rnd = entry.get("round")
        prefix = f"开发者说明（第 {rnd} 轮 {label}）：" if label else "开发者说明："
        out.append([{"tag": "text", "text": prefix + text}])
    if out:
        out.append([{"tag": "text", "text": "\n"}])
    return out


def _handle_dev_handoff(topic, event, thread_id, ticket_id,
                        approver_open_ids=None):
    """Developer hands the topic to the approver (DESIGN §1.23.7).

    Ends the self-service loop: posts the accumulated picture — the fix
    ledger and every issue the dev pushed back on, with their stated
    reasons — @-mentioning the approver for the first time in the topic,
    and moves to AWAITING_APPROVAL where the approver's verbs apply.
    """
    review = topic.setdefault("review", {})
    event_id = event.get("event_id") or event.get("message_id")

    if not _already_posted(topic, event_id):
        approver_id = _primary_approver_id(approver_open_ids)
        if not approver_id:
            return False, "no approver id resolvable for @-mention"
        identity = topic.get("identity") or {}
        variables = {
            "TICKET_ID": ticket_id,
            "APPROVER_ID": approver_id,
            "DEVELOPER_NAME": identity.get("developer") or "开发者",
            "ROUND": review.get("review_round") or 0,
            "ISSUE_STATUS_PARAGRAPHS": _handoff_status_paragraphs(review),
            "REJECTED_SECTION_PARAGRAPHS": _handoff_rejection_paragraphs(review),
        }
        if review.get("triage") == "simple":
            variables["ESCALATE_INSTRUCTION_PARAGRAPHS"] = [
                [{"tag": "text",
                  "text": "· 回复 \"full\" 或 \"完整版\" 进行完整审查"}]
            ]
        ok, resp = _render_and_post("handoff_summary", variables, thread_id)
        if not ok:
            return False, f"handoff_summary post failed: {resp}"
        topic_store.append_audit(topic,
            event="lark_reply_sent",
            reply_type="handoff_summary",
            triggered_by_event_id=event_id,
        )

    from_state = review.get("state")
    review["state"] = "AWAITING_APPROVAL"
    review.pop("pending_action", None)
    topic.setdefault("lifecycle", {})["updated_at"] = topic_store.now_ms()
    topic_store.append_audit(topic,
        event="state_transition",
        from_state=from_state,
        to_state="AWAITING_APPROVAL",
        triggered_by_event_id=event_id,
    )
    dev_triage = review.get("dev_triage") or {}
    topic_store.append_audit(topic,
        event="dev_handoff",
        from_state=from_state,
        accepted_indices=list(dev_triage.get("accepted_indices") or []),
        rejected_indices=list(dev_triage.get("rejected_indices") or []),
        triggered_by_event_id=event_id,
    )
    return True, "dev_handoff"


def _handle_close(topic, event, thread_id, ticket_id, role="approver"):
    """Close every MR, post plain-text notice, set state CLOSED.

    `role` is "approver" (sender ∈ approver namelist) or "developer" (sender
    matches identity.creator_open_id) — used to tag the audit + closed_reason
    so operators can see who triggered the close. Either path is authorised
    and behaves identically; this just preserves the trail.
    """
    mrs = topic.get("mrs") or {}
    for repo, mr_obj in mrs.items():
        ok, err = _glab_close(repo, mr_obj)
        if not ok:
            # Already-closed MRs return 405; tolerate that.
            if "405" not in err and "already" not in err.lower():
                return False, f"close failed for {repo}: {err}"

    if not _already_posted(topic, event.get("event_id") or event.get("message_id")):
        ok, resp = _post_plain_text(f"{ticket_id} MR 已关闭。", thread_id)
        if not ok:
            return False, f"close post failed: {resp}"
        topic_store.append_audit(topic,
            event="lark_reply_sent",
            reply_type="close",
            triggered_by_event_id=event.get("event_id") or event.get("message_id"),
        )

    review = topic.setdefault("review", {})
    from_state = review.get("state")
    review["state"] = "CLOSED"
    review.pop("pending_action", None)
    ls = topic.setdefault("lifecycle", {})
    ls["resolved_at"] = topic_store.now_ms()
    closed_reason = "developer_close" if role == "developer" else "approver_close"
    ls["closed_reason"] = closed_reason
    topic_store.append_audit(topic,
        event=closed_reason,
        from_state=from_state,
        to_state="CLOSED",
        triggered_by_event_id=event.get("event_id") or event.get("message_id"),
    )
    return True, "closed"


# ── Event classifier ──────────────────────────────────────────────────

def _close_cherrypick_window(topic):
    """End the window so the dispatcher archives the topic next cycle."""
    review = topic.setdefault("review", {})
    review["cherrypick_window_until"] = 0
    review.pop("cherrypick_mapping", None)
    review.pop("cherrypick_active", None)


def _handle_cherrypick(topic, event, parsed, thread_id, ticket_id):
    """Cherry-pick the merged commit onto the approver's chosen branches.

    Resolves tokens against the mapping captured when the offer was posted —
    not a fresh discovery — so `p2` means what the prompt said it meant even
    if an rc branch was cut in between (DESIGN §1.24).
    """
    import cherrypick

    review = topic.get("review") or {}
    mapping = review.get("cherrypick_mapping") or {}
    active_by_repo = review.get("cherrypick_active") or {}
    shas = (topic.get("lifecycle") or {}).get("merge_shas") or {}
    root_msg_id = topic.get("root_message_id", "")

    resolved, errors = cherrypick.resolve_tokens(
        parsed.get("branches") or [], mapping, active_by_repo)

    if not resolved:
        # Nothing actionable: tell the approver why and leave the window
        # open so they can correct the token.
        _post_plain_text(
            "⚠️ 未执行 cherry-pick：" + "；".join(errors or ["无有效分支代号"]),
            root_msg_id)
        topic_store.append_audit(topic, **{
            "ts": int(time.time() * 1000), "event": "cherrypick_rejected",
            "tokens": parsed.get("branches") or [], "errors": errors,
        })
        # Drained (the approver was told why); the window stays open so a
        # corrected token still works. Returning False would re-queue the
        # same bad token forever.
        return True, "cherrypick_rejected"

    results = []
    for _label, targets in resolved:
        # targets is {repo: branch} — the branch name differs per repo
        # (`rc_p1` in rage, `rage/rc_p1` in chaos), so never reuse one name
        # across repos (DESIGN §1.24).
        for repo, branch in sorted(targets.items()):
            sha = shas.get(repo)
            if not sha:
                results.append({"repo": repo, "branch": branch,
                                "mode": "failed", "url": "",
                                "error": "no merged sha recorded"})
                continue
            mr_obj = (topic.get("mrs") or {}).get(repo) or {}
            repo_slug = _repo_slug(repo, mr_obj)
            outcome = cherrypick.cherry_pick_to_branch(
                repo_slug, sha, branch, ticket_id)
            outcome["repo"] = repo
            results.append(outcome)

    lines = []
    for item in results:
        label = _label_for(item["repo"])
        if item["mode"] == "direct":
            lines.append("✅ %s → %s 已直接 cherry-pick" % (label, item["branch"]))
        elif item["mode"] == "mr":
            lines.append("🔀 %s → %s 已创建 MR：%s"
                         % (label, item["branch"], item.get("url") or ""))
        else:
            lines.append("❌ %s → %s 失败：%s"
                         % (label, item["branch"],
                            (item.get("error") or "")[:160]))
    for err in errors:
        lines.append("⚠️ " + err)

    _post_plain_text("\n".join(lines), root_msg_id)
    topic_store.append_audit(topic, **{
        "ts": int(time.time() * 1000), "event": "cherrypick_completed",
        "results": results, "errors": errors,
    })
    # One answer ends the offer; a follow-up needs a fresh manual cherry-pick.
    _close_cherrypick_window(topic)
    return True, "cherrypick_completed"


def _handle_cherrypick_skip(topic, thread_id, ticket_id):  # noqa: ARG001
    """Approver declined the offer — close the window, let it archive."""
    topic_store.append_audit(topic, **{
        "ts": int(time.time() * 1000), "event": "cherrypick_declined",
    })
    _close_cherrypick_window(topic)
    return True, "cherrypick_declined"


def _classify(topic, event, approver_open_ids):
    """Return (intent, parsed) or (None, None) if non-mechanical.

    Trusts router-stamped event.intent when present (post-namelist refactor).
    Falls back to re-parsing content for legacy events queued before the
    router started stamping. 3rd-party phase no longer defers to Claude —
    the in-process handlers know how to do approve/revision/close in either
    phase. Authorisation: dev `close` is allowed when `event.role` is
    `developer`; everything else requires `event.role == "approver"` (or,
    for legacy events, sender ∈ approver_open_ids).
    """
    review = topic.get("review") or {}
    state = review.get("state")
    if state not in _MECHANICAL_STATES:
        return None, None

    # Prefer router-stamped intent. New-style events always have this.
    intent = event.get("intent")
    role = event.get("role")
    indices = list(event.get("indices") or [])
    exclude = bool(event.get("exclude"))
    none = bool(event.get("none"))
    if intent is None:
        # Legacy event: re-parse content. Dev close not supported on the
        # legacy path (those events never carried a `role`); operator can
        # reply `close` themselves if needed.
        sender = event.get("sender_id") or ""
        if approver_open_ids and sender not in approver_open_ids:
            return None, None
        parsed = reply_parser.parse_approver_reply(
            event.get("content") or "", state)
        intent = parsed.get("intent")
        indices = parsed.get("indices") or []
        exclude = bool(parsed.get("exclude"))
        role = "approver"

    if intent not in _MECHANICAL_INTENTS:
        return None, None

    # MERGED admits ONLY the cherry-pick verbs, and only while the window is
    # open. An `approve`/`close` arriving on a merged topic would try to
    # approve or close already-merged MRs (DESIGN §1.24).
    if state == "MERGED":
        if intent not in _MERGED_STATE_INTENTS:
            return None, None
        window = review.get("cherrypick_window_until") or 0
        if window <= int(time.time() * 1000):
            return None, None
    elif intent in _MERGED_STATE_INTENTS:
        return None, None

    # State×intent validity for the inverted-triage states (DESIGN §1.23):
    # dev_triage only fires in DEV_TRIAGE; approver indices in DEV_TRIAGE
    # must wait for arbitration (the router classifies them ignored — this
    # guards legacy/foreign events). ARBITRATION escalate already defers to
    # Claude (not in _MECHANICAL_INTENTS).
    if intent == "dev_triage" and state != "DEV_TRIAGE":
        return None, None
    if state == "DEV_TRIAGE" and intent == "revision":
        return None, None
    # `done` only means something while the topic is still in the dev's
    # court (DESIGN §1.23.7).
    if intent == "dev_handoff" and state not in _HANDOFF_STATES:
        return None, None

    # Authorisation: approver intents (approve/revision) need an approver.
    # Close accepts both approver and developer (dev_close authorised by
    # the router via classify_intent's developer_id check); dev_triage is
    # the developer's verb by construction (router gates it to the topic
    # dev in DEV_TRIAGE).
    if role == "developer" and intent not in ("close", "dev_triage",
                                              "dev_handoff"):
        return None, None
    if role == "ignored":
        return None, None
    if intent == "dev_triage" and role != "developer":
        return None, None
    # Hand-off is the developer's verb: a bystander must not be able to
    # submit someone else's branch for final review.
    if intent == "dev_handoff" and role != "developer":
        return None, None

    # Revision states: only approve/close — no revision reshuffle.
    if state in _REVISION_STATES and intent not in _REVISION_STATE_INTENTS:
        return None, None

    # Hint short-circuit (optional): if review.pending_action names an
    # allowed mechanical intent set, require membership (defensive).
    hint = review.get("pending_action") or {}
    hint_mech = set(hint.get("mechanical_intents") or []) if hint else set()
    if hint_mech and intent not in hint_mech:
        return None, None

    return intent, {"intent": intent, "indices": indices, "role": role,
                    "exclude": exclude, "none": none,
                    "reason": event.get("reason") or "",
                    "branches": list(event.get("branches") or [])}


# ── Public entry point ────────────────────────────────────────────────

def _tag_mismatch(topic, event):
    """Return audit-event name if event's arrival tags no longer match topic.

    router.py stamps every inbox event with phase_at_arrival
    and state_at_arrival. If the topic has since moved (e.g. phase reset
    3p -> main, or dev pushed new revision between inbox arrival and drain)
    the event is stale and must not drive side effects.
    Returns None when tags match or are absent (legacy events).
    """
    review = topic.get("review") or {}
    phase_tag = event.get("phase_at_arrival")
    if phase_tag is not None and review.get("review_phase") != phase_tag:
        return "event_phase_mismatch"
    # state_at_arrival check removed: the inbox/drain-while-locked mechanism
    # (DESIGN.md §1.2.1) is the correct guard against stale events. The strict
    # equality check broke when events accumulated during a Monitor gap —
    # a new_topic and its approval got batched together, the agent advanced
    # state processing the first event, making the second event's tag stale.
    return None


def drain_mechanical(topics_dir, index_path, approver_open_ids, cycle_id,
                     withdrawn_ids=None):
    """Scan open topics and execute mechanical approver replies in-process.

    Params:
        withdrawn_ids: optional set/iterable of message_ids that reconcile
            saw withdrawn this cycle. Events whose message_id is in the
            set are dropped from pending with a `withdrawn_race_skipped`
            audit entry before any glab/lark side effect.

    For each topic:
      1. Skip if no pending events.
      2. Acquire the topic lock. If busy (Claude owns it), skip this cycle.
      3. Walk events.pending in order. For each mechanical event:
           - If the event's phase_at_arrival / state_at_arrival tag no
             longer matches the current topic, drop it with an audit
             (`event_phase_mismatch` / `event_state_mismatch`).
           - If the event's message_id is in withdrawn_ids, drop it with
             a `withdrawn_race_skipped` audit.
           - Otherwise execute side-effects (glab + lark post).
           - On success: remove from pending, write topic atomically.
           - On failure: leave in pending, log, break (order matters).
         Non-mechanical events stay in pending for Claude.
      4. If final state is terminal (CLOSED), archive to closed/.
      5. Release the lock.

    Returns a summary dict (handled counts by intent, errors, skipped).
    """
    topics_dir = Path(topics_dir)
    summary = {"approve": 0, "revision": 0, "close": 0, "dev_triage": 0,
               "dev_handoff": 0, "cherrypick": 0, "cherrypick_skip": 0,
               "errors": 0, "skipped_locked": 0, "topics_touched": 0,
               "tag_skipped": 0, "withdrawn_skipped": 0}
    if not topics_dir.exists():
        return summary

    withdrawn = set(withdrawn_ids or ())
    holder = f"mech-{cycle_id}"
    for topic_path in topic_store.iter_topic_files(topics_dir):
        try:
            topic = topic_store.read(topic_path)
        except (OSError, ValueError):
            continue
        pending = (topic.get("events") or {}).get("pending") or []
        if not pending:
            continue

        # Quick scan: any event look mechanical? Skip lock if not.
        any_candidate = any(
            _classify(topic, ev, approver_open_ids)[0] for ev in pending)
        if not any_candidate:
            continue

        if not topic_store.acquire_lock(topic_path, holder, cycle_id):
            summary["skipped_locked"] += 1
            continue
        summary["topics_touched"] += 1

        try:
            changed = True
            while changed:
                changed = False
                # Re-read each iteration: _handle_* mutates topic in place
                # and we write after each event.
                pending = (topic.get("events") or {}).get("pending") or []
                for idx, event in enumerate(pending):
                    intent, parsed = _classify(topic, event, approver_open_ids)
                    if intent is None:
                        continue
                    event_id = event.get("event_id") or event.get("message_id")
                    thread_id = topic.get("thread_id") or topic.get("root_message_id")

                    # #5 phase/state tag drift check.
                    mismatch = _tag_mismatch(topic, event)
                    if mismatch:
                        topic_store.append_audit(topic,
                            event=mismatch,
                            intent=intent,
                            triggered_by_event_id=event_id,
                            phase_at_arrival=event.get("phase_at_arrival"),
                            state_at_arrival=event.get("state_at_arrival"),
                            current_phase=(topic.get("review") or {}).get("review_phase"),
                            current_state=(topic.get("review") or {}).get("state"),
                        )
                        topic["events"]["pending"].pop(idx)
                        topic_store.write_atomic(topic_path, topic)
                        summary["tag_skipped"] += 1
                        _log_activity(f"mechanical_dispatch_skip thread={thread_id} "
                                      f"reason={mismatch} intent={intent}")
                        changed = True
                        break

                    # #3 withdrawal race check.
                    msg_id = event.get("message_id")
                    if msg_id and msg_id in withdrawn:
                        topic_store.append_audit(topic,
                            event="withdrawn_race_skipped",
                            intent=intent,
                            triggered_by_event_id=event_id,
                            message_id=msg_id,
                        )
                        topic["events"]["pending"].pop(idx)
                        topic_store.write_atomic(topic_path, topic)
                        summary["withdrawn_skipped"] += 1
                        _log_activity(f"mechanical_dispatch_skip thread={thread_id} "
                                      f"reason=withdrawn_race intent={intent}")
                        changed = True
                        break

                    ticket_id = (topic.get("identity") or {}).get("ticket_id", "")
                    state_at = (topic.get("review") or {}).get("state")
                    event["_state_at_dispatch"] = state_at

                    if intent == "approve":
                        if state_at == "ARBITRATION":
                            ok, reason = _handle_arbitration_accept(
                                topic, topic_path, event, thread_id, ticket_id)
                        else:
                            ok, reason = _handle_approve(
                                topic, topic_path, event, thread_id, ticket_id)
                    elif intent == "dev_triage":
                        ok, reason = _handle_dev_triage(
                            topic, event, parsed, thread_id, ticket_id,
                            approver_open_ids=approver_open_ids)
                    elif intent == "dev_handoff":
                        ok, reason = _handle_dev_handoff(
                            topic, event, thread_id, ticket_id,
                            approver_open_ids=approver_open_ids)
                    elif intent == "revision":
                        # Post-handoff, approver indices mean "reinstate the
                        # issues the dev rejected" — same semantics as the
                        # legacy arbitration reply, so the same handler
                        # (DESIGN §1.23.9). With nothing rejected there is
                        # nothing to reinstate and it is a plain revision.
                        _dt = (topic.get("review") or {}).get("dev_triage") or {}
                        if state_at == "ARBITRATION" or (
                                state_at == "AWAITING_APPROVAL"
                                and (_dt.get("rejected_indices") or [])):
                            ok, reason = _handle_arbitration_reinstate(
                                topic, event, parsed, thread_id, ticket_id)
                        else:
                            ok, reason = _handle_revision(
                                topic, event, parsed["indices"],
                                state_at, thread_id, ticket_id,
                                exclude=parsed.get("exclude", False))
                    elif intent == "close":
                        ok, reason = _handle_close(
                            topic, event, thread_id, ticket_id,
                            role=parsed.get("role", "approver"))
                    elif intent == "cherrypick":
                        ok, reason = _handle_cherrypick(
                            topic, event, parsed, thread_id, ticket_id)
                    elif intent == "cherrypick_skip":
                        ok, reason = _handle_cherrypick_skip(
                            topic, thread_id, ticket_id)
                    else:  # defensive
                        ok, reason = False, f"unknown intent {intent}"

                    event.pop("_state_at_dispatch", None)

                    if not ok:
                        summary["errors"] += 1
                        _log_activity(f"mechanical_dispatch_error thread={thread_id} "
                                      f"intent={intent} reason={reason}")
                        # Leave event in pending; order matters, so stop.
                        changed = False
                        break

                    # Success: drop this event, persist, restart scan.
                    topic["events"]["pending"].pop(idx)
                    topic["events"]["last_processed_event_id"] = event_id
                    # record the processed id in the ring so
                    # a re-sent raw event of the same id gets deduped.
                    topic_store.push_recent_event(topic, event_id)
                    topic_store.write_atomic(topic_path, topic)
                    summary[intent] += 1
                    _log_activity(f"mechanical_dispatch thread={thread_id} "
                                  f"intent={intent} indices={parsed.get('indices') or []}")
                    # Terminal check: CLOSED -> archive.
                    if (topic.get("review") or {}).get("state") == "CLOSED":
                        topic_store.archive_topic(
                            topics_dir, index_path,
                            topic.get("thread_id") or topic_path.stem)
                        changed = False
                        break
                    # Re-read topic in case archive moved file — but here
                    # we kept going in the open dir; just loop.
                    changed = True
                    break
        finally:
            topic_store.release_lock(topic_path)

    return summary


# ── Withdrawn-reply drain ─────────────────────────────────────────────

def _is_message_alive(message_id):
    """Query Lark: True if retrievable, False if withdrawn, None on transient error.

    `lark-cli im +messages-mget` returns the message with `deleted: true`
    when it has been withdrawn, `deleted: false` when live (it can also
    surface code 230011 if the message is fully gone). Any other non-zero
    exit or unparseable body (network blip, auth refresh) returns None so
    the caller leaves the event in pending and the next cycle retries — we
    never drop on ambiguity.

    NOTE: the previous implementation called `im messages get`, which is
    not a real lark-cli subcommand, so it always hit the non-zero branch
    and returned None — the withdrawn-reply drain never fired. The real
    getter is the `+`-prefixed `+messages-mget` (which exposes `deleted`).
    """
    cmd = subprocess_util.lark_cli_argv_prefix() + [
           "im", "+messages-mget",
           "--as", "bot", "--message-ids", message_id, "--format", "json"]
    try:
        proc = subprocess_util.hidden_run(
            cmd, capture_output=True, text=True,
            timeout=15, encoding="utf-8", errors="replace")
    except (OSError, Exception):  # noqa: BLE001
        return None
    combined = (proc.stdout or "") + (proc.stderr or "")
    if "230011" in combined:
        return False
    if proc.returncode != 0:
        return None
    try:
        body = json.loads(proc.stdout or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    msgs = (body.get("data") or {}).get("messages") or []
    if not msgs:
        return None  # not returned at all — ambiguous, retry next cycle
    return not bool(msgs[0].get("deleted"))


# ── No-new-commits short-circuit ─────────────────────────────────────

def _repo_root_for(repo, rage_root, chaos_root):
    """Map repo key to local git root. Returns None for repos we don't
    own locally (e.g. 3rd_party/*, which has its own revision flow)."""
    if repo == "rage":
        return rage_root
    if repo == "chaos":
        return chaos_root
    return None


def _ls_remote_sha(repo_root, branch):
    """Resolve a branch's current remote SHA via `git ls-remote`.

    Returns the SHA string, or None if the command failed or the branch
    was not found. A None return defers the topic to the agent — we
    prefer an unnecessary spawn over a false-positive "no new commits".
    """
    if not repo_root or not branch:
        return None
    try:
        proc = subprocess_util.hidden_run(
            ["git", "-C", str(repo_root), "ls-remote", "origin", branch],
            capture_output=True, text=True,
            timeout=20, encoding="utf-8", errors="replace",
        )
    except (OSError, Exception):  # noqa: BLE001
        return None
    if proc.returncode != 0:
        return None
    lines = (proc.stdout or "").strip().splitlines()
    if not lines:
        return None
    first = lines[0].split()
    return first[0].strip() if first else None


def _expected_sha_for_repo(repo, last_review_commit):
    """Return the stored last_review_commit SHA for `repo`.

    Topics created before the per-repo schema roll-out store a single
    string SHA; newer topics store a dict keyed by repo. Both shapes
    appear in production, so normalize here rather than migrating the
    on-disk data.
    """
    if not last_review_commit:
        return None
    if isinstance(last_review_commit, str):
        return last_review_commit
    if isinstance(last_review_commit, dict):
        return last_review_commit.get(repo)
    return None


def _is_candidate_no_op_reply(event, approver_open_ids):
    """True if `event` looks like a dev reply (intent=dev_reply).

    Router stamps `intent` on every inbox event after classify_intent.
    Approver replies (approver.*) and dev_question events are owned by
    other drains; only dev_reply lands here. Legacy events without an
    `intent` field fall back to the historical heuristic."""
    sender = event.get("sender_id") or ""
    if not sender:
        return False
    intent = event.get("intent")
    if intent is not None:
        return intent == "dev_reply"
    # Legacy fallback (events queued before router started stamping intent).
    if approver_open_ids and sender in approver_open_ids:
        return False
    if event.get("state_at_arrival") == "TRIAGING":
        return False
    if event_utils.has_bot_mention(event):
        return False
    return True


def _short_sha_label(last_review_commit):
    """Human-friendly short SHA string for the `no_new_commits` template."""
    if isinstance(last_review_commit, str) and last_review_commit:
        return last_review_commit[:7]
    if isinstance(last_review_commit, dict):
        parts = [f"{repo}={sha[:7]}"
                 for repo, sha in last_review_commit.items() if sha]
        return ", ".join(parts) or "unknown"
    return "unknown"


def drain_no_new_commits(topics_dir, index_path, cycle_id,
                         rage_root, chaos_root, approver_open_ids=None):
    """Short-circuit dev_reply events where no new commits have landed.

    When a developer pings a *_REVISION thread without having pushed any
    code since `review.last_review_commit`, spawning the topic agent to
    discover this costs ~40-60K input tokens. This drain refreshes the
    branch SHAs via `git ls-remote`, compares to last_review_commit, and
    if every touched repo still matches, posts the `no_new_commits`
    template + drops the event — same outcome, zero Opus spend.

    Returns {checked, posted, topics_touched, skipped_locked,
             ls_remote_failed, errors}.
    """
    del index_path  # unused; kept for parity with sibling drains
    topics_dir = Path(topics_dir)
    summary = {"checked": 0, "posted": 0, "topics_touched": 0,
               "skipped_locked": 0, "ls_remote_failed": 0, "errors": 0}
    if not topics_dir.exists():
        return summary

    holder = f"no-new-commits-{cycle_id}"
    for topic_path in topic_store.iter_topic_files(topics_dir):
        try:
            topic = topic_store.read(topic_path)
        except (OSError, ValueError):
            continue
        review = topic.get("review") or {}
        if review.get("state") not in _REVISION_STATES:
            continue
        if review.get("review_phase") == "3rd_party":
            continue
        if not review.get("last_review_commit"):
            continue
        pending = (topic.get("events") or {}).get("pending") or []
        if not any(_is_candidate_no_op_reply(event, approver_open_ids)
                   for event in pending):
            continue

        if not topic_store.acquire_lock(topic_path, holder, cycle_id):
            summary["skipped_locked"] += 1
            continue
        summary["topics_touched"] += 1
        try:
            # Re-read under lock: earlier drains in this cycle may have
            # mutated pending between our unlocked scan and the acquire.
            topic = topic_store.read(topic_path)
            review = topic.get("review") or {}
            mrs = topic.get("mrs") or {}
            lrc = review.get("last_review_commit")
            pending = (topic.get("events") or {}).get("pending") or []

            # Refresh branch SHAs. Any failure defers to the agent — we
            # never want to claim "no new commits" on partial info.
            all_match = True
            ls_remote_failed = False
            for repo, mr_obj in mrs.items():
                if repo.startswith("3rd_party/"):
                    continue
                branch = (mr_obj or {}).get("branch")
                repo_root = _repo_root_for(repo, rage_root, chaos_root)
                current_sha = _ls_remote_sha(repo_root, branch)
                if not current_sha:
                    ls_remote_failed = True
                    break
                expected_sha = _expected_sha_for_repo(repo, lrc)
                if not expected_sha or current_sha != expected_sha:
                    all_match = False
                    break

            if ls_remote_failed:
                summary["ls_remote_failed"] += 1
                continue
            if not all_match:
                continue

            identity = topic.get("identity") or {}
            ticket_id = identity.get("ticket_id", "")
            thread_id = topic.get("thread_id") or topic.get("root_message_id")
            sha_label = _short_sha_label(lrc)
            # Trailing @-dev mention (empty → template renders without it).
            dev_id = identity.get("creator_open_id") or ""
            mention_segs = (
                [{"tag": "at", "user_id": dev_id,
                  "user_name": identity.get("developer") or "开发者"}]
                if dev_id else [])
            kept = []
            dirty = False
            for event in pending:
                if not _is_candidate_no_op_reply(event, approver_open_ids):
                    kept.append(event)
                    continue
                event_id = event.get("event_id") or event.get("message_id")
                summary["checked"] += 1
                if _already_posted(topic, event_id):
                    # Audit already records the post; drop the replay.
                    dirty = True
                    continue
                ok, resp = _render_and_post(
                    "no_new_commits",
                    {"TICKET_ID": ticket_id, "LAST_SHA_SHORT": sha_label,
                     "DEVELOPER_MENTION_SEGMENTS": mention_segs},
                    thread_id)
                if not ok:
                    summary["errors"] += 1
                    _log_activity(
                        f"no_new_commits_post_error thread={thread_id} "
                        f"event={event_id} reason={resp}")
                    kept.append(event)
                    continue
                topic_store.append_audit(topic,
                    event="lark_reply_sent",
                    triggered_by_event_id=event_id,
                    template="no_new_commits",
                )
                topic_store.append_audit(topic,
                    event="no_new_commits_drained",
                    triggered_by_event_id=event_id,
                    last_review_commit=sha_label,
                )
                topic_store.push_recent_event(topic, event_id)
                summary["posted"] += 1
                dirty = True
                _log_activity(
                    f"no_new_commits_drain thread={thread_id} "
                    f"event={event_id} sha={sha_label}")

            if dirty:
                topic["events"]["pending"] = kept
                topic.setdefault("lifecycle", {})["updated_at"] = topic_store.now_ms()
                topic_store.write_atomic(topic_path, topic)
        finally:
            topic_store.release_lock(topic_path)

    return summary


def drain_rebase_conflict_ack(topics_dir, index_path, cycle_id,
                              rage_root, chaos_root, approver_open_ids=None):
    """Consume a developer `ok` on a rebase-conflict-parked topic.

    When an auto-rebase hits a content conflict, process_merge_queue parks the
    topic (`review.rebase_conflict_blocked = True`) instead of re-attempting
    every cycle, and tells the developer to rebase locally, push, and reply
    `ok`. This drain handles that `ok` mechanically (no Claude spawn):

      - If the branch SHA advanced past `review.rebase_conflict_shas[repo]`
        (they actually pushed), clear the park flag + post `merge_resuming`
        ("流水线运行中，完成后将自动合并。"). The topic re-enters the normal
        APPROVED flow next cycle, which waits for the push-triggered pipeline
        to pass and then auto-merges.
      - If no conflicted repo advanced (they typed `ok` without pushing), post
        `rebase_no_push` and stay parked.
      - ls-remote failure defers the topic to the next cycle (never claim "no
        push" on partial info — same safe default as drain_no_new_commits).

    The `ok` event is dropped from events.pending[] either way, so the parent
    Claude never sees it (it would otherwise be a §1.18.2 undefined-row drop).
    See DESIGN §1.6.7.

    Returns {checked, resumed, no_push, topics_touched, skipped_locked,
             ls_remote_failed, errors}.
    """
    del index_path  # unused; kept for parity with sibling drains
    topics_dir = Path(topics_dir)
    summary = {"checked": 0, "resumed": 0, "no_push": 0, "topics_touched": 0,
               "skipped_locked": 0, "ls_remote_failed": 0, "errors": 0}
    if not topics_dir.exists():
        return summary

    holder = f"rebase-conflict-ack-{cycle_id}"
    for topic_path in topic_store.iter_topic_files(topics_dir):
        try:
            topic = topic_store.read(topic_path)
        except (OSError, ValueError):
            continue
        review = topic.get("review") or {}
        if not review.get("rebase_conflict_blocked"):
            continue
        pending = (topic.get("events") or {}).get("pending") or []
        if not any(_is_candidate_no_op_reply(event, approver_open_ids)
                   for event in pending):
            continue

        if not topic_store.acquire_lock(topic_path, holder, cycle_id):
            summary["skipped_locked"] += 1
            continue
        summary["topics_touched"] += 1
        try:
            # Re-read under lock: earlier drains in this cycle may have mutated
            # pending between the unlocked scan and the acquire.
            topic = topic_store.read(topic_path)
            review = topic.get("review") or {}
            if not review.get("rebase_conflict_blocked"):
                continue
            mrs = topic.get("mrs") or {}
            conflict_shas = review.get("rebase_conflict_shas") or {}
            pending = (topic.get("events") or {}).get("pending") or []

            # Did the developer actually push? Compare the live branch head to
            # the conflict-time SHA for each parked repo. Any repo that moved
            # means a resolution landed. ls-remote failure defers the topic.
            advanced = False
            ls_remote_failed = False
            for repo, sha_at_conflict in conflict_shas.items():
                branch = (mrs.get(repo) or {}).get("branch")
                repo_root = _repo_root_for(repo, rage_root, chaos_root)
                current_sha = _ls_remote_sha(repo_root, branch)
                if not current_sha:
                    ls_remote_failed = True
                    break
                if current_sha != sha_at_conflict:
                    advanced = True
            if ls_remote_failed:
                summary["ls_remote_failed"] += 1
                continue

            identity = topic.get("identity") or {}
            ticket_id = identity.get("ticket_id", "")
            thread_id = topic.get("thread_id") or topic.get("root_message_id")
            dev_id = identity.get("creator_open_id", "")
            dev_name = identity.get("developer") or "开发者"

            posted = False
            kept = []
            dirty = False
            for event in pending:
                if not _is_candidate_no_op_reply(event, approver_open_ids):
                    kept.append(event)
                    continue
                event_id = event.get("event_id") or event.get("message_id")
                summary["checked"] += 1
                if posted:
                    # Already handled one `ok` this pass; drop redundant dups
                    # so we don't double-post or leave them for the parent.
                    topic_store.append_audit(topic,
                        event="rebase_conflict_ack_duplicate_dropped",
                        triggered_by_event_id=event_id)
                    topic_store.push_recent_event(topic, event_id)
                    dirty = True
                    continue
                if advanced:
                    ok, resp = _render_and_post(
                        "merge_resuming", {"TICKET_ID": ticket_id}, thread_id)
                    template = "merge_resuming"
                else:
                    ok, resp = _render_and_post(
                        "rebase_no_push",
                        {"TICKET_ID": ticket_id, "DEVELOPER_ID": dev_id,
                         "DEVELOPER_NAME": dev_name},
                        thread_id)
                    template = "rebase_no_push"
                if not ok:
                    summary["errors"] += 1
                    _log_activity(
                        f"rebase_conflict_ack_post_error thread={thread_id} "
                        f"event={event_id} template={template} reason={resp}")
                    kept.append(event)
                    continue
                posted = True
                if advanced:
                    review.pop("rebase_conflict_blocked", None)
                    review.pop("rebase_conflict_shas", None)
                    summary["resumed"] += 1
                    ack_event = "rebase_conflict_resolved_ack"
                    _log_activity(
                        f"rebase_conflict_resumed thread={thread_id} "
                        f"event={event_id}")
                else:
                    summary["no_push"] += 1
                    ack_event = "rebase_conflict_no_push"
                    _log_activity(
                        f"rebase_conflict_no_push thread={thread_id} "
                        f"event={event_id}")
                topic_store.append_audit(topic,
                    event="lark_reply_sent",
                    triggered_by_event_id=event_id,
                    template=template)
                topic_store.append_audit(topic,
                    event=ack_event,
                    triggered_by_event_id=event_id)
                topic_store.push_recent_event(topic, event_id)
                dirty = True

            if dirty:
                topic["events"]["pending"] = kept
                topic.setdefault("lifecycle", {})["updated_at"] = topic_store.now_ms()
                topic_store.write_atomic(topic_path, topic)
        finally:
            topic_store.release_lock(topic_path)

    return summary


def drain_withdrawn(topics_dir, index_path, cycle_id):  # noqa: ARG001
    """Drop pending events whose source message has been withdrawn in Lark.

    Router.py filters `deleted:true` events at ingest, but a reply that
    lands in `events.pending[]` and is *then* withdrawn by the user would
    otherwise be delivered to the Claude topic agent, which spends ~100K
    tokens booting up just to call the same lark-cli API and drop the
    event. Running the check in-process here costs ~30ms + one CLI call
    per pending event, and keeps the agent's invocation rate tied to
    events that still exist.

    Returns a summary dict: `{checked, withdrawn, transient_errors,
    topics_touched, skipped_locked}`.
    """
    topics_dir = Path(topics_dir)
    summary = {"checked": 0, "withdrawn": 0, "transient_errors": 0,
               "topics_touched": 0, "skipped_locked": 0}
    if not topics_dir.exists():
        return summary

    holder = f"withdrawn-{cycle_id}"
    for topic_path in topic_store.iter_topic_files(topics_dir):
        try:
            topic = topic_store.read(topic_path)
        except (OSError, ValueError):
            continue
        pending = ((topic.get("events") or {}).get("pending") or [])
        # Only check events that actually carry a message_id. new_topic
        # root messages also have one; pending[] should always populate it
        # via router.py.
        targets = [ev for ev in pending if ev.get("message_id")]
        if not targets:
            continue

        if not topic_store.acquire_lock(topic_path, holder, cycle_id):
            summary["skipped_locked"] += 1
            continue
        summary["topics_touched"] += 1

        try:
            # Re-read under lock: mechanical/ack drains may have mutated
            # pending between our unlocked scan and lock acquisition.
            topic = topic_store.read(topic_path)
            pending = (topic.get("events") or {}).get("pending") or []
            kept = []
            dirty = False
            for event in pending:
                msg_id = event.get("message_id")
                if not msg_id:
                    kept.append(event)
                    continue
                summary["checked"] += 1
                alive = _is_message_alive(msg_id)
                if alive is False:
                    topic_store.append_audit(topic,
                        event="withdrawn_message_drained",
                        triggered_by_event_id=event.get("event_id") or msg_id,
                        message_id=msg_id,
                    )
                    summary["withdrawn"] += 1
                    dirty = True
                    _log_activity(
                        f"withdrawn_drain thread={topic.get('thread_id','')} "
                        f"message_id={msg_id}")
                    continue
                if alive is None:
                    summary["transient_errors"] += 1
                kept.append(event)
            if dirty:
                topic["events"]["pending"] = kept
                topic.setdefault("lifecycle", {})["updated_at"] = topic_store.now_ms()
                topic_store.write_atomic(topic_path, topic)
        finally:
            topic_store.release_lock(topic_path)

    return summary


# ── Activity log (shared convention with dispatcher.py) ──────────────

def _log_activity(message):
    try:
        import datetime
        log_path = SKILL_DIR / "cfg" / "activity.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [INFO] {message}\n")
    except OSError:
        pass


# ── CLI (for replay/manual invocation) ────────────────────────────────

def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="Drain mechanical approver replies.")
    ap.add_argument("--topics-dir", required=True)
    ap.add_argument("--index", required=True)
    ap.add_argument("--approver-id", default=os.environ.get("REVIEW_BOT_APPROVER_ID", ""))
    ap.add_argument("--approver-open-ids",
                    default=os.environ.get("REVIEW_BOT_APPROVER_OPEN_IDS", ""),
                    help="Comma-separated authorized approver open_ids "
                         "(merged with --approver-id for back-compat).")
    ap.add_argument("--cycle-id", default="manual")
    args = ap.parse_args()
    namelist = [s.strip() for s in args.approver_open_ids.split(",") if s.strip()]
    if args.approver_id and args.approver_id not in namelist:
        namelist.append(args.approver_id)
    result = drain_mechanical(args.topics_dir, args.index,
                              namelist, args.cycle_id,
                              withdrawn_ids=None)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
