"""Regression tests for pipeline_state.py (v2 dir-mode lifecycle).

Pure-logic coverage of the exact behaviors the review chain depends on:
  - dir-mode atomic per-topic writes (nothing clobbers unrelated topics)
  - phase ordering guard (no backward jumps)
  - CLOSED is a hard terminal (cannot leave, ignored, skipped)
  - retry backoff -> exhausted
  - pending queue set/clear/list
These are the state invariants that R7 (auto-close on DONE) and other lifecycle
fixes will later lean on.
"""
import time
import os

import pytest

import pipeline_state as ps


@pytest.fixture
def store(state_dir):
    """An empty dir-mode state store at an isolated path."""
    state_dir.mkdir(parents=True, exist_ok=True)
    return str(state_dir)


def test_dir_mode_add_topic_and_roundtrip(store):
    t = ps.add_topic(store, message_id="om_1", jira_key="EV-1", jira_url="https://j/EV-1",
                     mode="scan", text_preview="hello", sender_id="u1")
    assert t["phase"] == "SCANNED"
    assert t["status"] == "RUNNING"
    got = ps.get_topic(store, "om_1")
    assert got["jira_key"] == "EV-1"
    assert got["sender_id"] == "u1"


def test_second_add_is_idempotent_for_scan(store):
    ps.add_topic(store, message_id="om_1", jira_key="EV-1", mode="scan", jira_url="https://j/EV-1")
    # A previous scan review reached DONE; a fresh scan add of the same message
    # must NOT reset it (dedup: terminal records are already-processed).
    ps.transition(store, "om_1", to="DONE", status="SUCCESS")
    ps.add_topic(store, message_id="om_1", jira_key="EV-1", mode="scan", jira_url="https://j/EV-1")
    after = ps.get_topic(store, "om_1")
    assert after["phase"] == "DONE"  # scan add leaves existing (terminal) record untouched

def test_manual_add_resets_terminal_topic(store):
    ps.add_topic(store, message_id="EV-9", jira_key="EV-9", mode="scan", jira_url="https://j/EV-9")
    ps.transition(store, "EV-9", to="DONE", status="SUCCESS")
    # Manual mode explicitly re-runs the same issue -> allowed to reset.
    fresh = ps.add_topic(store, message_id="EV-9", jira_key="EV-9", mode="manual", jira_url="https://j/EV-9")
    assert fresh["phase"] == "SCANNED"
    assert fresh["mode"] == "manual"


def test_unrelated_topics_do_not_clobber_each_other(store):
    ps.add_topic(store, message_id="om_a", jira_key="EV-1", mode="scan")
    ps.add_topic(store, message_id="om_b", jira_key="EV-2", mode="scan")
    # Update only A's phase; B must be untouched (per-topic atomic write).
    ps.transition(store, "om_a", to="DONE", status="SUCCESS")
    b = ps.get_topic(store, "om_b")
    assert b["phase"] == "SCANNED"
    a = ps.list_topic_keys(store)
    assert set(a) == {"om_a", "om_b"}


def test_illegal_backward_phase_transition_raises(store):
    ps.add_topic(store, message_id="om_1", jira_key="EV-1", mode="scan")
    ps.transition(store, "om_1", to="REVIEWING", status="RUNNING")
    with pytest.raises(ValueError):
        ps.transition(store, "om_1", to="PARSING", status="RUNNING")


def test_closed_is_hard_terminal_cannot_leave(store):
    ps.add_topic(store, message_id="om_1", jira_key="EV-1", mode="scan")
    ps.close_topic(store, "om_1", closed_by="u1", reason="done")
    t = ps.get_topic(store, "om_1")
    assert ps.is_closed(t)
    with pytest.raises(ValueError):
        ps.transition(store, "om_1", to="REVIEWING", status="RUNNING")
    # reset_for_retry must NOT open a closed topic (R18: hard-terminal invariant)
    reset = ps.reset_for_retry(store, "om_1")
    assert reset["phase"] == "CLOSED"
    assert reset["status"] == "CLOSED"
    assert reset["closed_by"] == "u1"


def test_retry_backoff_then_exhausted(store):
    ps.add_topic(store, message_id="om_1", jira_key="EV-1", mode="scan")
    # 5 backoff attempts, then exhausted.
    for _ in range(5):
        t = ps.record_failure(store, "om_1", "boom")
        assert t["failed_exhausted"] is False
    t = ps.record_failure(store, "om_1", "boom")
    assert t["failed_exhausted"] is True
    assert ps.can_retry(store, "om_1") is False
    assert ps.get_retryable(store) == []


def test_retryable_respects_backoff_window(store):
    ps.add_topic(store, message_id="om_1", jira_key="EV-1", jira_url="https://j/EV-1", mode="scan")
    ps.record_failure(store, "om_1", "boom")  # next_retry_at = now+60
    # Immediately: not retryable yet.
    assert ps.get_retryable(store) == []
    # Rewind the clock concept by rolling next_retry_at back.
    t = ps.get_topic(store, "om_1")
    t["next_retry_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 10))
    ps._save_topic(store, "om_1", t)
    assert [x["message_id"] for x in ps.get_retryable(store)] == ["om_1"]


def test_pending_queue_set_clear_list(store):
    ps.add_topic(store, message_id="om_1", jira_key="EV-1", mode="scan")
    assert ps.list_pending_topics(store) == []
    ps.set_pending(store, "om_1", "re_review")
    ps.add_topic(store, message_id="om_2", jira_key="EV-2", mode="scan", jira_url="https://j/EV-2")
    ps.set_pending(store, "om_2", "apply", patch={"file": "a.py"})
    keys = [k for k, _ in ps.list_pending_topics(store)]
    assert set(keys) == {"om_1", "om_2"}
    assert ps.get_pending(store, "om_2")["action"] == "apply"
    ps.clear_pending(store, "om_1")
    assert "om_1" not in [k for k, _ in ps.list_pending_topics(store)]
    assert "om_2" in [k for k, _ in ps.list_pending_topics(store)]


def test_set_repo_success_clears_stale_skip_reason(tmp_path):
    """重审后 SKIPPED 残留的 skip_reason 必须在 SUCCESS 时清除(ENG-34409)."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        sf = os.path.join(tmp, "s.json")
        ps.save_state({"schema_version": 1, "updated_at": "", "topics": {}}, sf)
        ps.add_topic(sf, message_id="om", jira_key="ENG-34409", project="ENG", sender_id="ou")
        # 先记 SKIPPED + skip_reason(首次 branch 不存在)
        ps.set_repo(sf, "om", "engine", status="SKIPPED",
                    skip_reason="branch ENG-34409 not remote", repo_url="x")
        # 重审后 SUCCESS —— skip_reason/error 必须被清掉
        ps.set_repo(sf, "om", "engine", status="SUCCESS",
                    stats="2 files changed", changed_files=2, repo_url="x")
        t = ps.get_topic(sf, "om")
        e = t["repos"]["engine"]
        assert e["status"] == "SUCCESS"
        assert e.get("skip_reason") == "", f"skip_reason should be cleared, got {e.get('skip_reason')!r}"
        assert e.get("error") == ""
        assert e.get("changed_files") == 2
