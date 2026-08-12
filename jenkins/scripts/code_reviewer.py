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


def _load_skill_review_instructions(repo_type=None):
    """若项目安装了 code-review-skill, 将它的方法论(severity 分级/Review Process/中文输出)
    + 与该被审代码语言匹配的语言参考的检查重点, 汇总成一条紧凑的 review system prompt。
    skill 文件缺失时返回 None(调用方回退默认)。控制长度: 不整篇塞入 238+893+1073 行, 抽核心。

    语言感知(repo_type): engine/game 都是 C/C++ 游戏引擎仓库 -> 倾向 C++ 维度, 不强行注入
    Python/前端 JS 维度。通用(security/arch/quality)维度仍注入, 但语义从"必须覆盖"改为
    "供参考, 仅当 diff 确实涉及才核对", 避免模型为凑维度而编造与 diff 无关的 finding
    (如把 web 的内存泄漏模板套到 C++ 引擎上)。
    """
    import os as _os
    scripts_dir = _os.path.dirname(_os.path.abspath(__file__))   # jenkins/scripts
    proj_root = _os.path.dirname(_os.path.dirname(scripts_dir)) # repo root
    # 优先读取随代码部署的 jenkins/skills(每个环境都有); 若无回退到开发 .claude/skills。
    candidates = [
        _os.path.join(proj_root, "jenkins", "skills", "code-review-skill"),
        _os.path.join(proj_root, ".claude", "skills", "code-review-skill"),
    ]
    skill_root = next((c for c in candidates if _os.path.isfile(_os.path.join(c, "SKILL.md"))), None)
    if not skill_root:
        return None
    sk = _os.path.join(skill_root, "SKILL.md")
    if not _os.path.isfile(sk):
        return None
    ref_dir = _os.path.join(skill_root, "reference")
    # SKILL.md: 提取 Review mindset / Process / severity / 中文输出的关键句
    sk_txt = open(sk, encoding="utf-8", errors="ignore").read()
    lines = [l for l in sk_txt.splitlines() if l.strip()]
    pick = []
    for l in lines:
        s = l.strip()
        if any(k in s for k in ("Catch bugs", "severity", "Review Process", "Phase 1", "Phase 2",
                                 "Phase 3", "Phase 4", "blocking", "important", "中文", "所有 review")):
            pick.append(s)
    # 跨切面维度: 抽取 security/architecture/performance/universal 的 ##/### 小节标题
    # 作为"供参考"的检查维度; 语言专属参考只注入与被审代码匹配的那份(避免把 Python /
    # 前端模板套到 C++ 引擎, 导致与 diff 无关的内存/LOD 类噪音 finding)。
    # engine/game 都是 C/C++ 引擎仓库 -> 默认 cpp; 其它 repo_type 视为通用。
    lang_guide = "cpp"
    if repo_type and "python" in str(repo_type).lower():
        lang_guide = "python"
    ref_priorities = ["security-review-guide", "architecture-review-guide",
                      "code-quality-universal", lang_guide]
    if repo_type and "web" in str(repo_type).lower():
        ref_priorities.append("performance-review-guide")  # 仅前端类才注入 web 性能维度
    dims = {}
    for fname in ref_priorities:
        p = _os.path.join(ref_dir, fname + ".md")
        if not _os.path.isfile(p):
            continue
        label = {"security-review-guide": "安全(Security)",
                 "architecture-review-guide": "架构(Architecture)",
                 "performance-review-guide": "性能(Performance)",
                 "code-quality-universal": "代码质量(Universal)", "cpp": "C++", "python": "Python"}[fname]
        heads = []
        for l in open(p, encoding="utf-8", errors="ignore").read().splitlines():
            s = l.strip()
            # 收集 ## / ### 小节标题(检查维度), 去掉纯"目录"等噪音
            if (s.startswith("## ") or s.startswith("### ")) and s[3:].strip() not in ("目录", "Table of Contents"):
                heads.append(s)
        if heads:
            dims[label] = heads[:14]   # 每文件最多 14 个维度控长度
    parts = ["## 依据 code-review-skill 方法论审查",
             "### 心态与流程", *pick[:40],
             "### 供参考的检查维度(来自 skill 的审查指南)",
             "这些维度**仅供参考**：仅当 diff 确实涉及对应主题时才逐一核对并给 finding；"
             "**切勿为了凑齐维度而编造与本次 diff 无关的问题**。优先给出与本次代码变更直接相关的发现。"]
    for label, heads in dims.items():
        parts.append(f"【{label}】")
        parts.extend(heads)   # 各维度小节标题
    parts += ["### 输出", "所有 finding 用中文描述问题与建议; 严重度分 🔴critical/🟡warning/ℹ️suggestion;",
              "每个 finding 给 file / severity / 问题 / 建议, 并附简明总结表。"]
    return "\n".join(parts).strip()[:16000]  # 上限控 token


def load_config(repo_type=None):
    """Load config from config.yaml (common), falling back to env for model.
    review_instructions 优先用 code-review-skill(skill 化方法论+中文，语言感知 repo_type);
    无 skill 回退配置/默认。"""
    cfg = get_claude_config()
    skill_instr = _load_skill_review_instructions(repo_type)
    return {
        "claude": {
            "model": cfg.get("model")
                     or os.environ.get("ANTHROPIC_MODEL", "deepseek-v4-flash"),
            "max_tokens": cfg.get("max_tokens", 8192),
            "review_instructions": skill_instr          # 优先 use skill(你要求)
                                   or cfg.get("review_instructions")
                                   or DEFAULT_REVIEW_INSTRUCTIONS.strip(),
        }
    }


# Structured-findings tool: forces the LLM to return findings as machine-readable
# JSON (via Anthropic tool_use), so severity counts are exact and never depend on
# parsing the free-form markdown report.
REVIEW_TOOLS = [{
    "name": "review_findings",
    "description": (
        "Return the code review findings as structured JSON. Use this tool "
        "EVERY time you complete a review. The findings array is the source of "
        "truth for severity counts. Also provide a short summary, a few strengths, "
        "and assign each finding a review category (architecture/security/performance/"
        "quality) so the report can be rendered in the code-review-skill template."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string",
                        "description": "1-2 sentence overview of what was reviewed and the overall conclusion (中文)."},
            "strengths": {"type": "array",
                          "items": {"type": "string"},
                          "description": "2-3 things that were done well (中文, optional)."},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string",
                                 "description": "Repo-relative path of the changed file."},
                        "severity": {
                            "type": "string",
                            "enum": ["critical", "warning", "suggestion"],
                            "description": "Severity of this finding."
                        },
                        "category": {
                            "type": "string",
                            "enum": ["architecture", "security", "performance", "quality"],
                            "description": "Review dimension of this finding (from the skill's cross-cutting guides)."
                        },
                        "issue": {"type": "string",
                                  "description": "One-line description of the problem."},
                        "suggestion": {"type": "string",
                                       "description": "How to fix it."}
                    },
                    "required": ["file", "severity", "issue", "suggestion"]
                }
            }
        },
        "required": ["findings"]
    },
}]


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
    # NOTE (arch): this pins diff_hash to the MR's fixed diff and ignores later
    # pushes to the source branch, so findings go stale vs the real code — which is
    # exactly why auto-fix findings didn't match the checkout. We now prefer the REAL
    # git-branch diff (below); MR diffs API stays only as a last-resort fallback.
    use_mr_api_diff = False  # flip to True only if you explicitly want MR-snapshot diff
    if use_mr_api_diff and mr_url and gitlab_token:
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


def _normalize_severity(sev):
    """Map the LLM's severity strings to our enum {critical, warning, suggestion}.
    The model sometimes writes 'info'/'Info' — treat as suggestion."""
    s = (sev or "").strip().lower()
    if s in ("critical", "high", "error", "blocker"):
        return "critical"
    if s in ("warning", "warn", "medium", "minor"):
        return "warning"
    return "suggestion"  # suggestion, info, note, low, etc.


def _findings_counts(findings):
    """Exact severity counts from a structured findings list."""
    counts = {"critical": 0, "warning": 0, "suggestion": 0}
    for f in findings or []:
        counts[_normalize_severity(f.get("severity"))] += 1
    return counts


def _one_line(text, limit):
    """Collapse arbitrary LLM prose into one line of at most `limit` chars, keeping
    the leading key point and clipping the tail with '…'. Newlines/whitespace are
    folded. Used so a finding's issue/suggestion reads as a terse one-liner instead
    of a multi-sentence paragraph (方案C: 言简意赅)."""
    if not text:
        return ""
    s = " ".join(str(text).split())          # fold all whitespace/newlines
    if len(s) <= limit:
        return s
    return s[: limit].rstrip() + "…"


def _build_markdown_from_findings(findings, meta=None):
    """Build a concise review report modeled on the code-review-skill PR template:
      Summary / Strengths / Architecture&Performance / Required(blocking) / Important / Nit.

    Uses `meta` (summary/strengths) and per-finding `category`(architecture/security/
    performance/quality) when provided; falls back to plain severity grouping when the
    model didn't supply them, so the card is always complete.

    方案C(言简意赅): keep the skill's severity grouping & sections, but compress every
    piece — summary/strengths to one-liners, and each finding to a single clipped line
    `file 问题 → 修法` — instead of dumping full LLM paragraphs into the group card.
    """
    meta = meta or {}
    findings = findings or []

    def _tag(f):
        s = (f.get("severity") or "").strip().lower()
        if s in ("critical", "high", "error", "blocker"):
            return ("🔴", "blocking", "必须修复")
        if s in ("warning", "warn", "medium", "minor"):
            return ("🟡", "important", "应处理")
        return ("🟢", "nit", "可选")

    groups = {"critical": [], "warning": [], "suggestion": []}
    for f in findings:
        s = (f.get("severity") or "").lower()
        k = ("critical" if s in ("critical", "high", "error", "blocker")
             else "warning" if s in ("warning", "warn", "medium", "minor")
             else "suggestion")
        groups[k].append({**f, "_tag": _tag(f)})

    c = {"critical": len(groups["critical"]), "warning": len(groups["warning"]),
         "suggestion": len(groups["suggestion"])}
    total = sum(c.values())

    # Trim lengths (characters). These bound how much prose lands in the group card
    # (方案C: 言简意赅 — keep the point, drop the padding).
    ISSUE_LIM, FIX_LIM, SUMMARY_LIM, STRENGTH_LIM = 48, 34, 92, 60

    parts = [f"🔍 Code Review — {total} 项 ({c['critical']} 必改)"]
    # Summary: one line
    if meta.get("summary"):
        parts.append(f"Summary：{_one_line(meta['summary'], SUMMARY_LIM)}")
    # Strengths: one compact line (fallback: omit)
    if meta.get("strengths"):
        strengths = [_one_line(s, STRENGTH_LIM) for s in meta["strengths"][:3]]
        strength_line = "；".join(s for s in strengths if s).strip("。；")
        if strength_line:
            parts.append(f"✅ {strength_line}")
    # Architecture & Performance -> folded straight into findings (one-liners below),
    # no separate verbose section.

    # Findings by severity (compact; each finding = one clipped line).
    for k, label in (("critical", "blocking"), ("warning", "important"), ("suggestion", "nit")):
        if not groups[k]:
            continue
        emoji, tag, rank = groups[k][0]["_tag"]
        parts.append(f"{emoji} {tag} ({len(groups[k])})")
        for f in groups[k][:10]:                       # cap per-severity lines for brevity
            issue = _one_line(f.get("issue"), ISSUE_LIM)
            fix = _one_line(f.get("suggestion"), FIX_LIM)
            # 只用最短文件名(basename), 不用完整路径 —— 完整路径太长会被截断丢信息
            fname = os.path.basename((f.get("file") or "").strip().rstrip("/")) or (f.get("file") or "?")
            desc = f"{_one_line(fname, 48)}: {issue}"
            if fix:
                desc += f" → {fix}"
            cat = (f.get("category") or "").strip()
            parts.append(f"· [{cat}] {desc}" if cat else f"· {desc}")

    # Count line (compact)
    parts.append(f"📊 🔴{c['critical']} / 🟡{c['warning']} / 🟢{c['suggestion']}")
    return "\n".join(p for p in parts if p)



def _count_severities(review_text):
    """
    Count findings by severity.

    Preferred: parse the LLM's own summary table at the end of the review text
    (e.g. `| 🔴 Critical | 3 |`), so the counts match what the LLM explicitly
    concluded in its report — this keeps the card summary consistent with the
    body (previously we counted 🔴/🟡/ℹ️ header lines, which can differ from the
    number of listed issues under a single header, making the data look fake).

    Fallback: count 🔴/🟡/ℹ️ header lines (then raw emoji) if no table is found.
    """
    # Try the LLM's own summary table first.
    critical = _extract_table_count(review_text, r'🔴|Critical|关键')
    warning = _extract_table_count(review_text, r'🟡|Warning|警告')
    suggestion = _extract_table_count(review_text, r'ℹ️|Suggestion|建议')
    if critical is not None and warning is not None and suggestion is not None:
        return {"critical": critical, "warning": warning, "suggestion": suggestion}

    # Fallback: count header lines.
    critical = len(re.findall(r'🔴\s*(?:Critical|关键)', review_text))
    warning = len(re.findall(r'🟡\s*(?:Warning|警告)', review_text))
    suggestion = len(re.findall(r'ℹ️?\s*(?:Suggestion|建议)', review_text))
    if critical == 0 and warning == 0 and suggestion == 0:
        critical = review_text.count("🔴")
        warning = review_text.count("🟡")
        suggestion = review_text.count("ℹ️")
    return {"critical": critical, "warning": warning, "suggestion": suggestion}


def _extract_table_count(review_text, cell_pattern):
    """
    Parse a markdown table row of the LLM summary:
        | 🔴 Critical | 3 |
        | 严重级别 | 数量 |
        |---------|------|
    Returns the integer cell next to a row whose label cell matches
    `cell_pattern`, or None if not found.
    """
    lines = review_text.splitlines()
    for line in lines:
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        label, value = cells[0], cells[1]
        if re.search(cell_pattern, label) and label not in ("严重级别", "🆚"):
            m = re.search(r'(\d+)', value)
            if m:
                return int(m.group(1))
    return None


def _call_llm_batch(system_prompt, user_prompt, api_key, base_url, model,
                    max_output_tokens):
    """
    Single LLM API call. Uses an Anthropic-tool_use 'review_findings' tool so the
    model returns structured findings JSON (exact severity counts), while still
    producing a free-form markdown report in the text block.

    Returns (review_text, findings_list, error). findings_list is a list of
    {file, severity, issue, suggestion} dicts from the tool input, or None if the
    tool_use block wasn't returned (the call still succeeded and review_text has
    the markdown report).
    """
    body = {
        "model": model,
        "max_tokens": max_output_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
        "tools": REVIEW_TOOLS,
        "tool_choice": {"type": "any"},
    }
    payload = json.dumps(body).encode("utf-8")

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
        return None, None, "LLM API request failed after retries"

    # Extract markdown text + structured findings from the response blocks.
    review_text = ""
    findings = None
    meta = {}
    try:
        blocks = result.get("content") or []
        text_parts = []
        for b in blocks:
            if b.get("type") == "text":
                text_parts.append(b.get("text", ""))
            elif b.get("type") == "tool_use" and b.get("name") == "review_findings":
                inp = b.get("input") or {}
                if isinstance(inp.get("findings"), list):
                    findings = inp["findings"]
                if inp.get("summary"):
                    meta["summary"] = inp["summary"]
                if inp.get("strengths"):
                    meta["strengths"] = inp["strengths"]
        review_text = "".join(text_parts)
        if findings:
            # Always produce a complete, count-consistent markdown report rebuilt
            # from the structured findings. This avoids truncated/fragmentary text
            # blocks from a tool-only or partial response, and guarantees the
            # detailed card matches severity_counts exactly.
            review_text = _build_markdown_from_findings(findings, meta=meta)
        if not review_text:
            review_text = result.get("completion", json.dumps(result))
    except Exception:
        review_text = json.dumps(result)
    return review_text, findings, None, meta


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
    all_findings = []
    total = {"critical": 0, "warning": 0, "suggestion": 0}
    agg_meta = {}   # 跨批次聚合 summary/strengths
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

        review_text, findings, err, batch_meta = _call_llm_batch(
            system_prompt, user_prompt, api_key, base_url, model, max_output_tokens
        )
        if err:
            if not first_error:
                first_error = err
            print(f"[review] Batch {idx}/{num_batches} failed: {err}", flush=True)
            continue
        # 聚合 batch 的 meta: summary 取首个非空; strengths 各批合并去重。
        batch_meta = batch_meta or {}
        if batch_meta.get("summary") and not agg_meta.get("summary"):
            agg_meta["summary"] = batch_meta["summary"]
        for s in (batch_meta.get("strengths") or []):
            if s and s not in agg_meta.get("strengths", []):
                agg_meta["strengths"] = agg_meta.get("strengths", []) + [s]

        if num_batches > 1:
            all_text.append(f"### 第 {idx}/{num_batches} 部分\n{review_text}")
        else:
            all_text.append(review_text)

        if findings is not None:
            # Exact counts from the structured tool_use JSON (source of truth).
            all_findings.extend(findings)
            batch_counts = _findings_counts(findings)
            for k in total:
                total[k] += batch_counts[k]
        else:
            # Fallback: count from the markdown report if no structured findings.
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
            "findings": [],
            "error": first_error,
            "batches": num_batches,
        }

    # 跨批次聚合: 用全部 findings + 聚合的 summary/strengths 重新渲染成完整 skill 模板,
    # 而非各批 "第 N/M 部分" 拼接(那会丢失聚合 meta 且 render 不完整)。
    final_text = _build_markdown_from_findings(all_findings, meta=agg_meta)
    return {
        "summary": agg_meta.get("summary", ""),
        "review_text": final_text,
        "severity_counts": total,
        "findings": all_findings,
        "error": first_error,   # non-None if at least one batch failed (partial results)
        "batches": num_batches,
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
    parser.add_argument("--dry", action="store_true",
                        help="Compute the diff hash only (no LLM call); for result caching")
    parser.add_argument("--mr-url", default="", help="Merge request URL")
    args = parser.parse_args()

    config = load_config(repo_type=args.repo_type)
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

    # Diff hash for result caching: unchanged diff -> reuse cached review (no LLM).
    import hashlib
    diff_hash = hashlib.sha1((diff_info["diff_text"] or "").encode("utf-8")).hexdigest()

    if not diff_info["diff_text"]:
        result = {
            "project": args.project,
            "issue_key": args.issue_key,
            "repo_type": args.repo_type,
            "branch": args.branch,
            "base_branch": args.base_branch,
            "mr_url": args.mr_url or "",
            "diff_hash": diff_hash,
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
        # Dry mode: report the diff hash + diff shape without calling the LLM,
        # so the orchestrator can decide whether a cached review can be reused.
        if args.dry:
            os.makedirs(args.workspace, exist_ok=True)
            result = {
                "project": args.project,
                "issue_key": args.issue_key,
                "repo_type": args.repo_type,
                "branch": args.branch,
                "base_branch": args.base_branch,
                "mr_url": args.mr_url or "",
                "diff_hash": diff_hash,
                "changed_files": diff_info["changed_files"],
                "stats": diff_info["stats"],
                "commits": diff_info["commit_log"],
                "branch_exists": diff_info["branch_exists"],
                "branch_merged": diff_info["branch_merged"],
                "dry": True,
                "review": None,
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
                "diff_hash": diff_hash,
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
