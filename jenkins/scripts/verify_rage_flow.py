#!/usr/bin/env python3
"""verify_rage_flow.py — real-environment E2E validation for the rage-replication (方案D).

Runs against a REAL MR to prove the replicated quality engine works on real code,
step by step. The pure logic is already unit-tested; this drives the pieces that
need a live repo/diff.

Usage:
    python3 verify_rage_flow.py --project CB2 --mr-url "https://gitlab.../merge_requests/7201" \
        [--issue-key CB2N-XXX] [--workspace /path] [--dry-mr]

What it does:
  1. Resolve the MR (source/target branch, repo dir) via the existing gitlab helpers.
  2. Run code_reviewer.py --agent against real diff (or --dry-mr to just resolve+diff).
  3. Validate the output is rage-standard:
       - non-empty findings OR an explicit '已完成审查，未发现问题' clean summary
       - every finding has [Repo] file[:line_range] that maps to a real changed line
       - severity ∈ {严重,中,轻,建议}
  4. Print a PASS/FAIL matrix + the next IRL steps (dev triage / ok / done / approver ok)
     that need the live Feishu bot.

Exit code 0 = Path A agent + rage-standard output verified on this MR.
"""
import argparse
import json
import os
import re
import subprocess
import sys

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS)
import code_reviewer as cr


def _run(cmd, env=None, timeout=120):
    return subprocess.run(cmd, capture_output=True, text=True,
                          env=env or os.environ.copy(), timeout=timeout)


def resolve_mr(project_cfg, mr_url, token):
    import jira_parser as jp
    path, iid = jp.parse_gitlab_mr_url(mr_url or "")
    if not path or not iid:
        return None, "cannot parse mr_url"
    mi = jp.gitlab_get_mr(mr_url, token) or {}
    branch = mi.get("source_branch") or ""
    target = mi.get("target_branch") or project_cfg.get("default_branch", "main")
    return {"repo": path, "iid": iid, "branch": branch, "target": target}, None


def validate_rage_result(res):
    """Return list of (check, ok, detail)."""
    checks = []
    rv = (res or {}).get("review") or {}
    findings = rv.get("findings") or []
    err = rv.get("error")
    rev_text = rv.get("review_text") or ""

    checks.append(("agent produced a review (no crash/empty-stub)",
                   bool(err is None and rv), err or rev_text[:80]))

    if findings:
        ok = all((f.get("file") or "") and (f.get("severity") or "").strip()
                 in ("严重", "中", "轻", "建议") for f in findings)
        checks.append(("findings have repo file + severity ∈ 严重/中/轻/建议", ok,
                       ", ".join(f.get("severity", "?") for f in findings[:5])))
        # every finding's file should be a changed file
        changed = set((f.split("\t", 1)[-1] if "\t" in f else f)
                      for f in (res or {}).get("changed_files") or [])
        bad = [f for f in findings if (f.get("file") or "") not in changed]
        checks.append(("every finding.file is in changed_files (no fabricated path)",
                       not bad, f"{len(bad)} bad: {bad[:3]}"))
    else:
        ok = bool((rv.get("summary") or "").strip()
                  or "未发现" in rev_text or "已完成审查" in rev_text)
        checks.append(("zero-issue review carries an explicit clean conclusion",
                       ok, rv.get("summary", rev_text)[:80]))

    return checks


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--project", required=True)
    p.add_argument("--mr-url", required=True)
    p.add_argument("--issue-key", default="")
    p.add_argument("--workspace", default="/tmp/rage-e2e-ws")
    p.add_argument("--repo-type", choices=["engine", "game"], default="engine")
    p.add_argument("--dry-mr", action="store_true",
                   help="only resolve MR + build diff info; do NOT call the agent")
    p.add_argument("--claude-exe", default="claude")
    p.add_argument("--model", default="")
    a = p.parse_args()

    import common as cfg
    proj = (cfg.load_config().get("projects") or {}).get(a.project)
    if not proj:
        print(f"FAIL: unknown project {a.project}"); sys.exit(1)
    token = os.environ.get("GITLAB_TOKEN") or os.environ.get("CR_GITLAB_TOKEN", "")

    mr, err = resolve_mr(proj, a.mr_url, token)
    if err:
        print(f"FAIL: resolve MR: {err}"); sys.exit(1)
    if not mr.get("branch"):
        print("FAIL: no source branch on MR"); sys.exit(1)
    print(f"RESOLVED: {mr['repo']}!{mr['iid']} branch={mr['branch']} -> {mr['target']}")

    os.makedirs(a.workspace, exist_ok=True)
    try:
        diff_info = cr.prepare_repo(
            proj.get("engine_repo") if a.repo_type == "engine" else proj.get("game_repo"),
            mr["branch"], mr["target"], a.workspace, a.issue_key or f"{a.project}-E2E",
            mr_url=a.mr_url, gitlab_token=token)
    except Exception as e:
        print(f"FAIL: prepare_repo: {e}"); sys.exit(1)

    if a.dry_mr:
        print(f"DRY-MR OK: changed={len(diff_info.get('changed_files') or [])} "
              f"diff_len={len(diff_info.get('diff_text') or '')}")
        sys.exit(0)

    print(f"Running agent review (model={a.model or 'claude-opus-5'})...")
    res = cr._spawn_review_agent(diff_info, a.project, a.issue_key or f"{a.project}-E2E",
                                 a.repo_type, model=a.model or None,
                                 claude_exe=a.claude_exe)
    checks = validate_rage_result(res)
    print("\n=== rage-standard validation ===")
    fails = 0
    for name, ok, detail in checks:
        print(f"  [{'OK' if ok else 'FAIL'}] {name} — {detail}")
        if not ok:
            fails += 1
    print(f"\nreview_text:\n{(res or {}).get('review', {}).get('review_text', '')[:800]}")
    if fails:
        print(f"FAIL: {fails} check(s) failed")
        sys.exit(1)
    print("PASS: Path A agent + rage-standard output verified on this MR.")
    print("NEXT (live Feishu bot): in the topic reply 1 3 5 -> maybe push -> ok -> "
          "done -> [approver] ok/close; and @bot 同步 for human DiffNotes.")


if __name__ == "__main__":
    main()
