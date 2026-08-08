"""Regression tests for 方案C: agent loop is strictly read-only Q&A.

C4 guarantees:
- AGENT_TOOLS exposes ONLY read-only tools (get_status/get_findings/
  generate_patch_preview/answer). No write tool (re_review/close_topic/
  apply_patch) is reachable from the agent loop, so the LLM can never claim to
  push/close/edit and then "pretend".
- _looks_like_operation intercepts write-intent replies BEFORE the agent loop,
  so a "确认 push 来确认推送并创建 MR" style message never reaches the read-only
  assistant (which would hallucinate "I'll push" without doing it).
"""
import pytest

from orchestrate import AGENT_TOOLS, _looks_like_operation

READ_ONLY = {"get_status", "get_findings", "generate_patch_preview", "answer"}
WRITE_TOOLS = {"re_review", "close_topic", "apply_patch"}


def test_agent_tools_are_read_only_only():
    names = {t["name"] for t in AGENT_TOOLS}
    assert names == READ_ONLY, f"expected only {READ_ONLY}, got {names}"


def test_no_write_tool_exposed():
    names = {t["name"] for t in AGENT_TOOLS}
    assert not (names & WRITE_TOOLS), f"write tools must not be in AGENT_TOOLS: {names & WRITE_TOOLS}"


def test_operation_intent_detected():
    # The exact message that previously fell through and produced a fake "I'll push".
    assert _looks_like_operation("确认 push 来确认推送并创建 MR") is True
    assert _looks_like_operation("@确认提交并建mr") is True
    assert _looks_like_operation("改码 修改所有的问题") is True
    assert _looks_like_operation("关闭") is True
    assert _looks_like_operation("请帮我 merge") is True


def test_qa_question_not_flagged_as_operation():
    # Genuine review questions must pass through to the agent loop.
    assert _looks_like_operation("这段代码的并发有什么问题") is False
    assert _looks_like_operation("datum_manager.cpp 为什么会 data race") is False
    assert _looks_like_operation("请问 review 结果在哪里看") is False
