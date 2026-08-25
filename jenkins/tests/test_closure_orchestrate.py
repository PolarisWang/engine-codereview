"""Tests for the P2 closure wiring in orchestrate (rage-style dev/approver loop).

Covers: _set_review_closure_fields (per-repo findings → review_issues + state),
_closure_human_text (Chinese copy per template), and that the closure hook is
reachable (review_state driving) without breaking the normal command path.
"""
import sys, os, tempfile, json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "scripts"))
import pipeline_state as ps
import orchestrate as orch


def _mk_state(tmp):
    p = os.path.join(tmp, "state.json")
    ps.save_state({"schema_version": 1, "updated_at": "", "scanner": {}, "topics": {}}, p)
    return p


def _mk_topic(sf, key="om_t", sender="ou_dev"):
    ps.add_topic(sf, message_id=key, jira_key="R-T", project="CB2", sender_id=sender)
    return key


def test_set_review_closure_fields_dev_triage():
    from code_reviewer import _build_review_dict
    eng = {"review": _build_review_dict(
        [{"file": "a.cpp", "severity": "严重", "line_range": "3", "issue": "泄漏"}],
        {"repo_dir": "/x", "base_branch": "m", "branch": "b"}, repo_label="engine")}
    gam = {"review": {"findings": []}}
    with tempfile.TemporaryDirectory() as tmp:
        sf = _mk_state(tmp)
        _mk_topic(sf)
        orch._set_review_closure_fields(sf, "om_t", eng, gam)
        t = ps.get_topic(sf, "om_t")
        assert t["issue_count"] == 1
        assert t["review_issues"][0]["severity"] == "严重"
        assert t["review_state"] == "DEV_TRIAGE"          # issues found → dev triages
        assert t["review_triage"] == "simple"             # 1 file / few lines → simple
        assert t["creator_open_id"] == "ou_dev"


def test_set_review_closure_fields_zero_issues_approver():
    eng = {"review": {"findings": [], "review_text": "🔍 Code Review — 0 项"}, "changed_files": [], "stats": ""}
    gam = {"review": {"findings": []}, "changed_files": [], "stats": ""}
    with tempfile.TemporaryDirectory() as tmp:
        sf = _mk_state(tmp)
        _mk_topic(sf)
        orch._set_review_closure_fields(sf, "om_t", eng, gam)
        t = ps.get_topic(sf, "om_t")
        assert t["issue_count"] == 0
        assert t["review_state"] == "TRIAGE_DECISION"     # zero issues → approver decides


def test_closure_human_text_templates():
    assert "交审查人" in orch._closure_human_text(
        {"template": "revision_request", "vars": {"accepted": [1], "rejected": [2]}},
        {}, 2, {})
    assert "已批准" in orch._closure_human_text({"template": "approval", "vars": {}}, {}, 0, {})
    assert "已关闭" in orch._closure_human_text({"template": "closed", "vars": {}}, {}, 0, {})
    assert "下一轮增量审查" in orch._closure_human_text(
        {"template": "re_review", "vars": {}}, {}, 0, {})
    assert "裁决" in orch._closure_human_text(
        {"template": "handoff_summary", "vars": {"dev_triage": {}}}, {}, 0, {})
