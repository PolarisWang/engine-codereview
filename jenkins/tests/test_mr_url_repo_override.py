"""Tests for mr_url repo override: when MR is on a different repo than project config,
override engine_repo/game_repo to the MR's actual repo so the review clones correctly.

方案A regression: the override applies ONLY to engine_repo; game_repo must NOT be pulled
to the MR's repo — otherwise a pure-engine MR (CB2N-27312) makes the game review reuse the
engine diff and impersonate a game review.
"""
import jira_parser as jp
from orchestrate import _apply_repo_override


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


# ── 方案A: engine 覆盖有效, game 不被覆盖 ─────────────────────────────
ENGINE_REPO = "git@gitlab.booming-inc.com:booming/dev/projects/conquerorsblade2/chaos-cb-2.git"
GAME_REPO = "git@gitlab.booming-inc.com:booming/dev/projects/conquerorsblade2/conquerors-blade-2.git"
MR_ENGINE = "https://gitlab.booming-inc.com/booming/dev/projects/conquerorsblade2/chaos-cb-2/-/merge_requests/6970"
MR_CONFIG_MISMATCH = "https://gitlab.booming-inc.com/booming/dev/projects/conquerorsblade2/chaos-cb-2/-/merge_requests/7009"


def test_game_repo_not_overridden_when_mr_is_engine():
    """核心回归(CB2N-27312): engine MR + engine repo -> game_repo 必须保持配置的真实游戏仓库,
    不能被覆盖成引擎仓库."""
    e, g = _apply_repo_override(ENGINE_REPO, GAME_REPO, MR_ENGINE)
    assert e == ENGINE_REPO          # MR 与 engine_repo 同项目 -> engine 覆盖不动(本来就对)
    assert g == GAME_REPO            # ← 关键: game 保持 conquerors-blade-2, 不被带偏


def test_game_repo_not_overridden_even_when_engine_override_happens():
    """即便 MR 项目 != 项目配置 engine_repo(触发 engine 覆盖), game_repo 也绝不能被覆盖."""
    # ENGINE_REPO 配置为 chaos.git(非 chaos-cb-2), 但 MR 在 chaos-cb-2 -> engine 会被覆盖
    chaos_cfg_engine = "git@gitlab.booming-inc.com:booming/dev/chaos.git"
    e, g = _apply_repo_override(chaos_cfg_engine, GAME_REPO, MR_CONFIG_MISMATCH)
    assert e == ENGINE_REPO          # engine_repo 被覆盖成 MR 所在仓库(chaos-cb-2)
    assert g == GAME_REPO            # game_repo 仍保持配置的真实游戏仓库


def test_no_mr_url_leaves_both_untouched():
    e, g = _apply_repo_override(ENGINE_REPO, GAME_REPO, "")
    assert e == ENGINE_REPO
    assert g == GAME_REPO

