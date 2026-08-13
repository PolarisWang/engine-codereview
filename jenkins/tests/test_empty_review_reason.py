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
