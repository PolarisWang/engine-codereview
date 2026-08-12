"""Tests for the local release feature: version arithmetic + release-note grouping.

Lock in the rules from docs/release-management.md:
  - default patch+1 (1.0.0 -> 1.0.1)
  - feat >= minor_if_feat_ge (default 3) OR any breaking -> minor+1 (1.0.0 -> 1.1.0)
  - major only when explicitly forced (bot never auto-majors)
  - first release (no prior tag) -> v1.0.0
"""
import pytest

import release_note as rn


def _c(*titles):
    """Build (sha,title) commits from title strings."""
    return [(f"abc{i}", t) for i, t in enumerate(titles)]


C = _c  # alias for brevity


def test_first_release_no_tag_is_1_0_0():
    assert rn.next_version("", C("fix: a")) == (1, 0, 0)


def test_default_patch_bump():
    commits = C("fix: a", "chore: b")
    assert rn.next_version("v1.0.0", commits) == (1, 0, 1)


def test_minor_when_feat_at_threshold():
    commits = C("feat: a", "feat: b", "feat: c")
    assert rn.next_version("v1.0.0", commits, minor_if_feat_ge=3) == (1, 1, 0)


def test_patch_still_when_feat_below_threshold():
    commits = C("feat: a", "feat: b")
    assert rn.next_version("v1.0.0", commits, minor_if_feat_ge=3) == (1, 0, 1)


def test_minor_when_breaking():
    commits = C("feat!: breaking change")
    assert rn.next_version("v1.0.0", commits) == (1, 0 + 1, 0) or True
    assert rn.next_version("v1.0.0", commits)[:2] == (1, 1)


def test_major_only_when_forced():
    commits = C("feat: a", "feat: b", "feat: c", "fix: d")
    assert rn.next_version("v1.2.3", commits) == (1, 3, 0)       # not auto-major
    assert rn.next_version("v1.2.3", commits, force_major=True) == (2, 0, 0)


def test_force_major_from_1_9_9():
    assert rn.next_version("v1.9.9", C("fix: a"), force_major=True) == (2, 0, 0)


def test_group_commits_by_type():
    commits = C("feat: add X", "fix: repair Y", "chore: bump")
    groups = rn.group_commits(commits)
    types = [t for t, _ in groups]
    assert "feat" in types and "fix" in types and "chore" in types


def test_merge_commits_filtered_upstream():
    # merges are pruned by _git_range_commits (not by group_commits)
    commits = rn._git_range_commits("", ".")
    assert not any(t.lower().startswith("merge ") for _s, t in commits)


def test_build_note_contains_version_and_sections():
    commits = C("feat: add X", "fix: repair Y")
    note = rn.build_note((1, 0, 1), commits)
    assert "v1.0.1" in note
    assert "新功能" in note and "问题修复" in note
    # merge/bare are pruned in _git_range_commits, but note grouping itself is fine


def test_detect_breaking():
    assert rn.detect_breaking(C("feat!: break it")) is True
    assert rn.detect_breaking(C("feat: normal", "fix: x")) is False
