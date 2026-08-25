"""gitlab_threads_http.py — fetch/normalize/resolve GitLab MR human review threads.

HTTP-reimplementation of rage's `gitlab_threads.py` (which used the `glab` CLI),
so manual-review integration (P4) works on our Linux/HTTP stack.

Load-bearing rage semantics preserved (DESIGN §1.14):
- Only `DiffNote` discussions are pulled (line-anchored review comments). System
  notes and free-form conversation notes (no `position`) are dropped.
- The bot is the SOLE arbiter of fix status: `resolved` is NOT read as input, but
  the bot WRITES `resolved=true` back when it decides `addressed`/`obsolete`.
- Never write `resolved=false`.

Depends only on gitlab_ci.get-style REST (token from `GITLAB_TOKEN`) — no glab CLI.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

HOST = os.environ.get("GITLAB_HOST", "") or "gitlab.booming-inc.com"
MAX_BODY_CHARS = 400


def _token():
    return os.environ.get("GITLAB_TOKEN") or os.environ.get("CI_JOB_TOKEN") or ""


def _api(path, method="GET", body=None, timeout=30):
    tok = _token()
    if not tok:
        return None
    url = f"https://{HOST}/api/v4/{path.lstrip('/')}"
    req = urllib.request.Request(url, method=method,
                                 headers={"PRIVATE-TOKEN": tok,
                                          "Content-Type": "application/json"})
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as r:
            raw = r.read()
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))
    except Exception as e:
        print(f"[gitlab_threads_http] {method} {path[:80]} err: {e}", file=sys.stderr)
        return None


def repo_slug_and_iid_from_mr_url(mr_url):
    """Extract (project_slug, mr_iid) from a GitLab MR URL, e.g.
    https://host/group/sub/project/-/merge_requests/7201 -> (group/sub/project, 7201)."""
    import re
    m = re.match(r'https?://[^/]+/(.+?)/-/merge_requests/(\d+)', mr_url or "")
    if not m:
        return None, None
    return m.group(1), m.group(2)


def fetch_discussions(repo_slug, mr_iid, timeout=30):
    slug = urllib.parse.quote(repo_slug, safe="")
    return _api(f"projects/{slug}/merge_requests/{mr_iid}/discussions",
                timeout=timeout) or [] or None


def _normalize_diffnote(thread, mr_web_url):
    notes = thread.get("notes") or []
    if not notes:
        return None
    first = notes[0]
    if first.get("type") != "DiffNote":
        return None
    pos = first.get("position") or {}
    if not pos:
        return None
    body_full = first.get("body") or ""
    body = body_full[:MAX_BODY_CHARS]
    if len(body_full) > MAX_BODY_CHARS:
        body = body[:-1] + "…"
    note_id = first.get("id")
    web_url = f"{mr_web_url}#note_{note_id}" if mr_web_url and note_id else ""
    author = (first.get("author") or {}).get("username") or "unknown"
    return {
        "discussion_id": thread.get("id") or "",
        "note_id": note_id,
        "author": author,
        "file": pos.get("new_path") or pos.get("old_path") or "",
        "line_old": pos.get("old_line"),
        "line_new": pos.get("new_line"),
        "base_sha": pos.get("base_sha") or "",
        "body": body,
        "body_full_length": len(body_full),
        "web_url": web_url,
        "created_at": first.get("created_at") or "",
        "verification": None,
        "verification_rationale": "",
        "verified_at_sha": "",
    }


def fetch_manual_issues(repo_slug, mr_iid, mr_web_url="", timeout=30):
    """Pull + normalize human DiffNote discussions for an MR.

    Returns (list[dict], error_str). Schema per manual_issues[] (rage §1.14.1).
    """
    if not repo_slug or not mr_iid:
        return [], "missing repo_slug/iid"
    threads = fetch_discussions(repo_slug, mr_iid, timeout=timeout)
    if threads is None:
        return [], f"fetch discussions failed for {repo_slug}!{mr_iid}"
    out = []
    for t in (threads or []):
        e = _normalize_diffnote(t, mr_web_url)
        if e is not None:
            out.append(e)
    out.sort(key=lambda e: (e.get("created_at") or "", e.get("discussion_id") or ""))
    return out, ""


def reconcile_manual_issues(existing, fetched):
    """Idempotent merge keyed by discussion_id (rage gitlab_threads semantics)."""
    existing = existing or []
    fetched = fetched or []
    by_id = {e.get("discussion_id"): e for e in existing if e.get("discussion_id")}
    seen = set()
    merged = []
    for fresh in fetched:
        did = fresh.get("discussion_id")
        if did and did in by_id:
            old = by_id[did]
            # preserve verification state the bot already settled; refresh
            # created/body from the fresh fetch.
            fresh["verification"] = old.get("verification")
            fresh["verification_rationale"] = old.get("verification_rationale")
            fresh["verified_at_sha"] = old.get("verified_at_sha")
            merged.append(fresh)
        else:
            merged.append(fresh)
        if did:
            seen.add(did)
    # drop stale existing threads no longer present upstream
    merged = [e for e in merged if e.get("discussion_id") in seen or not e.get("discussion_id")]
    return merged


def mark_resolved(repo_slug, mr_iid, discussion_id, timeout=15):
    """Set resolved=true on a discussion thread (write-not-read, rage §1.14.1).

    Never sets resolved=false. Returns (ok, err).
    """
    if not discussion_id:
        return False, "no discussion_id"
    slug = urllib.parse.quote(repo_slug, safe="")
    r = _api(f"projects/{slug}/merge_requests/{mr_iid}/discussions/{discussion_id}",
             method="PUT", body={"resolved": True}, timeout=timeout)
    if r is None:
        return False, "mark_resolved API failed"
    return True, ""


# ── manual issue verification context (rage manual_issue_verifier, git-based) ─

CONTEXT_RADIUS = 10


def _git_show(repo_root, sha, path, timeout=30):
    """git show <sha>:<path> → text or None."""
    import subprocess
    try:
        p = subprocess.run(["git", "-C", repo_root, "show", f"{sha}:{path}"],
                           capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            return None, (p.stderr or "").strip()[:200]
        return p.stdout, None
    except Exception as e:
        return None, str(e)


def _slice_lines(text, target_line, radius=CONTEXT_RADIUS):
    """Return (start_line_1based, end_line_1based, slice_text) around target."""
    lines = (text or "").splitlines()
    if not lines or not target_line:
        return 0, 0, ""
    t = int(target_line)
    lo = max(1, t - radius)
    hi = min(len(lines), t + radius)
    return lo, hi, "\n".join(lines[lo - 1:hi])


def build_verification_context(repo_root, manual_issue, head_sha):
    """Assemble the before/after + diff context for one manual issue (rage §1.14.1).

    Returns dict with keys {issue, original_code, current_code, diff_slice,
                            head_sha, fetch_errors}.
    """
    file = manual_issue.get("file") or ""
    line_new = manual_issue.get("line_new")
    line_old = manual_issue.get("line_old")
    base_sha = manual_issue.get("base_sha") or ""
    fetch_errors = []
    original_code = current_code = diff_slice_text = ""

    if base_sha and file:
        text, err = _git_show(repo_root, base_sha, file)
        if err:
            fetch_errors.append(f"original: {err}")
        elif text is not None:
            _, _, original_code = _slice_lines(text, line_new or line_old)
    if head_sha and file:
        text, err = _git_show(repo_root, head_sha, file)
        if err:
            fetch_errors.append(f"current: {err}")
        elif text is None:
            current_code = ""
        else:
            _, _, current_code = _slice_lines(text, line_new or line_old)
    if base_sha and head_sha and file and base_sha != head_sha:
        import subprocess
        try:
            p = subprocess.run(
                ["git", "-C", repo_root, "diff", "--unified=10", "-w",
                 "--ignore-cr-at-eol", f"{base_sha}..{head_sha}", "--", file],
                capture_output=True, text=True, timeout=30)
            if p.returncode == 0:
                diff_slice_text = p.stdout or ""
            else:
                fetch_errors.append(f"diff: {(p.stderr or '').strip()[:200]}")
        except Exception as e:
            fetch_errors.append(f"diff: {e}")
    return {
        "issue": {"index": manual_issue.get("index"),
                  "discussion_id": manual_issue.get("discussion_id"),
                  "author": manual_issue.get("author"),
                  "file": file, "line_old": line_old, "line_new": line_new,
                  "base_sha": base_sha, "body": manual_issue.get("body"),
                  "web_url": manual_issue.get("web_url")},
        "head_sha": head_sha, "original_code": original_code,
        "current_code": current_code, "diff_slice": diff_slice_text,
        "fetch_errors": fetch_errors,
    }


def build_verification_prompt(context):
    """Chinese verification prompt for the manual-issue adjudicator (rage).

    The adjudicator returns one JSON line: {status, rationale} where status ∈
    {addressed, not_addressed, partially_addressed, obsolete, unclear}. `unclear`
    is load-bearing — never promote to addressed to be helpful.
    """
    issue = context["issue"]
    body = issue.get("body") or "(空)"
    file = issue.get("file") or "(unknown)"
    line = issue.get("line_new") or issue.get("line_old") or "?"
    author = issue.get("author") or "(unknown)"
    base_sha = (issue.get("base_sha") or "")[:8]
    head_sha = (context.get("head_sha") or "")[:8]
    original_code = context.get("original_code") or "(无 — 文件可能在 base_sha 时不存在)"
    current_code = context.get("current_code") or "(无 — 当前 HEAD 文件不存在或行号越界，可能 obsolete)"
    diff_slice = context.get("diff_slice") or "(基线与 HEAD 之间该文件无差异 — 可能 not_addressed)"
    fetch_note = ""
    if context.get("fetch_errors"):
        fetch_note = ("\n注：部分 git 数据获取失败，可能影响判断：\n"
                      + "\n".join(f"  - {e}" for e in context["fetch_errors"][:5]) + "\n")
    json_shape = '{"status":"addressed|not_addressed|partially_addressed|obsolete|unclear","rationale":"一句中文"}'
    return (
        f"人工审查评论（{author}）在 {file}:{line} 提出：\n{body}\n"
        f"\n原代码（审查时 base {base_sha}）±{CONTEXT_RADIUS} 行：\n```\n{original_code}\n```\n"
        f"当前代码（HEAD {head_sha}）±{CONTEXT_RADIUS} 行：\n```\n{current_code}\n```\n"
        f"base..HEAD 该文件 diff：\n```diff\n{diff_slice}\n```\n"
        f"{fetch_note}"
        f"\n判断开发者改动是否解决了该评论。只回一行 JSON: {json_shape}\n"
        f"原则：仅当真正确定返回 addressed；模棱两可一律 unclear；unclear 是保守默认。"
    )


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--mr-url", required=True)
    a = p.parse_args()
    slug, iid = repo_slug_and_iid_from_mr_url(a.mr_url)
    issues, err = fetch_manual_issues(slug, iid, a.mr_url)
    print(json.dumps({"error": err, "count": len(issues),
                      "issues": issues}, ensure_ascii=False, indent=2))
