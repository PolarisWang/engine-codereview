"""Regression tests for 方案C: the review card is concise (言简意赅).

The group card previously rendered `review_text` from _build_markdown_from_findings,
which dumped full LLM prose per finding plus Summary/Strengths/Arch-Perf sections —
a multi-paragraph wall the user rejected. 方案C keeps the severity grouping but
compresses every piece to a one-liner and drops the wordy headings/footers.
These tests lock in the compact shape so it does not regress back to verbosity.
"""
import pytest

from code_reviewer import _build_markdown_from_findings, _one_line

VERBOSE_ISSUE = (
    "lod_chunk_task 中 tryGetData 可能返回空指针，且 entity 查找失败会产生无效 actor_id，"
    "直接强转传入 updateSingleGameActor 会导致未定义行为/崩溃，务必在合并前修复"
)
VERBOSE_FIX = (
    "先对 tryGetData 返回值做空判断并提前 return，再对无效 actor_id 做合法性校验，"
    "通过后才提交进入 updateSingleGameActor 的更新逻辑，避免空指针与越界的风险"
)
META = {"summary": "对 LOD 改动审查发现并发与空指针问题，game_actor 链路优先修。",
        "strengths": ["抽象清晰，LOD 职责分离良好", "对可空结果有部分防御性检查"]}


def _finding(**kw):
    base = {"file": "a/chaos_client_game_actor_manager.cpp", "severity": "critical",
            "category": "quality", "issue": VERBOSE_ISSUE, "suggestion": VERBOSE_FIX}
    base.update(kw)
    return base


@pytest.fixture
def compact():
    return _build_markdown_from_findings([_finding()], META)


def test_no_wordy_headings_or_footer(compact):
    # old verbose markers must be gone
    assert "# 🔍 Code Review 报告" not in compact
    assert "## 📋 Summary" not in compact
    assert "## ✅ Strengths" not in compact
    assert "建议修复顺序" not in compact           # wordy footer removed


def test_keeps_severity_grouping(compact):
    assert "🔴 blocking" in compact
    assert "blocking (1)" in compact


def test_uses_basename_not_full_path(compact):
    # finding file was "a/chaos_client_game_actor_manager.cpp"; only the basename
    # should appear (short paths don't get clipped by the truncation)
    assert "chaos_client_game_actor_manager.cpp" in compact
    assert "/a/chaos_client_game_actor_manager" not in compact
    # no truncated-path artifact: the leading dir must not be echoed with a "…"
    assert "a/chaos_client_game_actor_manag…" not in compact


def test_finding_is_a_single_line(compact):
    # each finding emitted as exactly one line containing `→`
    lines = [l for l in compact.splitlines() if l.startswith("·")]
    assert len(lines) == 1
    assert "→" in lines[0]                         # issue → fix kept
    assert "\n" not in lines[0]


def test_prose_is_truncated_not_full_paragraph(compact):
    # the full verbose issue/fix must NOT appear verbatim (it's too long)
    assert "导致的未定义行为/崩溃，务必在合并前修复" not in compact
    # summary is bounded
    sm = next(l for l in compact.splitlines() if l.startswith("Summary"))
    assert len(sm) <= 100


def test_one_line_collapses_whitespace_and_clips():
    txt = "第一句很长\n\n  第二句隔行缩进   第三句"
    assert _one_line(txt, 100) == "第一句很长 第二句隔行缩进 第三句"   # folded
    clipped = _one_line("x" * 200, 50)
    assert len(clipped) == 51 and clipped.endswith("…")                 # clipped tail
