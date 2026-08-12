"""Regression tests for 方案B: per-topic isolated checkout dirs + disk guards.

Root cause being locked in (MR 7091): every topic on one git repo previously shared
a single `{repo}-review` checkout, so a prior topic's git state (e.g. a mempool
branch's history) could leak into another topic's fix branch and pollute its MR.
The fix isolates checkout per topic into `{repo}-review/{slug}`, frees the dir on
topic close, and guards disk with a ceiling + free-space threshold.

These test only the PURE helpers (_checkout_dir_for / _checkout_slug /
_repo_name_from_checkout / _list_checkout_dirs / _disk_free_bytes) — nothing that
touches the network or production workspace.
"""
import os

from orchestrate import (
    _checkout_dir_for, _checkout_slug,
    _repo_name_from_checkout, _list_checkout_dirs, _disk_free_bytes,
)


def test_slug_is_deterministic_and_branch_scoped():
    topic = {"review_branch": "feature/lod_framework_optimization_P2_test1",
             "message_id": "om_x1"}
    a = _checkout_slug(topic)
    b = _checkout_slug(topic)
    assert a == b                       # deterministic within a topic's lifetime
    assert "/" not in a                 # filesystem-safe: no path separators
    assert "feature_lod_framework_optimization_P2_test1" == a  # branch-derived


def test_different_branches_get_different_dirs():
    t1 = {"review_branch": "feature/mempool_allocator"}
    t2 = {"review_branch": "feature/lod_framework"}
    d1 = _checkout_dir_for("/w", "chaos-cb-2", t1)
    d2 = _checkout_dir_for("/w", "chaos-cb-2", t2)
    assert d1 != d2                       # isolation: distinct checkout dirs
    assert os.path.dirname(d1) == os.path.dirname(d2)  # same repo pool parent


def test_checkout_dir_is_under_repo_review_pool():
    topic = {"review_branch": "feature/lod"}
    d = _checkout_dir_for("/w", "chaos-cb-2", topic)
    assert d == os.path.join("/w", "chaos-cb-2-review", "feature_lod")


def test_repo_name_from_checkout_both_layouts():
    # nested (per-topic, new) -> repo key
    assert _repo_name_from_checkout("/w/chaos-cb-2-review/feature_lod") == "chaos-cb-2"
    # flat (legacy) -> repo key
    assert _repo_name_from_checkout("/w/chaos-cb-2-review") == "chaos-cb-2"


def test_list_checkout_dirs_counts_only_git_dirs(tmp_path):
    # Regression: legacy pools have the whole repo tree flat at {repo}-review/, whose
    # content dirs (_source/ etc.) have NO .git and MUST NOT be counted as per-topic
    # checkouts (this caused the open-dir ceiling to miscount 18 when only 1 was real,
    # wrongly refusing 优化/改码). Count only dirs that are real checkouts (have .git).
    repo = "chaos-cb-2"
    pool = tmp_path / f"{repo}-review"
    # real per-topic checkouts (git-clone targets) -> have .git
    (pool / "feature_lod" / ".git").mkdir(parents=True)
    (pool / "feature_mempool" / ".git").mkdir(parents=True)
    # legacy flat repo CONTENT dirs -> no .git, must be ignored
    (pool / "_source").mkdir()
    (pool / "_content").mkdir()
    (pool / "not_a_dir").write_text("x")
    dirs = _list_checkout_dirs(str(tmp_path), repo)
    assert len(dirs) == 2                                  # only the 2 real checkouts
    assert all(os.path.isdir(d) and os.path.basename(d).startswith("feature_") for d in dirs)
    assert all(not os.path.basename(d).startswith("_") for d in dirs)  # no legacy content dirs


def test_disk_free_bytes_reports_positive(tmp_path):
    free = _disk_free_bytes(str(tmp_path))
    assert free > 0                      # the filesystem reports real free space
