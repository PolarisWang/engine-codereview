"""Tests for feishu_scanner S2 fix (ENG-34158): larger initial window so a topic
posted during a scanner outage/deploy gap is not permanently missed.

The scanner anchors its Feishu time-window on a persisted cursor. When the cursor
is missing (fresh deploy / workspace clean), it falls back to an "initial window".
Previously that was 300s — too small to cover a topic posted during the outage,
so the cursor advanced past it and the topic was dropped forever. Now it covers
1 hour (3600s).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import feishu_scanner as fs


def test_initial_window_is_one_hour():
    # S2: a just-past-a-short-window topic (e.g. 8 min old, as in ENG-34158) must
    # still be covered by the initial window. 3600s ≈ 1h >> 8min.
    assert fs.INITIAL_WINDOW == 3600, "initial window must be 3600s (1h) so young topics aren't missed"
    assert fs.OVERLAP == 60
    assert fs.MAX_GAP == 86400


def test_cursor_based_window_insets_overlap():
    # Ensure the cursor-based path is still what's used once a cursor exists
    # (not accidentally replaced by an always-initial window).
    import inspect
    src = inspect.getsource(fs)
    assert "window_start = last_scan - OVERLAP" in src
    assert "cursor-based" in src


def test_extract_jira_urls_eng():
    # extract_jira_urls recognizes an ENG browse link the scanner keys on.
    urls = fs.extract_jira_urls("see https://jira.boomingtechs.cn/browse/ENG-34158")
    assert any(u[1] == "ENG-34158" for u in urls)


def test_extract_jira_urls_none():
    assert fs.extract_jira_urls("just chatting, no link") == []
