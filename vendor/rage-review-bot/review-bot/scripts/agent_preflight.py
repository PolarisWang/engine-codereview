"""Re-verify a topic's review_phase / state right before an agent acts.

Called at the top of the per-topic agent prompt and the merge-queue agent.
Guards against phase/state drift between dispatch_plan generation and the
moment an agent actually starts doing work — e.g. the dispatcher wrote
review_phase=main at T0, but a user reply flipped it to 3rd_party at T+3s
before the agent picked it up.

Usage (CLI, from an agent prompt):
    python agent_preflight.py --topic FILE --expected-phase PHASE \
        [--expected-state STATE]

Returns JSON on stdout:
    {"status": "ok",          "phase": "main", "state": "APPROVED"}
    {"status": "phase_drift", "expected_phase": "main", "actual_phase": "3rd_party", ...}
    {"status": "state_drift", "expected_state": "APPROVED", "actual_state": "FULL_REVISION", ...}
    {"status": "missing",     "reason": "..."}

Exit code 0 on status=ok, 1 on any drift or missing — agents branch on this.
"""

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import topic_store


def _norm_phase(phase):
    """Normalize phase value to canonical form.

    Treat None / "null" / "None" / "" as missing — and missing means
    "main" (the dispatcher convention; see dispatcher.py:520,647,687
    where `... or "main"` is applied to every read of review_phase).
    Without this, legacy single-MR topics that never wrote review_phase
    fail preflight with `expected_phase=main actual_phase=None`, and
    agent prompts that pass `--expected-phase null` literal-string
    false-positive a phase_drift.
    """
    if phase in (None, "null", "None", ""):
        return "main"
    return phase


def _resolve_superseded_by(topic_path):
    """If the topic file is missing from `topics/` but present under
    `topics/closed/` AND its closed_reason matches the supersede pattern,
    return the canonical successor thread_id. Otherwise None.

    Used by `check_phase` so a drifted agent can route donation work
    to the correct canonical topic instead of just reporting `missing`.
    """
    topic_path = Path(topic_path)
    closed_path = topic_path.parent / "closed" / topic_path.name
    if not closed_path.exists():
        return None
    try:
        closed_topic = topic_store.read(closed_path)
    except (OSError, ValueError):
        return None
    return topic_store.parse_superseded_by(closed_topic)


def check_phase(topic_path, expected_phase=None, expected_state=None):
    """Compare current topic phase/state against expected values.

    Args:
        topic_path: path to the topic JSON file.
        expected_phase: string ("main" | "3rd_party") or None to skip.
        expected_state: string (e.g. "APPROVED") or None to skip.

    Returns a dict with `status` set to "ok", "phase_drift", "state_drift",
    or "missing". For drifts, includes expected_* + actual_* fields so the
    caller can log the mismatch without re-reading the topic.

    On `missing`, also surfaces `superseded_by` (canonical thread_id) when
    the closed topic's lifecycle.closed_reason matches the supersede
    pattern — so the agent can attempt work donation instead of just
    aborting.
    """
    topic_path = Path(topic_path)
    topic = topic_store.read_or_none(topic_path)
    if topic is None:
        result = {"status": "missing",
                  "reason": f"topic file not found: {topic_path}"}
        canonical = _resolve_superseded_by(topic_path)
        if canonical:
            result["superseded_by"] = canonical
        return result

    review = topic.get("review") or {}
    actual_phase = review.get("review_phase")
    actual_state = review.get("state")

    if expected_phase is not None and _norm_phase(actual_phase) != _norm_phase(expected_phase):
        return {
            "status": "phase_drift",
            "expected_phase": expected_phase,
            "actual_phase": actual_phase,
            "phase": actual_phase,
            "state": actual_state,
        }

    if expected_state is not None and actual_state != expected_state:
        return {
            "status": "state_drift",
            "expected_state": expected_state,
            "actual_state": actual_state,
            "phase": actual_phase,
            "state": actual_state,
        }

    return {"status": "ok", "phase": actual_phase, "state": actual_state}


def _cli():
    ap = argparse.ArgumentParser(description="Topic phase/state drift check")
    ap.add_argument("--topic", required=True,
                    help="Path to the topic JSON file")
    ap.add_argument("--expected-phase", default=None,
                    help='Expected review_phase ("main" | "3rd_party")')
    ap.add_argument("--expected-state", default=None,
                    help="Expected review state (e.g. APPROVED)")
    args = ap.parse_args()

    result = check_phase(args.topic, args.expected_phase, args.expected_state)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(_cli())
