"""Tests for mr_url repo override: when MR is on a different repo than project config,
override engine_repo/game_repo to the MR's actual repo so the review clones correctly.
"""
import jira_parser as jp


def test_repo_matches_mr_url_same_project():
    # engine_repo = chaos-cb-2, MR on chaos-cb-2 -> matches
    assert jp.repo_matches_mr_url(
        "git@gitlab.booming-inc.com:booming/dev/projects/conquerorsblade2/chaos-cb-2.git",
        "https://gitlab.booming-inc.com/booming/dev/projects/conquerorsblade2/chaos-cb-2/-/merge_requests/7009"
    ) is True


def test_repo_does_not_match_mr_url_different_project():
    # engine_repo = chaos.git, MR on chaos-cb-2 -> does NOT match
    assert jp.repo_matches_mr_url(
        "git@gitlab.booming-inc.com:booming/dev/chaos.git",
        "https://gitlab.booming-inc.com/booming/dev/projects/conquerorsblade2/chaos-cb-2/-/merge_requests/7009"
    ) is False


def test_parse_mr_url_extracts_correct_project():
    pp, iid = jp.parse_gitlab_mr_url(
        "https://gitlab.booming-inc.com/booming/dev/projects/conquerorsblade2/chaos-cb-2/-/merge_requests/7009")
    assert pp == "booming/dev/projects/conquerorsblade2/chaos-cb-2"
    assert iid == "7009"


def test_mr_repo_url_construction():
    """The override constructs: git@gitlab.booming-inc.com:{project_path}.git"""
    pp, _ = jp.parse_gitlab_mr_url(
        "https://gitlab.booming-inc.com/booming/dev/projects/conquerorsblade2/chaos-cb-2/-/merge_requests/7009")
    mr_repo = f"git@gitlab.booming-inc.com:{pp}.git"
    assert mr_repo == "git@gitlab.booming-inc.com:booming/dev/projects/conquerorsblade2/chaos-cb-2.git"
