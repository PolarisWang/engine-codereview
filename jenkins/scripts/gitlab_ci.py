#!/usr/bin/env python3
"""
gitlab_ci.py — MR CI status tracking + card-friendly rendering (arch-C).

The project's GitLab CI (`.gitlab-ci.yml`) only runs jobs on `merge_request_event`
(source) — a manual API pipeline trigger is always empty (GitLab 400). So we do
NOT trigger pipelines (would need to push the branch / fake an event). Instead we
TRACK: read the MR / branch's merge_request_event pipelines and per-job status,
and render a card-friendly status so the bot can post/refresh "CI: running/passed/
failed (+ which job)" in the topic.

Used by the executor/interaction ladder; safe (read-only, only GETs).
"""
import json
import os
import sys
import urllib.request
import urllib.error
import urllib.parse

# Reuse MR URL parsing from jira_parser (same scripts dir).
import jira_parser

GITLAB_HOST = "https://gitlab.booming-inc.com"


def _token():
    # The executor/interaction side reads GITLAB_TOKEN from the persistent env
    # (survives restarts). Fall back to current process env.
    return os.environ.get("GITLAB_TOKEN") or os.environ.get("CI_JOB_TOKEN") or ""


def _api(path, token=None):
    token = token or _token()
    if not token:
        return None
    url = f"{GITLAB_HOST}/api/v4/{path.lstrip('/')}"
    req = urllib.request.Request(url, headers={"PRIVATE-TOKEN": token})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=20).read())
    except urllib.error.HTTPError as e:
        print(f"[gitlab_ci] GET {url[:90]} -> HTTP {e.code}: {e.read()[:120]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[gitlab_ci] GET err: {e}", file=sys.stderr)
        return None


def mr_pipelines(mr_url, token=None, limit=3):
    """Return the merge_request_event pipelines for an MR (or its branch)."""
    project_path, iid = jira_parser.parse_gitlab_mr_url(mr_url or "")
    if not project_path:
        return []
    proj = urllib.parse.quote(project_path, safe="")
    pls = _api(f"projects/{proj}/merge_requests/{iid}/pipelines", token) or []
    # Prefer merge_request_event; fall back to any.
    ev = [p for p in pls if p.get("source") == "merge_request_event"]
    pls = ev or pls
    # sort by id desc, newest first
    pls.sort(key=lambda p: -int(p.get("id") or 0))
    return pls[:limit]


def pipeline_jobs(pipeline_id, project_path, token=None):
    proj = urllib.parse.quote(project_path, safe="")
    return _api(f"projects/{proj}/pipelines/{pipeline_id}/jobs", token) or []


def pipeline_summary(mr_url, token=None):
    """Return a compact dict summary of the most recent MR pipeline status."""
    project_path, iid = jira_parser.parse_gitlab_mr_url(mr_url or "")
    if not project_path:
        return {"error": "cannot parse mr_url"}
    pls = mr_pipelines(mr_url, token, limit=1)
    if not pls:
        return {"status": "none", "note": "尚未触发 GitLab CI（推送代码后会自动跑）"}
    p = pls[0]
    jobs = pipeline_jobs(p["id"], project_path, token)
    job_status = {j.get("name", "?"): j.get("status") for j in jobs}
    failed = [n for n, s in job_status.items() if s in ("failed", "canceled")]
    passed = [n for n, s in job_status.items() if s == "success"]
    return {
        "status": p.get("status"),
        "pipeline_id": p.get("id"),
        "ref": p.get("ref"),
        "jobs": job_status,
        "failed_jobs": failed,
        "passed_jobs": passed,
    }


def render_ci_card_block(mr_url, token=None):
    """Render a compact, card-friendly text block for the MR CI status."""
    s = pipeline_summary(mr_url, token)
    if s.get("error"):
        return f"ℹ️ CI 状态：{s['error']}"
    st = s.get("status", "none")
    emoji = {"success": "✅", "failed": "❌", "running": "⏳", "pending": "⏳",
             "canceled": "⏹️", "none": "—"}.get(st, "ℹ️")
    out = [f"{emoji} **GitLab CI: {st}**"]
    if s.get("pipeline_id"):
        out.append(f"  pipeline `#{s['pipeline_id']}`")
    jobs = s.get("jobs") or {}
    if jobs:
        for name, status in jobs.items():
            jm = {"success": "✅", "failed": "❌", "running": "⏳",
                  "canceled": "⏹️", "manual": "▶️", "pending": "⏳", "skipped": "⏭️"}.get(status, "·")
            out.append(f"    {jm} {name}: {status}")
    else:
        out.append("  （无 job 明细）")
    if st == "none":
        out.append("  👉 推送代码到 MR 源分支后，GitLab 会自动跑 CI。")
    return "\n".join(out)


if __name__ == "__main__":
    # CLI: python3 gitlab_ci.py <mr_url>
    if len(sys.argv) > 1:
        print(render_ci_card_block(sys.argv[1]))
    else:
        print("usage: gitlab_ci.py <mr_url>")
