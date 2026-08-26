"""Tests for P5/M4 full-review doc (build_review_doc_http).

Verifies the pure doc-markdown builder ("#N [严重] [repo] file:line （function）"
locked to the issues array in rage-standard ordering) and the PlanB long-post
fallback. The PlanA Feishu doc-creation path (R2-gated) is tested only for
structure, not live scope.
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "skills", "rage-review"))
import build_review_doc_http as b


def _issues():
    return [
        {"index": 2, "severity": "中", "repo": "engine", "file": "chaos/AssetService.cs",
         "line_range": "120-145", "function": "update", "description": "路径校验缺失",
         "suggestion": "加校验"},
        {"index": 1, "severity": "严重", "repo": "game", "file": "x.cs",
         "line_range": "30", "issue": "越界"},
    ]


def test_doc_markdown_structure_and_ordering():
    md = b.build_doc_markdown("CB2N-99", "这里越界。", _issues(),
                              [{"repo": "engine", "path": "chaos/a.cpp",
                                "insertions": 5, "deletions": 3, "description": "改了"}])
    # title / sections
    assert "# 代码审查 CB2N-99" in md
    assert "## 概述" in md and "## 变更概览" in md and "## 问题详情" in md
    # severity-sorted: 严重 first (#1), 中 second (#2)
    assert "#### #1 [严重] [Game] x.cs:30" in md
    assert "#### #2 [中] [Chaos] chaos/AssetService.cs:120-145 （update）" in md
    assert "建议：加校验" in md


def test_doc_no_issues():
    md = b.build_doc_markdown("K-1", "ok", [], [])
    assert "## 问题详情" in md and "未发现问题" in md


def test_long_post_planb_sorted():
    post = b.build_long_post(_issues())
    # severity-sorted: 严重 (#1) before 中 (#2)
    assert post.index("#1 [严重]") < post.index("#2 [中]")
    assert "[engine] chaos/AssetService.cs:120-145" in post


def test_long_post_no_issues():
    assert "未发现问题" in b.build_long_post([])


# ── R7: markdown → docx blocks content injection ───────────────────────────

def test_build_code_blocks_maps_markdown():
    md = ("# 代码审查 K-1\n"
          "## 概述\n"
          "这里一句 **加粗** 描述。\n"
          "## 变更概览\n"
          "- **[Chaos] a.cpp** +5/-3 — 改\n"
          "## 问题详情\n"
          "#### #1 [严重] [Game] x.cs:30\n"
          "越界\n"
          "```cpp\nint a = 1;\n```\n")
    blocks = b.build_code_blocks(md)
    types = [bl["block_type"] for bl in blocks]
    assert 3 in types and 4 in types and 6 in types       # heading1/2/4
    assert 12 in types and 2 in types                      # bullet + text (incl code-as-text)
    # fenced code becomes a multiline inline_code text block (code_block 14 400s;
    # a text block with inline_code renders the code acceptably per live probe).
    code = [bl for bl in blocks if bl["block_type"] == 2 and
            "int a" in bl["text"]["elements"][0]["text_run"]["content"]]
    assert code and "int a = 1;" in code[0]["text"]["elements"][0]["text_run"]["content"]
    # the inline_code run is flagged
    assert code[0]["text"]["elements"][0]["text_run"]["text_element_style"]["inline_code"] is True


def test_markdown_to_blocks_bold_heading_and_bullet():
    blocks = b.markdown_to_blocks("## 概述\n这是 **重要** 一句。\n- 子项\n")
    # heading2
    assert blocks[0]["block_type"] == 4
    # paragraph with a bold run
    para = blocks[1]
    assert para["block_type"] == 2
    texts = [e["text_run"]["content"] for e in para["text"]["elements"]]
    bolds = [e["text_run"]["content"] for e in para["text"]["elements"]
             if e["text_run"]["text_element_style"]["bold"]]
    assert "重要" in bolds and "这是" in texts[0]
    assert blocks[2]["block_type"] == 12                  # bullet


def test_set_public_readable_calls_v2_docx(monkeypatch):
    """方案B: _set_public_readable 调 v2 ?type=docx + tenant_readable."""
    calls = []
    def fake(tok, path, method='GET', body=None, timeout=30):
        calls.append((path, method, body))
        return {"code": 0, "data": {"permission_public": {"link_share_entity": "tenant_readable"}}}
    monkeypatch.setattr("build_review_doc_http._feishu_api", fake)
    ok, err = b._set_public_readable("tok", "doc_x")
    assert ok and not err
    path, method, body = calls[0]
    assert "drive/v2/permissions/doc_x/public?type=docx" in path
    assert body == {"link_share_entity": "tenant_readable"}


def test_create_doc_members_use_type_docx(monkeypatch):
    """create_lark_doc_http 的 member grant 带 ?type=docx(否则 docx 400)."""
    seq = {"n": 0}
    def fake(tok, path, method='GET', body=None, timeout=30):
        seq["n"] += 1
        # 第1次=create doc; 第2次=public? no; 顺序: token, create, public, members
        if "docx/v1/documents" in path and method == "POST":
            return {"code": 0, "data": {"document": {"document_id": "doc123"}}}
        if "permissions/doc123/public" in path:
            return {"code": 0, "data": {"permission_public": {"link_share_entity": "tenant_readable"}}}
        if "permissions/doc123/members" in path:
            assert "?type=docx" in path, f"members must carry ?type=docx, got {path}"
            return {"code": 0, "data": {}}
        return {"code": 0}
    monkeypatch.setattr("build_review_doc_http._feishu_api", fake)
    monkeypatch.setattr("build_review_doc_http._tenant_token", lambda a, s: "tok")
    monkeypatch.setattr("build_review_doc_http.build_code_blocks", lambda md: [])
    ok, tok_, url, err = b.create_lark_doc_http("a", "s", "T", "# 标题")
    assert ok and url.endswith("/doc123")
