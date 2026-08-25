# -*- coding: utf-8 -*-
"""Build verification context for a single manual review issue.

Used by the topic agent to verify whether a code change addresses a
human reviewer's GitLab `DiffNote` comment. Outputs a self-contained
prompt + structured context that can be fed directly to a Sonnet
sub-agent. See DESIGN §1.14.1 for the verdict semantics.

The topic agent typically calls this once per manual issue and spawns
Sonnet sub-agents in parallel, one per issue. Each sub-agent returns
JSON `{status, rationale}` which the parent persists to the topic
file.

Why a helper, not just inline shell: pulling original-code-at-base_sha
and current-code-at-HEAD is fiddly (line numbers shift, files may be
renamed or deleted). Centralising the context build means every
sub-agent gets the same shape and the parent doesn't repeat the same
git plumbing N times.

Usage:
    python manual_issue_verifier.py context \\
        --repo-root <path> --topic <topic.json> --index <M-index>
"""
import argparse
import json
import os
import sys
import textwrap
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import subprocess_util
import topic_store


# Lines of context above and below the cited line.
CONTEXT_RADIUS = 10

# Five canonical verdicts. The sub-agent MUST return one of these.
VALID_STATUSES = {
    "addressed",
    "not_addressed",
    "partially_addressed",
    "obsolete",
    "unclear",
}


def _run_git(args, cwd, timeout_s=30):
    try:
        proc = subprocess_util.hidden_run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True,
            timeout=timeout_s, encoding="utf-8", errors="replace")
        return proc.stdout, proc.stderr, proc.returncode
    except OSError as exc:
        return "", str(exc), -1


def _show_file_at(repo_root, sha, path):
    """Return file contents at <sha>:<path>, or '' if missing/error."""
    out, err, rc = _run_git(["show", f"{sha}:{path}"], cwd=repo_root)
    if rc != 0:
        return None, (err or "").strip()[:200]
    return out, None


def _slice_lines(text, target_line, radius=CONTEXT_RADIUS):
    """Return (range_start, range_end, text_slice) around target_line.

    Lines are 1-indexed. range_start/end are inclusive. If target_line
    is out of range (e.g. the file is shorter than the cited line —
    happens for obsolete code), the slice is empty and the caller
    should treat the result as "obsolete" candidate.
    """
    if not text:
        return 0, 0, ""
    lines = text.splitlines()
    if target_line is None or target_line < 1 or target_line > len(lines):
        return 0, 0, ""
    lo = max(1, target_line - radius)
    hi = min(len(lines), target_line + radius)
    numbered = []
    for i in range(lo, hi + 1):
        numbered.append(f"{i:5d}  {lines[i - 1]}")
    return lo, hi, "\n".join(numbered)


def _diff_slice(repo_root, base_sha, head_sha, path):
    """Get the unified diff for one file between two SHAs.

    Empty string when the file didn't change between the two SHAs (the
    issue's region is unchanged — likely `not_addressed`).
    """
    # -w / --ignore-cr-at-eol: same CRLF/whitespace tolerance as the review diff — a
    # whitespace-only re-normalization of the issue's region must read as "unchanged".
    out, err, rc = _run_git(
        ["diff", "--unified=10", "-w", "--ignore-cr-at-eol", f"{base_sha}..{head_sha}", "--", path],
        cwd=repo_root)
    if rc != 0:
        return None, (err or "").strip()[:200]
    return out or "", None


def build_context(repo_root, manual_issue, head_sha):
    """Assemble the verification context for one manual issue.

    Args:
        repo_root: Path to the repo (chaos or rage workspace).
        manual_issue: a dict from `review.manual_issues[]`.
        head_sha: current MR branch HEAD SHA (caller refreshed via
                  `git ls-remote` or `glab api`).

    Returns:
        dict with keys {issue, original_code, current_code, diff_slice,
                         head_sha, fetch_errors}.
    """
    file = manual_issue.get("file") or ""
    line_new = manual_issue.get("line_new")
    line_old = manual_issue.get("line_old")
    base_sha = manual_issue.get("base_sha") or ""

    fetch_errors = []
    original_code = ""
    current_code = ""
    diff_slice_text = ""

    # Best-effort: ensure both SHAs are present locally.
    if base_sha:
        _run_git(["fetch", "origin", base_sha], cwd=repo_root, timeout_s=60)
    if head_sha:
        _run_git(["fetch", "origin", head_sha], cwd=repo_root, timeout_s=60)

    # Original code at the comment's base_sha (the snapshot the
    # reviewer was looking at).
    if base_sha and file:
        text, err = _show_file_at(repo_root, base_sha, file)
        if err:
            fetch_errors.append(f"original: {err}")
        elif text is not None:
            target = line_new or line_old
            _, _, slice_text = _slice_lines(text, target)
            original_code = slice_text

    # Current code at HEAD. Line may have shifted; we attempt to
    # locate the corresponding region by fuzzy line first (the diff
    # slice below carries the authoritative shift information).
    if head_sha and file:
        text, err = _show_file_at(repo_root, head_sha, file)
        if err:
            fetch_errors.append(f"current: {err}")
        elif text is None:
            current_code = ""  # file deleted — strong obsolete signal
        else:
            target = line_new or line_old
            _, _, slice_text = _slice_lines(text, target)
            current_code = slice_text

    # Diff for this one file across the verification window. The
    # sub-agent uses this to see exactly what changed in the region.
    if base_sha and head_sha and file and base_sha != head_sha:
        diff_text, err = _diff_slice(repo_root, base_sha, head_sha, file)
        if err:
            fetch_errors.append(f"diff: {err}")
        elif diff_text is not None:
            diff_slice_text = diff_text

    return {
        "issue": {
            "index": manual_issue.get("index"),
            "discussion_id": manual_issue.get("discussion_id"),
            "author": manual_issue.get("author"),
            "file": file,
            "line_old": line_old,
            "line_new": line_new,
            "base_sha": base_sha,
            "body": manual_issue.get("body"),
            "web_url": manual_issue.get("web_url"),
        },
        "head_sha": head_sha,
        "original_code": original_code,
        "current_code": current_code,
        "diff_slice": diff_slice_text,
        "fetch_errors": fetch_errors,
    }


def build_prompt(context):
    """Assemble a Chinese verification prompt for the Sonnet sub-agent.

    Output is a single string. The sub-agent's only job is to return
    one JSON line matching `VALID_STATUSES`. Keep this prompt tight —
    every extra paragraph is an Opus/Sonnet token bill on every cycle.
    """
    issue = context["issue"]
    body = issue.get("body") or "(空)"
    file = issue.get("file") or "(unknown)"
    line = issue.get("line_new") or issue.get("line_old") or "?"
    author = issue.get("author") or "(unknown)"
    base_sha = (issue.get("base_sha") or "")[:8]
    head_sha = (context.get("head_sha") or "")[:8]

    original_code = context.get("original_code") or "(无 — 文件可能在 base_sha 时不存在)"
    current_code = context.get("current_code") or "(无 — 当前 HEAD 文件不存在或行号越界，可能是 obsolete 信号)"
    diff_slice = context.get("diff_slice") or "(基线与 HEAD 之间该文件无差异 — 可能是 not_addressed 信号)"
    fetch_errors = context.get("fetch_errors") or []

    fetch_note = ""
    if fetch_errors:
        fetch_note = (
            "\n注：部分 git 数据获取失败，可能影响判断准确性：\n"
            + "\n".join(f"  - {e}" for e in fetch_errors[:5])
            + "\n"
        )

    prompt = textwrap.dedent(f"""\
        你是一个代码审查验证助手。判断开发者的代码改动是否解决了人工审查者提出的问题。

        ## 人工审查评论

        作者: {author}
        文件: {file}
        行号: {line}（基线 {base_sha}，HEAD {head_sha}）
        评论内容:
        {body}

        ## 评论提出时的源代码（base_sha）

        ```
        {original_code}
        ```

        ## 当前 HEAD 的源代码

        ```
        {current_code}
        ```

        ## 该文件在 base_sha..HEAD 的差异

        ```
        {diff_slice}
        ```
        {fetch_note}
        ## 任务

        基于以上信息，判断改动是否解决了评论的关切。返回**一行 JSON**，无其他文字：
        ```json
        {{"status": "...", "rationale": "<一句中文说明>"}}
        ```

        `status` 必须是以下五个值之一：
        - `addressed`           改动直接解决了评论的关切
        - `not_addressed`       文件改了但评论的问题在引用位置仍然存在
        - `partially_addressed` 部分解决，仍有遗漏（rationale 必须指出遗漏点）
        - `obsolete`            原代码已被删除/重构，问题不再适用
        - `unclear`             无法判断（评论涉及上下文外的内容、改动语义不明等）

        判断原则：
        1. 不信任任何"已解决"标记 —— 你是唯一的仲裁者。
        2. 仅当真正确定时返回 `addressed`；模棱两可的情况返回 `unclear`。
        3. rationale 一句话即可，需具体到行/逻辑，不要空泛。
        """)
    return prompt


def _cmd_context(args):
    topic = topic_store.read(Path(args.topic))
    manual_issues = (topic.get("review") or {}).get("manual_issues") or []
    target = next((m for m in manual_issues
                   if m.get("index") == args.index), None)
    if target is None:
        print(json.dumps({"error": f"no manual issue with index {args.index}"},
                         ensure_ascii=False))
        return 1

    head_sha = args.head_sha
    if not head_sha:
        # Resolve from topic mrs[repo].branch_sha
        repo = target.get("repo") or ""
        head_sha = ((topic.get("mrs") or {}).get(repo) or {}).get("branch_sha", "")

    ctx = build_context(args.repo_root, target, head_sha)
    import io
    utf8_out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    if args.prompt_only:
        utf8_out.write(build_prompt(ctx))
        utf8_out.flush()
        return 0

    out = {
        "context": ctx,
        "prompt": build_prompt(ctx),
    }
    json.dump(out, utf8_out, ensure_ascii=False)
    utf8_out.flush()
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("context",
                       help="Build verification context + prompt for one issue")
    c.add_argument("--topic", required=True)
    c.add_argument("--repo-root", required=True,
                   help="chaos workspace or rage workspace path")
    c.add_argument("--index", required=True, type=int,
                   help="manual_issues[].index (M-numbering)")
    c.add_argument("--head-sha",
                   help="Override HEAD SHA (default: read from topic mrs[repo].branch_sha)")
    c.add_argument("--prompt-only", action="store_true",
                   help="Print only the prompt text (no JSON wrapper)")
    c.set_defaults(func=_cmd_context)

    args = p.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
