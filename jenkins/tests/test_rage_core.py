"""Tests for the vendored-from-rage review quality core.

These cover the *pure* logic that was copied verbatim from upstream
rage/review-bot (`vendor/rage-review-bot/`) and is platform-agnostic:
the state machine (self-service dev loop), the settled-issue ledger, and
the index/triage reply-parse grammar. They guard the port: any future
platform-adapter edit must not change this behavior.
"""
import sys, os
_HERE = os.path.dirname(os.path.abspath(__file__))
# rage-review core (vendored / ported) + jenkins/scripts (code_reviewer)
sys.path.insert(0, os.path.join(_HERE, "..", "skills", "rage-review"))
sys.path.insert(0, os.path.join(_HERE, "..", "scripts"))

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


# ── rage-standard renderer (severity 严重/中/轻/建议 + [Repo] file:line) ───

def test_rage_render_severity_sort_and_location():
    import code_reviewer as cr
    findings = [
        {"file": "a.cpp", "severity": "轻", "issue": "const 缺失"},
        {"file": "a.cpp", "severity": "严重", "line_range": "120-145",
         "issue": "路径校验缺失", "suggestion": "加校验"},
    ]
    d = cr._build_review_dict(findings, {"repo_dir": "/x", "base_branch": "m",
                                         "branch": "b"}, repo_label="chaos")
    text = d["review_text"]
    # 严重 first (sorted), #N assigned after sort
    assert "#1 [严重]" in text and "#2 [轻]" in text
    # [Repo] file:line_range prefix
    assert "[chaos] a.cpp:120-145" in text
    # severity count line uses the 4-tier rack
    assert "📊 严重1 / 中0 / 轻1 / 建议0" in text
    # downstream severity_counts kept in 3-tier for legacy cache/render compat
    assert d["severity_counts"] == {"critical": 0, "warning": 0, "suggestion": 2}


def test_severity_zh_mapping():
    import code_reviewer as cr
    assert cr._severity_zh("严重") == "严重"
    assert cr._severity_zh("critical") == "严重"
    assert cr._severity_zh("中") == "中"
    assert cr._severity_zh("轻") == "轻"
    assert cr._severity_zh("建议") == "建议"
    assert cr._severity_zh("unknown-garbage") == "建议"


# ── agent subprocess parse: claude -p envelope + fenced JSON + pygments-less ─

def test_agent_envelope_unwrap_and_parse():
    import code_reviewer as cr
    under = '{"findings":[{"file":"asset.cpp","severity":"严重","line_range":"3","issue":"内存泄漏"}]}'
    env = f'{{"type":"result","subtype":"success","result":"{under} · 已经修复"}}'
    inner = cr._unwrap_claude_json(env)
    assert "findings" in inner
    # fenced form (the agent wrapped JSON in ```json ... ```)
    fenced = '{"type":"result","result":"```json\\n{\\"findings\\":[{\\"file\\":\\"a.cpp\\",\\"severity\\":\\"轻\\"}]}\\n```"}'
    f = cr._parse_agent_json(cr._unwrap_claude_json(fenced))
    assert f is not None and f[0]["file"] == "a.cpp"


def test_lex_identifiers_pygments_absent_fallback():
    import code_reviewer as cr
    # regex fallback must still extract identifier-like tokens without pygments
    toks = cr._lex_identifiers("call updateFoo(n) and load asset_file_1")
    assert "updateFoo" in toks
    assert "asset_file_1" in toks


# ── 修法1/2/3: # 前缀、闭路序号优先、approver 反查 ─────────────────────────

def test_hash_prefix_indices_parse():
    """M1: #N 前缀被解析为闭路序号, 与纯数字等价"""
    for txt in ("#1", "#1 3", "#1 #3", "#-2"):
        r = rp.parse_indices_with_mode(txt, allow_none=True, allow_trailing_text=True)
        assert r is not None, f"{txt} should parse"
    assert rp.parse_indices_with_mode("#1")["indices"] == [1]
    assert rp.parse_indices_with_mode("#1 3")["indices"] == [1, 3]


def test_closure_state_bare_number_not_command(monkeypatch=None):
    """修法1: 闭路状态下裸数字不做命令(不触发 fix_patch), 非闭路可做命令"""
    import orchestrate as orch
    # 闭路状态: 裸数字是序号 -> dispatch 返回 None(不落命令)
    topic = {"review_state": "DEV_TRIAGE"}
    ctx = {"word": "1", "topic": topic, "all_findings": [], "findings_status": "ok"}
    assert orch._is_closure_state(topic) is True
    assert orch._looks_like_index_word("1") is True
    assert orch._dispatch_command(ctx) is None  # 不触发 fix_patch
    # 非闭路: 裸数字可作命令
    topic2 = {"review_state": ""}
    entry = orch._HANDLER_REGISTRY.get("1")
    assert entry is not None  # 1 仍是注册命令


def test_jira_key_project_reverse_lookup():
    """修法3: CB2N→CB2 反查, 拿到 CB2 的 approver"""
    import common as cm
    import jira_parser as jp
    pid, pcfg = jp.identify_project("CB2N-30597", cm.load_config())
    assert pid == "CB2"
    assert "ou_55bca7b7dae982e96749bd84f57c21e8" in (pcfg or {}).get("approver_open_ids", [])
