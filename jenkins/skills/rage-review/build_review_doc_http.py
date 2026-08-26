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
import re
import urllib.parse
import urllib.request

MAX_ISSUES_IN_THREAD = 40
MAX_BLOCKS_PER_REQ = 30   # Feishu docx children-append batch limit (conservative)


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


def _set_public_readable(token, doc_token, type_name="docx", timeout=20):
    """Set the doc's sharing to tenant_readable (组织内获得链接者可读, 方案B).

    Uses the v2 endpoint with the doc type as a query param:
      PATCH /drive/v2/permissions/{token}/public?type=docx
      body: {"link_share_entity": "tenant_readable"}

    The v1 endpoint (drive/v1/permissions/.../public) does NOT support docx and
    keeps rejecting the `type` field; v2 + ?type=docx works (live-probed).
    Returns (ok, err).
    """
    r = _feishu_api(token,
                    f"drive/v2/permissions/{doc_token}/public?type={type_name}",
                    method="PATCH",
                    body={"link_share_entity": "tenant_readable"},
                    timeout=timeout)
    if r.get("code") == 0 and (r.get("data") or {}).get("permission_public", {}).get(
            "link_share_entity") == "tenant_readable":
        return True, ""
    return False, f"set tenant_readable failed: {r.get('msg')}"


def create_lark_doc_http(app_id, app_secret, title, markdown, grant_view=(),
                         public_link=True):
    """PlanA: create a Feishu doc + grant view. Returns (ok, token, url, err).

    Requires the app to have docx/drive scope (P0.5 R2). On any scope/API failure
    (other than the best-effort public link) returns (False, ...) so the caller
    falls back to a long-post (PlanB).

    Sharing (方案B): after creating, set the doc to tenant_readable (组织内可读) via
    v2 permission public, so anyone in the tenant with the link can read — not just
    approver/developer. The explicit `permission.members create` grant still runs so
    named reviewers get full_access regardless.
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
    url = f"https://www.feishu.cn/docx/{doc_token}"
    # ── 方案B: 组织内可读 (tenant_readable). best-effort: 失败不阻断创建. ──
    pub_ok = True
    if public_link:
        pub_ok, pub_err = _set_public_readable(token, doc_token, type_name="docx")
        if not pub_ok:
            import sys as _sys
            print(f"[lark_doc_http] public link skipped: {pub_err}", file=_sys.stderr)
    # Best-effort permission grant to approver + developer (drive scope).
    # v1 members needs ?type=docx for docx documents (probed: without it -> 400).
    granted = []
    for uid in grant_view or []:
        gr = _feishu_api(token,
                         f"drive/v1/permissions/{doc_token}/members?type=docx",
                         method="POST", body={"member_type": "openid",
                                              "member_id": uid,
                                              "perm": "edit"})
        if gr.get("code") == 0:
            granted.append(uid)
    # R7: write the review-doc content into the doc as native docx blocks (not an
    # empty container). code fences render as code_block, the rest inline.
    blocks = build_code_blocks(markdown)
    if blocks:
        okb, errb = _write_doc_blocks(token, doc_token, blocks)
        if not okb:
            # content write failed -> surface but still ok (doc created + granted)
            return True, doc_token, url, errb
    return True, doc_token, url, ""


def _text_run(content, bold=False):
    return {"text_run": {"content": content,
                         "text_element_style": {"bold": bold, "inline_code": False,
                                                "italic": False, "strikethrough": False,
                                                "underline": False}}}


def markdown_to_blocks(markdown):
    """Convert review-doc markdown (the non-code, non-fence portions) into Feishu
    docx block JSON. Handles `#..###` headings, `- ` bullets, and plain paragraphs
    with `**bold**`. Fences/code are handled by build_code_blocks. Applies a
    single line or a whole block (each non-empty line becomes one docx block)."""
    blocks = []
    for raw in (markdown or "").splitlines():
        line = raw.rstrip()
        s = line.strip()
        if not s:
            continue
        m = re.match(r'^(#{1,6})\s+(.*)$', s)
        if m:
            lvl = len(m.group(1))
            block_type = {1: 3, 2: 4, 3: 5, 4: 6, 5: 7, 6: 8}[lvl]
            key = {3: "heading1", 4: "heading2", 5: "heading3",
                   6: "heading4", 7: "heading5", 8: "heading6"}[block_type]
            blocks.append({"block_type": block_type, key: {"elements": [_text_run(m.group(2).strip())]}})
            continue
        if s.startswith(("- ", "* ")):
            blocks.append({"block_type": 12,
                           "bullet": {"elements": [_text_run(s[2:].strip())]}})
            continue
        elems = []
        for seg in re.split(r'(\*\*.+?\*\*)', s):
            if not seg:
                continue
            if seg.startswith("**") and seg.endswith("**"):
                elems.append(_text_run(seg[2:-2], bold=True))
            else:
                elems.append(_text_run(seg))
        blocks.append({"block_type": 2, "text": {"elements": elems}})
    return blocks


def _write_doc_blocks(token, doc_token, blocks):
    """Write docx blocks into document doc_token (page block = doc_token, replaced
    by appending to its children after the title block). Returns (ok, err).

    Appends in batches of MAX_BLOCKS_PER_REQ under the page block id = doc_token.
    """
    for i in range(0, len(blocks), MAX_BLOCKS_PER_REQ):
        batch = blocks[i:i + MAX_BLOCKS_PER_REQ]
        r = _feishu_api(token,
                        f"docx/v1/documents/{doc_token}/blocks/{doc_token}/children",
                        method="POST", body={"children": batch})
        if r.get("code") != 0:
            return False, f"docx append batch {i} failed: {r.get('msg')}"
    return True, ""


def build_code_blocks(markdown):
    """Split the markdown, converting ``` fences into code-style text blocks.
    Used by create_lark_doc_http to render diff/code sections natively.

    Note: Feishu's native `code_block`(14) insert API 400s here; a multiline
    plain-text block with inline_code runs renders the diff/code acceptably and
    is stable. Headings/bullets/paragraphs go to their real block types.
    """
    blocks = []
    in_code = False
    code_buf = []
    lines = (markdown or "").splitlines()
    for line in lines:
        s = line.strip()
        if s.startswith("```"):
            if in_code:
                blocks.append({"block_type": 2, "text": {"elements": [
                    {"text_run": {"content": "\n".join(code_buf),
                                  "text_element_style": {"bold": False, "inline_code": True,
                                                         "italic": False, "strikethrough": False,
                                                         "underline": False}}}]}})
                code_buf = []
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_buf.append(line)
            continue
        # non-code line -> normal markdown_to_blocks for this single line
        blocks.extend(markdown_to_blocks(line))
    if code_buf:
        blocks.append({"block_type": 2, "text": {"elements": [
            {"text_run": {"content": "\n".join(code_buf),
                          "text_element_style": {"bold": False, "inline_code": True,
                                                 "italic": False, "strikethrough": False,
                                                 "underline": False}}}]}})
    return blocks


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
