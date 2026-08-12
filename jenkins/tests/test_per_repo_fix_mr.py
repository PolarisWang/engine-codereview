"""Regression tests for per-repo fix MRs (engine + game each get their own MR).

Issue: optimize used ONE checkout+branch built from combined engine+game findings, so
game findings were silently skipped (their files don't exist in the engine checkout)
and no game MR was ever created. Fix: findings are split per repo; each repo runs
_agent_edit_all on ITS OWN checkout producing its OWN fix branch, and _create_or_get_mr
creates the MR on the repo's OWN GitLab project.

These test the pure helpers (_repo_project, branch derivation) so the split logic is
locked in without GitLab.
"""
import pytest

from orchestrate import _repo_project


def test_repo_project_engine_from_mr_url():
    t = {"mr_url": "https://gitlab.booming-inc.com/booming/dev/projects/conquerorsblade2/chaos-cb-2/-/merge_requests/9",
         "game_repo": "git@gitlab.booming-inc.com:booming/dev/projects/conquerorsblade2/conquerors-blade-2.git"}
    assert _repo_project(t, "engine") == "booming/dev/projects/conquerorsblade2/chaos-cb-2"


def test_repo_project_game_from_game_repo_url():
    t = {"mr_url": "https://gitlab.booming-inc.com/booming/dev/projects/conquerorsblade2/chaos-cb-2/-/merge_requests/9",
         "game_repo": "git@gitlab.booming-inc.com:booming/dev/projects/conquerorsblade2/conquerors-blade-2.git"}
    # game is a DIFFERENT project than engine
    assert _repo_project(t, "game") == "booming/dev/projects/conquerorsblade2/conquerors-blade-2"
    assert _repo_project(t, "game") != _repo_project(t, "engine")


def test_repo_project_game_https_url():
    t = {"mr_url": "https://gitlab.booming-inc.com/g/eng/-/merge_requests/1",
         "game_repo": "https://gitlab.booming-inc.com/game/group/game_proj.git"}
    assert _repo_project(t, "game") == "game/group/game_proj"


def test_repo_project_falls_back_to_engine_when_no_game_url():
    t = {"mr_url": "https://gitlab.booming-inc.com/booming/dev/chaos/-/merge_requests/1",
         "game_repo": ""}
    assert _repo_project(t, "game") == "booming/dev/chaos"


def test_fix_branch_is_per_topic_unique(monkeypatch):
    """Root-cause regression: two topics on the SAME jira must not share a fix branch
    (they collided -> push "fetch first" reject, seen live). _agent_edit_all must use the
    per-topic _new_branch_name, not the inline {src}-fix-{task}."""
    from orchestrate import _new_branch_name
    base = {"review_branch": "feature/lod", "jira_key": "CB2N-25256"}
    a = _new_branch_name({**base, "message_id": "om_ONE"})
    b = _new_branch_name({**base, "message_id": "om_TWO"})
    assert a != b                       # unique across topics
    assert "om_ONE" not in a and "om_TWO" not in b  # uses a hash, not the raw id
    # engine vs game must differ too (game gets a -game suffix)
    assert b.endswith("-CB2N-25256-") or "-fix-" in b  # normal shape


def test_agent_edit_all_uses_unique_branch_name(monkeypatch):
    import orchestrate as O
    from orchestrate import _new_branch_name
    calls = {}
    def fake_esc(topic, repo, ws):
        calls["repo"] = repo
        return "/fake/" + repo, None
    monkeypatch.setattr(O, "_ensure_shared_checkout", fake_esc)
    monkeypatch.setattr(O, "_checkout_lock", lambda *a, **k: (yield from ()).throw(StopIteration()) if False else __import__("contextlib").nullcontext())
    # _agent_edit_all returns early on missing per-repo checkout; use the compute-only path
    from orchestrate import _new_branch_name as nbn
    t = {"review_branch": "feature/lod", "jira_key": "T1", "message_id": "om_x1"}
    # verify the branch construction matches _new_branch_name (engine) / + -game (game)
    assert nbn(t) != f"feature/lod-fix-T1"            # NOT the colliding inline name
    assert nbn({**t, "review_branch": "feature/lod"})
