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
from common import c_claude_model, c_claude_base_url, c_gitlab_host

# Resolve git path explicitly (agent may not have it on PATH)
GIT_PATH = shutil.which("git") or "/usr/bin/git"


DEFAULT_REVIEW_INSTRUCTIONS = """
You are a senior game engine engineer reviewing a merge request. Provide concise findings in Chinese.

## 真实审查约束（必须遵守）
- 只审查 **diff 中实际出现的 `+`/`-` 代码行**；只引用 diff/该文件中**真实存在**的函数/符号/变量。
- **禁止编造**：diff 里没有的变更、不存在的函数名、未改动的代码块，不得作为 finding。
- 若某处虽有改动但无真实问题，**不要为了凑数编造问题**；可无 finding。
- 引用方法名时务必与该文件实际代码一致（例如确认真实方法名，而非相近词）。

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
            "model": c_claude_model(),
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


# ── 阶段2: 独立复核(自检回环)工具 ───────────────────────────────────────
# 对 B 判定为 flag(存疑)的 finding, 用**另一个独立 LLM session**(同默认模型)做抗
# 附和复核, 输出 verdicts。只有 B 的 symbol_check 已表明"符号在变更集不存在"且复核
# 也判 drop 时才真正删除——否则 keep/unknown(保留 + 打标)。绝不让复核成为新的误杀源。
VERIFY_TOOLS = [{
    "name": "verdicts",
    "description": (
        "Return a verdict for each flagged finding. Each finding has a trace index. "
        "Decide independently whether, based ONLY on the given evidence, the finding "
        "references a real code change. Default to 'unknown' unless clearly justified."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "description": "1-based index into the given list."},
                        "verdict": {"type": "string",
                                    "enum": ["keep", "drop", "unknown"],
                                    "description": ("keep=real finding, keep it; drop=clearly fabricated, "
                                                    "referenced symbol does not exist; "
                                                    "unknown=cannot decide, keep but mark.")},
                        "reason": {"type": "string", "description": "one sentence, Chinese"},
                    },
                    "required": ["index", "verdict"],
                }
            }
        },
        "required": ["verdicts"]
    },
}]


def _call_verify_batch(system_prompt, user_prompt, api_key, base_url, model, max_output_tokens):
    """独立复核调用: 用 VERIFY_TOOLS 工具壳, 返回 (verdicts_list, error)。

    verdicts_list: list[dict{index, verdict, reason}] 或 None(工具块缺失/失败)。
    mirrors _call_llm_batch 的 HTTP 结构与超时/重试。
    """
    body = {
        "model": model,
        "max_tokens": max_output_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
        "tools": VERIFY_TOOLS,
        "tool_choice": {"type": "any"},
    }
    payload = json.dumps(body).encode("utf-8")
    result = http_request(
        "POST", f"{base_url}/v1/messages",
        raw_body=payload.decode("utf-8"),
        headers={"Content-Type": "application/json", "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"},
        timeout=180, retries=3, backoff=2.0,
    )
    if result is None:
        return None, "LLM API request failed after retries"
    verdicts = None
    try:
        for b in (result.get("content") or []):
            if b.get("type") == "tool_use" and b.get("name") == "verdicts":
                inp = b.get("input") or {}
                if isinstance(inp.get("verdicts"), list):
                    verdicts = inp["verdicts"]
    except Exception:
        return None, "parse error"
    return verdicts, None



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
    url = f"https://{c_gitlab_host()}/api/v4/{path.lstrip('/')}"
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


def _diff_block_has_real_changes(block):
    """True if a per-file diff block contains at least one '+'/'-' CONTENT line that
    isn't pure whitespace/empty and isn't a header (diff --git / --- / +++ / @@ / slash-N).
    i.e. a real code change, not just EOL/whitespace or path/hunk markers. Used to drop
    files that appear in changed_files but have no actual text change — otherwise the
    model tends to invent findings on them (e.g. it fabricated 'tryClimb() 把...改为...'
    on an unchanged climbing block)."""
    for line in block.splitlines():
        if not line:
            continue
        if line.startswith(("\\ No newline", "diff --git", "--- a/", "+++ b/", "@@")):
            continue
        if (line.startswith("+") or line.startswith("-")) and line[1:].strip() != "":
            return True
    return False


def _sanitize_diff_blocks(blocks):
    """Given per-file diff blocks, return (kept_blocks, dropped_files). Drops any file
    block with no real content change (#3), so only genuinely-changed files reach the
    model. `dropped_files` are returned so callers can note/skip them in findings."""
    kept, dropped = [], []
    for b in blocks:
        if _diff_block_has_real_changes(b):
            kept.append(b)
        else:
            # record the file path for callers
            path = ""
            for line in b.splitlines():
                if line.startswith("+++ b/"):
                    path = line[6:].strip()   # '+++ b/xxx' -> 'xxx'
                    break
            dropped.append(path or "(unknown)")
    return kept, dropped


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


# ──────────────────────────────────────────────────────────────────────────
# 反编造 · 结构化防误杀审计 (阶段0/1)
# 目标: B 用确定性手段(pygments 词法 + checkout 真实 git diff)对每条 finding
#       校验 file 定位与符号真实性,产出 keep/flag/drop 三态 + 完整审计痕迹
#       (verification.vault),原始审查文本永不丢失,便于日后查误杀原因。
# 原则: 宁可 flag(保留打角标/复核),不可误删真实 finding。
# ──────────────────────────────────────────────────────────────────────────

def _strip_diff_prefix(leaf_or_path):
    """去掉 git diff 头的 'a/' / 'b/' 前缀,便于用 diff 里的返回路径匹配 finding.
    e.g. 'a/_source/foo.cpp' -> '_source/foo.cpp'"""
    if "/" in leaf_or_path:
        head, rest = leaf_or_path.split("/", 1)
        if head in ("a", "b"):
            return rest
    return leaf_or_path


def _locate_finding_file(raw_file, changed_files):
    """把 finding 的 file 定位为唯一完整相对路径。

    Returns (path, status):
      status ∈ {resolved, ambiguous, not_found, empty}
    - raw_file 是空 → empty
    - raw_file 完整且唯一命中 changed_files → resolved
    - 否则按 basename 在 changed_files 里找(去 a/ b/ 前缀):
        唯一 → resolved; 多个(client/server 同名)→ ambiguous; 0 → not_found
    """
    if not raw_file or not raw_file.strip():
        return "", "empty"
    raw = raw_file.strip()
    # 先建 leaf→[fullpaths] 映射
    leaf_map = {}
    for cf in changed_files or []:
        path = _strip_diff_prefix((cf.split("\t", 1)[-1] if "\t" in cf else cf).strip())
        leaf_map.setdefault(os.path.basename(path), []).append(path)

    # 1) 完整路径唯一命中
    if raw in leaf_map and len(leaf_map[os.path.basename(raw)]) == 1:
        return raw, "resolved"
    # 2) 带 a/ b/ 前缀的完整路径
    stripped = _strip_diff_prefix(raw)
    if stripped in leaf_map and len(leaf_map[os.path.basename(stripped)]) == 1:
        return stripped, "resolved"
    # 3) basename 唯一命中
    cands = leaf_map.get(os.path.basename(raw), [])
    if len(cands) == 1:
        return cands[0], "resolved"
    if len(cands) > 1:
        return "", "ambiguous"
    return "", "not_found"


def _normalize_findings_files(findings, changed_files):
    """统一 finding.file 为唯一完整相对路径。

    Returns (findings, trace_refs).
    - 每条 finding 写入 finding["file"] 为完整路径(若 resolved),并在 finding["trace_ref"] 记录稳定下标。
    - 非 resolved(empty/ambiguous/not_found)置 finding["_loc_state"],**不 drop**,交阶段1 flag。
    """
    out = []
    trace_refs = []
    for idx, f in enumerate(findings or []):
        raw = f.get("file") or ""
        path, status = _locate_finding_file(raw, changed_files)
        f = dict(f)                      # 浅拷贝,避免污染调度缓存
        f["trace_ref"] = idx             # 稳定下标(本次诊断会话内)
        if status == "resolved":
            f["file"] = path
        else:
            f["_loc_state"] = status     # empty/ambiguous/not_found
        out.append(f)
        trace_refs.append(idx)
    return out, trace_refs


# 渲染/计数路径白名单键 —— 除这些外,新加的 trace_ref/_loc_state 等键应被忽略。
# _build_markdown_from_findings / _findings_counts 均用 .get() 读已知键,余键自然被忽略。
_DISPLAY_FINDING_KEYS = ("file", "severity", "issue", "suggestion", "category", "line_number")


# ── 阶段1: B 确定性诊断 + 审计痕迹(verification.vault) ────────────────────

def _lex_identifiers(text):
    """用 pygments 从一段自然语言+代码文本里抽标识符(Name token)。

    只收 Name.* 类(排除 Comment / String / Operator / Punctuation),并过滤:
      - 纯数字、过短(<2)、中文串、pygments 元字符;
      - 常见英文 stopword / 中文连通词。
    Returns: list[str] 去重后的标识符。
    """
    if not text:
        return []
    from pygments import lex
    from pygments.lexers.c_cpp import CLexer
    from pygments.token import Name
    lexer = CLexer()
    _STOPWIN = set("""the a an and or to of in on for with from as by if else return void int
        float bool this nullptr true false const static class struct enum is are was were be been
        has have had should would could do does did not no yes when while than that then there here
        it its it's i we you they this thatthese""".split())
    tokens = []
    for ttype, value in lex(text, lexer):
        if ttype not in Name:
            continue
        s = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", s):
            continue
        if len(s) < 2 or len(s) > 96:
            continue
        if s in _STOPWIN:
            continue
        tokens.append(s)
    seen = set()
    out = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out



def _read_file_content(path):
    """读取文件内容(容 CRLF/编码),失败返回 None。"""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return None


def _lex_file_symbols(content):
    """对文件内容 pygments lex,返回 {identifier: count} (仅 Name token)"""
    from pygments.lexers.c_cpp import CLexer
    if not content:
        return {}
    from pygments import lex
    counts = {}
    lexer = CLexer()
    try:
        for ttype, value in lex(content, lexer):
            from pygments.token import Name
            if ttype not in Name:
                continue
            s = value.strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", s) and len(s) >= 2 and len(s) <= 96:
                counts[s] = counts.get(s, 0) + 1
    except Exception:
        pass
    return counts


def _build_repo_union_text(repo_dir, changed_files):
    """把全部变更文件内容并集为一份文本,作为"符号是否真实存在"的子串判据的语料。

    关键: code review 是对整个 MR 的审查,finding 常引用*其它*变更文件里的符号/fragment
    (如 prose 里的 `move_end`、`biped` 是较长真标识符 `_SquadMotorMoveState_move_end` /
    `BipedMotorDriver` 的子串)。因此"某 token 是否真实存在"用**子串出现在全变更文件
    并集**来判,而不是精确 token 匹配 —— 精确匹配会把真实 fragment 误判为不存在而误杀。

    返回 (union_text, repo_available)。repo 不可用 → (None, False)。
    """
    if not repo_dir or not os.path.isdir(repo_dir):
        return None, False
    parts = []
    for cf in changed_files or []:
        path = (_strip_diff_prefix(cf.split("\t", 1)[-1] if "\t" in cf else cf)).strip()
        if not path:
            continue
        content = _read_file_content(os.path.join(repo_dir, path))
        if content:
            parts.append(content)
    return "\n".join(parts), True


def _is_code_ish(token):
    """是否"代码式"标识符: 含大写字母或下划线(CamelCase / snake_case / UPPER)。

    纯小写单词(prose 词,如 bug/id/limit/driver)不算代码式 → 永不触发铁证 drop,
    避免把 prose 词汇误判为"不存在的符号"而误杀。
    """
    return bool(re.search(r"[A-Z_]", token))


def _token_present_in_union(union_text, token):
    """token 是否作为子串出现在全变更文件并集文本里。union_text=None → 视为存在(不定)。"""
    if union_text is None:
        return True   # repo 不可用 → 保守视为存在,不 drop
    return token in union_text


def _post_validate_findings(findings, diff_info):
    """对全量 findings 做 B 确定性诊断,产出 (kept_findings, traces)。

    - 逐条产出 trace = {trace_ref, loc_state, original, symbol_check,
                        decision, decision_reason, verification}
    - decision ∈ {keep, flag, drop}; drop 仅当 finding 里所有"代码式"标识符
      (含大写/下划线的 CamelCase|snake_case, 排除纯小写 prose 词)在**全变更文件并集**
      里子串都不存在 → 铁证编造。
    - 被 drop 的 finding 不进 kept_findings,但**完整 original+证据 留在 traces(→vault)**。
    - repo_dir 缺失 → 全部降级 flag,不 drop(拿不到真实语料时忌误删)。
    """
    repo_dir = diff_info.get("repo_dir") if diff_info else None
    base_branch = (diff_info.get("base_branch") or "") if diff_info else ""
    branch = (diff_info.get("branch") or "") if diff_info else ""
    changed_files = [(f.split("\t", 1)[-1] if "\t" in f else f) for f in
                     (diff_info.get("changed_files") or [])] if diff_info else []

    # 先做 file 唯一化(阶段0),拿到每条 finding 的最终 file / loc
    findings_norm, _ = _normalize_findings_files(findings, changed_files)

    repo_ok = bool(repo_dir) and os.path.isdir(repo_dir)
    union_text, repo_available = _build_repo_union_text(repo_dir, changed_files) if repo_ok else (None, False)

    kept = []
    traces = []
    for f in findings_norm:
        raw = f.get("file") or ""
        loc = f.get("_loc_state", "resolved")
        trace = {
            "trace_ref": f.get("trace_ref"),
            "loc_state": loc,
            "original": {
                "file": raw,
                "severity": f.get("severity"),
                "issue": f.get("issue"),
                "suggestion": f.get("suggestion"),
                "category": f.get("category"),
            },
            "symbol_check": [],
            "decision": "keep",
            "decision_reason": "",
            "verification": None,
        }

        # --- 定位不落实 → flag(绝不 drop) ---
        if loc in ("empty", "not_found"):
            trace["decision"] = "flag"
            trace["decision_reason"] = f"file 无法定位(状态={loc}),可能 LLM 路径错误或歧义"
            traces.append(trace)
            kept.append(f)
            continue
        if loc == "ambiguous":
            trace["decision"] = "flag"
            trace["decision_reason"] = "basename 撞名,无法确定唯一文件,留待复核"
            traces.append(trace)
            kept.append(f)
            continue

        # --- repo 不可用 → 全 flag 不 drop ---
        if not repo_ok:
            trace["decision"] = "flag"
            trace["decision_reason"] = "缺少可用 checkout(无法取变更文件语料),保守保留"
            traces.append(trace)
            kept.append(f)
            continue

        # --- B3: 代码式标识符是否存在(子串判据,全变更文件并集) ---
        tokens = _lex_identifiers((f.get("issue") or "") + "\n" + (f.get("suggestion") or ""))
        code_tokens = [t for t in tokens if _is_code_ish(t)]
        code_tokens = [t for t in code_tokens if t not in _VERIFY_STOPWIN]

        if not code_tokens:
            # 没有可判代码式符号 → 无法证伪,保留
            trace["decision_reason"] = "未抽取到代码式标识符(worded),无法证伪"
            traces.append(trace)
            kept.append(f)
            continue

        checks = []
        absent = []      # 代码式标识符在并集里子串不存在 → 编造候选
        for t in code_tokens:
            present = _token_present_in_union(union_text, t)
            checks.append({"token": t, "present_in_changed_files": present})
            if not present:
                absent.append(t)
        trace["symbol_check"] = checks

        if absent and len(absent) == len(code_tokens) and repo_available:
            # 所有代码式标识符全都不存在(并非只有个别符号不存在) → 铁证编造(drop)。
            # 保守方向: 只要 finding 引用到任何一个真实存在于变更集的符号,就保留,
            # 避免因个别 token 在变更集外(头文件/依赖/未变更文件)而误杀真实 finding。
            trace["decision"] = "drop"
            trace["decision_reason"] = "引用的代码式符号全部(" + "、".join(
                f"`{t}`" for t in absent[:5]) + ")在本 MR 变更文件中子串不存在 → 铁证编造"
            traces.append(trace)
            continue  # 不进 kept(drop)
        elif absent and repo_available:
            # 部分代码式符号缺(混合真实+疑似编造): 不整体 drop,保留 + 打标待复核
            trace["decision"] = "flag"
            trace["decision_reason"] = "部分代码式符号(" + "、".join(
                f"`{t}`" for t in absent[:3]) + ")不在变更集,但其余符号真实存在,保留待复核"
            traces.append(trace)
            kept.append(f)
            continue
        elif absent and not repo_available:
            trace["decision"] = "flag"
            trace["decision_reason"] = "语料不可用,`" + "、".join(absent[:3]) + \
                                       "` 无法跨文件核验,保守保留"
            traces.append(trace)
            kept.append(f)
            continue
        else:
            trace["decision"] = "keep"
            trace["decision_reason"] = "引用的代码式符号均在本 MR 变更文件中存在,保留"
            traces.append(trace)
            kept.append(f)

    return kept, traces


# ── 阶段1.5: 结论可信度分层(确定 keep/drop + 存疑降 warn,无人工复核) ──────────
# 目标: 把"语义推断型"结论(锁/循环边界/off-by-one)与"客观可证事实"(垃圾文件/符号缺失)
# 区分开。确定不成立/误报的 drop,拿不准的降 warn。全部确定性规则,不调第二个 LLM。
# 复用: stage-1 symbol_check(符号真伪) + changed_files + diff +/- 行。

# 垃圾/误提交文件启发词
_GARBAGE_FILE_MARKERS = ("is not recognized as", "not recognized as", "operable program",
                         "internal or external command", "cannot find the path",
                         "未被识别为", "不是内部或外部命令")


def _looks_like_garbage(rel_path, content):
    """确定性判"是否为误提交的垃圾文件"。
    要么文件名含 '('(shell 重定向残留,如 *.cpp(40),铁证),
    要么内容完全不像是源码(纯 shell 错误 / 无任何 C++ 标识符)。"""
    if not rel_path:
        return False
    if "(" in os.path.basename(rel_path):
        return True
    if content is None:
        return False   # 读不到不算垃圾(避免误判)
    c = content or ""
    # 明确 shell 错误文本
    for mark in _GARBAGE_FILE_MARKERS:
        if mark in c:
            return True
    # 内容非空但没有任何 C/C++ 标识符/关键字 → 极可能是垃圾
    if c.strip() and not _lex_file_symbols(c):
        # 再确认确实不是合法纯注释文件(允许只有注释的头)
        stripped = re.sub(r"//.*|/\*.*?\*/|#.*", "", c, flags=re.S).strip()
        if not stripped:
            return False   # 只是注释 → 不算垃圾
        return True
    return False


# 存疑话术(真正的"拿不准/待核实"),用于 warn 降级。避开"若/可能/如果"等几乎所有 finding
# 都会天然带的条件词 —— 否则 warn 击穿 80% 以上(实测 CB2N-27312: 30/37),指标无区分度。
_HEDGE_MARKERS = ("建议确认", "需确认", "需要确认", "是否存在", "建议核实", "待确认",
                  "无法确定", "未能确定", "建议进一步", "需进一步", "尚需", "存疑",
                  "不确定", "需要人工", "仍需核实", "无法证明")


def _is_hedged(txt):
    low = (txt or "").lower()
    return any(h in low for h in _HEDGE_MARKERS)


# ── A-3: 文本冲突判定(把 finding 的"硬断言"与真实代码比对, 矛盾即误报) ──────────
# 只在 finding 用了"明确硬否定"(函数 X 无锁/未修改/未加锁/删除了 foo)时才开枪; 且
# 用真实源码逐字核实该断言是否被推翻。命中 -> drop(客观误报)。任何模糊表述/拿不准 ->
# 绝不动(宁 keep/warn 不误删)。这是判定"误报"的唯一有判别力的判据。
_LOCK_TOKENS = ("CMP_SCOPE_SHARED_LOCK", "CMP_SCOPE_EXCLUSIVE_LOCK", "CMP_SCOPE_SPINLOCK",
                "std::lock_guard", "std::unique_lock", "mutex", "SpinLock", "spin_lock",
                "AcquireRwLock", "AcquireTrimShared", "m_lock", "lua_lock",
                "LockGuard", "AutoLock", "ScopedLock", "Relock", r"\.lock(")

# 硬否定句式: 明确断言某符号"无锁/未被修改/未加X/未使用"。捕获符号名。
_HARD_NEG_RE = re.compile(
    r"([A-Za-z_]\w{2,})\s*(?:仍无锁|无锁|未加锁|不加锁|没有锁|未被修改|未修改|未加保护|未加任何锁"
    r"|不携带锁|未使用任何锁|未受保护|未加同步|无任何锁保护)", re.IGNORECASE)


def _function_body(content, sym):
    """近似提取源码里符号 sym 的【函数定义体】片段。

    A-3 用: 判断该函数体内是否真含锁宏。刻意跳过"调用点"(如 `x = GetBlockSize( ... )`),
    只认**定义处**(`Type Class::sym(...) {` 或 `Type sym(...) {`)。找不到/无法配平 -> None(不判冲突)。
    """
    if not content or not sym:
        return None
    start = 0
    while True:
        idx = content.find(sym, start)
        if idx < 0:
            return None
        # 前一个非空白字符: 定义通常前面是 `::`(成员)或返回类型/行首; 调用点前多是 `= ( . , >`
        before = ""
        j = idx - 1
        while j >= 0 and content[j] in " \t":
            j -= 1
        if j >= 0:
            before = content[j]
        # 定义: 前面是 `::`(成员定义) 或 字母数字/下划线(返回类型紧贴) 或 行首
        if before in ("=", "(", ")", ".", ",", ">", "-"):
            is_def_like = False          # 明显是调用点/赋值/成员访问
        else:
            is_def_like = True           # ::(定义) / 返回类型 / 行首空白
        # 符号后必须是 '(' (函数)
        k = idx + len(sym)
        while k < len(content) and content[k] in " \t":
            k += 1
        if k >= len(content) or content[k] != "(":
            is_def_like = False
        if not is_def_like:
            start = idx + 1
            continue
        # 找函数体首 '{'
        open_b = content.find("{", k)
        if open_b < 0:
            return None
        depth = 0
        i = open_b
        n = len(content)
        while i < n:
            c = content[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    line_start = content.rfind("\n", 0, idx) + 1
                    return content[line_start:i + 1]
            i += 1
        return None



def _A3_assertion_conflicts(txt, file, repo_dir):
    """A-3 文本冲突判定。Returns (conflicts: bool, evidence: str).

    对 finding 里每个"硬否定函数命题"(sym 无锁/未被修改), 取该函数体真实源码片段,
    若片段里含锁宏 -> finding 断言被源码推翻 -> conflict(误报, drop)。
    找不到函数体 / 源码读不到 / 无锁宏 -> 不判冲突(宁 keep 不误删)。
    """
    if not (file and repo_dir):
        return False, ""
    content = _read_file_content(os.path.join(repo_dir, file))
    if not content:
        return False, ""
    for m in _HARD_NEG_RE.finditer(txt):
        sym = m.group(1)
        body = _function_body(content, sym)
        if body is None:
            continue
        for lock in _LOCK_TOKENS:
            if lock in body:
                return True, f"finding 断言 `{sym}` 无锁/未被修改, 但源码 {sym} 函数体内含锁宏 {lock} -> 断言与代码矛盾"
    return False, ""



def _objective_verdict(f, repo_dir):
    """A-1 垃圾文件客观判定。返回 (verdict, reason)；非垃圾文件 -> ('keep_nonobjective', '')。

    - 指向真实误提交垃圾文件且 finding 在讲清理/删除 -> ('keep', …) 客观为真
    - 指向垃圾文件却硬造代码 bug -> ('drop', …) 客观为假(无代码可审)
    其余(非垃圾文件) -> ('keep_nonobjective', '') 交给后续阶段判定。
    """
    file = (f.get("file") or "").strip()
    if not file:
        return "keep_nonobjective", ""
    content = _read_file_content(os.path.join(repo_dir, file)) if repo_dir else None
    if not _looks_like_garbage(file, content):
        return "keep_nonobjective", ""
    txt = ((f.get("issue") or "") + " " + (f.get("suggestion") or "")).lower()
    if any(k in txt for k in ("垃圾", "rm", "删除", "清理", "误提交", "garbage", "废")):
        return "keep", "指向确定的误提交垃圾文件, 客观事实"
    return "drop", "指向的 file 本身是误提交垃圾文件, 无有效代码可审"


def _mark_objective_findings(findings, repo_dir):
    """阶段0.5: 把所有 A-1 垃圾文件客观事实先定死(_objective=True), 冻结 keep/drop。

    必须在阶段1 符号检查/阶段2 复核之前跑——否则垃圾文件事实(如 ref5)会因某个符号
    (如 `PowerShell`) 不在变更集而在后续阶段被误 drop。冻结后后续阶段不再重判它。"""
    out = []
    for f in (findings or []):
        verdict, reason = _objective_verdict(f, repo_dir)
        if verdict in ("keep", "drop"):
            f = dict(f)
            f["_objective"] = True
            f["confidence"] = verdict
            f["_confidence_reason"] = reason
        out.append(f)
    return out


def _classify_confidence(finding, changed_files, repo_dir):
    """对单条 finding 做结论可信度分层,返回 ('keep'|'drop'|'warn', reason)。

    - drop: 客观确定不成立/误报(垃圾文件的错误断言 / 核心符号确不在变更集)
    - keep: 客观可证为真的 / 明确关键改动 / 无需降级的正式分析
    - warn: 存疑(显式猜测话术,或纯风格/性能建议,无确凿缺陷主张),降级展示
    不调用 LLM,不人工复核。任何不确定性 → 宁 keep 不误杀、宁 warn 不误删。
    """
    f = finding or {}
    txt = (f.get("issue") or "") + "\n" + (f.get("suggestion") or "")
    file = (f.get("file") or "").strip()

    # A-1 垃圾文件: 复用 _objective_verdict(已在阶段0.5 提前定死; 此处为兜底以防未提前标记)
    if file:
        ov, oreason = _objective_verdict(f, repo_dir)
        if ov in ("keep", "drop"):
            return ov, oreason

    # A-3 文本冲突: finding 硬断言"函数 X 无锁/未修改/删除了 foo", 但真实源码推翻它 -> 误报, drop
    if repo_dir and file:
        conflict, ev = _A3_assertion_conflicts(txt, file, repo_dir)
        if conflict:
            return "drop", f"误报: {ev}"

    # B hedge → warn(显式猜测话术)
    if _is_hedged(txt):
        return "warn", "存疑: 使用了可能/若/建议确认等猜测话术, 结论需以真代码核对"

    # C 纯风格/性能建议、无确凿缺陷主张 → warn(不算确定问题, 保留)
    sev = (f.get("severity") or "").strip().lower()
    cat = (f.get("category") or "").strip().lower()
    if sev in ("suggestion", "info", "note", "low", "nit", "minor") and cat == "quality":
        return "warn", "纯风格/性能建议, 非确定缺陷"

    # 其余(明确 critical/warning 且非猜测话术的正式分析) → keep
    return "keep", "正式分析, 保留(语义是否成立以 diff/真代码为准, 不作客观删除)"


def _apply_confidence(findings, diff_info):
    """对全量 findings 应用(阶段1.5)结论可信度分层。

    返回 (kept, notes)。
      - keep: 保留, finding["confidence"]="keep"
      - drop: 从返回里移除(进 notes["drop"] 供 vault 溯源),不再出现在 report
      - warn: 保留, finding["confidence"]="warn", severity 不升
    """
    repo_dir = (diff_info or {}).get("repo_dir")
    changed_files = [(f.split("\t", 1)[-1] if "\t" in f else f) for f in
                     ((diff_info or {}).get("changed_files") or [])]
    kept = []
    notes = {"drop": [], "warn": 0}
    for f in (findings or []):
        # 已由阶段0.5 冻结的客观判定(垃圾文件 keep/drop) -> 不再重判; 照原样归置
        if f.get("_objective"):
            if f.get("confidence") == "drop":
                notes["drop"].append(f)
            else:
                kept.append(f)
            continue
        verdict, reason = _classify_confidence(f, changed_files, repo_dir)
        if verdict == "drop":
            f = dict(f)
            f["confidence"] = "drop"
            f["_confidence_reason"] = reason
            notes["drop"].append(f)          # 进 vault,不进 report
            continue
        f = dict(f)
        if verdict == "warn":
            f["confidence"] = "warn"
            f["_confidence_reason"] = reason
            notes["warn"] += 1
        else:
            f["confidence"] = "keep"
        kept.append(f)
    return kept, notes


def _verify_flags(kept_findings, traces, diff_info, api_key, base_url, model, max_output_tokens):
    """阶段2: 独立复核(自检回环)。对 B 判定为 flag 且 file 可定位的 finding,
    用另一个独立 LLM session(同默认模型)做抗附和复核, 输出 verdicts 后合并。

    规则(防误杀, 复核绝不成为新的误杀源):
      - 只有 B 的 symbol_check 已有一条"该代码式符号在变更集不存在(absent)"的记录,
        且独立复核也判 drop 时, 才真正 drop。
      - 复核判 keep → 保留。
      - 复核判 unknown / 复核调用失败 / 无 absent 记录的 flag → 保留 + 打标(不改 verdict)。
      - 只处理 file 可定位(resolved)的 flag; 定位不实(empty/ambiguous/not_found)的
        flag 不交复核(避免在无法确定文件时依赖复核误判)。

    返回 (final_findings, traces_updated)。任何复核异常都不抛, 回退为"全部保留"。
    """
    # 选可复核的 flag: decision==flag 且 loc_state==resolved 且【所有】代码式符号都不在变更集。
    # （与阶段1 的 drop 口径一致: absent==len(code_tokens)。用 any() 太松——真实 finding 常引用
    #  1-2 个跨文件/拼接错的符号, 其余都是真实代码, 任一 absent 就交复核/可能 drop 会误杀
    #  如 `Uninitialize 未释放 m_slabInfos`、垃圾文件 等真实 finding。）
    # 已由阶段0.5 冻结的 _objective finding(垃圾文件事实) 不交复核, 不被 drop。
    objective_refs = {f.get("trace_ref") for f in (kept_findings or []) if f.get("_objective")}
    verifyable = []
    for t in traces:
        if t["decision"] != "flag":
            continue
        if t["loc_state"] != "resolved":
            continue
        if t["trace_ref"] in objective_refs:
            continue          # 客观事实(垃圾文件)不交给复核
        checks = t.get("symbol_check") or []
        code_absent = [c for c in checks if not c.get("present_in_changed_files")]
        if not code_absent or len(code_absent) != len(checks) or not checks:
            continue          # 只复核"全部符号都缺失"的疑似编造
        verifyable.append(t)

    if not verifyable:
        return kept_findings, traces

    if not api_key:
        return kept_findings, traces

    # 组装复核输入(一条 prompt, 一次调用; 证据取原 finding + B 的 symbol_check)
    lines = []
    for i, t in enumerate(verifyable, start=1):
        o = t["original"]
        lines.append(
            f"[{i}] file: {o['file']}\n"
            f"    severity: {o.get('severity')}\n"
            f"    issue: {o.get('issue')}\n"
            f"    suggestion: {o.get('suggestion')}\n"
            f"    B检查: " + " ".join(
                (("'"+c['token']+"' present" if c['present_in_changed_files']
                  else "'"+c['token']+"' NOT-in-changed-files"))
                for c in (t.get("symbol_check") or []))
        )
    prompt = (
        "你是独立的 code review 复核员。下面是另一个 review session 对同一 MR diff 生成、"
        "被初筛判为'存疑(flag)'的 finding, 以及自动校验器(B)对每个引用符号的检查结果。\n"
        "请**只基于以下证据**、独立思考, 对每条给出 verdict:\n"
        "  - keep   : 引用的代码式符号确实存在/确实是真实改动 → 保留。\n"
        "  - drop   : 引用的代码式符号在本 MR 变更文件中完全不存在(B 已标 NOT-in-changed-files)",
        "→ 判定为明显编造。\n"
        "  - unknown: 证据不足、无法确定 → 保留并标 unknown。\n"
        "**除非有确凿证据(尤其 B 的 NOT-in-changed-files 且该符号确实无出处), 否则默认 keep 或 unknown;"
        " 不要臆测 diff 里没有的改动。不要新增 finding, 不扩大范围。**\n\n"
        + "\n\n".join(lines)
    )
    sys_prompt = ("You are an independent code-review verifier. You only arbitrate whether "
                  "each given finding references real changed code, based strictly on the "
                  "provided evidence. Default to keep/unknown unless the evidence clearly "
                  "shows fabrication. Reply in Chinese via the verdicts tool.")

    try:
        verdicts, err = _call_verify_batch(
            sys_prompt, prompt, api_key, base_url, model, max_output_tokens)
    except Exception as e:
        err = f"verify call exception: {e}"
        verdicts = None
    if err or not verdicts:
        # 复核失败/缺结果 → 全部保留(flag 保持, 不打 drop)
        for t in verifyable:
            t["verification"] = {"error": err or "no verdicts", "verdict": "unknown"}
        return kept_findings, traces

    # 合并 verdicts
    by_idx = {}
    for v in verdicts:
        try:
            by_idx[int(v.get("index"))] = v.get("verdict", "unknown").strip().lower()
        except (TypeError, ValueError):
            continue

    drop_trace_refs = set()
    for i, t in enumerate(verifyable, start=1):
        vd = by_idx.get(i, "unknown")
        checks = t.get("symbol_check") or []
        # 只有【全部】代码式符号都缺失(content+include+非宏三样)且复核也判 drop 才真正删除
        code_absent = [c for c in checks if not c.get("present_in_changed_files")]
        all_absent = (checks and len(code_absent) == len(checks))
        reason = ""
        for v in verdicts:
            if str(v.get("index")) == str(i):
                reason = v.get("reason") or ""
                break
        t["verification"] = {"verdict": vd, "reason": reason}
        if vd == "drop" and all_absent:
            drop_trace_refs.add(t["trace_ref"])

    final = [f for f in kept_findings if f.get("trace_ref") not in drop_trace_refs]
    for t in traces:
        if t["trace_ref"] in drop_trace_refs:
            t["decision"] = "drop"
            t["decision_reason"] = (t["decision_reason"] + "[阶段2复核确认 drop]" if t["decision_reason"]
                                    else "[阶段2复核确认 drop]")
    return final, traces





# 常见 stopword: 诊断复核时跳过,避免把普通英文动词当"符号"
_VERIFY_STOPWIN = set("""the a an and or to of in on for with from as by is are was were be
    been has have had do does did should would could not no when while than then this that
    will shall can may must need use using used change changes changed add adds added remove
    removes removed make makes made fix fixes fixed call calls called trigger triggers triggered
    review reviews reviewed suggest suggests suggested confirm confirms confirmed ensure ensures
    checked check checking""".split())


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

    # (方案C更新: 不截断字符 —— 显示完整 issue/suggestion/summary/strengths。
    #  仅当某严重度条数过多时按 MAX_FINDINGS_PER_SEV 封顶，并明确标注剩余条数，绝不静默截断。)
    MAX_FINDINGS_PER_SEV = 20

    parts = [f"🔍 Code Review — {total} 项 ({c['critical']} 必改)"]
    # Summary: 完整
    if meta.get("summary"):
        parts.append(f"Summary：{meta['summary']}")
    # Strengths: 完整（至多 3 条）
    if meta.get("strengths"):
        strengths = [s for s in meta["strengths"][:3] if s]
        if strengths:
            parts.append("✅ " + "；".join(strengths))
    # Architecture & Performance -> folded straight into findings (full below).

    # Findings by severity (每条完整显示)。
    for k, label in (("critical", "blocking"), ("warning", "important"), ("suggestion", "nit")):
        if not groups[k]:
            continue
        emoji, tag, rank = groups[k][0]["_tag"]
        parts.append(f"{emoji} {tag} ({len(groups[k])})")
        shown = groups[k][:MAX_FINDINGS_PER_SEV]
        for f in shown:
            issue = (f.get("issue") or "").strip()
            fix = (f.get("suggestion") or "").strip()
            fname = os.path.basename((f.get("file") or "").strip().rstrip("/")) or (f.get("file") or "?")
            desc = f"{fname}: {issue}"
            if fix:
                desc += f" → {fix}"
            cat = (f.get("category") or "").strip()
            # 结论可信度角标(阶段1.5): confidence==warn 的语义推断型结论, 标注存疑、勿作确定结论
            conf = (f.get("confidence") or "").strip()
            warn_note = ""
            prefix = "·"
            if conf == "warn":
                prefix = "⚠️"
                warn_note = "（存疑，结论可能不成立）"
            head = f"{prefix} [{cat}] {desc}{warn_note}" if cat else f"{prefix} {desc}{warn_note}"
            parts.append(head)
        if len(groups[k]) > MAX_FINDINGS_PER_SEV:
            parts.append(f"  （该级别共 {len(groups[k])} 条，已显示 {MAX_FINDINGS_PER_SEV} 条，其余见完整报告）")

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

    base_url = c_claude_base_url()
    model = config["claude"]["model"]
    max_output_tokens = config["claude"]["max_tokens"]
    system_prompt = config["claude"]["review_instructions"] \
                    or DEFAULT_REVIEW_INSTRUCTIONS.strip()

    # Split into per-file blocks and group into batches
    blocks = _split_diff_by_files(diff_text)
    # 方案#3: 过滤掉「出现在 changed_files 但无真实 +/- 内容」的文件块(如某文件在 diff 里 0
    # 行实际改动), 避免模型在这些文件上编造 finding。同时把 dropped 文件从 changed_files 里移除。
    blocks, dropped_files = _sanitize_diff_blocks(blocks)
    if dropped_files:
        print(f"[review] dropped {len(dropped_files)} no-real-change file block(s): {dropped_files[:5]}...", flush=True)
    if not blocks:
        blocks = [diff_text]
    if dropped_files:
        all_files = diff_info.get("changed_files") or []
        drop_leaf = {p.rsplit("/", 1)[-1] for p in dropped_files if p}
        keep = []
        for f in all_files:
            path = f.split("\t", 1)[-1] if "\t" in f else f   # strip 'M\t' prefix
            leaf = path.rsplit("/", 1)[-1]
            # drop if this changed-file's basename matches any dropped-file basename
            # (no-real-change block was dropped; don't list it as a changed file).
            if leaf in drop_leaf:
                continue
            keep.append(f)
        diff_info["changed_files"] = keep
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

只审查上面 diff 中真实出现的 `+`/`-` 行与真实函数；引用不存在的函数/未改动代码即视为编造，应避免。若无真实问题则无 finding。

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

只审查上面 diff 中真实出现的 `+`/`-` 行与真实函数/符号；引用不存在的函数或未改动代码即编造，应避免。若无真实问题则无 finding。

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

    # 阶段0.5: 垃圾文件客观事实(keep/drop 冻结) 提前定死, 必须在阶段1/2 之前——
    # 否则 ref5(垃圾文件)会因某符号(如 PowerShell)不在变更集而被后续阶段误 drop。
    try:
        all_findings = _mark_objective_findings(all_findings, diff_info.get("repo_dir") if diff_info else None)
    except Exception as e:
        print(f"[review] objective-marker skipped: {e}", flush=True)

    # 反编造 · 结构化防误杀(阶段0/1): 对全量 findings 做确定性校验,产出 keep/flag/drop
    # + 完整审计痕迹(verification.vault)。原始审查文本随 trace 保留,drop 的 finding 不进
    # findings 但原文+证据留在 vault,便于日后查误杀。done: 不阻塞主流程,任何异常降级保留。
    try:
        kept_findings, traces = _post_validate_findings(all_findings, diff_info)
    except Exception as e:            # 非常稳的一层: 校验失败绝不影响出报告
        kept_findings, traces = all_findings, []
        print(f"[review] verification layer skipped: {e}", flush=True)

    # 阶段2: 独立复核(自检回环)—— 对 B 判定 flag 且 file 可定位的 finding 用另一个
    # LLM session(同默认模型)复核。默认开启, 可用 config claude.verify_flags=false 关闭。
    # 复核失败/被关 → 原样保留(flag 保持, 新增 verification 记录)。绝不因复核引入新误杀。
    step2 = config.get("claude", {}).get("verify_flags", True)
    if step2 and not traces:
        step2 = False                     # 无 trace(校验层跳过)→ 无事可复核
    if step2:
        try:
            kept_findings, traces = _verify_flags(
                kept_findings, traces, diff_info,
                api_key, c_claude_base_url(), model, max_output_tokens,
            )
        except Exception as e:
            print(f"[review] stage-2 verify skipped: {e}", flush=True)   # 保守: 保留原状

    # 阶段1.5: 结论可信度分层(确定 keep/drop + 存疑降 warn,无人工复核)。
    # 在阶段0/1/2 的基础上,把"语义推断型"结论与"客观可证事实"区分: 确定不成立/误报的 drop,
    # 拿不准(hedging/架构质量推断)的降 warn,客观为真的 keep。任何不确定宁 warn 不误杀。
    try:
        kept_findings, conf_notes = _apply_confidence(kept_findings, diff_info)
    except Exception as e:
        conf_notes = {"drop": [], "warn": 0}
        print(f"[review] confidence layer skipped: {e}", flush=True)
    conf_dropped = len(conf_notes.get("drop") or [])
    conf_warned = conf_notes.get("warn") or 0
    # confidence-drop 也进 verification.vault 供溯源(不改阶段1 的 traces,单独记)
    conf_vault = {"dropped": conf_notes.get("drop") or [], "warned_count": conf_warned}

    drop_n = sum(1 for t in traces if t["decision"] == "drop")
    flag_n = sum(1 for t in traces if t["decision"] == "flag")
    keep_n = sum(1 for t in traces if t["decision"] == "keep")
    verification_block = {
        "vault": traces,
        "counts": {"kept": keep_n, "flagged": flag_n, "dropped": drop_n},
        "confidence": conf_vault,
        "dropped_decision": "symbol absent in changed-file union, non-macro; "
                            "confirmed by stage-2 re-verify when flagged",
        "repo_dir_available": bool(diff_info.get("repo_dir")),
        "base_branch": diff_info.get("base_branch") or "",
        "branch": diff_info.get("branch") or "",
        "stage2_verify": step2,
        "generated_by": {"model": model},
    }
    if drop_n:
        print(f"[review] anti-fab verified: dropped {drop_n} fabricated finding(s), "
              f"flagged {flag_n}, kept {keep_n}", flush=True)
    if conf_dropped or conf_warned:
        print(f"[review] confidence: dropped {conf_dropped} objective-false / "
              f"warned {conf_warned} speculative finding(s)", flush=True)

    # 关键: severity_counts 必须与【最终 kept_findings】一致,不能用批次聚合的 `total`。
    # 否则阶段1/2/1.5 过滤(drop 掉某些 finding)之后,severity_counts 会残留被删 finding 的计数,
    # 造成"卡片显示 0 critical / 但 summary 说 2 critical"的自相矛盾(ENG-32269 命中的 bug)。
    # 也顺带修掉"某批次走文本回退 _count_severities 造成的计数与结构化 findings 不一致"的旧问题。
    final_counts = _findings_counts(kept_findings)

    # 若阶段1/2/1.5 真正删除了 finding(drop_n>0 或 conf_dropped>0), 卡片文案必须按 kept_findings
    # 重建,否则卡片仍显示被删的 finding。仅在确有 drop 时重建,避免每次多花一次渲染。
    if drop_n > 0 or conf_dropped > 0:
        final_text = _build_markdown_from_findings(kept_findings, meta=agg_meta)

    return {
        "summary": agg_meta.get("summary", ""),
        "review_text": final_text,
        "severity_counts": final_counts,
        "findings": kept_findings,
        "error": first_error,   # non-None if at least one batch failed (partial results)
        "batches": num_batches,
        "verification": verification_block,
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
