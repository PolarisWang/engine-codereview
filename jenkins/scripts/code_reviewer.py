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

import anthropic
import yaml


def load_config():
    """Load config.yaml (from repo root: engine-codereview/config.yaml)."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(script_dir, "..", ".."))
    config_path = os.path.join(repo_root, "config.yaml")
    with open(config_path) as f:
        return yaml.safe_load(f)


def run_git(cmd, cwd, timeout=120):
    """Run a git command, return (returncode, stdout, stderr)."""
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def prepare_repo(repo_url, branch, base_branch, workspace, issue_key, cache=True):
    """
    Clone (or fetch) repo, checkout branch, return path and diff info.
    Returns dict with: diff_text, changed_files, insertions, deletions, commit_log
    """
    repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")
    repo_dir = os.path.join(workspace, repo_name)

    if cache and os.path.isdir(repo_dir):
        print(f"[git] Updating cached repo: {repo_name}")
        run_git(["git", "fetch", "--all"], repo_dir)
        run_git(["git", "reset", "--hard"], repo_dir)
        rc, _, _ = run_git(["git", "checkout", branch], repo_dir)
        if rc != 0:
            rc, _, _ = run_git(["git", "checkout", "-b", branch, f"origin/{branch}"], repo_dir)
        if rc != 0:
            run_git(["git", "checkout", base_branch], repo_dir)
            run_git(["git", "pull", "origin", base_branch], repo_dir)
            rc, _, _ = run_git(["git", "checkout", "-b", branch, f"origin/{branch}"], repo_dir)
    else:
        if os.path.isdir(repo_dir):
            run_git(["rm", "-rf", repo_dir], "/tmp")
        print(f"[git] Cloning repo: {repo_name}")
        rc, out, err = run_git(
            ["git", "clone", "--branch", branch, "--single-branch", repo_url, repo_dir],
            "/tmp", timeout=300
        )
        if rc != 0:
            # Branch may not exist remotely yet — clone default and checkout
            run_git(["git", "clone", "--single-branch", repo_url, repo_dir], "/tmp", timeout=300)
            run_git(["git", "checkout", "-b", branch, f"origin/{branch}"], repo_dir)

    # Ensure base_branch ref is available
    run_git(["git", "fetch", "origin", base_branch], repo_dir)
    # Get merge-base for accurate diff
    rc, merge_base, _ = run_git(
        ["git", "merge-base", branch, f"origin/{base_branch}"], repo_dir
    )
    if rc != 0:
        print("[git] merge-base failed, falling back to origin/base")
        merge_base = f"origin/{base_branch}"

    # Generate diff
    rc, diff_text, _ = run_git(
        ["git", "diff", merge_base + "..." + branch, "--", "."], repo_dir
    )
    if not diff_text:
        # Try direct diff
        rc, diff_text, _ = run_git(
            ["git", "diff", f"origin/{base_branch}...{branch}", "--", "."], repo_dir
        )

    # Changed files list
    rc, changed_files_str, _ = run_git(
        ["git", "diff", "--name-status", f"origin/{base_branch}...{branch}", "--", "."], repo_dir
    )
    changed_files = [line for line in changed_files_str.split("\n") if line.strip()]

    # Stats
    rc, stats_str, _ = run_git(
        ["git", "diff", "--shortstat", f"origin/{base_branch}...{branch}", "--", "."], repo_dir
    )

    # Commit log
    rc, commit_log, _ = run_git(
        ["git", "log", f"origin/{base_branch}..{branch}", "--oneline", "--no-decorate"], repo_dir
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
    Call Claude API to review the diff.
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
    # Truncate diff if too large (Claude context limit)
    max_diff_chars = 80000
    if len(diff_text) > max_diff_chars:
        diff_text = diff_text[:max_diff_chars] + f"\n\n... [truncated, original {len(diff_text)} chars]"

    claude_cfg = config.get("claude", {})
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"summary": "ANTHROPIC_API_KEY not set", "findings": [], "severity_counts": {}}

    client = anthropic.Anthropic(api_key=api_key)

    system_prompt = claude_cfg.get("review_instructions", "Review this code diff.")

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
- **Line**: approximate line number (if applicable)
- **Issue**: what the problem is
- **Suggestion**: how to fix it

At the end, provide a summary with count of each severity level."""

    try:
        response = client.messages.create(
            model=claude_cfg.get("model", "claude-sonnet-4-6-20250610"),
            max_tokens=claude_cfg.get("max_tokens", 8192),
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        review_text = response.content[0].text

        # Rough severity counting
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
    except Exception as e:
        return {
            "summary": f"Claude API error: {e}",
            "review_text": None,
            "severity_counts": {},
            "error": str(e),
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
