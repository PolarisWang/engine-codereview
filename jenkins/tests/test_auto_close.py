"""Regression tests for R7: lazy auto-close must apply to DONE/FAILED topics.

Previously the auto-close gate was `if phase not in ("DONE","FAILED"): ...`, so a
finished (DONE) review that stays ignored never released its fix MR/branch until
the 30-day cleanup. The extracted `_should_auto_close(topic)` must return True
for ANY non-CLOSED topic idle past IDLE_CLOSE_DAYS (including DONE and FAILED).
"""
import time

import pytest

from orchestrate import _should_auto_close

# Past IDLE_CLOSE_DAYS (default 2) — a topic this old is "dusty".
NOW = time.time()
OLD = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW - 5 * 86400))
FRESH = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW))


def _topic(phase="DONE", updated_at=OLD):
    return {"phase": phase, "updated_at": updated_at}


def test_done_topic_old_is_due_for_auto_close():
    # R7 core regression: a finished review that's been idle IS auto-closed now.
    assert _should_auto_close(_topic("DONE", OLD), now_ts=NOW) is True


def test_done_topic_fresh_is_not_closed():
    assert _should_auto_close(_topic("DONE", FRESH), now_ts=NOW) is False


def test_failed_topic_old_is_due():
    assert _should_auto_close(_topic("FAILED", OLD), now_ts=NOW) is True


def test_in_progress_old_is_due():
    assert _should_auto_close(_topic("REVIEWING", OLD), now_ts=NOW) is True


def test_closed_topic_never_auto_closed_again():
    assert _should_auto_close(_topic("CLOSED", OLD), now_ts=NOW) is False


def test_null_topic_not_due():
    assert _should_auto_close(None, now_ts=NOW) is False


def test_missing_updated_at_counts_as_old():
    # No updated_at -> treated as beyond the idle window (releases it safely).
    assert _should_auto_close({"phase": "DONE"}, now_ts=NOW) is True


def test_garbage_updated_at_not_due():
    # Unparseable timestamp -> idle_days=0 -> not due (fail-safe, don't spuriously close).
    assert _should_auto_close({"phase": "DONE", "updated_at": "not-a-date"}, now_ts=NOW) is False


# --- F2: idle 判定用独立 last_user_activity, 不被 ci/background 写入污染 ---

def test_idle_uses_last_user_activity_not_updated_at():
    # updated_at 是新的(被 ci-poll 刷新) 但 last_user_activity 很旧 -> 应判定为 idle
    NOW2 = time.time()
    # last_user_activity 旧, updated_at 新
    t = {"phase": "DONE",
         "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW2 - 3600)),   # 1h 前(background 写)
         "last_user_activity": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW2 - 3 * 86400))}  # 3 天前(用户)
    # last_user_activity 3天前>2天 -> 应 idle=True (即便 updated_at 只有1h 前)
    assert _should_auto_close(t, now_ts=NOW2) is True


def test_fresh_user_activity_not_idle(monkeypatch):
    # Pin a 48h window so "1 hour ago" is genuinely fresh (config default may be 1h).
    monkeypatch.setattr("orchestrate.AUTO_CLOSE_HOURS", 48)
    NOW2 = time.time()
    t = {"phase": "DONE",
         "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW2 - 5 * 86400)),
         "last_user_activity": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW2 - 3600))}  # 用户1h前
    assert _should_auto_close(t, now_ts=NOW2) is False


def test_no_last_user_activity_falls_back_to_updated_at():
    NOW2 = time.time()
    t = {"phase": "DONE",
         "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW2 - 3 * 86400))}  # 无 last_user_activity
    assert _should_auto_close(t, now_ts=NOW2) is True  # 回退到 updated_at, 3 天 > 2 天


# --- 方案B: 无 last_user_activity 时用 created_at 而非 updated_at (修复永不自动关) ---

def test_no_user_activity_uses_created_at_not_updated_at():
    NOW2 = time.time()
    # created_at 很旧(按创建算闲置), updated_at 很新(被扫描刷新), 无 last_user_activity
    t = {"phase": "DONE",
         "created_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW2 - 3 * 86400)),
         "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW2 - 60))}  # 被后台刷新
    # 方案B: 用 created_at(3天) 而非 updated_at(1分) -> 应 idle
    assert _should_auto_close(t, now_ts=NOW2) is True


def test_legacy_no_created_at_falls_back_to_updated_at():
    NOW2 = time.time()
    t = {"phase": "DONE", "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW2 - 3 * 86400))}
    assert _should_auto_close(t, now_ts=NOW2) is True  # 缺 created_at -> 兜底 updated_at


def test_has_last_user_activity_still_priority(monkeypatch):
    monkeypatch.setattr("orchestrate.AUTO_CLOSE_HOURS", 48)
    NOW2 = time.time()
    t = {"phase": "DONE",
         "created_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW2 - 5 * 86400)),
         "last_user_activity": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW2 - 3600))}  # 用户1h前
    assert _should_auto_close(t, now_ts=NOW2) is False  # 用户活动优先, 不算 idle


# --- auto_close_hours wiring + correct group notice ---

def test_config_wires_auto_close_hours_to_one():
    # config.yaml sets auto_close_hours: 1; it must now be read (was ignored, stuck at 48)
    from config import AUTO_CLOSE_HOURS
    assert AUTO_CLOSE_HOURS == 1


def test_should_auto_close_uses_auto_close_hours_threshold(monkeypatch):
    from orchestrate import _should_auto_close
    import time as _t
    monkeypatch.setattr("orchestrate.AUTO_CLOSE_HOURS", 1)  # 1h window
    now = _t.time()
    # 2h idle (last_user_activity 2h ago) under a 1h window -> due
    t = {"phase": "DONE",
         "last_user_activity": _t.strftime("%Y-%m-%dT%H:%M:%S", _t.localtime(now - 2 * 3600))}
    assert _should_auto_close(t, now_ts=now) is True
    # 30min idle -> not due under 1h
    t2 = {"phase": "DONE",
          "last_user_activity": _t.strftime("%Y-%m-%dT%H:%M:%S", _t.localtime(now - 1800))}
    assert _should_auto_close(t2, now_ts=now) is False


def test_autoclose_message_is_topic_specific_and_states_threshold(monkeypatch):
    from orchestrate import _autoclose_message
    monkeypatch.setattr("orchestrate.AUTO_CLOSE_HOURS", 1)
    m = _autoclose_message({"jira_key": "CB2N-25256"})
    assert "CB2N-25256" in m
    assert "1小时" in m
    assert "自动关闭" in m
    m2 = _autoclose_message({"jira_key": "EV-9"}, note="删除孤儿 fix 分支 foo")
    assert "已释放" in m2 and "foo" in m2


# --- 防抖动: repeated 优化/按钮 -> 不重复入队 ---

def test_edit_already_pending_when_agent_edit(monkeypatch):
    import orchestrate as O
    monkeypatch.setattr(O.pipeline_state, "get_topic",
                        lambda s, k: {"pending": {"action": "agent_edit"}, "pending_patch": {}})
    assert O._edit_already_pending("/s", "k") is True


def test_edit_already_pending_when_staged(monkeypatch):
    import orchestrate as O
    monkeypatch.setattr(O.pipeline_state, "get_topic",
                        lambda s, k: {"pending": {}, "pending_patch": {"state": "staged_agent_edit"}})
    assert O._edit_already_pending("/s", "k") is True


def test_edit_already_pending_false_when_idle(monkeypatch):
    import orchestrate as O
    monkeypatch.setattr(O.pipeline_state, "get_topic",
                        lambda s, k: {"pending": {}, "pending_patch": {}})
    assert O._edit_already_pending("/s", "k") is False


def test_card_action_debounce(monkeypatch):
    """Feishu redelivery / double-click of the same button must run once."""
    import event_server as es
    from event_server import _claim_action, _ACTION_GUARD, _ACTION_LAST
    _ACTION_LAST.clear()
    with _ACTION_GUARD:
        pass
    assert _claim_action("optimize", "t1", "u1") is True      # first: allowed
    assert _claim_action("optimize", "t1", "u1") is False     # repeat in window: dropped
    # different action or topic -> allowed
    assert _claim_action("re_review", "t1", "u1") is True
    assert _claim_action("optimize", "t2", "u1") is True
