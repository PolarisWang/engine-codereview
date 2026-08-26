"""Regression tests for the @-gate fix: a thread reply is bot-directed ONLY when the
'@' targets the bot or a command keyword — NOT when the user @-mentions another human.

Bug: `_is_bot_directed` treated ANY text starting with '@' as bot-directed, so
`@其他用户 …` (at a human) wrongly triggered a bot reply inside the review card's thread.
"""
import pytest
from event_server import _is_bot_directed


def test_at_other_user_not_directed():
    """@ 一个人类用户(非 bot) → 不应触发 bot 回复(核心回归)."""
    assert _is_bot_directed("@张三 你看下这个", []) is False
    assert _is_bot_directed("@李四 能不能改一下", []) is False


def test_at_command_keyword_directed():
    assert _is_bot_directed("@优化", []) is True
    assert _is_bot_directed("@改码", []) is True
    assert _is_bot_directed("@指引 修复所有问题", []) is True
    assert _is_bot_directed("@重新审查", []) is True


def test_at_command_with_punctuation():
    assert _is_bot_directed("@优化！", []) is True
    assert _is_bot_directed("@改码，谢谢", []) is True


def test_plain_text_not_directed():
    # 无 @, 纯文字(即使落在 card 线程) → 不回复
    assert _is_bot_directed("优化", []) is False
    assert _is_bot_directed("这个逻辑有问题", []) is False


def test_at_bot_by_open_id_directed(monkeypatch):
    # mentions 里明确指向 bot(open_id) → 应触发(前提: 配置好 FEISHU_BOT_OPEN_ID)
    monkeypatch.setattr("common.c_feishu_bot_open_id", lambda: "ou_bot_123")
    mentions = [{"open_id": "ou_bot_123", "name": "代码审查机器人"}]
    assert _is_bot_directed("@代码审查机器人 看看这个", mentions) is True


def test_at_bot_by_name_directed(monkeypatch):
    monkeypatch.setattr("common.c_feishu_bot_name", lambda: "代码审查机器人")
    mentions = [{"open_id": "ou_xyz", "name": "代码审查机器人"}]
    assert _is_bot_directed("@代码审查机器人 优化一下", mentions) is True


def test_at_other_user_when_bot_identity_set(monkeypatch):
    # 即使配置了 bot 身份, @ 别的用户也不应触发(mentions 里不是 bot)
    monkeypatch.setattr("common.c_feishu_bot_open_id", lambda: "ou_bot_123")
    mentions = [{"open_id": "ou_human_a", "name": "张三"}]
    assert _is_bot_directed("@张三 你看这个", mentions) is False



def test_empty_text_not_directed():
    assert _is_bot_directed("", []) is False
    assert _is_bot_directed(None, None) is False


# ── fix 1+2: command can be non-first token after an @mention prefix ──
def test_at_mention_then_command_directed():
    """核心回归(真实日志): `@_user_1 3 没有问题…`(Feishu 把 @ 渲染成 @_user_1, 命令 3 在第二位)
    必须视为 bot-directed. 此前被错当成 '非bot' 忽略."""
    assert _is_bot_directed("@_user_1 3 没有问题，lua脚本里面的字段名不轻易更改", [{
        "open_id": "ou_zzz"}, {"open_id": "ou_1a4385da2771c92b5c05e8c08afe3b47"}]) is True
    # mentions 里没有 bot 也行, 只要命令词出现在前导 @ 之后
    assert _is_bot_directed("@_user_1 优化", []) is True
    assert _is_bot_directed("@_user_1 @_user_2 改码 这个文件", []) is True


def test_at_mention_then_casual_text_not_directed():
    """@ 一个人类 + 非命令闲聊 -> 仍不回复(不能误触发)."""
    assert _is_bot_directed("@_user_1 你看下这个", []) is False
    assert _is_bot_directed("@张三 能不能改一下", []) is False


def test_plain_command_no_at_not_directed():
    """纯文本命令(无 @)仍不触发 —— 保留原有'无@不回复'语义, 避免闲聊词误触发."""
    assert _is_bot_directed("3 没有问题", []) is False
    assert _is_bot_directed("优化", []) is False


def test_at_mention_that_is_bot_identity_directed(monkeypatch):
    """① mentions 里任一 open_id 是 bot -> directed(不要求是首个 mention)."""
    monkeypatch.setattr("common.c_feishu_bot_open_id", lambda: "ou_bot_1")
    mentions = [{"open_id": "ou_human", "name": "张三"},
                {"open_id": "ou_bot_1", "name": "Chaos Code Review"}]
    assert _is_bot_directed("@张三 顺便 @下机器人", mentions) is True


# ── regression: @-gate 命令词须含权威注册表(重新review / review) ─────────
def test_at_command_words_include_review_words(monkeypatch):
    """orchestrate._COMMAND_FIRST_WORDS 里有 review/重新review, @-gate 必须认 ——
    否则 '@机器人重新review' 会被当成非 bot-directed 忽略(曾发生, MS-30918 群)."""
    import event_server as es
    es._COMMAND_WORDS = None
    w = es._command_words()
    assert "review" in w
    assert "重新review" in w
    assert "重新审查" in w


def test_at_review_command_directed(monkeypatch):
    import event_server as es
    es._COMMAND_WORDS = None
    # 用户 @机器人 重新review（Feishu 渲染成 @_user_1 重新review）
    assert es._is_bot_directed("@_user_1 重新review", []) is True
    assert es._is_bot_directed("@_user_1 review", []) is True
    # 但 @某人类 + 非命令闲聊 仍不触发
    assert es._is_bot_directed("@_user_1 你看下这个", []) is False


def test_closure_token_after_at_mention_directed():
    """真实场景: Feishu 把 bot 的 @ 渲染成 @_user_1; 回复 `@_user_1 #1` 应视为
    bot-directed(闭路序号回复), 否则闭路交互永远被 gate 挡住."""
    from event_server import _is_bot_directed
    assert _is_bot_directed("@_user_1 #1", []) is True
    assert _is_bot_directed("@_user_1 1", []) is True
    assert _is_bot_directed("@_user_1 1 3 5", []) is True
    assert _is_bot_directed("@_user_1 ok", []) is True
    # 无 @ 纯数字(无 mentions 命中 bot) 仍保持不打扰
    assert _is_bot_directed("#1", []) is False
    assert _is_bot_directed("1", []) is False
    assert _is_bot_directed("3 没有问题", []) is False
