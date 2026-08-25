"""Tests for 方案C: rage-standard card (render_rage_card) + command routing.

Verifies the card renders 4-tier #N findings + doc link + dual command footers
(rage closure + custom, with 改码/指引 removed), and that custom commands are
NOT intercepted by the closure (they route to the existing interact command-word
path) while closure intents (序号/ok/done/close) are.
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "rage-review"))
import feishu_notifier as fn
import closure as c


def _findings():
    return [
        {"severity": "中", "repo": "engine", "file": "chaos/a.cpp",
         "line_range": "1303-1320", "function": "activateX", "issue": "mask 不对称",
         "suggestion": "改掉"},
        {"severity": "严重", "repo": "game", "file": "x.cs", "line_range": "30",
         "issue": "越界"},
        {"severity": "建议", "repo": "engine", "file": "chaos/c.cpp", "issue": "缩进"},
    ]


def test_rage_card_4tier_and_numbering():
    t = fn.render_rage_card("CB2N-99", _findings(), doc_url="https://www.feishu.cn/docx/X",
                            triage="complex", round_no=1, mr_url="https://g/1",
                            jira_url="https://j/1")
    # 4-tier #N, severity-sorted (严重 first)
    assert "#1 [严重] [game] x.cs:30" in t
    assert "#2 [中] [engine] chaos/a.cpp:1303-1320 activateX" in t
    assert "#3 [建议] [engine] chaos/c.cpp" in t
    assert "📊 严重1 / 中1 / 轻0 / 建议1" in t


def test_rage_card_doc_link():
    t = fn.render_rage_card("K-1", _findings(), doc_url="https://www.feishu.cn/docx/DOC",
                            triage="complex")
    assert "📄 完整评审文档：https://www.feishu.cn/docx/DOC" in t


def test_rage_card_doc_link_first():
    # R7-I2: 复杂审查的 doc 链接放最前(比 MR 更突出)
    t = fn.render_rage_card("K-1", _findings(), doc_url="https://www.feishu.cn/docx/DOC",
                            mr_url="https://g/1")
    assert t.index("📄 完整评审文档") < t.index("🔗 MR"), "doc link should precede MR"


def test_rage_card_no_findings_hides_index_footer():
    # R7-I1: 无 findings 时不显示"回复问题序号"(没得编号), 只留 ok/done/close + 自定义
    t = fn.render_rage_card("K-1", [], round_no=0)
    assert "回复问题序号" not in t
    assert "ok：审查人批准" in t
    assert "关闭" in t


def test_rage_card_dual_footer_no_changma():
    t = fn.render_rage_card("K-1", _findings())
    assert "【闭环指令】" in t and "【自动修改 / 其它指令】" in t
    # rage closure verbs present (as Chinese tokens, since card is Chinese)
    for w in ("回复问题序号", "ok：", "done：", "@bot 同步"):
        assert w in t, f"missing closure token {w!r} in:\n{t}"
    # customs present
    for w in ("优化", "mr", "深入", "质疑", "重新审查", "更新结论", "关闭"):
        assert w in t, f"missing custom token {w!r} in:\n{t}"
    # 改码/指引 REMOVED
    assert "改码" not in t and "指引" not in t


def test_rage_card_no_findings_clean():
    t = fn.render_rage_card("K-1", [], round_no=0)
    assert "已完成审查，未发现问题" in t
    assert "🔍 K-1 · 审查结果" in t


def test_custom_commands_not_intercepted_by_closure():
    # custom commands → ignored → fall through to interact command-word path
    for cmd in ("优化", "mr", "深入", "质疑", "重新审查", "更新结论"):
        cls = c.classify(cmd, "dev1", "DEV_TRIAGE", ["apr1"], developer_id="dev1")
        assert cls["intent"] is None and cls["role"] == "ignored"


def test_closure_intents_still_routed():
    # rage closure intents still resolve (dev_triage / approve / handoff / dev_reply)
    r = c.reconcil("1 3", "dev1", "DEV_TRIAGE", ["apr1"], "dev1", issue_count=3)
    assert r["intent"] == "dev_triage"
    r = c.reconcil("ok", "apr1", "AWAITING_APPROVAL", ["apr1"], "dev1", 0)
    assert r["intent"] == "approve"
    r = c.reconcil("done", "dev1", "DEV_TRIAGE", ["apr1"], "dev1", 0)
    assert r["intent"] == "dev_handoff"
