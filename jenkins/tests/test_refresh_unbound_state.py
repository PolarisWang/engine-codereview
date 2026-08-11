"""Regression tests for P0: interact() refactored activity-refresh helper.

The original bug (orchestrate.py interact()): `state_file` was referenced inside
a try block before its assignment, so every user reply raised UnboundLocalError
that a bare `except: pass` swallowed — last_user_activity was NEVER refreshed,
silently defeating the F2 idle-detection (and thereby auto-close) feature.

The fix extracts `_refresh_last_user_activity(state_file, key)`, takes state_file
as a parameter (no before-use), and logs instead of swallowing silently.
"""
import time

import pytest

import pipeline_state as ps
from orchestrate import _refresh_last_user_activity


@pytest.fixture
def store(state_dir):
    state_dir.mkdir(parents=True, exist_ok=True)
    return str(state_dir)


def test_reply_refreshes_last_user_activity(store):
    ps.add_topic(store, message_id="om_1", jira_key="EV-1", mode="scan",
                 jira_url="https://j/EV-1")
    assert _refresh_last_user_activity(store, "om_1") is True
    t = ps.get_topic(store, "om_1")
    assert t.get("last_user_activity")  # field IS set now (was never set before)


def test_does_not_refresh_closed_topic(store):
    ps.add_topic(store, message_id="om_c", jira_key="EV-2", mode="scan",
                 jira_url="https://j/EV-2")
    ps.close_topic(store, "om_c", closed_by="u1", reason="x")
    assert _refresh_last_user_activity(store, "om_c") is False
    assert ps.get_topic(store, "om_c").get("last_user_activity") is None


def test_refresh_sets_recent_timestamp(store):
    ps.add_topic(store, message_id="om_a", jira_key="EV-3", mode="scan",
                 jira_url="https://j/EV-3")
    assert _refresh_last_user_activity(store, "om_a")
    ts = ps.get_topic(store, "om_a").get("last_user_activity") or ""
    assert ts  # non-empty now (previously always empty)
    # recent (within 5 min) — an idle gate of hours/days will treat it as active
    recent = time.strptime(ts, "%Y-%m-%dT%H:%M:%S")
    assert (time.time() - time.mktime(recent)) < 300


def test_missing_topic_is_noop(store):
    assert _refresh_last_user_activity(store, "does_not_exist") is False
