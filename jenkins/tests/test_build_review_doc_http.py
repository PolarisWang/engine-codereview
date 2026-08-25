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
