"""Regression tests for per-topic-unique fix branch (根治 R1 drift).

Root cause: `_new_branch_name` returned `{src}-fix-{task}`, IDENTICAL for every
topic sharing a Jira. Different topics pushed to the SAME GitLab fix branch,
advancing its HEAD and making R1's recorded checkout_sha drift -> confirm
silently rejected ("checkout drifted", no MR). Now each topic gets a unique
branch suffix (short hash of message_id), deterministic per topic.
"""
import pytest

from orchestrate import _new_branch_name


def _topic(review_branch, jira, msg_id):
    return {"review_branch": review_branch, "jira_key": jira, "message_id": msg_id}


def test_same_topic_same_branch_deterministic():
    t = _topic("feature/CB2N-27312-clean-rebase3", "CB2N-27312", "om_AAAA1111")
    assert _new_branch_name(t) == _new_branch_name(t)


def test_different_topics_same_jira_different_branch():
    t1 = _topic("feature/CB2N-27312-clean-rebase3", "CB2N-27312", "om_AAAA")
    t2 = _topic("feature/CB2N-27312-clean-rebase3", "CB2N-27312", "om_BBBB")
    assert _new_branch_name(t1) != _new_branch_name(t2)


def test_branch_has_per_topic_suffix():
    t = _topic("feature/CB2N-27312-clean-rebase3", "CB2N-27312", "om_AAAA")
    n = _new_branch_name(t)
    assert "fix-" in n
    assert "-om_AAAA" not in n  # uses hash, not raw msg id (keep gitlab-safe)
    assert n.endswith("-") is False
    assert len(n) < 120  # branch name length safe


def test_missing_message_id_falls_back():
    t = _topic("feature/X", "EV-1", "")
    assert _new_branch_name(t) == "feature/X-fix-EV-1"


# --- 加固: _fix_branch 单源(staged 优先) + 一致性 ---

def _fxtopic(branch_staged=None, msg="om_ZZ", review="feature/R-1", jira="J-1"):
    t = {"review_branch": review, "jira_key": jira, "message_id": msg}
    if branch_staged:
        t["pending_patch"] = {"branch": branch_staged}
    return t


def test_fix_branch_uses_staged_when_present():
    from orchestrate import _fix_branch
    t = _fxtopic(branch_staged="staged/fix-abc")
    assert _fix_branch(t) == "staged/fix-abc"


def test_fix_branch_falls_back_to_new_branch_without_staged():
    from orchestrate import _fix_branch
    t = _fxtopic(branch_staged=None)
    assert _fix_branch(t) == _new_branch_name(t)


def test_assert_branch_consistent_ok_when_equal():
    from orchestrate import _assert_branch_consistent
    assert _assert_branch_consistent("a/b", "a/b", "k") is False


def test_assert_branch_consistent_flags_mismatch():
    from orchestrate import _assert_branch_consistent
    assert _assert_branch_consistent("a/b", "c/d", "k") is True
