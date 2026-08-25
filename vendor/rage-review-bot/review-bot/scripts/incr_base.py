"""Shared per-repo incremental-review base resolution.

`review.last_review_commit` records the SHA each repo was at when last
reviewed. It is stored EITHER as a per-repo dict `{"rage": sha,
"chaos": sha}` (current shape — required for cross-repo topics) OR, on
legacy topics, as a single scalar SHA. `expected_sha_for` normalizes
both.

A stored base SHA is only a valid two-dot diff base when it is still an
ancestor of the current head. After a rebase / force-push it is not
(and may even be orphaned/unfetchable), so a naive `git diff base..head`
either fails or — worse — silently pulls in every target-branch commit
the rebase absorbed. `resolve_incr_range` detects that with
`git merge-base --is-ancestor` and falls back to a three-dot diff
against the target branch (the round-1-style full branch diff), which is
correct regardless of rebase.

See DESIGN §1.4.5.
"""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import subprocess_util

FETCH_TIMEOUT_S = 30


def expected_sha_for(repo, last_review_commit):
    """Normalize the two on-disk shapes of review.last_review_commit.

    dict → per-repo SHA (`None` if the repo isn't present);
    str  → the same scalar for every repo (legacy single-repo topics);
    anything else / falsy → None.
    """
    if not last_review_commit:
        return None
    if isinstance(last_review_commit, str):
        return last_review_commit
    if isinstance(last_review_commit, dict):
        return last_review_commit.get(repo)
    return None


def _git(repo_root, args, timeout=FETCH_TIMEOUT_S):
    """Run git in repo_root. Returns (rc, stdout, stderr); rc None on exec error."""
    try:
        proc = subprocess_util.hidden_run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
    except (OSError, Exception):  # noqa: BLE001
        return None, "", ""
    return proc.returncode, (proc.stdout or ""), (proc.stderr or "")


def _have_commit(repo_root, sha):
    rc, _, _ = _git(repo_root, ["cat-file", "-e", f"{sha}^{{commit}}"], timeout=5)
    return rc == 0


def resolve_incr_range(repo_root, expected_sha, current_sha, target_branch,
                       diff_timeout=60):
    """Resolve the log/diff ranges for an incremental review of one repo.

    Fetches what it needs (target ref, expected_sha, current_sha), then
    decides between an incremental range (base is an ancestor of head) and
    a rebase-safe full range (three-dot vs the target branch).

    Returns a dict:
        {"mode": "incremental"|"rebased_full",
         "base_ref":  "<sha>" | "origin/<target>",
         "log_range": "<a>..<b>",
         "diff_range":"<a>..<b>" | "<a>...<b>"}
    or None when nothing reviewable could be resolved (no current head, or
    a rebase with no target branch to fall back to). The caller then skips
    the repo / falls back to its own path.
    """
    if not repo_root or not current_sha:
        return None

    # Make the current head resolvable locally (dev may have just pushed).
    if not _have_commit(repo_root, current_sha):
        _git(repo_root, ["fetch", "origin", current_sha], timeout=diff_timeout)

    # Try the stored base first (no target fetch needed on the common
    # linear path). Fetch the base if absent (may fail if orphaned).
    if expected_sha and expected_sha != current_sha:
        if not _have_commit(repo_root, expected_sha):
            _git(repo_root, ["fetch", "origin", expected_sha])
        if _have_commit(repo_root, expected_sha):
            rc, _, _ = _git(
                repo_root,
                ["merge-base", "--is-ancestor", expected_sha, current_sha],
                timeout=10)
            if rc == 0:
                return {
                    "mode": "incremental",
                    "base_ref": expected_sha,
                    "log_range": f"{expected_sha}..{current_sha}",
                    "diff_range": f"{expected_sha}..{current_sha}",
                }

    # Rebased / force-pushed / orphaned / no base → full branch diff vs the
    # target branch. Pull the target ref so origin/<target> resolves locally.
    if target_branch:
        rc, _, _ = _git(
            repo_root,
            ["fetch", "origin", f"+{target_branch}:refs/remotes/origin/{target_branch}"],
        )
        if rc == 0:
            target_ref = f"origin/{target_branch}"
            return {
                "mode": "rebased_full",
                "base_ref": target_ref,
                "log_range": f"{target_ref}..{current_sha}",
                "diff_range": f"{target_ref}...{current_sha}",
            }
    return None
