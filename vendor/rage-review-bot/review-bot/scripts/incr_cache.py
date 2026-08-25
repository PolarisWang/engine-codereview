"""Pre-compute per-repo incremental log/diff for topics awaiting dev_reply.

When a topic is in SIMPLE_REVISION or FULL_REVISION and the dev has
pushed new commits since `review.last_review_commit`, the topic agent
would normally run `git fetch` + `git log expected..current` +
`git diff expected..current` per repo. Running those once here — in the
dispatcher, before emitting dispatch_plan.json — lets the agent read
the log/diff from cache files instead, eliminating 2-3 Bash tool
rounds per spawn.

Cache semantics:
- Files live under `$WRITE_TMP_DIR/review_bot_incr/` and are named
  `{thread_id}_{repo}_{current_sha}_incr.{log,diff}`. The SHA is baked
  into the filename so staleness is detectable: if the dev pushes
  again between pre-compute and the spawn, the agent's refreshed SHA
  will not match `incr_cache[repo].sha` and the agent falls back to
  running git directly.
- Skipped topics (wrong state, 3rd-party phase, missing
  last_review_commit, unreachable repo root, any git failure) produce
  no cache entry; the agent's fallback path keeps working as before.
"""

import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import subprocess_util
import incr_base


INCR_STATES = {"SIMPLE_REVISION", "FULL_REVISION"}

FETCH_TIMEOUT_S = 30
DIFF_TIMEOUT_S = 60


def cache_dir():
    base = os.environ.get("WRITE_TMP_DIR", str(SCRIPT_DIR.parent / "cfg"))
    directory = Path(base) / "review_bot_incr"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _run_git(repo_root, args, timeout=FETCH_TIMEOUT_S):
    """Invoke git with hidden_run. Returns (rc, stdout, stderr);
    rc is None on exec failure."""
    try:
        proc = subprocess_util.hidden_run(
            ["git", "-C", str(repo_root)] + list(args),
            capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
    except (OSError, Exception):  # noqa: BLE001
        return None, "", ""
    return proc.returncode, (proc.stdout or ""), (proc.stderr or "")


def _repo_root_for(repo, rage_root, chaos_root):
    if repo == "rage":
        return rage_root
    if repo == "chaos":
        return chaos_root
    return None


def _populate_cache(repo_root, log_range, diff_range,
                    log_path, diff_path):
    """Run git log + git diff over the resolved ranges, write to cache
    files. Returns True on success."""
    rc_log, log_out, _ = _run_git(
        repo_root,
        ["log", "--no-color", "--format=%H %s", log_range],
    )
    if rc_log != 0:
        return False
    try:
        log_path.write_text(log_out, encoding="utf-8")
    except OSError:
        return False
    # -w / --ignore-cr-at-eol: tolerate whitespace + CRLF-normalization noise. Editing a
    # file in this core.autocrlf repo re-normalizes line endings (and sometimes re-indents)
    # untouched lines, which would otherwise surface pre-existing code as "+added" and get
    # reviewed as new. Whitespace-only churn is never review-worthy here (C++/Lua).
    rc_diff, diff_out, _ = _run_git(
        repo_root,
        ["diff", "--no-color", "-w", "--ignore-cr-at-eol", diff_range],
        timeout=DIFF_TIMEOUT_S,
    )
    if rc_diff != 0:
        _remove_if_exists(log_path)
        return False
    try:
        diff_path.write_text(diff_out, encoding="utf-8")
    except OSError:
        _remove_if_exists(log_path)
        return False
    return True


def _remove_if_exists(path):
    try:
        path.unlink()
    except OSError:
        pass


def precompute_for_topic(topic, rage_root, chaos_root):
    """Pre-compute per-repo incremental log/diff cache for a topic.

    Returns a dict keyed by repo:
        {"rage":  {"log_path": "...", "diff_path": "...",
                   "sha": "...", "expected_sha": "...",
                   "mode": "incremental"|"rebased_full", "base_ref": "..."},
         "chaos": {...}}

    `mode == "rebased_full"` means the stored base was not an ancestor of
    the head (rebase / force-push), so the cached diff is the full branch
    diff vs `base_ref` (the target branch), not an incremental delta.
    or None if pre-compute does not apply (wrong state, no new commits,
    3rd-party phase, etc.). Any failure for an individual repo leaves
    that repo out of the returned dict without aborting siblings; the
    agent's fallback path handles missing entries per repo.
    """
    review = topic.get("review") or {}
    if review.get("state") not in INCR_STATES:
        return None
    if review.get("review_phase") == "3rd_party":
        return None
    last_review_commit = review.get("last_review_commit")
    if not last_review_commit:
        return None
    mrs = topic.get("mrs") or {}
    if not mrs:
        return None
    thread_id = topic.get("thread_id") or ""
    if not thread_id:
        return None
    cache_root = cache_dir()
    entries = {}
    for repo, mr_obj in mrs.items():
        if repo.startswith("3rd_party/"):
            continue
        branch = (mr_obj or {}).get("branch")
        current_sha = (mr_obj or {}).get("branch_sha")
        if not branch or not current_sha:
            continue
        expected_sha = incr_base.expected_sha_for(repo, last_review_commit)
        if expected_sha and expected_sha == current_sha:
            # No new commits since last review; drain_no_new_commits
            # short-circuits that case separately.
            continue
        repo_root = _repo_root_for(repo, rage_root, chaos_root)
        if not repo_root:
            continue
        target_branch = (mr_obj or {}).get("target_branch") or (
            "rage/master" if repo == "chaos" else "master")
        log_path = cache_root / f"{thread_id}_{repo}_{current_sha}_incr.log"
        diff_path = cache_root / f"{thread_id}_{repo}_{current_sha}_incr.diff"
        resolved = None
        if not (log_path.exists() and diff_path.exists()):
            # resolve_incr_range fetches what it needs, then picks an
            # incremental two-dot range (base still an ancestor) or a
            # rebase-safe three-dot range vs the target branch.
            resolved = incr_base.resolve_incr_range(
                repo_root, expected_sha, current_sha, target_branch,
                diff_timeout=DIFF_TIMEOUT_S)
            if not resolved:
                continue
            if not _populate_cache(repo_root, resolved["log_range"],
                                   resolved["diff_range"], log_path, diff_path):
                continue
        entries[repo] = {
            "log_path":     str(log_path),
            "diff_path":    str(diff_path),
            "sha":          current_sha,
            "expected_sha": expected_sha,
            "mode":         (resolved or {}).get("mode"),
            "base_ref":     (resolved or {}).get("base_ref"),
        }
    return entries or None
