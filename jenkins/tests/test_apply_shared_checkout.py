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
import subprocess

import pytest

import pipeline_state as ps
from orchestrate import (
    _resolve_repo_checkout, _sweep_orphan_checkout_dirs, _checkout_dir_for,
    _ensure_shared_checkout,
)


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


# ── _ensure_shared_checkout (apply/push/rollback checkout) ─────────────────
# These exercise the FULL function with subprocess.run mocked, catching regressions
# the pure resolve-helper tests miss (e.g. a missing `_sp` import, or the clone URL).

class _FakeSubprocess:
    """Stands in for subprocess.run, faking 'git clone' by materializing the target
    dir as a (stub) repo so the function sees .git present on a subsequent call."""

    def __init__(self):
        self.calls = []
        self._made = set()

    def __call__(self, cmd, *a, **k):
        self.calls.append(cmd)
        # Resolve the repo dir from `-C <dir>` if present (git -C form), else cwd kwarg.
        repo_dir = k.get("cwd")
        if cmd and len(cmd) >= 2 and cmd[0] == "git" and cmd[1] == "-C":
            repo_dir = cmd[2]
        # Simulate `git clone <url> <dest>` by creating <dest>/.git
        if cmd and cmd[0] == "git" and "clone" in cmd:
            dest = cmd[-1]
            self._made.add(dest)
            if not os.path.isdir(os.path.join(dest, ".git")):
                os.makedirs(os.path.join(dest, ".git"))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd and cmd[0] == "git":
            # fetch/reset/clean/etc on an existing repo -> success
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected cmd")

    def replaced(self):
        return {d for d in self._made}


def test_shared_checkout_clones_into_per_topic_dir(monkeypatch, tmp_path):
    fake = _FakeSubprocess()
    monkeypatch.setattr(subprocess, "run", fake)
    wb = str(tmp_path / "w")
    t = {"review_branch": "CB-101887",
         "game_repo": "git@gitlab.booming-inc.com:booming/dev/projects/conquerorsblade2/chaos-cb-2.git",
         "mr_url": "https://gitlab.booming-inc.com/booming/dev/projects/conquerorsblade2/chaos-cb-2/-/merge_requests/1"}
    co, err = _ensure_shared_checkout(t, "game", wb)
    assert err is None
    assert os.path.isdir(os.path.join(co, ".git"))          # clone materialized
    assert os.path.dirname(co).endswith("chaos-cb-2-review")  # per-topic pool
    assert co == _checkout_dir_for(wb, "chaos-cb-2", t)       # matches per-topic dir


def test_shared_checkout_no_repo_url_is_error():
    co, err = _ensure_shared_checkout({"review_branch": "x", "game_repo": "", "engine_repo": ""}, "game", "/w")
    assert co is None and "no repo_url" in (err or "")


def test_shared_checkout_reuse_already_cloned(monkeypatch, tmp_path):
    fake = _FakeSubprocess()
    monkeypatch.setattr(subprocess, "run", fake)
    wb = str(tmp_path / "w")
    t = {"review_branch": "CB-101887",
         "game_repo": "git@gitlab.booming-inc.com:booming/dev/projects/conquerorsblade2/chaos-cb-2.git",
         "mr_url": "https://gitlab.booming-inc.com/booming/dev/projects/conquerorsblade2/chaos-cb-2/-/merge_requests/1"}
    co1, _ = _ensure_shared_checkout(t, "game", wb)   # clone
    co2, err = _ensure_shared_checkout(t, "game", wb)   # reuse (dir now exists)
    assert err is None and co1 == co2
    assert os.path.isdir(os.path.join(co2, ".git"))

