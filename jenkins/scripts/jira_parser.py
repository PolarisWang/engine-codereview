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
import urllib.parse


# ── Built-in project config (no external file dependency) ────────────────────

PROJECT_CONFIG = {
    "EV": {
        "jira_project_key": "EV",
        "name": "EngineVerse",
        "engine_repo": "git@gitlab.booming-inc.com:booming/dev/chaos.git",
        "game_repo": "git@gitlab.booming-inc.com:booming/dev/engine/cb-engine-verify.git",
        "default_branch": "dev",
    },
    "CB2": {
        "jira_project_key": "CB2",
        "name": "CB2",
        "engine_repo": "git@gitlab.booming-inc.com:booming/dev/projects/conquerorsblade2/chaos-cb-2.git",
        "game_repo": "git@gitlab.booming-inc.com:booming/dev/projects/conquerorsblade2/conquerors-blade-2.git",
        "default_branch": "master",
    },
    "Mars": {
        "jira_project_key": "MARS",
        "name": "Mars",
        "engine_repo": "git@gitlab.booming-inc.com:booming/dev/projects/mars/chaos-mars.git",
        "game_repo": "git@gitlab.booming-inc.com:booming/dev/projects/mars/mars.git",
        "default_branch": "master",
    },
    "Rage": {
        "jira_project_key": "RAGE",
        "name": "Rage",
        "engine_repo": "git@gitlab.booming-inc.com:booming/dev/projects/rage/chaos.git",
        "game_repo": "git@gitlab.booming-inc.com:booming/dev/projects/rage/rage.git",
        "default_branch": "master",
        "engine_default_branch": "rage/master",
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


def gitlab_api_get(path, token):
    """Make a GitLab API GET request."""
    url = f"https://gitlab.booming-inc.com/api/v4/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"[gitlab] API error {e.code} for {url[:80]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[gitlab] Request error: {e}", file=sys.stderr)
        return None


def parse_gitlab_mr_url(url):
    """
    Parse a GitLab MR URL to extract project path and MR IID.

    Handles formats like:
      https://gitlab.booming-inc.com/group/subgroup/project/-/merge_requests/123
      https://gitlab.booming-inc.com/group/project/-/merge_requests/123
    """
    m = re.match(r'https://gitlab\.booming-inc\.com/(.+?)/-/merge_requests/(\d+)', url)
    if m:
        return m.group(1), m.group(2)
    return None, None


def gitlab_get_mr(mr_url, token):
    """
    Fetch GitLab MR details via API.
    Returns dict with source_branch, target_branch, or None on failure.

    Uses parse_gitlab_mr_url to extract project path and MR IID from URL.
    """
    project_path, mr_iid = parse_gitlab_mr_url(mr_url)
    if not project_path:
        print(f"[gitlab] Cannot parse MR URL: {mr_url}", file=sys.stderr)
        return None

    # URL-encode the project path
    project_encoded = urllib.parse.quote(project_path, safe='')

    data = gitlab_api_get(f"projects/{project_encoded}/merge_requests/{mr_iid}", token)
    if not data:
        return None

    return {
        "source_branch": data.get("source_branch", ""),
        "target_branch": data.get("target_branch", ""),
        "state": data.get("state", ""),
        "title": data.get("title", ""),
    }


def extract_issue_key(url):
    """Extract Jira issue key from URL like https://jira.company/browse/EV-123"""
    m = re.search(r'(?:browse|issues)/([A-Za-z][A-Za-z0-9]+-\d+)', url)
    return m.group(1) if m else None


def identify_project(issue_key, config):
    """Map issue key prefix to project config entry.
    Matches longest prefix first: e.g. 'CB2N' → checks 'CB2N', then 'CB2', then 'C'.
    """
    prefix = issue_key.split('-')[0].upper()
    projects = config.get("projects", {})

    # Try longest prefix match: e.g. CB2N → try CB2N, then CB2, then C
    for length in range(len(prefix), 0, -1):
        sub = prefix[:length]
        for proj_id, proj_cfg in projects.items():
            if proj_cfg["jira_project_key"].upper() == sub:
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


def get_remote_links(issue_key, host, token, gitlab_token=None):
    """Get remote links (GitLab MR links etc) from Jira issue.
    If gitlab_token is provided, fetches real branch info from GitLab API
    for each GitLab MR link found.
    """
    path = f"api/2/issue/{issue_key}/remotelink"
    data = jira_request(path, host, token)
    if not data:
        return []

    result = []
    for link in data:
        obj = link.get("object", {})
        url = obj.get("url", "")
        title = obj.get("title", "")
        if url and ("merge request" in title.lower() or "mr" in title.lower()):
            branch = ""
            target_branch = ""
            # Try GitLab API for real branch info
            if gitlab_token:
                mr_info = gitlab_get_mr(url, gitlab_token)
                if mr_info:
                    branch = mr_info["source_branch"]
                    target_branch = mr_info["target_branch"]
                    print(f"[gitlab] MR {url}: source={branch}, target={target_branch}",
                          file=sys.stderr)

            result.append({
                "title": title,
                "url": url,
                "branch": branch,
                "target_branch": target_branch,
            })
    return result


def guess_branch_from_issue(issue_key, config, host, token):
    """
    Fallback: try to get the issue summary and guess branch name,
    OR search for branches via Jira API.
    """
    # Try to fetch issue details — custom fields may hold PR links
    issue_data = jira_request(f"api/2/issue/{issue_key}?fields=summary,description,status,customfield_*,issuelinks", host, token)
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
    parser.add_argument("--gitlab-token", default=os.environ.get("GITLAB_TOKEN", ""),
                        help="GitLab personal access token")
    args = parser.parse_args()

    config = load_config()
    gitlab_token = args.gitlab_token or os.environ.get("GITLAB_TOKEN", "")

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
    # Use engine-specific default branch if configured
    engine_default_branch = project_cfg.get("engine_default_branch", project_cfg["default_branch"])
    result = {
        "issue_key": issue_key,
        "project": project_id,
        "project_name": project_cfg["name"],
        "engine_repo": project_cfg["engine_repo"],
        "game_repo": project_cfg["game_repo"],
        "default_branch": project_cfg["default_branch"],
        "engine_default_branch": engine_default_branch,
        "mr_info": None,
        "mr_links": [],
        "mr_url": "",
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

        # Step 3b: Try remote links for GitLab MR info (with GitLab API)
        if not result.get("mr_info"):
            remote_links = get_remote_links(issue_key, args.jira_host, args.jira_token, gitlab_token)
            if remote_links:
                result["mr_links"] = remote_links
                # Use the first MR URL
                if remote_links:
                    result["mr_url"] = remote_links[0].get("url", "")
                # Use branch info from GitLab API if available
                for link in remote_links:
                    branch = link.get("branch", "")
                    target_branch = link.get("target_branch", "")
                    if branch:
                        result["mr_info"] = {
                            "branch": branch,
                            "target_branch": target_branch or project_cfg["default_branch"],
                        }
                        break

        # Step 3c: If we have mr_info from GitLab, also try to find per-repo MR URLs
        # (engine and game repos may use different MRs)
        if result.get("mr_info") and result.get("mr_url"):
            mr_info = result["mr_info"]
            # Try to find game repo MR URL from the same Jira remote links
            for link in result.get("mr_links", []):
                if link.get("url") and link["url"] != result.get("mr_url"):
                    l_branch = link.get("branch", "")
                    l_target = link.get("target_branch", "")
                    if l_branch:
                        result.setdefault("game_mr_info", {
                            "branch": l_branch,
                            "target_branch": l_target or project_cfg["default_branch"],
                        })
                        result["game_mr_url"] = link["url"]
                        break

    # Step 4: Fallback — fetch issue details
    if args.jira_host and args.jira_token and not result.get("mr_info") and not result.get("mr_links"):
        issue_data = guess_branch_from_issue(issue_key, config, args.jira_host, args.jira_token)
        if issue_data:
            result["issue_info"] = issue_data

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
