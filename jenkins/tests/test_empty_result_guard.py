"""Regression tests for CB2N-30597: an LLM that returns an empty structured
review (no findings, no summary, no real text) for a REAL diff must NOT be
published as a fake "0 项 / 0 必改" success, and must never be written to the
review cache.

Previously a 17-file MR review posted "🔍 Code Review — 0 项 (0 必改)\n📊 🔴0 /
🟡0 / 🟢0" — that stub was produced unconditionally by
_build_markdown_from_findings([], {}) and cached for reuse, so the ticket kept
showing an empty card.
"""
import code_reviewer as cr
import orchestrate as orch


# ── code_reviewer: empty-result synthesis + guard ──────────────────────────

def test_empty_plain_text_answer_is_flagged_not_authorized():
    """Token answered in plain text with NO content at all:
    - the cell produces a synthesized no-finding stub (findings=None) ...
    - which the empty-result guard REJECTS (stub contains sev chars but the
      output still has neither findings nor summary, so it is treated as an
      LLM failure), never published as a real review.
    """
    assert not cr._empty_output_allowed({}, "🔍 Code Review — 0 项 (0 必改)\n📊 🔴0 / 🟡0 / 🟢0")
    # A card that explicitly states "未发现" IS allowed.
    assert cr._empty_output_allowed({}, "已完成审查，未发现问题，整体干净。")


def test_clean_with_summary_is_allowed():
    # Stub + explicit summary — a valid "clean" outcome.
    assert cr._empty_output_allowed({"summary": "本次修改引入 unloadFoliageInRange，审查后未发现需处理问题。"},
                                    "🔍 Code Review — 0 项 (0 必改)\n📊 🔴0 / 🟡0 / 🟢0")


def test_stub_text_alone_is_still_a_stub():
    # The uncompiled stub (rebuilt unconditionally during aggregation) must not
    # be allowed by itself — it only looks like a conclusion because of 🇨hars.
    # (With no findings and no summary it is rejected.)
    assert not cr._empty_output_allowed({}, "🔍 Code Review — 0 项 (0 必改)\n📊 🔴0 / 🟡0 / 🟢0")


def test_empty_output_allowed_requires_explicit_conclusion():
    assert not cr._empty_output_allowed({}, "")
    assert not cr._empty_output_allowed({}, " ")


# ── orchestrate: bad-empty caching guard ───────────────────────────────────

def _res(**kw):
    rv = {
        "branch": kw.get("branch", "feature/x"),
        "base_branch": kw.get("base", "master"),
        "changed_files": kw.get("changed", ["a.cpp"]),
        "branch_exists": kw.get("branch_exists", True),
        "branch_merged": kw.get("branch_merged", False),
    }
    rv["review"] = kw.get("review", {"findings": [], "error": None})
    return rv


def test_is_bad_empty_flags_empty_results():
    # Exactly the CB2N-30597 shape: no findings, no summary, stub/none text.
    assert orch._is_bad_empty_result(_res(review={
        "findings": [], "summary": "", "review_text": "🔍 Code Review — 0 项 (0 必改)\n📊 🔴0 / 🟡0 / 🟢0",
    }))
    assert orch._is_bad_empty_result(_res(review={"findings": [], "summary": ""}))


def test_is_bad_empty_allows_real_clean_and_findings():
    # Clean WITH conclusion -> cacheable.
    assert not orch._is_bad_empty_result(_res(review={
        "findings": [], "summary": "已完成审查，未发现问题", "review_text": "",
    }))
    # Non-empty findings -> cacheable.
    assert not orch._is_bad_empty_result(_res(review={
        "findings": [{"file": "a.cpp", "severity": "warning", "issue": "x"}], "summary": "s",
    }))
    # No review block at all -> not a bad-empty (guard only inspects review).
    assert not orch._is_bad_empty_result({})