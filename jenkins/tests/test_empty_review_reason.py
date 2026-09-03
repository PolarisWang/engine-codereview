"""Tests for _empty_review_reason: when engine+game have no findings, render WHY.

C(merged 优先) + A(兜底) per-repo attribution, so a merged/nonexistent/no-diff/error
review shows a clear reason instead of the misleading "未发现需要处理的代码问题".
"""
import feishu_notifier as fn


def _res(**kw):
    return {
        "branch": kw.get("branch", "feature/x"),
        "base_branch": kw.get("base", "master"),
        "changed_files": kw.get("changed_files", ["a.cpp"]),
        "branch_exists": kw.get("branch_exists", True),
        "branch_merged": kw.get("branch_merged", False),
        "review": {"findings": [], "error": kw.get("error")},
        "mr_state": kw.get("mr_state", ""),
    }


def test_merged_mr_c_priority():
    """MR 已合并 -> C 优先, 报'已合并无新改动', 不落入 branch_exists=False 的 A 判据."""
    eng = _res(branch_exists=False, base="master")
    gam = _res(branch_exists=False, base="master")
    eng["mr_state"] = gam["mr_state"] = "merged"
    out = fn._empty_review_reason(eng, gam)
    assert "已合并" in out and "没有新增改动" in out
    assert "不存在" not in out   # 不被 branch_exists=False 带偏


def test_branch_missing_a():
    out = fn._empty_review_reason(_res(branch_exists=False), _res(branch_exists=False))
    assert "不存在" in out and "分支" in out


def test_no_diff_a():
    """无变更文件, 无 merged -> 报'相对 base 无代码变更'."""
    out = fn._empty_review_reason(_res(changed_files=[]), _res(changed_files=[]))
    assert "无代码变更" in out or "不存在" in out


def test_clean_both_repos():
    """两仓库均 clean(有文件, 无merged/缺失/error) -> 报'未发现需要处理的代码问题'."""
    out = fn._empty_review_reason(_res(), _res())
    assert "未发现需要处理的代码问题" in out


def test_mixed_one_repo_dirty_dedup():
    """一 clean + 一 missing: 只显示非 clean 的原因(去重)."""
    eng = _res(changed_files=[])          # no_diff
    gam = _res(changed_files=[])          # 同样 no_diff
    out = fn._empty_review_reason(eng, gam)
    # 两仓库同因 -> 只写一遍(去重)
    assert out.count("无代码变更") == 1 or out.count("不存在") == 1


def test_error_shown():
    out = fn._empty_review_reason(_res(error="API timeout"), _res(error="API timeout"))
    assert "出错" in out or "error" in out.lower() or "API" in out


# ── ENG-33381 回归: 一个仓库真 clean review + 另一仓库 infra SKIPPED ───────────────
def _clean_res(**kw):
    """分支在、有改动、无 error(真实走完 review 且 0 findings 的"真 clean")。"""
    return _res(changed_files=["a.cpp"], branch_exists=True, branch_merged=False,
                error=None, mr_state=kw.get("mr_state", "opened"))


def _skip_branch_missing_res(branch="bugfix/ENG-33381-aifix-1"):
    """分支在游戏仓不存在(branch_exists=False) → infra SKIPPED, 没真正审。"""
    r = _res(changed_files=[])
    r["branch"] = branch
    r["branch_exists"] = False
    return r


def test_engine_clean_game_skip_branch_missing():
    """ENG-33381: 引擎(有实质改动)审出 0 findings(真干净), 游戏仓找不到分支(SKIPPED)。

    修复前: 被 SKIPPED 的游戏仓带偏 -> "无可审内容/游戏: 分支不存在"(误导, 明明审过了)。
    修复后: 有真审查结果 -> 报"未发现问题", 仅附带说明某仓未纳入实质审查。
    """
    out = fn._empty_review_reason(_clean_res(), _skip_branch_missing_res())
    assert "未发现需要处理的代码问题" in out
    assert "不存在" not in out          # 不被 SKIPPED 仓 带偏
    assert "游戏" in out                 # 附加说明是游戏仓

def test_engine_skip_game_clean():
    """镜像对称: 游戏真 clean, 引擎找不到分支 -> 仍报"未发现问题", 附带引擎."""
    out = fn._empty_review_reason(_skip_branch_missing_res(), _clean_res())
    assert "未发现需要处理的代码问题" in out
    assert "不存在" not in out
    assert "引擎" in out

def test_all_clean_still_reports_clean():
    """两个仓都真 clean -> 无任何附注(不带'未纳入'尾巴)."""
    out = fn._empty_review_reason(_clean_res(), _clean_res())
    assert out == "✅ 未发现需要处理的代码问题。"

def test_all_skip_branch_missing_still_attributed():
    """两个仓都找不到分支(都没审) -> 保留原"无可审内容/分支不存在"归因."""
    out = fn._empty_review_reason(_skip_branch_missing_res(), _skip_branch_missing_res())
    assert "无可审内容" in out
    assert "不存在" in out

