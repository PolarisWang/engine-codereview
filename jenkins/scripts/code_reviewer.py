#!/usr/bin/env python3
"""
Code Reviewer — Clone repo, get diff, call Claude API for structured review.

Usage:
    python3 code_reviewer.py --repo "git@github.com:PolarisWang/ev-engine.git" \
                             --branch "feature/EV-123-fix" \
                             --base-branch main \
                             --project "EV" \
                             --issue-key "EV-123" \
                             --repo-type "engine"
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse

import shutil
import re

# Shared helpers: config.yaml loading, Jira pattern, HTTP with retry
from common import load_config, JIRA_URL_PATTERN, http_request
from common import get_claude_config, get_workspace_config

# Resolve git path explicitly (agent may not have it on PATH)
GIT_PATH = shutil.which("git") or "/usr/bin/git"


DEFAULT_REVIEW_INSTRUCTIONS = """
You are a senior game engine engineer reviewing a merge request. Provide concise findings in Chinese.

## Review Focus
1. Logic correctness and potential bugs
2. Memory safety and resource leaks
3. Concurrency / thread safety
4. Performance issues
5. API design
6. Error handling and edge cases
7. Security concerns

## Output Format (Chinese)
Group findings by severity. Each finding:

- **Severity**: 🔴 Critical / 🟡 Warning / ℹ️ Suggestion
- **File**: path/to/file
- **问题**: one-line description
- **建议**: how to fix

Keep each finding brief (2-3 lines max). Number findings sequentially (1, 2, 3...).

## Summary (in Chinese)
At the end:
| 严重程度 | 数量 |
|---------|------|
| 🔴 Critical | X |
| 🟡 Warning | X |
| ℹ️ Suggestion | X |
| **合计** | **X** |

IMPORTANT: Counts must match actual findings.
"""


def load_config():
    """Load config from config.yaml (common), falling back to env for model."""
    cfg = get_claude_config()
    return {
        "claude": {
            "model": cfg.get("model")
                     or os.environ.get("ANTHROPIC_MODEL", "deepseek-v4-flash"),
            "max_tokens": cfg.get("max_tokens", 8192),
            "review_instructions": cfg.get("review_instructions")
                                   or DEFAULT_REVIEW_INSTRUCTIONS.strip(),
        }
    }


def run_git(cmd, cwd, timeout=600):
    """Run a git command, return (returncode, stdout, stderr)."""
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def ssh_to_https(repo_url):
    """Convert git@ SSH URL to HTTPS URL."""
    if repo_url.startswith("git@"):
        # git@host:path/repo.git → https://host/path/repo.git
        m = re.match(r'git@([^:]+):(.+)', repo_url)
        if m:
            return f"https://{m.group(1)}/{m.group(2)}"
    return repo_url


def gitlab_api_get(path, token):
    """Make a GitLab API GET request (with retry)."""
    url = f"https://gitlab.booming-inc.com/api/v4/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    resp = http_request("GET", url, headers=headers)
    if resp is None:
        print(f"[gitlab] API request failed for {url[:80]}", file=sys.stderr)
    return resp


def parse_gitlab_mr_url(url):
    """Parse a GitLab MR URL to extract project path and MR IID."""
    m = re.match(r'https://gitlab\.booming-inc\.com/(.+?)/-/merge_requests/(\d+)', url)
    if m:
        return m.group(1), m.group(2)
    return None, None


def get_mr_diff_from_gitlab(mr_url, gitlab_token):
    """Fetch MR diff from GitLab API. Returns dict with diff_text and changed_files, or None."""
    project_path, mr_iid = parse_gitlab_mr_url(mr_url)
    if not project_path:
        print(f"[gitlab] Cannot parse MR URL: {mr_url}", file=sys.stderr)
        return None
    project_encoded = urllib.parse.quote(project_path, safe='')
    diffs = gitlab_api_get(f"projects/{project_encoded}/merge_requests/{mr_iid}/diffs", gitlab_token)
    if not diffs:
        return None
    diff_parts = []
    changed_files = []
    for d in diffs:
        diff_text = d.get("diff", "")
        if diff_text:
            diff_parts.append(diff_text)
        new_path = d.get("new_path", "")
        status = d.get("status", "modified")
        if new_path:
            status_char = {"added": "A", "deleted": "D", "renamed": "R"}.get(status, "M")
            changed_files.append(f"{status_char}\t{new_path}")
    return {
        "diff_text": "\n".join(diff_parts),
        "changed_files": changed_files,
    }


def _resolve_git_token():
    """Resolve a GitLab token from env (without ever logging it)."""
    return os.environ.get("GITLAB_TOKEN") or os.environ.get("CI_JOB_TOKEN") or ""


# Path to the git askpass helper (sibling script). Token is delivered via env,
# never on the command line or in the URL.
_ASKPASS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "git_askpass.sh")


def _auth_env(token):
    """Env vars that carry the GitLab credentials for the askpass helper."""
    user = os.environ.get("GITLAB_USER", "gitlab-ci-token")
    return {
        "GIT_ASKPASS": _ASKPASS_PATH,
        "GIT_TERMINAL_PROMPT": "0",
        "CR_GITLAB_USER": user,
        "CR_GITLAB_TOKEN": token or "",
    }


def git_cmd(subcmd, token, cwd=None, timeout=600):
    """
    Run a git command, authenticating via a GIT_ASKPASS helper so the token is
    never on the command line (no `-c http.extraheader`, no URL embedding), which
    keeps it out of `ps`, `git remote -v`, config, and process-log capture.

    subcmd: list starting after `git`, e.g. ["clone", "--branch", x, url, dir]
    Returns (returncode, stdout, stderr).
    """
    cmd = [GIT_PATH] + subcmd
    env = os.environ.copy()
    if token:
        env.update(_auth_env(token))
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout, env=env
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def prepare_repo(repo_url, branch, base_branch, workspace, issue_key, cache=True, mr_url="", gitlab_token=""):
    """
    Clone (or fetch) repo, checkout branch, return path and diff info.
    Returns dict with: diff_text, changed_files, insertions, deletions, commit_log, branch_exists, branch_merged
    """
    repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")
    repo_dir = os.path.join(workspace, repo_name)
    branch_exists = True
    branch_merged = False

    # Always convert SSH→HTTPS for GitLab, keep the URL clean (no token).
    # Auth is injected per-command via git_cmd() — never embedded in the URL.
    if repo_url.startswith("git@"):
        https_url = ssh_to_https(repo_url)
        print(f"[git] Converting to HTTPS: {https_url}", flush=True)
        os.environ.setdefault("GIT_SSH_COMMAND", "/bin/false")
        repo_url = https_url

    if not gitlab_token:
        gitlab_token = _resolve_git_token()
    if not gitlab_token:
        print(f"[git] WARNING: No GITLAB_TOKEN set, clone may fail", flush=True)

    # If MR URL is available, fetch diff directly from GitLab API (most reliable)
    if mr_url and gitlab_token:
        print(f"[gitlab] Fetching MR diff from {mr_url}", flush=True)
        mr_diff = get_mr_diff_from_gitlab(mr_url, gitlab_token)
        if mr_diff and mr_diff["diff_text"]:
            print(f"[gitlab] Using MR diff from GitLab API ({len(mr_diff['changed_files'])} files)", flush=True)
            return {
                "diff_text": mr_diff["diff_text"],
                "changed_files": mr_diff["changed_files"],
                "stats": f"{len(mr_diff['changed_files'])} files changed",
                "commit_log": f"MR diff from {mr_url}",
                "branch_exists": True,
                "branch_merged": False,
                "repo_dir": repo_dir,
            }
        print(f"[gitlab] MR diff unavailable, falling back to git clone/diff", flush=True)

    tok = gitlab_token
    if cache and os.path.isdir(repo_dir):
        print(f"[git] Updating cached repo: {repo_name}")
        git_cmd(["fetch", "origin"], tok, repo_dir, timeout=300)
        # Explicitly fetch the review branch to ensure it's up to date
        git_cmd(["fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"], tok, repo_dir, timeout=60)
        git_cmd(["reset", "--hard", f"origin/{branch}"], tok, repo_dir)
        rc, _, _ = git_cmd(["checkout", branch], tok, repo_dir)
        if rc != 0:
            rc, _, _ = git_cmd(["checkout", "-b", branch, f"origin/{branch}"], tok, repo_dir)
        if rc != 0:
            branch_exists = False
            git_cmd(["checkout", base_branch], tok, repo_dir)
            if "/" in base_branch:
                git_cmd(["fetch", "origin", f"+refs/heads/{base_branch}:refs/remotes/origin/{base_branch}"], tok, repo_dir)
            else:
                git_cmd(["fetch", "origin", base_branch], tok, repo_dir)
            git_cmd(["reset", "--hard", f"origin/{base_branch}"], tok, repo_dir)
            rc, _, _ = git_cmd(["checkout", "-b", branch, f"origin/{branch}"], tok, repo_dir)
    else:
        if os.path.isdir(repo_dir):
            run_git(["rm", "-rf", repo_dir], "/tmp")
        print(f"[git] Cloning repo: {repo_name}")
        rc, out, err = git_cmd(
            ["clone", "--branch", branch, repo_url, repo_dir],
            tok, "/tmp", timeout=600
        )
        if rc != 0:
            print(f"[git] Clone failed for '{branch}': {err[:300]}", flush=True)
            branch_exists = False
            # Branch may not exist remotely — clone default branch
            rc, out, err = git_cmd(
                ["clone", repo_url, repo_dir],
                tok, "/tmp", timeout=600
            )
            if os.path.isdir(repo_dir):
                git_cmd(["checkout", "-b", branch, f"origin/{branch}"], tok, repo_dir, timeout=30)

    # Ensure base_branch ref is available
    # Handle branch names containing "/" (e.g., "rage/master") by using explicit refspec
    if "/" in base_branch:
        fetch_ref = f"+refs/heads/{base_branch}:refs/remotes/origin/{base_branch}"
        git_cmd(["fetch", "origin", fetch_ref], tok, repo_dir)
    else:
        git_cmd(["fetch", "origin", base_branch], tok, repo_dir)
    # Get merge-base for accurate diff
    rc, merge_base, _ = git_cmd(
        ["merge-base", branch, f"origin/{base_branch}"], tok, repo_dir
    )
    if rc != 0:
        print("[git] merge-base failed, falling back to origin/base")
        merge_base = f"origin/{base_branch}"

    # Generate diff
    rc, diff_text, _ = git_cmd(
        ["diff", merge_base + "..." + branch, "--", "."], tok, repo_dir
    )
    if not diff_text:
        # Try direct diff
        rc, diff_text, _ = git_cmd(
            ["diff", f"origin/{base_branch}...{branch}", "--", "."], tok, repo_dir
        )

    # Changed files list
    rc, changed_files_str, _ = git_cmd(
        ["diff", "--name-status", f"origin/{base_branch}...{branch}"], tok, repo_dir
    )
    changed_files = [line for line in changed_files_str.split("\n") if line.strip()]

    # Stats
    rc, stats_str, _ = git_cmd(
        ["diff", "--shortstat", f"origin/{base_branch}...{branch}"], tok, repo_dir
    )

    # Commit log
    rc, commit_log, _ = git_cmd(
        ["log", f"origin/{base_branch}..{branch}", "--oneline", "--no-decorate"], tok, repo_dir
    )

    # Detect if branch is merged (branch exists but no new commits vs base)
    if not diff_text and branch_exists:
        rc, merge_base_commit, _ = git_cmd(
            ["merge-base", branch, f"origin/{base_branch}"], tok, repo_dir
        )
        rc, head_commit, _ = git_cmd(
            ["rev-parse", branch], tok, repo_dir
        )
        if merge_base_commit == head_commit:
            branch_merged = True
            print(f"[git] Branch '{branch}' is fully merged into '{base_branch}' (HEAD at merge-base)", flush=True)

    # Clean any real token from the cached remote URL (in case a legacy clone persisted `https://user:token@host`).
    # This prevents the token from leaking via `git remote -v` / logs going forward.
    try:
        rc, origin_url, _ = run_git([GIT_PATH, "remote", "get-url", "origin"], repo_dir)
        if rc == 0 and origin_url and re.search(r'https://[^/@]+:[^/@]*@', origin_url):
            clean = re.sub(r'https://[^/@]+:[^/@]*@', 'https://', origin_url)
            run_git([GIT_PATH, "remote", "set-url", "origin", clean], repo_dir)
            print("[git] Sanitized embedded credentials from cached origin URL", flush=True)
    except Exception:
        pass

    return {
        "diff_text": diff_text,
        "changed_files": changed_files,
        "stats": stats_str,
        "commit_log": commit_log[:5000] if len(commit_log) > 5000 else commit_log,
        "branch_exists": branch_exists,
        "branch_merged": branch_merged,
        "repo_dir": repo_dir,
    }


def _split_diff_by_files(diff_text):
    """
    Split a `git diff` output into per-file blocks, preserving file boundaries.

    Returns list of block strings. Each block starts with its `diff --git` header
    and includes every hunk that belongs to it.
    """
    if not diff_text:
        return []
    parts = diff_text.split("\ndiff --git ")
    blocks = []
    for i, part in enumerate(parts):
        part = part.strip("\n")
        if not part:
            continue
        if i == 0:
            blocks.append(part)
        else:
            blocks.append("diff --git " + part)
    return blocks


def _group_into_batches(blocks, max_batch_chars):
    """
    Group file blocks into batches, each staying under max_batch_chars
    (a single oversized file becomes its own batch, possibly further truncated).
    Returns list of batches; each batch is a string.
    """
    batches = []
    cur = []
    cur_len = 0
    for block in blocks:
        block_len = len(block)
        # If a single block alone exceeds the budget, flush it as its own batch
        # (truncation happens later, per batch, in the caller).
        if cur and cur_len + block_len > max_batch_chars:
            batches.append("\n".join(cur))
            cur = []
            cur_len = 0
        cur.append(block)
        cur_len += block_len
    if cur:
        batches.append("\n".join(cur))
    return batches


def _count_severities(review_text):
    """Count findings by severity heading patterns, with emoji fallback."""
    critical = len(re.findall(r'🔴\s*(?:Critical|关键)', review_text))
    warning = len(re.findall(r'🟡\s*(?:Warning|警告)', review_text))
    suggestion = len(re.findall(r'ℹ️?\s*(?:Suggestion|建议)', review_text))
    if critical == 0 and warning == 0 and suggestion == 0:
        critical = review_text.count("🔴")
        warning = review_text.count("🟡")
        suggestion = review_text.count("ℹ️")
    return {"critical": critical, "warning": warning, "suggestion": suggestion}


def _call_llm_batch(system_prompt, user_prompt, api_key, base_url, model,
                    max_output_tokens):
    """
    Single LLM API call. Returns (review_text, error_message).
    error_message is None on success.
    """
    payload = json.dumps({
        "model": model,
        "max_tokens": max_output_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }).encode("utf-8")

    # Use the shared retry-enabled HTTP helper: the LLM call is the most
    # latency/transience-prone step, so give it extra attempts and a long timeout.
    result = http_request(
        "POST",
        f"{base_url}/v1/messages",
        raw_body=payload.decode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        timeout=180,
        retries=3,
        backoff=2.0,
    )
    if result is None:
        return None, "LLM API request failed after retries"

    # Extract response text
    try:
        if "content" in result:
            review_text = "".join(
                block.get("text", "") for block in result["content"]
                if block.get("type") == "text"
            )
        else:
            review_text = result.get("completion", json.dumps(result))
    except Exception:
        review_text = json.dumps(result)
    return review_text, None


def review_with_claude(diff_info, config, project, issue_key, repo_type):
    """
    Call LLM API to review the diff.

    For large diffs, the input is split into multiple per-file batches so every
    changed file is covered (no silent trailing truncation). Batch results are
    aggregated into a single review_text and summed severity counts.
    """
    max_batch_chars = 60000   # input chars per LLM call (token-adjacent budget)
    max_diff_total = 400000   # hard cap on total diff processed (defensive)

    if not diff_info["diff_text"]:
        return {
            "summary": "No diff found — no changes or branch up to date with base.",
            "findings": [],
            "severity_counts": {},
            "error": None,
        }

    diff_text = diff_info["diff_text"]

    api_key = (os.environ.get("ANTHROPIC_AUTH_TOKEN") or
               os.environ.get("ANTHROPIC_API_KEY") or "")
    if not api_key:
        return {"summary": "ANTHROPIC_AUTH_TOKEN not set", "findings": [], "severity_counts": {}}

    base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    model = config["claude"]["model"]
    max_output_tokens = config["claude"]["max_tokens"]
    system_prompt = config["claude"]["review_instructions"] \
                    or DEFAULT_REVIEW_INSTRUCTIONS.strip()

    # Split into per-file blocks and group into batches
    blocks = _split_diff_by_files(diff_text)
    if not blocks:
        blocks = [diff_text]
    batches = _group_into_batches(blocks, max_batch_chars)

    # Defensive: even batches over budget get truncated per-batch, but cap the
    # total amount of diff actually sent to avoid runaway token cost.
    if len(diff_text) > max_diff_total:
        diff_text = diff_text[:max_diff_total] + f"\n\n... [truncated, original {len(diff_text)} chars]"
        blocks = _split_diff_by_files(diff_text)
        batches = _group_into_batches(blocks, max_batch_chars)
        print(f"[review] Diff exceeds {max_diff_total} chars; reviewing truncated view", flush=True)

    changed_files = diff_info.get("changed_files") or []
    commits = diff_info.get("commit_log") or ""
    num_batches = len(batches)
    print(f"[review] Splitting into {num_batches} batch(es)", flush=True)

    all_text = []
    total = {"critical": 0, "warning": 0, "suggestion": 0}
    first_error = None

    for idx, batch_diff in enumerate(batches, start=1):
        # A single batch may still exceed the budget (e.g. one giant file) —
        # truncate per-batch so a record-breaking file cannot blow the token cap.
        truncated_flag = False
        original_len = len(batch_diff)
        if original_len > max_batch_chars:
            batch_diff = batch_diff[:max_batch_chars] + \
                f"\n\n... [truncated, this batch original {original_len} chars]"
            truncated_flag = True

        if num_batches > 1:
            user_prompt = f"""Project: {project} ({repo_type} repository)
Issue: {issue_key}
Review part {idx} of {num_batches}{' (truncated)' if truncated_flag else ''}.

Commits in this branch:
{commits}

Diff (part {idx}):
```diff
{batch_diff}
```

Review the code in this part. For each finding, provide:
- **Severity**: 🔴 Critical / 🟡 Warning / ℹ️ Suggestion
- **File**: the file path
- **Issue**: what the problem is
- **Suggestion**: how to fix it

At the end of THIS part, give a count of each severity level for the findings in this part.

IMPORTANT: Reply in Chinese (中文). Keep it concise — focus on the most critical issues only."""
        else:
            user_prompt = f"""Project: {project} ({repo_type} repository)
Issue: {issue_key}

Changed files:
{chr(10).join(changed_files)}

Commits in this branch:
{commits}

Diff:
```diff
{batch_diff}
```

Please review this code change. For each finding, provide:
- **Severity**: 🔴 Critical / 🟡 Warning / ℹ️ Suggestion
- **File**: the file path
- **Issue**: what the problem is
- **Suggestion**: how to fix it

At the end, provide a summary with count of each severity level.

IMPORTANT: Reply in Chinese (中文). Keep it concise — focus on the most critical issues only."""

        review_text, err = _call_llm_batch(
            system_prompt, user_prompt, api_key, base_url, model, max_output_tokens
        )
        if err:
            if not first_error:
                first_error = err
            print(f"[review] Batch {idx}/{num_batches} failed: {err}", flush=True)
            continue

        if num_batches > 1:
            all_text.append(f"### 第 {idx}/{num_batches} 部分\n{review_text}")
        else:
            all_text.append(review_text)

        counts = _count_severities(review_text)
        for k in total:
            total[k] += counts[k]

    if not all_text and first_error:
        # Every batch failed — surface the error as the result.
        # review_text stays empty so the renderer shows "❌ 审查失败"
        # (a non-empty review_text would be mistaken for partial results).
        return {
            "summary": f"API error: {first_error}",
            "review_text": "",
            "severity_counts": {},
            "error": first_error,
        }

    return {
        "summary": "",
        "review_text": "\n\n".join(all_text),
        "severity_counts": total,
        "error": first_error,   # non-None if at least one batch failed (partial results)
    }




def main():
    parser = argparse.ArgumentParser(description="Code Review via Claude API")
    parser.add_argument("--repo", required=True, help="Git repository URL")
    parser.add_argument("--branch", required=True, help="Branch to review")
    parser.add_argument("--base-branch", default="main", help="Base branch for diff")
    parser.add_argument("--project", required=True, help="Project name (EV/CB2/Mars/Rage)")
    parser.add_argument("--issue-key", required=True, help="Jira issue key")
    parser.add_argument("--repo-type", choices=["engine", "game"], default="engine",
                        help="Repository type")
    parser.add_argument("--workspace", default="/tmp/codereview-workspace",
                        help="Workspace directory")
    parser.add_argument("--output", help="Write result JSON to file")
    parser.add_argument("--mr-url", default="", help="Merge request URL")
    args = parser.parse_args()

    config = load_config()
    os.makedirs(args.workspace, exist_ok=True)

    # Prepare repo and get diff
    print(f"[{args.repo_type}] Cloning/preparing repo...", flush=True)
    gitlab_token = os.environ.get("GITLAB_TOKEN") or os.environ.get("CI_JOB_TOKEN", "")
    diff_info = prepare_repo(
        args.repo, args.branch, args.base_branch,
        args.workspace, args.issue_key,
        mr_url=args.mr_url, gitlab_token=gitlab_token,
    )

    changed_file_count = len([f for f in diff_info["changed_files"] if f])
    print(f"[{args.repo_type}] Diff: {changed_file_count} files changed", flush=True)

    if not diff_info["diff_text"]:
        result = {
            "project": args.project,
            "issue_key": args.issue_key,
            "repo_type": args.repo_type,
            "branch": args.branch,
            "base_branch": args.base_branch,
            "mr_url": args.mr_url or "",
            "changed_files": diff_info["changed_files"],
            "stats": diff_info["stats"],
            "branch_exists": diff_info["branch_exists"],
            "branch_merged": diff_info["branch_merged"],
            "review": {
                "summary": "No changes to review — branch is up to date with base.",
                "findings": [],
                "severity_counts": {},
            },
        }
    else:
        # Code review via Claude
        print(f"[{args.repo_type}] Sending to Claude API for review...", flush=True)
        review_result = review_with_claude(
            diff_info, config, args.project, args.issue_key, args.repo_type
        )
        result = {
            "project": args.project,
            "issue_key": args.issue_key,
            "repo_type": args.repo_type,
            "branch": args.branch,
            "base_branch": args.base_branch,
            "mr_url": args.mr_url or "",
            "changed_files": diff_info["changed_files"],
            "stats": diff_info["stats"],
            "commits": diff_info["commit_log"],
            "branch_exists": diff_info["branch_exists"],
            "branch_merged": diff_info["branch_merged"],
            "review": review_result,
        }

    output_json = json.dumps(result, indent=2, ensure_ascii=False)

    if args.output:
        with open(args.output, "w") as f:
            f.write(output_json)
        print(f"[{args.repo_type}] Results written to {args.output}", flush=True)

    # Always print JSON result to stdout for Jenkins consumption
    print(output_json)


if __name__ == "__main__":
    main()
