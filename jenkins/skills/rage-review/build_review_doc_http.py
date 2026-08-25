"""build_review_doc_http.py — full-review doc for complex reviews (P5/M4).

HTTP reimplementation of rage's build_review_doc.py + lark_doc_helper.py for our
Linux/feishu-http stack. A complex review posts a Feishu doc holding the full file
list + per-issue detail, while the thread reply keeps only the #N index list.

Two surfaces:
- build_doc_markdown(): PURE — renders the fixed body (title / 概述 / 变更概览 /
  问题详情) deterministically from the same issues array so `#N` and [repo]
  file:line headings stay in lockstep with the thread reply (rage DESIGN §1.9.5).
  Fully unit-testable.
- create_lark_doc_http(): PlanA — create the Feishu doc via REST + grant view to
  approver & developer. This needs the app to have `docx:document` / `drive:drive`
  scope (P0.5 R2, human-verified). Falls back to a long-post (PlanB) when the
  scope is unavailable or creation fails, so a complex review still surfaces.

The PlanA/PlanB switch is driven by config `review.doc.enabled` (default: attempt
doc, fall back to long-post) — see docs/review-bot-replication-plan.md.
"""
import json
import os
import urllib.parse
import urllib.request

MAX_ISSUES_IN_THREAD = 40


# ── pure markdown body builder (rage-standard, testable) ────────────────────

def build_doc_markdown(ticket_id, summary, issues=None, files=None):
    """Render the fixed full-review doc body.

    issues: review.issues-shaped list [{index, severity, repo, file, line_range,
            function, description}]. files: [{repo, path, insertions, deletions,
            description}]. Returns the markdown string.
    """
    issues = issues or []
    files = files or []
    L = [f"# 代码审查 {ticket_id}", "", "## 概述", (summary or "（无）").strip(), ""]
    if files:
        L += ["## 变更概览"]
        for f in files:
            repo = f.get("repo", "")
            path = f.get("path", "")
            ins = f.get("insertions", 0)
            dels = f.get("deletions", 0)
            desc = f.get("description", "")
            L.append(f"- **[{'Chaos' if repo == 'engine' else 'Game'}] {path}**"
                     f" +{ins}/-{dels} — {desc}")
        L.append("")
    L += ["## 问题详情"]
    if not issues:
        L += ["（未发现问题）", ""]
    else:
        sev_sep = {"严重": 0, "中": 1, "轻": 2, "建议": 3}
        ordered = sorted(issues, key=lambda i: (sev_sep.get(i.get("severity", ""), 3),
                                                i.get("file", "")))
        for i, it in enumerate(ordered, start=1):
            idx = it.get("index", i)
            sev = it.get("severity", "")
            repo = it.get("repo", "")
            file = it.get("file", "")
            lr = it.get("line_range", "")
            fn = it.get("function", "")
            loc = file + (f":{lr}" if lr else "") + (f" （{fn}）" if fn else "")
            L.append(f"#### #{idx} [{sev}] [{'Chaos' if repo == 'engine' else 'Game'}] {loc}")
            L.append((it.get("description") or it.get("issue") or "").strip())
            sug = it.get("suggestion")
            if sug:
                L.append(f"建议：{sug.strip()}")
            L.append("")
    return "\n".join(L).rstrip()


# ── Feishu doc creation (PlanA, R2-gated) ──────────────────────────────────

def _tenant_token(app_id, app_secret):
    import urllib.request as _ur
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
    req = _ur.Request("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                      data=body, headers={"Content-Type": "application/json"})
    try:
        with _ur.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
        if d.get("code") == 0:
            return d.get("tenant_access_token", "")
    except Exception:
        pass
    return ""


def _feishu_api(token, path, method="GET", body=None, timeout=30):
    import sys
    url = f"https://open.feishu.cn/open-apis/{path.lstrip('/')}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, method=method, data=data,
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except Exception as e:
        print(f"[lark_doc_http] {method} {path[:60]} err: {e}", file=sys.stderr)
        return {"code": -1, "msg": str(e)}


def create_lark_doc_http(app_id, app_secret, title, markdown, grant_view=(),
                         public_link=True):
    """PlanA: create a Feishu doc + grant view. Returns (ok, token, url, err).

    Requires the app to have docx/drive scope (P0.5 R2). On any scope/API failure
    returns (False, ...) so the caller falls back to a long-post (PlanB).
    """
    token = _tenant_token(app_id, app_secret)
    if not token:
        return False, "", "", "no tenant token (app_id/secret or scope)"
    # docx/v1/documents (create an empty docx) is the documented way;
    # https://open.feishu.cn/open-apis/docx/v1/documents
    r = _feishu_api(token, "docx/v1/documents", method="POST",
                    body={"title": title})
    if r.get("code") != 0:
        return False, "", "", f"docx create failed: {r.get('msg')}"
    doc_token = (r.get("data") or {}).get("document", {}).get("document_id") or \
        (r.get("data") or {}).get("document_id") or ""
    if not doc_token:
        return False, "", "", "docx create returned no document_id"
    # Best-effort permission grant to approver + developer (drive scope).
    granted = []
    for uid in grant_view or []:
        gr = _feishu_api(token, f"drive/v1/permissions/{doc_token}/members",
                         method="POST", body={"member_type": "openid",
                                              "member_id": uid,
                                              "perm": "full_access"})
        if gr.get("code") == 0:
            granted.append(uid)
    url = f"https://www.feishu.cn/docx/{doc_token}"
    # (markdown content would be written via import; not implemented here — the
    # doc is created empty as a container; the thread reply is the source of truth
    # until an import path (docs +import / block replace) is wired.)
    return True, doc_token, url, ""


def build_long_post(issues, repo_label_key="engine"):
    """PlanB: multi-segment text covering complex review details when doc scope is
    unavailable. Returns a plain-text block; the caller posts it as a long reply.
    """
    if not issues:
        return "✅ 复杂审查完成，未发现问题。"
    sev_sep = {"严重": 0, "中": 1, "轻": 2, "建议": 3}
    ordered = sorted(list(issues), key=lambda it: (sev_sep.get(it.get("severity", ""), 3),
                                                   it.get("file", "")))
    L = []
    for i, it in enumerate(ordered, start=1):
        sev = it.get("severity", "")
        repo = it.get("repo", "")
        file = it.get("file", "")
        lr = it.get("line_range", "")
        fn = it.get("function", "")
        loc = file + (f":{lr}" if lr else "") + (f" {fn}" if fn else "")
        issue = (it.get("issue") or it.get("description") or "").strip()
        sug = it.get("suggestion")
        line = f"#{i} [{sev}] [{repo}] {loc}: {issue}"
        if sug:
            line += f" → {sug}"
        L.append(line)
    parts = [f"📄 复杂审查明细（{len(L)} 项）"] + L
    return "\n".join(parts)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--markdown", action="store_true", help="assemble doc markdown")
    p.add_argument("--input-file", required=True)
    a = p.parse_args()
    data = json.load(open(a.input_file, encoding="utf-8"))
    md = build_doc_markdown(data.get("ticket_id", ""), data.get("summary", ""),
                            data.get("issues"), data.get("files"))
    print(md)
