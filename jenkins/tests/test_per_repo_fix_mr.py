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
