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

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

import shutil
import re

# Resolve git path explicitly (agent may not have it on PATH)
GIT_PATH = shutil.which("git") or "/usr/bin/git"


DEFAULT_REVIEW_INSTRUCTIONS = """
You are a senior game engine engineer reviewing a merge request.

## Review Focus Areas
1. Logic correctness and potential bugs (race conditions, null pointers, type errors)
2. Memory safety and resource management (leaks, dangling pointers, ownership)
3. Concurrency and thread safety (shared state, locks, data races)
4. Performance issues (unnecessary allocations, hot path optimization, algorithm complexity)
5. API design and interfaces (consistency, backward compatibility, encapsulation)
6. Error handling and edge cases (unhandled errors, invalid inputs, boundary conditions)
7. Code style and maintainability (readability, naming, complexity, duplication)
8. Security concerns (input validation, privilege escalation, data exposure)
9. Testing coverage (missing tests, untestable code, testability)
10. Logging and observability (useful error messages, debugability)

## Output Format
Group findings by severity level. For each finding, use this exact format:
- **Severity**: 🔴 Critical / 🟡 Warning / ℹ️ Suggestion
- **File**: path/to/file
- **Issue**: what the problem is
- **Suggestion**: how to fix it

Number findings sequentially and continuously across ALL severity groups (e.g., if there are 3 critical items and 2 warnings, the last warning should be #5).

## Summary
At the very end, provide a summary table:

## Summary
| Severity | Count |
|----------|-------|
| 🔴 Critical | X |
| 🟡 Warning | X |
| ℹ️ Suggestion | X |
| **Total** | **X** |

IMPORTANT: The number of findings listed under each severity level MUST exactly match the count in the summary table. The Total MUST equal the sum of the three counts.
"""


def load_config():
    """Return minimal config (no YAML needed)."""
    return {
        "claude": {
            "model": os.environ.get("ANTHROPIC_MODEL", "deepseek-v4-flash"),
            "max_tokens": 8192,
            "review_instructions": DEFAULT_REVIEW_INSTRUCTIONS.strip(),
        }
    }


def run_git(cmd, cwd, timeout=120):
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


def prepare_repo(repo_url, branch, base_branch, workspace, issue_key, cache=True):
    """
    Clone (or fetch) repo, checkout branch, return path and diff info.
    Returns dict with: diff_text, changed_files, insertions, deletions, commit_log
    """
    repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")
    repo_dir = os.path.join(workspace, repo_name)

    # Always convert SSH→HTTPS and use token auth for GitLab
    if repo_url.startswith("git@"):
        https_url = ssh_to_https(repo_url)
        print(f"[git] Converting to HTTPS: {https_url}", flush=True)
        gitlab_user = os.environ.get("GITLAB_USER", "gitlab-ci-token")
        gitlab_token = os.environ.get("GITLAB_TOKEN") or os.environ.get("CI_JOB_TOKEN", "")
        if gitlab_token:
            https_url = https_url.replace("https://", f"https://{gitlab_user}:{gitlab_token}@")
            print(f"[git] Using token auth for GitLab", flush=True)
        else:
            print(f"[git] WARNING: No GITLAB_TOKEN set, clone may fail", flush=True)
            https_url = https_url.replace("https://", "https://git:@")
        os.environ.setdefault("GIT_SSH_COMMAND", "/bin/false")
        repo_url = https_url

    if cache and os.path.isdir(repo_dir):
        print(f"[git] Updating cached repo: {repo_name}")
        run_git([GIT_PATH, "fetch", "--all"], repo_dir)
        run_git([GIT_PATH, "reset", "--hard"], repo_dir)
        rc, _, _ = run_git([GIT_PATH, "checkout", branch], repo_dir)
        if rc != 0:
            rc, _, _ = run_git([GIT_PATH, "checkout", "-b", branch, f"origin/{branch}"], repo_dir)
        if rc != 0:
            run_git([GIT_PATH, "checkout", base_branch], repo_dir)
            run_git([GIT_PATH, "pull", "origin", base_branch], repo_dir)
            rc, _, _ = run_git([GIT_PATH, "checkout", "-b", branch, f"origin/{branch}"], repo_dir)
    else:
        if os.path.isdir(repo_dir):
            run_git(["rm", "-rf", repo_dir], "/tmp")
        print(f"[git] Cloning repo: {repo_name}")
        rc, out, err = run_git(
            [GIT_PATH, "clone", "--branch", branch, "--single-branch", repo_url, repo_dir],
            "/tmp", timeout=300
        )
        if rc != 0:
            print(f"[git] Clone failed for '{branch}': {err[:300]}", flush=True)
            # Branch may not exist remotely — clone default branch
            rc, out, err = run_git(
                [GIT_PATH, "clone", "--single-branch", repo_url, repo_dir],
                "/tmp", timeout=300
            )
            if rc != 0:
                print(f"[git] Fallback clone also failed: {err[:300]}", flush=True)
            if os.path.isdir(repo_dir):
                run_git([GIT_PATH, "checkout", "-b", branch, f"origin/{branch}"], repo_dir, timeout=30)

    # Ensure base_branch ref is available
    run_git([GIT_PATH, "fetch", "origin", base_branch], repo_dir)
    # Get merge-base for accurate diff
    rc, merge_base, _ = run_git(
        [GIT_PATH, "merge-base", branch, f"origin/{base_branch}"], repo_dir
    )
    if rc != 0:
        print("[git] merge-base failed, falling back to origin/base")
        merge_base = f"origin/{base_branch}"

    # Generate diff
    rc, diff_text, _ = run_git(
        [GIT_PATH, "diff", merge_base + "..." + branch, "--", "."], repo_dir
    )
    if not diff_text:
        # Try direct diff
        rc, diff_text, _ = run_git(
            [GIT_PATH, "diff", f"origin/{base_branch}...{branch}", "--", "."], repo_dir
        )

    # Changed files list
    rc, changed_files_str, _ = run_git(
        [GIT_PATH, "diff", "--name-status", f"origin/{base_branch}...{branch}", "--", "."], repo_dir
    )
    changed_files = [line for line in changed_files_str.split("\n") if line.strip()]

    # Stats
    rc, stats_str, _ = run_git(
        [GIT_PATH, "diff", "--shortstat", f"origin/{base_branch}...{branch}", "--", "."], repo_dir
    )

    # Commit log
    rc, commit_log, _ = run_git(
        [GIT_PATH, "log", f"origin/{base_branch}..{branch}", "--oneline", "--no-decorate"], repo_dir
    )

    return {
        "diff_text": diff_text,
        "changed_files": changed_files,
        "stats": stats_str,
        "commit_log": commit_log[:5000] if len(commit_log) > 5000 else commit_log,
        "repo_dir": repo_dir,
    }


def review_with_claude(diff_info, config, project, issue_key, repo_type):
    """
    Call LLM API to review the diff (using urllib, no external deps).
    Returns structured review results.
    """
    if not diff_info["diff_text"]:
        return {
            "summary": "No diff found — no changes or branch up to date with base.",
            "findings": [],
            "severity_counts": {},
            "error": None,
        }

    diff_text = diff_info["diff_text"]
    max_diff_chars = 80000
    if len(diff_text) > max_diff_chars:
        diff_text = diff_text[:max_diff_chars] + f"\n\n... [truncated, original {len(diff_text)} chars]"

    api_key = (os.environ.get("ANTHROPIC_AUTH_TOKEN") or
               os.environ.get("ANTHROPIC_API_KEY") or "")
    if not api_key:
        return {"summary": "ANTHROPIC_AUTH_TOKEN not set", "findings": [], "severity_counts": {}}

    base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    model = os.environ.get("ANTHROPIC_MODEL") or "deepseek-v4-flash"

    system_prompt = DEFAULT_REVIEW_INSTRUCTIONS.strip()

    user_prompt = f"""Project: {project} ({repo_type} repository)
Issue: {issue_key}

Changed files:
{chr(10).join(diff_info["changed_files"])}

Commits in this branch:
{diff_info["commit_log"]}

Diff:
```diff
{diff_text}
```

Please review this code change. For each finding, provide:
- **Severity**: 🔴 Critical / 🟡 Warning / ℹ️ Suggestion
- **File**: the file path
- **Issue**: what the problem is
- **Suggestion**: how to fix it

At the end, provide a summary with count of each severity level."""

    payload = json.dumps({
        "model": model,
        "max_tokens": 8192,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{base_url}/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")[:500]
        return {
            "summary": f"API error: HTTP {e.code}",
            "review_text": f"HTTP {e.code}: {error_body}",
            "severity_counts": {},
            "error": f"HTTP {e.code}: {error_body}",
        }
    except Exception as e:
        return {
            "summary": f"API error: {e}",
            "review_text": None,
            "severity_counts": {},
            "error": str(e),
        }

    # Extract response content
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

    # Count findings by severity heading patterns (more accurate than emoji count)
    critical = len(re.findall(r'🔴\s*(?:Critical|关键)', review_text))
    warning = len(re.findall(r'🟡\s*(?:Warning|警告)', review_text))
    suggestion = len(re.findall(r'ℹ️?\s*(?:Suggestion|建议)', review_text))

    # Fallback to emoji counting if pattern didn't match
    if critical == 0 and warning == 0 and suggestion == 0:
        critical = review_text.count("🔴")
        warning = review_text.count("🟡")
        suggestion = review_text.count("ℹ️")

    return {
        "summary": "",
        "review_text": review_text,
        "severity_counts": {
            "critical": critical,
            "warning": warning,
            "suggestion": suggestion,
        },
        "error": None,
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
    diff_info = prepare_repo(
        args.repo, args.branch, args.base_branch,
        args.workspace, args.issue_key
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
