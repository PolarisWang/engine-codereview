"""Regression tests for 方案A (P1): the fix-MR baseline-consistency guard.

Root cause attached (MR7091): a fix MR can be created whose diff drags in an
unrelated feature (e.g. CB2N-27312 mempool commits) because the fix branch was not
built cleanly on the review branch — the shared checkout carried a foreign baseline.

The guard asserts a fix MR may ONLY carry bot-generated `[codereview-agent]`
commits; any non-bot commit in the compare range means the baseline is polluted, so
we refuse to create rather than emit a garbage MR. The decision rule is a pure
function so it can be tested without GitLab.
"""
from orchestrate import _is_bot_fix_commit


def test_bot_fix_commit_detected():
    assert _is_bot_fix_commit("[codereview-agent] auto-fix om_x1 (2 files)") is True
    assert _is_bot_fix_commit("[codereview-agent] apply review fix for EV-1") is True


def test_non_bot_commit_rejected():
    # e.g. a dev feature commit that must NOT ride along in a fix MR
    assert _is_bot_fix_commit("CB2N-27312: mempool 完整修复 - Slab/TLSF/BigSize") is False
    assert _is_bot_fix_commit("", ) is False
    assert _is_bot_fix_commit(None) is False
    assert _is_bot_fix_commit("chore: bump deps") is False


def test_rejects_strings_that_only_contain_bot_word_elsewhere():
    # must be a PREFIX match, not a substring: "my [codereview-agent] ..." is not ours
    assert _is_bot_fix_commit("docs: mention [codereview-agent] here") is False
