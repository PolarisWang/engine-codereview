"""Regression tests for R9: checkout file-path traversal.

Finding `file` values come from LLM review output (untrusted). If a file path
contains `..`, is absolute, or resolves via a symlink out of the checkout, we
must refuse it — never let code write outside the topic's checkout directory.

This tests the centralized `_safe_checkout_path` helper in orchestrate.py
(方案 A): every join(checkout, file) call site must route through it.
"""
import os

import pytest

from orchestrate import _safe_checkout_path


def _make_checkout(tmp_path):
    """A real checkout dir + one nested subdir, so join/repath is meaningful."""
    co = tmp_path / "checkout"
    co.mkdir()
    (co / "sub").mkdir()
    (co / "src").mkdir()
    return co


def test_accepted_relative_child(co_fix):
    assert _safe_checkout_path(str(co_fix), "src") == str(co_fix / "src")


def test_refuses_path_traversal_escape(co_fix):
    with pytest.raises(ValueError):
        _safe_checkout_path(str(co_fix), "../secret.yaml")


def test_refuses_deep_traversal(co_fix):
    with pytest.raises(ValueError):
        _safe_checkout_path(str(co_fix), "a/../../etc/passwd")


def test_refuses_absolute_path(co_fix):
    with pytest.raises(ValueError):
        _safe_checkout_path(str(co_fix), "/etc/passwd")


def test_refuses_traversal_via_symlink(co_fix, tmp_path):
    # A symlink INSIDE checkout that points outside must be resolved away.
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (co_fix / "link.txt").symlink_to(outside)
    with pytest.raises(ValueError):
        _safe_checkout_path(str(co_fix), "link.txt")


def test_normal_path_with_depth(co_fix):
    assert _safe_checkout_path(str(co_fix), "src/util/module.py") == str(co_fix / "src/util/module.py")
