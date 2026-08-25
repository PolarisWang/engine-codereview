#!/usr/bin/env python3
"""migrate_legacy_to_closure.py — migrate pre-closure topics to the rage style.

Legacy topics use `phase` (single-pass SCANNED/PARSING/REVIEWING/NOTIFYING/DONE/
FAILED). The rage closure uses `review_state` (TRIAGING/DEV_TRIAGE/...). Migration
strategy (docs/review-bot-replication-runbook.md §2):

- DONE (old single-pass success) → CLOSED (closed_reason="迁移:旧版本单程审查完成").
  The old findings stay in topic.review_summary; the topic no longer enters the
  closure loop.
- FAILED / CLOSED (already terminal) → left as-is.
- In-progress (SCANNED/PARSING/REVIEWING/NOTIFYING) → reset_for_retry so the new
  flow re-runs them as rage closure.

Idempotent; does not touch new topics that already carry `review_state`.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
import pipeline_state as ps

TERMINAL = {"CLOSED", "FAILED"}


def migrate(path, dry_run=False):
    """Return (migrated, skipped, errors)."""
    migrated = {"DONE_TO_CLOSED": [], "INPROG_RESET": []}
    skipped = []
    errors = []
    doc = ps.load_state(path)
    for key, t in (doc.get("topics") or {}).items():
        if not isinstance(t, dict) or t.get("review_state"):
            continue  # already closure-style or malformed
        phase = t.get("phase") or ""
        try:
            if phase in TERMINAL:
                skipped.append(key)
            elif phase == "DONE":
                if dry_run:
                    migrated["DONE_TO_CLOSED"].append(key)
                    continue
                ps.set_topic_fields(path, key, phase="CLOSED",
                                    closed_reason="迁移:旧版本单程审查完成", closed_by="migration")
                migrated["DONE_TO_CLOSED"].append(key)
            else:
                # SCANNED/PARSING/REVIEWING/NOTIFYING — non-terminal in-progress.
                # Reset to SCANNED so the new flow re-runs them as closure.
                if dry_run:
                    migrated["INPROG_RESET"].append(key)
                    continue
                ps.set_topic_fields(path, key, phase="SCANNED", status="RUNNING")
                migrated["INPROG_RESET"].append(key)
        except Exception as e:
            errors.append((key, str(e)))
    return migrated, skipped, errors


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--state-file", required=True)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    migrated, skipped, errors = migrate(a.state_file, dry_run=a.dry_run)
    print(json.dumps({"dry_run": a.dry_run, "migrated": migrated,
                      "skipped": skipped, "errors": errors}, ensure_ascii=False,
                      indent=2))
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
