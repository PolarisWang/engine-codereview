#!/usr/bin/env python3
"""
Jira Scanner — Scan Jira projects for issues with open MRs/PRs needing review.

Called by Jenkins cron (every 30 min) when no explicit JIRA_URL is provided.
Outputs JSON list of issues ready for code review.

Usage:
    python3 jira_scanner.py --jira-host "https://jira.boomingtechs.cn" \
                            --jira-token "xxx" \
                            --output /tmp/to_review.json
"""
import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error


# ── Built-in project Jira keys (no YAML dependency) ─────────────────────────

PROJECT_JIRA_KEYS = ["EV", "CB2", "MARS", "RAGE"]


def load_config():
    """Return minimal config (no YAML needed)."""
    return {"projects": {k: {"jira_project_key": k} for k in PROJECT_JIRA_KEYS}}


def jira_get(path, host, token):
    """Jira API GET request, tries Bearer then Basic."""
    url = f"{host.rstrip('/')}/rest/{path}"

    for auth_type in [f"Bearer {token}", f"Basic {token}"]:
        req = urllib.request.Request(
            url, headers={"Authorization": auth_type, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code not in (401, 403):
                return None
            continue
    return None


def main():
    parser = argparse.ArgumentParser(description="Scan Jira for issues needing review")
    parser.add_argument("--jira-host", default=os.environ.get("JIRA_HOST", ""))
    parser.add_argument("--jira-token", default=os.environ.get("JIRA_TOKEN", ""))
    parser.add_argument("--output", default="/tmp/codereview-scan-result.json")
    parser.add_argument("--state-file", default="/tmp/codereview-reviewed.json")
    args = parser.parse_args()

    if not args.jira_host or not args.jira_token:
        print(json.dumps({"error": "JIRA_HOST and JIRA_TOKEN required", "issues": []}))
        sys.exit(0)

    config = load_config()
    project_keys = [p["jira_project_key"] for p in config["projects"].values()]

    # ── Load previously reviewed issues ──
    reviewed = set()
    if os.path.exists(args.state_file):
        try:
            with open(args.state_file) as f:
                reviewed = set(json.load(f).get("reviewed", []))
        except Exception:
            reviewed = set()

    # ── JQL: issues updated in last 2 hours, in our projects, not closed ──
    jql = (f"project IN ({','.join(project_keys)}) "
           f"AND updated >= -2h "
           f"AND status NOT IN (Done, Closed, Resolved) "
           f"ORDER BY updated DESC")
    from urllib.parse import quote
    jql_encoded = quote(jql)
    data = jira_get(
        f"api/3/search?jql={jql_encoded}&maxResults=20&fields=summary,updated,status",
        args.jira_host, args.jira_token
    )
    if not data or "issues" not in data:
        print(json.dumps({"error": "Jira search failed", "issues": []}))
        sys.exit(0)

    # ── Check each issue for dev info (PRs/MRs) ──
    to_review = []
    for issue in data.get("issues", []):
        key = issue.get("key", "")
        if key in reviewed:
            continue

        # Look for linked development info (PRs)
        dev_info = jira_get(
            f"dev-status/latest/issue/{key}",
            args.jira_host, args.jira_token
        )

        has_open_pr = False
        pr_url = ""
        branch = ""

        if dev_info:
            for detail in dev_info.get("detail", []):
                for pr in detail.get("pullRequests", []):
                    if pr.get("status", "").upper() in ("OPEN",):
                        has_open_pr = True
                        pr_url = pr.get("url", "")
                        branch = pr.get("sourceBranch", "")
                        break
                if not has_open_pr:
                    for b in detail.get("branches", []):
                        branch = b.get("name", "")
                        if branch:
                            has_open_pr = True
                            break

        if has_open_pr or True:  # Always include issues with recent updates
            fields = issue.get("fields", {})
            to_review.append({
                "key": key,
                "summary": fields.get("summary", ""),
                "status": fields.get("status", {}).get("name", ""),
                "updated": fields.get("updated", ""),
                "has_open_pr": has_open_pr,
                "branch": branch or key.lower(),
            })

    # ── Write scan result ──
    result = {
        "issues": to_review,
        "scanned_at": __import__("datetime").datetime.now().isoformat(),
    }

    with open(args.output, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    # Don't mark as reviewed here — let the review pipeline do that after success

    summary = f"Scanned {len(data['issues'])} issues, {len(to_review)} need review"
    # Send brief notification if there's something to review
    feishu_webhook = os.environ.get("FEISHU_WEBHOOK_URL", "")
    if to_review and feishu_webhook:
        keys_str = ", ".join([i["key"] for i in to_review[:5]])
        if len(to_review) > 5:
            keys_str += f" ... 还有 {len(to_review) - 5} 个"
        text = f"🔍 发现 {len(to_review)} 个待 Review 的 Jira Issue:\n{keys_str}"
        payload = json.dumps({"msg_type": "text", "content": {"text": text}}).encode()
        try:
            urllib.request.urlopen(
                urllib.request.Request(feishu_webhook, data=payload,
                    headers={"Content-Type": "application/json"}, method="POST"),
                timeout=10
            )
        except Exception:
            pass

    # Print summary for Jenkins log
    print(json.dumps({"summary": summary, "count": len(to_review)}))


if __name__ == "__main__":
    main()
