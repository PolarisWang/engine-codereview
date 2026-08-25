"""Tests for the vendored-from-rage review quality core.

These cover the *pure* logic that was copied verbatim from upstream
rage/review-bot (`vendor/rage-review-bot/`) and is platform-agnostic:
the state machine (self-service dev loop), the settled-issue ledger, and
the index/triage reply-parse grammar. They guard the port: any future
platform-adapter edit must not change this behavior.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "rage-review"))

import state_machine as sm
import review_rounds as rr
import reply_parser as rp


# ── state machine (rage DESIGN §1.5 / §1.23 self-service dev loop) ─────────

def test_terminal_and_open_states():
    assert sm.TERMINAL_STATES == {"MERGED", "CLOSED"}
    assert "APPROVED" in sm.OPEN_STATES  # APPROVED is NOT terminal (merge train)
    assert len(sm.OPEN_STATES) == 10


def test_round1_inverted_triage_to_dev():
    # simple -> inline review -> DEV_TRIAGE (developer triages first)
    assert sm.transition("TRIAGING", "triage_simple") == "INLINE_REVIEW"
    assert sm.transition("INLINE_REVIEW", "dev_triage_ready") == "DEV_TRIAGE"
    # complex -> full review -> DEV_TRIAGE
    assert sm.transition("TRIAGING", "triage_complex") == "FULL_REVIEW"
    assert sm.transition("FULL_REVIEW", "dev_triage_ready") == "DEV_TRIAGE"


def test_dev_loop():
    # dev triage -> fix state, per review.triage
    assert sm.transition("DEV_TRIAGE", "revision_simple") == "SIMPLE_REVISION"
    assert sm.transition("DEV_TRIAGE", "revision_full") == "FULL_REVISION"
    # every round lands back in DEV_TRIAGE (non-legacy path)
    assert sm.transition("SIMPLE_REVISION", "dev_triage_ready") == "DEV_TRIAGE"
    assert sm.transition("FULL_REVISION", "dev_triage_ready") == "DEV_TRIAGE"
    # `done` -> hand to approver
    assert sm.transition("DEV_TRIAGE", "handoff") == "AWAITING_APPROVAL"
    assert sm.transition("SIMPLE_REVISION", "handoff") == "AWAITING_APPROVAL"


def test_approver_window_and_override():
    assert sm.transition("AWAITING_APPROVAL", "approve") == "APPROVED"
    assert sm.transition("AWAITING_APPROVAL", "close") == "CLOSED"
    # approver reinstate of dev-rejected issues
    assert sm.transition("AWAITING_APPROVAL", "revision_simple") == "SIMPLE_REVISION"
    assert sm.transition("AWAITING_APPROVAL", "revision_full") == "FULL_REVISION"
    # approver override during DEV_TRIAGE
    assert sm.transition("DEV_TRIAGE", "approve") == "APPROVED"


def test_illegal_transition_returns_none():
    assert sm.transition("TRIAGING", "approve") is None
    assert sm.transition("DEV_TRIAGE", "approve_unsupported") is None
    assert sm.transition("NOPE", "approve") is None


def test_approved_transient():
    assert sm.transition("APPROVED", "merge_detected") == "MERGED"
    assert sm.transition("APPROVED", "mr_closed") == "CLOSED"
    assert sm.transition("MERGED", "mr_closed") == "MERGED"  # defensive no-op


# ── settled-issue ledger (rage DESIGN §1.4.8) ──────────────────────────────

def test_settled_verdicts():
    assert rr.SETTLED_VERDICTS == ("addressed", "obsolete")


# ── reply grammar (rage DESIGN §1.18 / §1.23.1) ────────────────────────────

def test_indices_grammar():
    assert rp.parse_indices_with_mode("1,3,5")["indices"] == [1, 3, 5]
    assert rp.parse_indices_with_mode("1 3 5")["indices"] == [1, 3, 5]
    assert rp.parse_indices_with_mode("1，3，5")["indices"] == [1, 3, 5]
    assert rp.parse_indices_with_mode("all")["exclude"] is True
    assert rp.parse_indices_with_mode("all")["indices"] == []
    excl = rp.parse_indices_with_mode("-1 -3")
    assert excl["exclude"] is True and excl["indices"] == [1, 3]


def test_indices_none_needs_flag():
    # `none`/`0`/`不修` only when allow_none=True
    assert rp.parse_indices_with_mode("none") is None
    n = rp.parse_indices_with_mode("none", allow_none=True)
    assert n is not None and n.get("none") is True
