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


def test_closure_consumes_number_on_real_topic():
    """Regression: _try_handle_closure MUST consume `1`/`#1` as dev_triage on a
    review topic. Previously config.load_config() (nonexistent) threw and closure
    always returned NO_MATCH -> 数字回复落到命令分发(fix_patch)."""
    import common as cm
    # no FEISHU creds in test harness -> post is skipped but closure consumes
    t = {
        "message_id": "om_t1", "jira_key": "CB2N-30597", "project": "",
        "sender_id": "ou_x", "creator_open_id": "ou_x",
        "review_state": "DEV_TRIAGE", "review_triage": "complex",
        "issue_count": 6, "render_msg_id": "card",
    }
    # _try_handle_closure returns 0 on consumed (no creds -> post skipped, still consumed)
    with tempfile.TemporaryDirectory() as tmp:
        sf = os.path.join(tmp, "s.json")
        ps.save_state({"schema_version": 1, "updated_at": "", "topics": {
            "om_t1": dict(t, phase="DONE")}}, sf)
        r = orch._try_handle_closure("om_t1", t, "1", "ou_x", tmp, sf)
        assert r == 0, f"closure should consume 1, got {r}"
        # verify state transitioned (dev_triage recorded)
        tt = ps.get_topic(sf, "om_t1")
        assert tt.get("dev_triage"), "dev_triage should be recorded after consuming 1"


def test_build_review_doc_args_pure():
    """P0-R1: _build_review_doc_args 纯函数 —— grant 从 config 反查 + 去重, md 组装。"""
    topic = {"jira_key": "CB2N-99", "creator_open_id": "ou_dev",
             "approver_open_ids": ["ou_ap", "ou_ap"]}  # 重复 approver 应去重
    findings = [{"severity": "严重", "repo": "engine", "file": "x.cs",
                 "line_range": "30", "issue": "越界", "suggestion": "加校验"}]
    title, md, grant = orch._build_review_doc_args("CB2", topic, findings, issue_key="CB2N-99")
    assert title == "代码审查 CB2N-99"
    # grant: config CB2(王浩川)+topic approver 去重+creator
    assert grant[0] == "ou_55bca7b7dae982e96749bd84f57c21e8"  # config CB2 approver
    assert "ou_ap" in grant and grant.count("ou_ap") == 1       # 去重
    assert "ou_dev" in grant
    # md 含概述/问题/严重
    assert "## 概述" in md and "越界" in md


def test_build_review_doc_args_no_jira():
    """project 空 + 无 jira_key 时 title 兜底, 不抛。"""
    title, md, grant = orch._build_review_doc_args("", {}, [], issue_key="")
    assert title == "代码审查"


def test_closure_full_loop(monkeypatch):
    """P1-M2: 完整闭环单测。mock _finalize(不发真卡), 模拟:
    DEV_TRIAGE 回 1 -> dev_triage + 状态推进 ->
    回 ok -> re_review pending -> done 交审查人 -> approver ok -> APPROVED"""
    # mock _finalize 免得真发卡(测试无 creds)
    monkeypatch.setattr(orch, "_finalize", lambda *a, **k: None)
    with tempfile.TemporaryDirectory() as tmp:
        sf = os.path.join(tmp, "s.json")
        t = {"jira_key": "CB2N-1", "project": "CB2", "sender_id": "ou_dev",
             "creator_open_id": "ou_dev", "review_state": "DEV_TRIAGE",
             "review_triage": "complex", "issue_count": 3, "render_msg_id": "card"}
        ps.save_state({"schema_version": 1, "updated_at": "", "topics": {}}, sf)
        ps.add_topic(sf, message_id="om", jira_key="CB2N-1", project="CB2", sender_id="ou_dev")
        ps.set_topic_fields(sf, "om", review_state="DEV_TRIAGE", review_triage="complex",
                            issue_count=3, creator_open_id="ou_dev")

        # 1) dev 回 1 -> dev_triage
        _t = ps.get_topic(sf, "om")
        r = orch._try_handle_closure("om", _t, "1", "ou_dev", tmp, sf)
        assert r == 0
        _t = ps.get_topic(sf, "om")
        assert _t["dev_triage"].get("accepted_indices") == [1]
        assert _t["review_state"] == "FULL_REVISION"   # complex -> FULL_REVISION

        # 2) 回 ok (dev 表示修好, push 了) -> 触发 re_review pending
        r = orch._try_handle_closure("om", _t, "ok", "ou_dev", tmp, sf)
        assert r == 0
        _t = ps.get_topic(sf, "om")
        # ok 是 dev_reply -> 置 re_review pending(round-N)
        _p = _t.get("pending") or {}
        assert (_p.get("action") if isinstance(_p, dict) else _p) == "re_review"

        # 3) done (dev 提交给审查人) -> AWAITING_APPROVAL
        ps.set_topic_fields(sf, "om", review_state="FULL_REVISION", pending=None)
        _t = ps.get_topic(sf, "om")
        r = orch._try_handle_closure("om", _t, "done", "ou_dev", tmp, sf)
        r
        _t = ps.get_topic(sf, "om")
        assert _t["review_state"] == "AWAITING_APPROVAL"

        # 4) approver ok (王浩川) -> APPROVED
        r = orch._try_handle_closure("om", ps.get_topic(sf, "om"), "ok",
                                     "ou_55bca7b7dae982e96749bd84f57c21e8", tmp, sf)
        r
        _t = ps.get_topic(sf, "om")
        assert _t["review_approved"] is True
