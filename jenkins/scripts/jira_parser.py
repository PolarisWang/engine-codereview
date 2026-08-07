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
import urllib.parse

# Shared helpers: config from config.yaml, Jira URL pattern, HTTP with retry
from common import load_config, get_project, get_projects, get_claude_config, \
    JIRA_URL_PATTERN, http_request


# ── Helpers ──────────────────────────────────────────────────────────────────

def jira_request(path, host, token):
    """Make an authenticated Jira API request.
    Tries Bearer first, then Basic auth if Bearer fails.
    """
    url = f"{host.rstrip('/')}/rest/{path}"
    headers = {"Accept": "application/json"}

    # Try 1: Bearer token (PAT)
    resp = http_request("GET", url, headers={**headers, "Authorization": f"Bearer {token}"})
    if resp is not None:
        return resp

    # Try 2: Basic auth (in case token is pre-encoded base64 of user:apitoken)
    return http_request("GET", url, headers={**headers, "Authorization": f"Basic {token}"})


def gitlab_api_get(path, token):
    """Make a GitLab API GET request."""
    url = f"https://gitlab.booming-inc.com/api/v4/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    resp = http_request("GET", url, headers=headers)
    if resp is None:
        print(f"[gitlab] API request failed for {url[:80]}", file=sys.stderr)
    return resp


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


def repo_matches_mr_url(repo_url, mr_url):
    """True if a repo URL's project path appears inside an MR URL (same project)."""
    if not repo_url or not mr_url:
        return False
    # repo e.g git@host:booming/dev/projects/x/chaos-cb-2.git
    path = repo_url.rstrip(".git").split(":", 1)[-1].lstrip("/")
    return path in mr_url


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


def gitlab_branch_exists(project_path, branch, token):
    """True if a branch actually exists in the GitLab repo. Used to reject MRs whose
    source branch is gone (e.g. closed/merged/remote-deleted), so review binds to a
    real, current branch rather than a guessed/stale one."""
    if not branch or not token:
        return False
    try:
        proj = urllib.parse.quote(project_path, safe="")
        b = urllib.parse.quote(branch, safe="")
        data = gitlab_api_get(f"projects/{proj}/repository/branches/{b}", token)
        return bool(data and data.get("name"))
    except Exception:
        return False


def gitlab_search_issue_mrs(project_path, issue_key, token):
    """GitLab fallback (Jira remote-links may be unavailable without Jira creds):
    find OPEN MRs in the repo whose source_branch or title mentions the issue key.
    Returns list of dicts {branch, target_branch} for OPEN MRs whose source branch
    actually exists. Used to bind review to the real, current MR."""
    if not issue_key or not token or not project_path:
        return []
    try:
        proj = urllib.parse.quote(project_path, safe="")
        data = gitlab_api_get(
            f"projects/{proj}/merge_requests?state=opened&per_page=100&scope=all", token) or []
        out = []
        for m in data or []:
            src = m.get("source_branch", "")
            title = m.get("title", "") or ""
            if issue_key in src or issue_key in title:
                if gitlab_branch_exists(project_path, src, token):
                    out.append({
                        "branch": src,
                        "target_branch": m.get("target_branch", ""),
                        "iid": m.get("iid"),
                    })
        return out
    except Exception:
        return []



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
            mr_state = ""
            # Try GitLab API for real branch info
            if gitlab_token:
                # Skip non-MR URLs (e.g., commit links)
                proj, iid = parse_gitlab_mr_url(url)
                if not proj:
                    continue
                mr_info = gitlab_get_mr(url, gitlab_token)
                if mr_info:
                    branch = mr_info["source_branch"]
                    target_branch = mr_info["target_branch"]
                    mr_state = mr_info.get("state", "")
                    print(f"[gitlab] MR {url}: source={branch}, target={target_branch}, state={mr_state}",
                          file=sys.stderr)

            result.append({
                "title": title,
                "url": url,
                "branch": branch,
                "target_branch": target_branch,
                "state": mr_state,
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

        # Step 3b: Try remote links for GitLab MR info (with GitLab API).
        # ALWAYS scan them; 3a's guessed branch may be stale (bare issue name), so the
        # authoritative pick is: first MR that is OPEN and whose source branch exists.
        remote_links = get_remote_links(issue_key, args.jira_host, args.jira_token, gitlab_token)
        if remote_links:
            result["mr_links"] = remote_links
            result["mr_url"] = remote_links[0].get("url", "")
            best_mr = None
            for link in remote_links:
                branch = link.get("branch", "")
                target_branch = link.get("target_branch", "")
                mr_state = link.get("state", "")
                # Resolve the project path from THIS MR's URL (authoritative), so branch
                # existence is checked against the right repo.
                proj_path = ""
                murl = link.get("url", "")
                if murl:
                    proj_path, _ = parse_gitlab_mr_url(murl)
                if branch:
                    candidate = {
                        "branch": branch,
                        "target_branch": target_branch or project_cfg["default_branch"],
                        "state": mr_state,
                    }
                    # Only accept a branch that really exists in the repo.
                    if not proj_path or not gitlab_branch_exists(proj_path, branch, gitlab_token):
                        print(f"[gitlab] skip MR branch not present: {branch}", file=sys.stderr)
                        continue
                    # Prefer OPENED MRs (authoritative); else fall back to the first with a live branch.
                    if (mr_state or "").lower() == "opened":
                        best_mr = candidate
                        break
                    if best_mr is None:
                        best_mr = candidate
            if best_mr:
                result["mr_info"] = best_mr

        # Step 3b2: GitLab fallback — if we still have no MR (e.g. Jira remote-links
        # unavailable without Jira creds, or no OPEN MR among them), find the real OPEN
        # MR for this issue directly from GitLab and use its source branch.
        if not result.get("mr_info"):
            project_path = ""
            if remote_links:
                project_path, _ = parse_gitlab_mr_url(remote_links[0].get("url") or "")
            if not project_path:
                # derive from engine_repo config: git@host:path/repo.git -> path/repo
                eng = project_cfg.get("engine_repo") or ""
                if eng and "git@" in eng:
                    project_path = eng.split(":")[-1].lstrip("/").rstrip(".git")
            if project_path and gitlab_token:
                gl_mrs = gitlab_search_issue_mrs(project_path, issue_key, gitlab_token)
                print(f"[gitlab] gitlab-search found {len(gl_mrs)} OPEN MR(s) for {issue_key}", file=sys.stderr)
                for gm in gl_mrs:
                    if gm.get("branch"):
                        result["mr_info"] = {
                            "branch": gm["branch"],
                            "target_branch": gm.get("target_branch") or project_cfg["default_branch"],
                        }
                        result["mr_url"] = f"https://gitlab.booming-inc.com/{project_path}/-/merge_requests/{gm.get('iid')}"
                        break

        # Step 3c: If we have mr_info from GitLab, also try to find per-repo MR URLs
        # (engine and game repos may use different MRs)
        if result.get("mr_info") and result.get("mr_url"):
            mr_info = result["mr_info"]
            # Try to find game repo MR URL from the same Jira remote links
            game_mr_info = None
            for link in result.get("mr_links", []):
                if link.get("url") and link["url"] != result.get("mr_url"):
                    l_branch = link.get("branch", "")
                    l_target = link.get("target_branch", "")
                    l_state = link.get("state", "")
                    if l_branch:
                        candidate = {
                            "branch": l_branch,
                            "target_branch": l_target or project_cfg["default_branch"],
                        }
                        if l_state == "opened":
                            game_mr_info = candidate
                            result["game_mr_url"] = link["url"]
                            break
                        if not game_mr_info:
                            game_mr_info = candidate
                            result["game_mr_url"] = link["url"]
            if game_mr_info:
                result["game_mr_info"] = game_mr_info

    # Step 4: Fallback — fetch issue details
    if args.jira_host and args.jira_token and not result.get("mr_info") and not result.get("mr_links"):
        issue_data = guess_branch_from_issue(issue_key, config, args.jira_host, args.jira_token)
        if issue_data:
            result["issue_info"] = issue_data

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
