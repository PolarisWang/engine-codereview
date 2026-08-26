"""Tests for closure.py — rage-style review closure (self-service dev loop).

Covers: intent classification (dev_triage / approve / close / handoff /
dev_reply / ignored), state transitions, running dev_triage merge, and
per-project approver resolution. Pure logic, no I/O.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "skills", "rage-review"))
import closure as c


# ── approver resolution ────────────────────────────────────────────────────

def test_approver_ids_per_project_wins():
    cfg = {"approver_open_ids": ["ou_A", "ou_B"]}
    assert c.approver_ids_for(cfg, ["ou_admin"]) == ["ou_A", "ou_B"]


def test_approver_ids_falls_back_to_admins():
    assert c.approver_ids_for({}, ["ou_admin1", "ou_admin2"]) == ["ou_admin1", "ou_admin2"]


def test_approver_ids_empty_list():
    assert c.approver_ids_for({"approver_open_ids": []}, []) == []


# ── dev triage (round 1: dev fixes chosen, disputes the rest) ─────────────

def test_dev_triage_positive_choice():
    r = c.reconcil("1 3", "dev1", "DEV_TRIAGE", ["apr1"], "dev1", issue_count=3,
                   triage="simple")
    assert r["intent"] == "dev_triage"
    assert r["next_state"] == "SIMPLE_REVISION"
    dt = r["persist"]["dev_triage"]
    assert dt["accepted_indices"] == [1, 3]
    assert dt["rejected_indices"] == []


def test_dev_triage_complex_routes_full_revision():
    r = c.reconcil("all", "dev1", "DEV_TRIAGE", ["apr1"], "dev1", issue_count=3,
                   triage="complex")
    assert r["next_state"] == "FULL_REVISION"


def test_dev_triage_none_disputes_all():
    r = c.reconcil("none", "dev1", "DEV_TRIAGE", ["apr1"], "dev1", issue_count=2,
                   triage="simple")
    assert r["persist"]["dev_triage"]["rejected_indices"] == [1, 2]
    assert r["persist"]["dev_triage"]["accepted_indices"] == []


def test_dev_triage_exclude_form():
    # -2 means: dispute #2, fix the rest
    r = c.reconcil("-2", "dev1", "DEV_TRIAGE", ["apr1"], "dev1", issue_count=3,
                   triage="simple")
    dt = r["persist"]["dev_triage"]
    assert dt["accepted_indices"] == [1, 3]
    assert dt["rejected_indices"] == [2]


def test_dev_triage_dispute_retraction():
    # naming an index later retracts the dispute
    existing = {"accepted_indices": [1], "rejected_indices": [2, 3],
                "reinstated_indices": [], "reasons": {2: "误报"}, "triage": "simple"}
    r = c.reconcil("3", "dev1", "DEV_TRIAGE", ["apr1"], "dev1", issue_count=3,
                   triage="simple", dev_triage=existing)
    dt = r["persist"]["dev_triage"]
    assert 3 in dt["accepted_indices"]
    assert 3 not in dt["rejected_indices"]
    assert "误报" not in dt["reasons"]


# ── approver verbs ─────────────────────────────────────────────────────────

def test_approver_ok_approves():
    r = c.reconcil("ok", "apr1", "AWAITING_APPROVAL", ["apr1"], "dev1", 0)
    assert r["intent"] == "approve"
    assert r["next_state"] == "APPROVED"
    assert r["persist"]["approved"] is True


def test_approver_ok_in_revision_is_dev_reply():
    # In *_REVISION, `ok` is the developer's re-review trigger, NOT approval.
    r = c.reconcil("ok", "apr1", "SIMPLE_REVISION", ["apr1"], "dev1", 2)
    assert r["intent"] == "dev_reply"
    assert r["next_state"] is None


def test_approver_close():
    r = c.reconcil("close", "apr1", "AWAITING_APPROVAL", ["apr1"], "dev1", 0)
    assert r["intent"] == "close"
    assert r["next_state"] == "CLOSED"


def test_non_approver_ok_not_approve():
    r = c.reconcil("ok", "dev1", "AWAITING_APPROVAL", ["apr1"], "dev1", 0)
    assert r["intent"] == "dev_reply"  # filtered by approver namelist


# ── handoff (done) ─────────────────────────────────────────────────────────

def test_dev_handoff_ends_loop():
    r = c.reconcil("done", "dev1", "DEV_TRIAGE", ["apr1"], "dev1", issue_count=3)
    assert r["intent"] == "dev_handoff"
    assert r["next_state"] == "AWAITING_APPROVAL"
    assert r["post"]["template"] == "handoff_summary"


def test_bystander_done_ignored():
    r = c.reconcil("done", "stranger", "DEV_TRIAGE", ["apr1"], "dev1", issue_count=3)
    assert r["intent"] is None or r["role"] == "ignored"


# ── ignored / no-match ─────────────────────────────────────────────────────

def test_ignored_garbage():
    r = c.reconcil("随便说点什么", "dev1", "DEV_TRIAGE", ["apr1"], "dev1", issue_count=2)
    assert r["intent"] is None


def test_parse_open_ids_minimal_yaml_string_forms():
    # prod minimal YAML 把内联列表解析成字符串, 必须兼容
    assert c.parse_open_ids('["ou_a", "ou_b"]') == ["ou_a", "ou_b"]
    assert c.parse_open_ids('["ou_a"]') == ["ou_a"]
    assert c.parse_open_ids("ou_a,ou_b") == ["ou_a", "ou_b"]
    assert c.parse_open_ids(["ou_a", "ou_b"]) == ["ou_a", "ou_b"]
    assert c.parse_open_ids(['"ou_a"', "ou_b"]) == ["ou_a", "ou_b"]
    assert c.parse_open_ids([]) == []
    assert c.parse_open_ids("") == []


def test_approver_ids_for_minimal_yaml_string(monkeypatch):
    # config 里 approver_open_ids 是字符串形式时, approver_ids_for 仍解析出正确 id
    cfg = {"approver_open_ids": '["ou_aaa", "ou_bbb"]'}
    assert c.approver_ids_for(cfg, []) == ["ou_aaa", "ou_bbb"]
