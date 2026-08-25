"""Tests for migrate_legacy_to_closure.py (P7 legacy→closure migration).

Verifies DONE→CLOSED, in-progress→SCANNED reset, terminal left alone, and that it
skips already-closure-style topics. Idempotent / dry-run safe.
"""
import os, tempfile
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import pipeline_state as ps
import migrate_legacy_to_closure as mig


def _mk(path):
    ps.save_state({"schema_version": 1, "updated_at": "", "scanner": {}, "topics": {}}, path)


def _topic(sf, key, phase, extra=None):
    ps.add_topic(sf, message_id=key, jira_key="R-T", project="CB2", sender_id="ou_dev")
    ps.set_topic_fields(sf, key, phase=phase, **(extra or {}))


def test_done_to_closed():
    with tempfile.TemporaryDirectory() as tmp:
        sf = os.path.join(tmp, "s.json")
        _mk(sf)
        _topic(sf, "dv_done", "DONE")
        _topic(sf, "dv_inprog", "REVIEWING")
        _topic(sf, "dv_closed", "CLOSED")
        _topic(sf, "dv_nonew", "DONE", extra={"review_state": "DEV_TRIAGE"})  # already closure
        mig.migrate(sf)
        t_done = ps.get_topic(sf, "dv_done")
        assert t_done["phase"] == "CLOSED"
        assert "迁移" in t_done.get("closed_reason", "")
        t_inp = ps.get_topic(sf, "dv_inprog")
        assert t_inp["phase"] == "SCANNED"          # reset for re-run
        assert ps.get_topic(sf, "dv_closed")["phase"] == "CLOSED"   # left alone
        assert ps.get_topic(sf, "dv_nonew")["phase"] == "DONE"      # already closure → skip


def test_migrate_skips_closure_style():
    with tempfile.TemporaryDirectory() as tmp:
        sf = os.path.join(tmp, "s.json")
        _mk(sf)
        _topic(sf, "k", "DONE", extra={"review_state": "TRIAGING"})
        mig.migrate(sf)
        assert ps.get_topic(sf, "k")["phase"] == "DONE"   # untouched


def test_migrate_failed_is_terminal():
    # FAILED is terminal (like CLOSED) → left as-is; a user re-review resets it, not migration.
    with tempfile.TemporaryDirectory() as tmp:
        sf = os.path.join(tmp, "s.json")
        _mk(sf)
        _topic(sf, "f", "FAILED")
        _m, _sk, _e = mig.migrate(sf)
        assert "f" in _sk                 # skipped (terminal)
        assert ps.get_topic(sf, "f")["phase"] == "FAILED"   # untouched
