"""Regression tests for interaction-enhancement (plan: 质疑/深入/更新结论 + registry).

Covers the new building blocks:
- B: resolve_findings(#N / file / all / critical) + _findings_indexed for stable ids
- C3: apply_review_overrides merge (amend/reclassify/resolve/add) that leaves the base
  findings intact
- A: command registry routes keywords to handlers (and unknown words fall through)
"""
import pytest

import orchestrate as O
from orchestrate import resolve_findings, apply_review_overrides

FINDINGS = [
    {"file": "a/one.cpp", "severity": "critical", "issue": "null deref in foo", "suggestion": "check null"},
    {"file": "b/two.cpp", "severity": "warning", "issue": "leak in bar", "suggestion": "free"},
    {"file": "c/three.cpp", "severity": "suggestion", "issue": "style nit", "suggestion": "rename"},
]


# ── B: finding ids + resolve ──

def test_findings_indexed_stable():
    ix = list(O._findings_indexed(FINDINGS))
    assert [(i, f["file"]) for i, f in ix] == [(1, "a/one.cpp"), (2, "b/two.cpp"), (3, "c/three.cpp")]


def test_resolve_by_number():
    out, hint = resolve_findings(FINDINGS, "#2")
    assert len(out) == 1 and out[0]["file"] == "b/two.cpp"
    assert hint == ""


def test_resolve_by_multiple_numbers():
    out, _ = resolve_findings(FINDINGS, "#1,#3")
    assert [f["file"] for f in out] == ["a/one.cpp", "c/three.cpp"]


def test_resolve_by_file_substring():
    out, hint = resolve_findings(FINDINGS, "one.cpp")
    assert [f["file"] for f in out] == ["a/one.cpp"]
    assert hint == ""


def test_resolve_all_critical():
    out, _ = resolve_findings(FINDINGS, "critical")
    assert [f["severity"] for f in out] == ["critical"]


def test_resolve_missing_number_hints():
    out, hint = resolve_findings(FINDINGS, "#99")
    assert out == [] and "第 99 条" in hint


# ── C3: overrides merge ──

def test_override_amend_changes_severity_inplace():
    base_before = [dict(f) for f in FINDINGS]
    merged, notes = apply_review_overrides(
        FINDINGS, [{"ref": "#2", "action": "reclassify", "severity": "critical"}])
    assert merged[1]["severity"] == "critical"      # amended in overlay copy
    # base findings dicts untouched (result_*.json stays immutable)
    assert base_before[1]["severity"] == "warning"
    assert "重新定级" in notes[0]


def test_override_resolve_marks_finding():
    merged, notes = apply_review_overrides(
        FINDINGS, [{"ref": "#3", "action": "resolve"}])
    assert merged[2].get("_resolved") is True
    assert any("#3 已关闭" in n for n in notes)


def test_override_add_appends():
    merged, notes = apply_review_overrides(
        FINDINGS, [{"action": "add", "file": "d/new.cpp", "issue": "extra", "suggestion": "fix"}])
    assert len(merged) == len(FINDINGS) + 1
    assert merged[-1]["file"] == "d/new.cpp"


# ── A: registry ──

def test_registry_has_new_interaction_commands():
    assert "优化" in O._HANDLER_REGISTRY
    assert "更新结论" in O._HANDLER_REGISTRY
    assert "深入" in O._HANDLER_REGISTRY
    assert "质疑" in O._HANDLER_REGISTRY


def test_dispatch_unknown_returns_none():
    assert O._dispatch_command({"word": "不存在词"}) is None
