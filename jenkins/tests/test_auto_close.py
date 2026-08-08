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
