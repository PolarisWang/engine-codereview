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
