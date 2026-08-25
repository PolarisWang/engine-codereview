"""Poll GitLab MR state for topics currently in APPROVED.

Called each dispatcher cycle. For each APPROVED topic's MRs:
1. check_mr(repo, mr_obj) — query MR state (merged/closed/opened)
2. check_pipeline_mr(repo, mr_obj) — query head pipeline status
3. rebase_mr(repo, mr_obj) — attempt server-side rebase (skip_ci, pipeline already passed)
4. merge_mr(repo, mr_obj) — merge the MR directly

The dispatcher uses check_mr() + check_pipeline_mr() each cycle. The merge-queue
agent calls rebase_mr() + merge_mr() sequentially for topics with passed pipelines.
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import subprocess_util


REPO_SLUGS = {
    "chaos": "booming/dev/projects/rage/chaos",
    "rage":  "booming/dev/projects/rage/rage",
}


def _urlenc(s):
    """Minimal URL-encoder for glab api project paths."""
    return s.replace("/", "%2F")


def _run_glab(args, timeout_s=30):
    """Run a glab command, return (stdout, stderr, returncode)."""
    try:
        proc = subprocess_util.hidden_run(
            args,
            capture_output=True, text=True,
            timeout=timeout_s, encoding="utf-8", errors="replace")
        return proc.stdout, proc.stderr, proc.returncode
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return "", str(e), -1


def _get_iid(mr_obj):
    """Get MR iid, tolerating both 'mr_iid' and 'iid' field names."""
    return mr_obj.get("mr_iid") or mr_obj.get("iid")


def check_mr(repo, mr_obj, timeout_s=30):
    """Query glab for the current MR state.

    Returns dict with keys:
        merged: bool
        closed: bool  (state == 'closed' AND not merged)
        state:  str ('opened' | 'merged' | 'closed') or None on error
        sha:    merge_commit_sha when merged, else None
        error:  str or None
    """
    mr_iid = _get_iid(mr_obj)
    repo_slug = mr_obj.get("repo_slug") or REPO_SLUGS.get(repo)
    if not mr_iid or not repo_slug:
        return {"merged": False, "closed": False, "state": None,
                "sha": None, "error": "missing mr_iid or unknown repo"}

    stdout, stderr, rc = _run_glab(
        ["glab", "api", f"projects/{_urlenc(repo_slug)}/merge_requests/{mr_iid}"],
        timeout_s=timeout_s,
    )

    if rc != 0:
        return {"merged": False, "closed": False, "state": None,
                "sha": None, "error": (stderr or "").strip()[:200]}

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        return {"merged": False, "closed": False, "state": None,
                "sha": None, "error": f"bad json: {e}"}

    state = data.get("state")
    merge_sha = data.get("merge_commit_sha") or data.get("squash_commit_sha")

    return {
        "merged": state == "merged",
        "closed": state == "closed",
        "state":  state,
        "sha":    merge_sha if state == "merged" else None,
        "error":  None,
    }


def check_pipeline_mr(repo, mr_obj, timeout_s=30):
    """Query the head pipeline status for the MR.

    Returns dict with keys:
        status:  str ('success' | 'running' | 'pending' | 'failed' |
                      'canceled' | None)
        error:   str or None
    """
    mr_iid = _get_iid(mr_obj)
    repo_slug = mr_obj.get("repo_slug") or REPO_SLUGS.get(repo)
    if not mr_iid or not repo_slug:
        return {"status": None, "error": "missing mr_iid or unknown repo"}

    stdout, stderr, rc = _run_glab(
        ["glab", "api", f"projects/{_urlenc(repo_slug)}/merge_requests/{mr_iid}"],
        timeout_s=timeout_s,
    )

    if rc != 0:
        return {"status": None, "error": (stderr or "").strip()[:200]}

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        return {"status": None, "error": f"bad json: {e}"}

    pipeline = data.get("head_pipeline") or {}
    return {
        "status":           pipeline.get("status"),
        "pipeline_id":      pipeline.get("id"),
        "pipeline_web_url": pipeline.get("web_url"),
        "head_sha":         data.get("sha"),
        "error":            None,
    }


def rebase_mr(repo, mr_obj, timeout_s=60):
    """Attempt server-side rebase via GitLab API (skip_ci).

    PUT /projects/:id/merge_requests/:iid/rebase
    Body: {"skip_ci": true}

    Returns dict with keys:
        success:  bool
        error:    str or None
    """
    mr_iid = _get_iid(mr_obj)
    repo_slug = mr_obj.get("repo_slug") or REPO_SLUGS.get(repo)
    if not mr_iid or not repo_slug:
        return {"success": False, "error": "missing mr_iid or unknown repo"}

    # Capture pre-rebase head SHA so wait_for_mergeable can verify the
    # post-rebase recompute (sha must change AND detailed_merge_status
    # must transition through a non-"mergeable" state).
    pre_out, _, pre_rc = _run_glab(
        ["glab", "api",
         f"projects/{_urlenc(repo_slug)}/merge_requests/{mr_iid}"],
        timeout_s=timeout_s,
    )
    pre_rebase_sha = None
    if pre_rc == 0:
        try:
            pre_rebase_sha = json.loads(pre_out).get("sha")
        except json.JSONDecodeError:
            pass

    # GitLab API: PUT rebase with skip_ci (pipeline already passed on dev's commit)
    stdout, stderr, rc = _run_glab(
        ["glab", "api", "--method", "PUT",
         f"projects/{_urlenc(repo_slug)}/merge_requests/{mr_iid}/rebase",
         "--raw-field", "skip_ci=true"],
        timeout_s=timeout_s,
    )

    if rc != 0:
        err = (stderr or "").strip()[:200]
        # Check for conflict indicators
        if "conflict" in err.lower() or "rebase" in err.lower():
            return {"success": False, "error": f"rebase_conflict: {err}"}
        return {"success": False, "error": err}

    # Parse response — GitLab returns {"rebase_in_progress": true/false}
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        data = {}

    if data.get("rebase_in_progress") is True:
        # Rebase is async; poll until done or timeout
        import time
        for _ in range(30):  # 30 * 1s = 30s max wait (skip_ci rebase is fast)
            time.sleep(1)
            chk_out, _, chk_rc = _run_glab(
                ["glab", "api",
                 f"projects/{_urlenc(repo_slug)}/merge_requests/{mr_iid}"],
                timeout_s=30,
            )
            if chk_rc != 0:
                continue
            try:
                mr_data = json.loads(chk_out)
            except json.JSONDecodeError:
                continue
            rebase_err = mr_data.get("merge_error") or ""
            rebase_in_progress = mr_data.get("rebase_in_progress")
            # Treat None as False (completed) — GitLab may stop returning the field after completion
            if rebase_in_progress is False or rebase_in_progress is None:
                if "conflict" in rebase_err.lower():
                    return {"success": False, "error": f"rebase_conflict: {rebase_err}"}
                return {"success": True, "error": None,
                        "post_rebase_sha": mr_data.get("sha"),
                        "pre_rebase_sha": pre_rebase_sha}
        return {"success": False, "error": "rebase_timeout",
                "pre_rebase_sha": pre_rebase_sha}

    # Immediate response (no async) — rebase already done
    merge_error = data.get("merge_error") or ""
    if "conflict" in merge_error.lower():
        return {"success": False, "error": f"rebase_conflict: {merge_error}"}

    # Follow-up query to capture the new head SHA (the rebase response body
    # doesn't always include it). wait_for_mergeable needs this to detect
    # when GitLab has re-indexed the new commit.
    sha_out, _, sha_rc = _run_glab(
        ["glab", "api",
         f"projects/{_urlenc(repo_slug)}/merge_requests/{mr_iid}"],
        timeout_s=30,
    )
    post_rebase_sha = None
    if sha_rc == 0:
        try:
            post_rebase_sha = json.loads(sha_out).get("sha")
        except json.JSONDecodeError:
            pass

    return {"success": True, "error": None,
            "post_rebase_sha": post_rebase_sha,
            "pre_rebase_sha": pre_rebase_sha}


def wait_for_mergeable(repo, mr_obj, cap_s=60, poll_s=1, timeout_s=30,
                       expected_sha=None, previous_sha=None):
    """Poll detailed_merge_status after a rebase until GitLab confirms the
    MR can be merged, or give up after cap_s seconds.

    GitLab recomputes detailed_merge_status asynchronously after a rebase.
    During that window the MR's cached head SHA is still the pre-rebase
    commit and detailed_merge_status may transiently read "mergeable" from
    the old state — firing PUT /merge at that moment returns 405 Method
    Not Allowed because GitLab then flips the status to a transitional
    value once it finishes re-indexing the new commit.

    To avoid the stale-read trap we require ALL of these before settled:
      1. last_sha != previous_sha — GitLab indexed the rebased commit
         (the rebase truly landed; not a stale view of the old head).
      2. seen_non_mergeable — observed detailed_merge_status leave
         "mergeable" at least once during this poll loop, proving the
         post-rebase mergeability cache invalidated and is being
         recomputed (not a stale "mergeable" cached from pre-rebase).
      3. last_status == "mergeable" — final state.

    `expected_sha` (post-rebase SHA from rebase_mr) is kept as a
    secondary anchor: if provided, last_sha must also equal it. This
    mirrors the GitLab UI's "Checking if branch can be merged…" spinner.

    Returns dict with keys:
        settled: bool          — True if new SHA re-indexed AND
                                 transition observed AND mergeable
        status:  str or None   — last observed detailed_merge_status
        sha:     str or None   — last observed head SHA
        waited_s: float        — seconds actually spent polling
    """
    import time
    mr_iid = _get_iid(mr_obj)
    repo_slug = mr_obj.get("repo_slug") or REPO_SLUGS.get(repo)
    if not mr_iid or not repo_slug:
        return {"settled": False, "status": None, "sha": None, "waited_s": 0.0}

    start = time.time()
    deadline = start + cap_s
    last_status = None
    last_sha = None
    seen_non_mergeable = False
    while True:
        stdout, _stderr, rc = _run_glab(
            ["glab", "api",
             f"projects/{_urlenc(repo_slug)}/merge_requests/{mr_iid}"],
            timeout_s=timeout_s,
        )
        if rc == 0:
            try:
                data = json.loads(stdout)
                last_status = data.get("detailed_merge_status")
                last_sha = data.get("sha")
                if last_status is not None and last_status != "mergeable":
                    seen_non_mergeable = True
                sha_advanced = (previous_sha is None) or (
                    last_sha is not None and last_sha != previous_sha)
                sha_anchored = (expected_sha is None) or (
                    last_sha == expected_sha)
                if (sha_advanced and sha_anchored
                        and seen_non_mergeable
                        and last_status == "mergeable"):
                    return {"settled": True, "status": last_status,
                            "sha": last_sha,
                            "waited_s": time.time() - start}
            except (json.JSONDecodeError, AttributeError):
                pass
        if time.time() >= deadline:
            return {"settled": False, "status": last_status, "sha": last_sha,
                    "waited_s": time.time() - start}
        time.sleep(poll_s)


def merge_mr(repo, mr_obj, timeout_s=30):
    """Merge the MR directly via GitLab API PUT.

    Uses the GitLab merge API endpoint instead of glab CLI to avoid the
    CLI silently setting auto-merge (merge_when_pipeline_succeeds) which
    causes MRs to hang when the rebase was done with skip_ci.

    Prerequisites: pipeline already passed on dev's commit, rebase done
    with skip_ci. Direct merge should succeed immediately.

    Returns dict with keys:
        success:  bool
        error:    str or None
    """
    mr_iid = _get_iid(mr_obj)
    repo_slug = mr_obj.get("repo_slug") or REPO_SLUGS.get(repo)
    if not mr_iid or not repo_slug:
        return {"success": False, "error": "missing mr_iid or unknown repo"}

    # GitLab API: PUT /projects/:id/merge_requests/:iid/merge
    # This performs an immediate merge, unlike glab CLI which may set auto-merge.
    # Retry up to 3 times with 5s spacing only on 405 ("merge-status not
    # settled" race — GitLab's detailed_merge_status can lag pipeline success
    # by a few seconds). The main 30s settle window lives upstream in
    # dispatcher._check_approved_topics; this is just a short safety net
    # for topics that slip through right at the edge of the window.
    #
    # 405 ALSO means the MR is no longer open — most importantly, an
    # already-merged MR (a concurrent/duplicate merge pass beat us to it,
    # or this is a retry after a lost MERGED write). The PUT response alone
    # can't distinguish "not settled yet" / "config rejects merge" /
    # "already merged", so on any persistent failure we re-fetch live state
    # below and treat "already merged" as success (idempotent merge). This
    # is what stops a harmless double-merge from raising a false
    # manual-merge alarm. See DESIGN §1.6.8 / RAGE-15898.
    #
    # Always pass squash=true. Chaos has squash_option="always" and returns
    # 422 without it; other repos silently accept the flag. Unconditional
    # squash keeps the logic simple and repo-agnostic.
    import time
    merge_path = (
        f"projects/{_urlenc(repo_slug)}/merge_requests/{mr_iid}/merge")
    cmd = ["glab", "api", "--method", "PUT", merge_path,
           "--field", "squash=true"]
    last_err = ""
    for attempt in range(3):
        stdout, stderr, rc = _run_glab(cmd, timeout_s=timeout_s)
        if rc == 0:
            try:
                data = json.loads(stdout)
                if data.get("state") == "merged":
                    # Capture the landed commit. Chaos squashes always
                    # (§1.6.9), so squash_commit_sha is the thing a
                    # cherry-pick must port — the branch head would replay
                    # commits master never took. Discarding this response
                    # body is why bot-merged topics had no SHA to
                    # cherry-pick (DESIGN §1.24).
                    return {"success": True, "error": None,
                            "sha": (data.get("squash_commit_sha")
                                    or data.get("merge_commit_sha"))}
                # rc==0 but state isn't "merged": unexpected. Don't retry —
                # fall through to the live-state recheck below.
                last_err = f"unexpected state: {data.get('state')}"
            except (json.JSONDecodeError, AttributeError):
                # Merge went through but the body was unreadable, so there is
                # no sha to report. `_offer_cherrypick` warns about a merged
                # repo with no sha rather than dropping it silently (§1.24.4).
                return {"success": True, "error": None, "sha": None}
            break
        last_err = (stderr or stdout or "").strip()[:200]
        if "405" not in last_err or attempt == 2:
            break
        time.sleep(5)

    # Idempotency net: the PUT did not confirm a merge. Re-fetch live MR
    # state — if it is already merged, the merge effectively succeeded and
    # the caller should treat the topic as MERGED rather than raise a
    # manual-merge alarm. check_mr returns state=="opened" for a genuinely
    # blocked 405 (required approver / unresolved threads / protected
    # branch), so this only rescues the already-merged case.
    try:
        state_check = check_mr(repo, mr_obj)
        if state_check.get("merged"):
            # Carry the sha through. `check_mr` already extracted it, and a
            # success without one drops this repo out of
            # `lifecycle.merge_shas` — which silently excludes it from the
            # post-merge cherry-pick offer (DESIGN §1.24.4).
            return {"success": True, "error": None, "already_merged": True,
                    "sha": state_check.get("sha")}
    except Exception as exc:  # noqa: BLE001
        last_err = f"{last_err}; already-merged recheck failed: {exc}"[:200]
    return {"success": False, "error": last_err}


# ── Infra failure detection + auto-retry ────────────────────────────

# Patterns that indicate infra/runner failures (not code issues).
# These jobs fail fast (<10s) with short logs — never reach compilation.
_INFRA_PATTERNS = [
    "Permission denied",
    "fatal: unable to access",
    "fatal: unable to create",
    "Connection refused",
    "Connection timed out",
    "Connection reset by peer",
    "unable to checkout",
    "could not lock config file",
    "unable to unlink old",
    "failed to remove",
    "No space left on device",
    "disk quota exceeded",
    "System cannot find the path",
    "runner system failure",
    "stuck or timeout",
]

# Max retry attempts per pipeline (tracked externally via topic audit)
MAX_INFRA_RETRIES = 2


def _strip_ansi(text):
    """Remove ANSI escape codes from CI log output."""
    import re
    return re.sub(r'\x1b\[[0-9;]*[mKHJ]|\[0K', '', text)


def check_infra_failure_and_retry(repo, mr_obj, timeout_s=30):
    """Check if a failed pipeline is an infra failure and auto-retry.

    1. Get the pipeline's failed jobs
    2. For each failed job, fetch the trace (log)
    3. If log is short and matches infra patterns -> retry the job

    Returns dict:
        is_infra:  bool — True if this was an infra failure
        retried:   bool — True if a retry was triggered
        job_id:    int or None — the retried job ID
        reason:    str — detected infra failure reason
        error:     str or None
    """
    mr_iid = _get_iid(mr_obj)
    repo_slug = mr_obj.get("repo_slug") or REPO_SLUGS.get(repo)
    if not mr_iid or not repo_slug:
        return {"is_infra": False, "retried": False, "job_id": None,
                "reason": "", "error": "missing mr_iid or unknown repo"}

    # Step 1: Get MR to find pipeline ID
    stdout, stderr, rc = _run_glab(
        ["glab", "api", f"projects/{_urlenc(repo_slug)}/merge_requests/{mr_iid}"],
        timeout_s=timeout_s,
    )
    if rc != 0:
        return {"is_infra": False, "retried": False, "job_id": None,
                "reason": "", "error": (stderr or "").strip()[:200]}
    try:
        mr_data = json.loads(stdout)
    except json.JSONDecodeError:
        return {"is_infra": False, "retried": False, "job_id": None,
                "reason": "", "error": "bad json from MR API"}

    pipeline = mr_data.get("head_pipeline") or {}
    pipeline_id = pipeline.get("id") if pipeline.get("status") == "failed" else None

    # Fallback: head_pipeline may be "skipped" (superseded) or "success"/"running"
    # while the actual failed pipeline sits earlier in the MR's pipeline history.
    # This matches the GitLab UI's per-row "Run again" button for a prior pipeline.
    # Do NOT try POST /pipelines/{id}/retry (returns "skipped" when no failed jobs
    # remain at that level) or POST /projects/{id}/pipeline?ref=BRANCH (rejected
    # because .gitlab-ci.yml restricts to merge_request_event).
    if not pipeline_id:
        list_out, list_err, list_rc = _run_glab(
            ["glab", "api",
             f"projects/{_urlenc(repo_slug)}/merge_requests/{mr_iid}/pipelines"
             "?per_page=20"],
            timeout_s=timeout_s,
        )
        if list_rc != 0:
            return {"is_infra": False, "retried": False, "job_id": None,
                    "reason": "", "error": (list_err or "").strip()[:200]}
        try:
            mr_pipelines = json.loads(list_out)
        except json.JSONDecodeError:
            return {"is_infra": False, "retried": False, "job_id": None,
                    "reason": "", "error": "bad json from MR pipelines API"}
        # MR pipelines endpoint returns most-recent first; pick first failed.
        failed_pipe = next(
            (p for p in mr_pipelines if p.get("status") == "failed"),
            None,
        )
        if not failed_pipe:
            return {"is_infra": False, "retried": False, "job_id": None,
                    "reason": "", "error": None}
        pipeline_id = failed_pipe.get("id")
        if not pipeline_id:
            return {"is_infra": False, "retried": False, "job_id": None,
                    "reason": "", "error": None}

    # Step 2: Get failed jobs from the pipeline
    stdout, stderr, rc = _run_glab(
        ["glab", "api",
         f"projects/{_urlenc(repo_slug)}/pipelines/{pipeline_id}/jobs"
         f"?scope=failed&per_page=5"],
        timeout_s=timeout_s,
    )
    if rc != 0:
        return {"is_infra": False, "retried": False, "job_id": None,
                "reason": "", "error": (stderr or "").strip()[:200]}
    try:
        jobs = json.loads(stdout)
    except json.JSONDecodeError:
        return {"is_infra": False, "retried": False, "job_id": None,
                "reason": "", "error": "bad json from jobs API"}

    if not jobs:
        return {"is_infra": False, "retried": False, "job_id": None,
                "reason": "", "error": None}

    # Step 3: Check each failed job's trace for infra patterns
    for job in jobs:
        job_id = job.get("id")
        job_duration = job.get("duration") or 999

        # Infra failures are fast — skip jobs that ran >60s (likely real builds)
        if job_duration > 60:
            continue

        # Fetch job trace (log)
        stdout, stderr, rc = _run_glab(
            ["glab", "api",
             f"projects/{_urlenc(repo_slug)}/jobs/{job_id}/trace"],
            timeout_s=timeout_s,
        )
        if rc != 0:
            continue

        log_text = _strip_ansi(stdout)
        log_lines = [ln for ln in log_text.split('\n') if ln.strip()]

        # Infra failures have short logs (< 50 meaningful lines)
        if len(log_lines) > 50:
            continue

        # Check for infra patterns
        matched_pattern = None
        for pattern in _INFRA_PATTERNS:
            if pattern.lower() in log_text.lower():
                matched_pattern = pattern
                break

        if not matched_pattern:
            continue

        # Step 4: Retry the job
        retry_out, retry_err, retry_rc = _run_glab(
            ["glab", "api", "--method", "POST",
             f"projects/{_urlenc(repo_slug)}/jobs/{job_id}/retry"],
            timeout_s=timeout_s,
        )

        if retry_rc == 0:
            return {
                "is_infra": True,
                "retried": True,
                "job_id": job_id,
                "reason": f"infra: {matched_pattern} (job {job_id}, "
                          f"{job_duration}s, {len(log_lines)} lines)",
                "error": None,
            }
        else:
            return {
                "is_infra": True,
                "retried": False,
                "job_id": job_id,
                "reason": f"infra: {matched_pattern} (retry failed)",
                "error": (retry_err or "").strip()[:200],
            }

    # No infra pattern matched — this is a real build failure
    return {"is_infra": False, "retried": False, "job_id": None,
            "reason": "", "error": None}


# ── Central pipeline-status + infra-retry helper (#1) ────────────────
#
# Single canonical path for resolving "what is this MR's pipeline_status
# right now, and should we auto-retry an infra failure before reporting
# it?" All three approval paths (dispatcher poll, mechanical handler,
# 3rd-party topic-agent via post_approval.py) MUST go through this to
# close the infra-retry-asymmetry gap.
#
# Mutates `mr_obj["pipeline_status"]` in place using the same mapping
# dispatcher._check_approved_topics and mechanical_reply_handler._pipeline_msg
# previously duplicated:
#   None (no head_pipeline)       -> "passed"
#   "success"                     -> "passed"
#   "running"/"pending"/"created" -> "running"
#   "failed" (infra retried)      -> "running"   (job kicked, treat as running)
#   "failed"/"canceled" (real)    -> "failed"
#   anything else                 -> passthrough or "unknown"
#
# Caller owns audit: if retried is True, append an "infra_failure_retried"
# entry with the returned reason+job_id. Caller also owns write_atomic.

def recheck_pipeline_with_retry(repo, mr_obj, timeout_s=30):
    """Check MR pipeline, auto-retry infra failures, write pipeline_status.

    Returns dict:
        status:    final pipeline_status written to mr_obj
                   ("passed" | "running" | "failed" | "unknown" | <raw>)
        retried:   bool — True if an infra job was retried this call
        reason:    str — infra retry reason (empty unless retried)
        job_id:    int or None — retried job id
        raw:       str or None — upstream pipeline status before mapping
        error:     str or None — network / API error; status unchanged on error
    """
    pl_result = check_pipeline_mr(repo, mr_obj, timeout_s=timeout_s)
    raw = pl_result.get("status")
    err = pl_result.get("error")
    pipeline_id = pl_result.get("pipeline_id")
    pipeline_web_url = pl_result.get("pipeline_web_url")
    head_sha = pl_result.get("head_sha")
    if err:
        return {"status": mr_obj.get("pipeline_status"),
                "retried": False, "reason": "", "job_id": None,
                "raw": raw, "error": err,
                "pipeline_id": None, "pipeline_web_url": None,
                "head_sha": None}

    if raw is None:
        mr_obj["pipeline_status"] = "passed"
    elif raw in ("success", "skipped"):
        # "skipped" = head pipeline ran no jobs (e.g. the rage game repo has no
        # CI, or every job was filtered out by rules). Same merge-gate semantic
        # as a missing head_pipeline (None -> passed): nothing to wait on.
        mr_obj["pipeline_status"] = "passed"
    elif raw in ("running", "pending", "created"):
        mr_obj["pipeline_status"] = "running"
    elif raw == "failed":
        retry = check_infra_failure_and_retry(repo, mr_obj, timeout_s=timeout_s)
        if retry.get("retried"):
            mr_obj["pipeline_status"] = "running"
            return {"status": "running",
                    "retried": True,
                    "reason": retry.get("reason", ""),
                    "job_id": retry.get("job_id"),
                    "raw": raw, "error": None,
                    "pipeline_id": pipeline_id,
                    "pipeline_web_url": pipeline_web_url,
                    "head_sha": head_sha}
        mr_obj["pipeline_status"] = "failed"
    elif raw == "canceled":
        mr_obj["pipeline_status"] = "failed"
    else:
        mr_obj["pipeline_status"] = raw or "unknown"

    return {"status": mr_obj["pipeline_status"],
            "retried": False, "reason": "", "job_id": None,
            "raw": raw, "error": None,
            "pipeline_id": pipeline_id,
            "pipeline_web_url": pipeline_web_url,
            "head_sha": head_sha}
