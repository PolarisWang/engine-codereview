"""Regression tests for C#2 / C#3.

C#2: the legacy apply/push/rollback executor previously used a shared flat
{workspace}/{repo} checkout, so one topic's applied patch / branch switch could leak
into another topic's apply/push. It now resolves to the same per-topic
{repo}-review/{slug} dir as autofix (_resolve_repo_checkout).

C#3: a topic that crashes before close can leave its per-topic dir behind, which never
enters the open-dir ceiling and is never cleaned -> enough crashes exhaust
MAX_OPEN_CHECKOUT_DIRS. _sweep_orphan_checkout_dirs removes dirs whose slug belongs to
no live (non-CLOSED) topic, conservatively never touching a live topic's dir.
"""
import os
import pytest

import pipeline_state as ps
from orchestrate import _resolve_repo_checkout, _sweep_orphan_checkout_dirs, _checkout_dir_for


def _topic(**kw):
    base = {"engine_repo": "git@gitlab.booming-inc.com:booming/dev/chaos-cb-2.git",
            "review_branch": "feature/lod", "phase": "SCANNED"}
    base.update(kw)
    return base


# ── C#2 ─────────────────────────────────────────────────────────────────────
def test_apply_checkout_resolves_to_per_topic_dir():
    t = _topic()
    checkout, name = _resolve_repo_checkout("/w", t, "engine")
    assert name == "chaos-cb-2"
    assert checkout == _checkout_dir_for("/w", "chaos-cb-2", t)   # per-topic, not flat /w/chaos-cb-2
    assert checkout.startswith("/w/chaos-cb-2-review/")             # isolated pool + slug


def test_apply_checkout_differs_between_topics():
    t1 = _topic(review_branch="feature/lod")
    t2 = _topic(review_branch="feature/mempool_allocator")
    c1, _ = _resolve_repo_checkout("/w", t1, "engine")
    c2, _ = _resolve_repo_checkout("/w", t2, "engine")
    assert c1 != c2                          # isolation: two topics, two dirs
    assert os.path.dirname(c1) == os.path.dirname(c2)  # same repo pool


def test_apply_checkout_no_url_returns_none():
    assert _resolve_repo_checkout("/w", _topic(engine_repo=""), "game")[0] is None


# ── C#3 ─────────────────────────────────────────────────────────────────────
def test_sweep_removes_only_orphans(tmp_path, state_dir):
    state_dir.mkdir(parents=True, exist_ok=True)
    ws = str(tmp_path / "ws")
    repo = "chaos-cb-2"
    pool = os.path.join(ws, f"{repo}-review")
    os.makedirs(pool)

    live = "feature_lod"
    orphan = "feature_crashed_topic"
    for slug in (live, orphan):
        d = os.path.join(pool, slug)
        os.makedirs(os.path.join(d, ".git"))   # each dir is a real (stub) git repo

    # a live topic owning `feature_lod`, plus a closed and a missing topic
    ps.add_topic(str(state_dir), message_id="om_a", jira_key="EV-1", mode="scan", jira_url="https://j/EV-1")
    ps.set_topic_fields(str(state_dir), "om_a", review_branch="feature/lod")

    _sweep_orphan_checkout_dirs(ws, str(state_dir))

    assert os.path.isdir(os.path.join(pool, live))     # live topic's dir kept
    assert not os.path.exists(os.path.join(pool, orphan))  # orphan reclaimed


def test_sweep_keeps_live_topic_dir(tmp_path, state_dir):
    state_dir.mkdir(parents=True, exist_ok=True)
    ws = str(tmp_path / "ws")
    pool = os.path.join(ws, "chaos-cb-2-review", "feature_lod")
    os.makedirs(os.path.join(pool, ".git"))

    ps.add_topic(str(state_dir), message_id="om_x", jira_key="EV-9", mode="scan", jira_url="https://j/EV-9")
    ps.set_topic_fields(str(state_dir), "om_x", review_branch="feature/lod")

    _sweep_orphan_checkout_dirs(ws, str(state_dir))
    assert os.path.isdir(pool)               # live topic dir NOT deleted
