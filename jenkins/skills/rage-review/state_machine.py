"""
Review bot state machine — validates and executes state transitions.

Given (current_state, action) returns the next state, or None if invalid.

Usage:
    python state_machine.py --state TRIAGE_DECISION --action approve
"""

import argparse
import json
import sys


# Valid transitions: (current_state, action) -> next_state
TRANSITIONS = {
    ("TRIAGING", "triage_simple"): "INLINE_REVIEW",
    ("TRIAGING", "triage_complex"): "FULL_REVIEW",
    ("INLINE_REVIEW", "review_done"): "TRIAGE_DECISION",
    ("TRIAGE_DECISION", "approve"): "APPROVED",
    ("TRIAGE_DECISION", "escalate"): "FULL_REVIEW",
    ("TRIAGE_DECISION", "revision"): "SIMPLE_REVISION",
    ("SIMPLE_REVISION", "dev_reply"): "TRIAGE_DECISION",   # legacy, pre-1.23.6
    # Approver override in revision states: skip waiting for dev push.
    ("SIMPLE_REVISION", "approve"): "APPROVED",
    ("SIMPLE_REVISION", "close"):   "CLOSED",
    ("FULL_REVIEW", "review_done"): "AWAITING_APPROVAL",
    ("AWAITING_APPROVAL", "approve"): "APPROVED",
    ("AWAITING_APPROVAL", "close"): "CLOSED",
    ("AWAITING_APPROVAL", "revision"): "FULL_REVISION",
    ("FULL_REVISION", "dev_reply"): "AWAITING_APPROVAL",   # legacy, pre-1.23.6
    ("FULL_REVISION", "approve"):   "APPROVED",
    ("FULL_REVISION", "close"):     "CLOSED",
    # Inverted round-1 triage (DESIGN §1.23): on the main phase, round 1
    # goes to the developer first (DEV_TRIAGE), then the approver
    # arbitrates the dev's rejections (ARBITRATION). `review_done` stays
    # for the 3rd-party phase and round-N escalation.
    ("INLINE_REVIEW", "dev_triage_ready"): "DEV_TRIAGE",
    ("FULL_REVIEW",   "dev_triage_ready"): "DEV_TRIAGE",
    # Self-service dev loop (DESIGN 1.23.6): every round lands back in
    # DEV_TRIAGE, and the dev's triage goes straight to the fix state
    # instead of blocking on the approver. ARBITRATION is no longer
    # entered -- its rows below are retained so topics already parked
    # there when this shipped still drain.
    ("SIMPLE_REVISION", "dev_triage_ready"): "DEV_TRIAGE",
    ("FULL_REVISION",   "dev_triage_ready"): "DEV_TRIAGE",
    ("DEV_TRIAGE", "revision_simple"): "SIMPLE_REVISION",
    ("DEV_TRIAGE", "revision_full"):   "FULL_REVISION",
    ("DEV_TRIAGE", "approve"):     "APPROVED",   # approver override
    ("DEV_TRIAGE", "close"):       "CLOSED",
    # The developer hands the topic to the approver when satisfied
    # (DESIGN 1.23.7). Valid from the triage state and from either
    # revision state (hand off without one more round).
    ("DEV_TRIAGE",      "handoff"): "AWAITING_APPROVAL",
    ("SIMPLE_REVISION", "handoff"): "AWAITING_APPROVAL",
    ("FULL_REVISION",   "handoff"): "AWAITING_APPROVAL",
    # Post-handoff approver decisions. `revision_*` here means
    # "reinstate dev-rejected issues" (DESIGN 1.23.9).
    ("AWAITING_APPROVAL", "revision_simple"): "SIMPLE_REVISION",
    ("AWAITING_APPROVAL", "revision_full"):   "FULL_REVISION",
    ("AWAITING_APPROVAL", "escalate"):        "FULL_REVIEW",
    ("ARBITRATION", "approve"):         "APPROVED",  # agreed fix set empty
    ("ARBITRATION", "revision_simple"): "SIMPLE_REVISION",
    ("ARBITRATION", "revision_full"):   "FULL_REVISION",
    ("ARBITRATION", "escalate"):        "FULL_REVIEW",
    ("ARBITRATION", "close"):           "CLOSED",
    # APPROVED is transient: the topic has been enqueued on the merge
    # train and is waiting for the merged-results pipeline.
    # merge_tracker.py drives one of these transitions each cycle.
    ("APPROVED", "merge_detected"): "MERGED",
    ("APPROVED", "mr_closed"):      "CLOSED",
    ("APPROVED", "finalize"):       "CLOSED",      # manual override
    # Defensive no-op so late glab polls on already-merged topics
    # don't raise a transition error.
    ("MERGED",   "mr_closed"):      "MERGED",
}

# States where no further transitions are expected.
# APPROVED is NOT here — it means "queued on merge train, waiting",
# and merge_tracker.py resolves it to MERGED or CLOSED.
TERMINAL_STATES = {"MERGED", "CLOSED"}

# States that still need polling / work (not terminal and not locked
# to a specific human action).
OPEN_STATES = {
    "TRIAGING", "INLINE_REVIEW", "TRIAGE_DECISION",
    "DEV_TRIAGE", "ARBITRATION",
    "FULL_REVIEW", "AWAITING_APPROVAL",
    "SIMPLE_REVISION", "FULL_REVISION",
    "APPROVED",  # waiting on merge train
}


def transition(current_state, action):
    """
    Look up the next state for a (current_state, action) pair.

    Returns:
        The next state string, or None if the transition is invalid.
    """
    return TRANSITIONS.get((current_state, action))


def main():
    p = argparse.ArgumentParser(description="Review bot state machine")
    p.add_argument("--state", required=True, help="Current topic state")
    p.add_argument("--action", required=True, help="Action to apply")
    args = p.parse_args()

    next_state = transition(args.state, args.action)
    result = {"current": args.state, "action": args.action, "next": next_state}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()