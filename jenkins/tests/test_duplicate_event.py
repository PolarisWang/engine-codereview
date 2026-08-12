"""Regression tests for the duplicate-EVENT fix (方案 A + B).

Root cause: on_p2_im_message_receive ran the blocking `interact` subprocess
inline in the ws handler, so the lark SDK couldn't ack Feishu before its
timeout and Feishu redelivered the SAME event (same msg_id) up to N times,
posting duplicate cards and double-appending chat_history.

方案 A: handler returns fast; interact runs in a background thread.
方案 B: `_claim_msg_id` dedups a repeated msg_id so a retry cannot spawn a
second interact.
"""
import threading
import time

from event_server import _claim_msg_id, _route


# --- 方案 B: dedup ---

def test_claim_msg_id_new_then_repeat():
    assert _claim_msg_id("om_aaa") is True
    assert _claim_msg_id("om_aaa") is False   # duplicate -> skip
    assert _claim_msg_id("om_bbb") is True    # different -> process


def test_claim_msg_id_no_id_always_allowed():
    assert _claim_msg_id("") is True          # no id -> cannot dedup
    assert _claim_msg_id(None) is True


def test_claim_msg_id_survives_other_ids():
    _claim_msg_id("om_1"); _claim_msg_id("om_2")
    assert _claim_msg_id("om_1") is False      # still seen
    assert _claim_msg_id("om_3") is True


# --- 方案 A: non-blocking route (interact offloaded to background thread) ---

def test_route_returns_fast_and_spawns_single_interact(monkeypatch):
    """_route must return quickly (not run interact inline) and, for a repeated
    msg_id, spawn only ONE interact worker (dedup)."""
    calls = []
    barrier = threading.Event()

    def fake_orchestrate(args):
        calls.append(args)
        barrier.wait(timeout=5)  # block the worker if allowed to run
        return 0

    monkeypatch.setattr("event_server._run_orchestrate", fake_orchestrate)
    monkeypatch.setattr("event_server._state_file", lambda: "/state/path")
    monkeypatch.setattr("event_server._workspace", lambda: "/ws")
    monkeypatch.setattr("event_server._resolve_topic_key", lambda s, p: "om_parent")

    # First delivery. Use an @-directed reply (@MR单 = command word) so the @-gate
    # lets it through; a plain "MR单" (no @) would now be ignored (see gate test).
    _route("om_msg1", "om_parent", "@MR单", "ou_1")
    # Simulate Feishu redelivering the SAME event (same msg_id) right away.
    _route("om_msg1", "om_parent", "@MR单", "ou_1")
    # Give background workers a moment to (try) to run — dedup should have made the
    # second a no-op, so only one worker ever calls _run_orchestrate.
    time.sleep(0.3)
    barrier.set()
    time.sleep(0.2)

    assert len(calls) == 1, f"expected exactly 1 interact, got {len(calls)}"
    # The interact arg carries the correct topic key + reply.
    assert "interact" in calls[0][0]
    assert "om_msg1" in calls[0]


def test_route_ignores_reply_not_directed_at_bot(monkeypatch):
    """@-gate: a threaded reply that does NOT @ the bot (plain chat in the card
    thread) must NOT spawn interact — prevents spamming when users chat with each
    other. Only @-directed replies (mention/command-keyword prefix) are handled."""
    calls = []
    monkeypatch.setattr("event_server._run_orchestrate",
                        lambda args: calls.append(args) or 0)
    monkeypatch.setattr("event_server._state_file", lambda: "/state/path")
    monkeypatch.setattr("event_server._workspace", lambda: "/ws")
    monkeypatch.setattr("event_server._resolve_topic_key", lambda s, p: "om_parent")

    # Plain chat, no @ -> ignored entirely.
    _route("om_x1", "om_parent", "这个改法好像不太对", "ou_1", mentions=[])
    assert calls == [], "bot must NOT reply to non-@ chat"
    # @ + command keyword -> handled.
    _route("om_x2", "om_parent", "@优化", "ou_2", mentions=[])
    time.sleep(0.1)
    assert len(calls) == 1, "bot SHOULD reply to @-command"
    # @ of ANOTHER user (mention name != bot, no @-command) -> ignored.
    _route("om_x3", "om_parent", "@李四 这里呢", "ou_3", mentions=[{"name": "李四"}])
    time.sleep(0.1)
    assert len(calls) == 1, "mentioning another user must NOT trigger bot"
