"""Regression tests for the Chinese @-mention routing fix (方案 A).

Root cause: interact() stripped a leading @-mention with ``@[\\w.-]+``, which only
matches ASCII words. A Chinese @-mention like ``@指引 修复所有问题`` (user types the
command after a literal @) or ``@机器人 改码`` was NOT stripped correctly, so the first
command token came out wrong and the message fell into the agent loop — producing
raw [tool_use] history instead of the fixed 指引/改码 route.

Fix: ``_strip_mention`` strips a leading @-mention (ASCII or Chinese), but if the
token after '@' is itself a known command keyword, keeps it as the command.
"""
import pytest

from orchestrate import _strip_mention


def _first_word(text):
    return _strip_mention(text).split()[0] if _strip_mention(text) else ""


def test_ascii_mention_stripped():
    assert _first_word("@_user_1 指引 修复所有的问题") == "指引"


def test_chinese_bot_name_mention_stripped():
    assert _first_word("@机器人 改码") == "改码"


def test_at_command_keyword_kept():
    # Core regression: `@指引 ...` — the @-token is the command, must route to 指引.
    assert _first_word("@指引 修复所有问题") == "指引"
    assert _first_word("@改码") == "改码"
    assert _first_word("@关闭") == "关闭"


def test_chinese_mention_number_command():
    assert _first_word("@机器人 1") == "1"


def test_ascii_mention_mr_command():
    assert _first_word("@_user_1 MR单") == "MR单"


def test_no_mention_untouched():
    assert _first_word("指引 修复") == "指引"
    assert _first_word("改码") == "改码"


def test_mention_placeholder_vs_keyword_boundary():
    # A real user id is stripped entirely; a command keyword is preserved.
    assert _strip_mention("@ou_55bca7b7dae982e96749bd84f57c21e8 指引") == "指引"
    assert _strip_mention("@_user_1 状态") == "状态"
