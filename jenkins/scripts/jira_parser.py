#!/usr/bin/env python3
"""
Jira Parser — Extract issue key, project, and linked MR/branch info from a Jira URL.

Outputs JSON to stdout for Jenkins pipeline consumption.

Usage:
    python3 jira_parser.py --jira-url "https://jira.company/browse/EV-123" \
                           --jira-token "xxx" \
                           --jira-host "https://jira.company"
"""
import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error


# ── Built-in project config (no external file dependency) ────────────────────

PROJECT_CONFIG = {
    "EV": {
        "jira_project_key": "EV",
        "name": "EngineVerse",
        "engine_repo": "git@gitlab.booming-inc.com:booming/dev/chaos.git",
        "game_repo": "git@gitlab.booming-inc.com:booming/dev/engine/cb-engine-verify.git",
        "default_branch": "main",
    },
    "CB2": {
        "jira_project_key": "CB2",
        "name": "CB2",
        "engine_repo": "git@gitlab.booming-inc.com:booming/dev/projects/conquerorsblade2/chaos-cb-2.git",
        "game_repo": "git@gitlab.booming-inc.com:booming/dev/projects/conquerorsblade2/conquerors-blade-2.git",
        "default_branch": "main",
    },
    "Mars": {
        "jira_project_key": "MARS",
        "name": "Mars",
        "engine_repo": "git@gitlab.booming-inc.com:booming/dev/projects/mars/chaos-mars.git",
        "game_repo": "git@gitlab.booming-inc.com:booming/dev/projects/mars/mars.git",
        "default_branch": "main",
    },
    "Rage": {
        "jira_project_key": "RAGE",
        "name": "Rage",
        "engine_repo": "git@gitlab.booming-inc.com:booming/dev/projects/rage/chaos.git",
        "game_repo": "git@gitlab.booming-inc.com:booming/dev/projects/rage/rage.git",
        "default_branch": "main",
    },
}

# ── Helpers ──────────────────────────────────────────────────────────────────

def load_config():
    """Return built-in project config (no YAML dependency)."""
    return {"projects": PROJECT_CONFIG}


def jira_request(path, host, token):
    """Make an authenticated Jira API request.
    Tries Bearer first, then Basic auth if Bearer fails.
    """
    url = f"{host.rstrip('/')}/rest/{path}"
    headers = {"Accept": "application/json"}

    # Try 1: Bearer token (PAT)
    req = urllib.request.Request(url, headers={**headers, "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403):
            return None

    # Try 2: Basic auth (in case token is pre-encoded base64 of user:apitoken)
    req = urllib.request.Request(url, headers={**headers, "Authorization": f"Basic {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return None
    except Exception as e:
        return None


def extract_issue_key(url):
    """Extract Jira issue key from URL like https://jira.company/browse/EV-123"""
    m = re.search(r'(?:browse|issues)/([A-Za-z]+-\d+)', url)
    return m.group(1) if m else None


def identify_project(issue_key, config):
    """Map issue key prefix to project config entry."""
    prefix = issue_key.split('-')[0].upper()
    for proj_id, proj_cfg in config["projects"].items():
        if proj_cfg["jira_project_key"].upper() == prefix:
            return proj_id, proj_cfg
    return None, None


def get_dev_info(issue_key, host, token):
    """Try Jira Dev Status API for linked branches and PRs."""
    path = f"dev-status/latest/issue/{issue_key}"
    data = jira_request(path, host, token)
    if not data:
        return {"branches": [], "pull_requests": []}

    result = {"branches": [], "pull_requests": []}
    for detail in data.get("detail", []):
        for branch in detail.get("branches", []):
            result["branches"].append({
                "name": branch.get("name", ""),
                "url": branch.get("url", ""),
                "repo": branch.get("repository", ""),
            })
        for pr in detail.get("pullRequests", []):
            result["pull_requests"].append({
                "title": pr.get("name", ""),
                "url": pr.get("url", ""),
                "branch": pr.get("sourceBranch", ""),
                "target_branch": pr.get("destinationBranch", ""),
                "status": pr.get("status", ""),
            })
    return result


def guess_branch_from_issue(issue_key, config, host, token):
    """
    Fallback: try to get the issue summary and guess branch name,
    OR search for branches via Jira API.
    """
    # Try to fetch issue details — custom fields may hold PR links
    issue_data = jira_request(f"api/3/issue/{issue_key}", host, token)
    if not issue_data:
        return None

    summary = issue_data.get("fields", {}).get("summary", "")
    return {
        "summary": summary,
        "description": issue_data.get("fields", {}).get("description", ""),
        "status": issue_data.get("fields", {}).get("status", {}).get("name", ""),
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Parse Jira URL and extract info")
    parser.add_argument("--jira-url", required=True, help="Full Jira issue URL")
    parser.add_argument("--jira-host", default=os.environ.get("JIRA_HOST", ""),
                        help="Jira host (e.g. https://jira.company)")
    parser.add_argument("--jira-token", default=os.environ.get("JIRA_TOKEN", ""),
                        help="Jira API token or password")
    args = parser.parse_args()

    config = load_config()

    # Step 1: Extract issue key
    issue_key = extract_issue_key(args.jira_url)
    if not issue_key:
        print(json.dumps({"error": "Could not extract Jira issue key from URL"}))
        sys.exit(1)

    # Step 2: Identify project
    project_id, project_cfg = identify_project(issue_key, config)
    if not project_id:
        print(json.dumps({"error": f"Unknown project for issue key: {issue_key}"}))
        sys.exit(1)

    # Step 3: Try to get dev info (branches/PRs linked in Jira)
    result = {
        "issue_key": issue_key,
        "project": project_id,
        "project_name": project_cfg["name"],
        "engine_repo": project_cfg["engine_repo"],
        "game_repo": project_cfg["game_repo"],
        "default_branch": project_cfg["default_branch"],
        "mr_info": None,
        "issue_info": None,
    }

    if args.jira_host and args.jira_token:
        dev_info = get_dev_info(issue_key, args.jira_host, args.jira_token)
        if dev_info.get("pull_requests"):
            # Use the first open PR
            for pr in dev_info["pull_requests"]:
                if pr["status"] in ("OPEN", "open"):
                    result["mr_info"] = pr
                    break
            if not result["mr_info"]:
                result["mr_info"] = dev_info["pull_requests"][0]
        elif dev_info.get("branches"):
            result["mr_info"] = {
                "branch": dev_info["branches"][0]["name"],
                "target_branch": project_cfg["default_branch"],
            }

    # Step 4: Fallback — fetch issue details
    if args.jira_host and args.jira_token and not result.get("mr_info"):
        issue_data = guess_branch_from_issue(issue_key, config, args.jira_host, args.jira_token)
        if issue_data:
            result["issue_info"] = issue_data

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
