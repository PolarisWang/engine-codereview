"""Tests for 方案C: rage-standard review card (render_rage_card) + interact card.

Verifies:
- render_rage_card normalizes English severity (critical/warning/suggestion) to
  严重/中/轻/建议, gives each an icon (🔴🟠🟡🟢), severity-sorts, and does NOT
  embed command footers (方案C: review card = results only).
- build_interact_card carries ALL interactive commands (rage closure + our
  auto-edit/MR), with 改码/指引 removed.
- custom commands are NOT intercepted by closure; closure intents still route.
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "rage-review"))
import feishu_notifier as fn
import closure as c


def _findings_zh():
    return [
        {"severity": "中", "repo": "engine", "file": "chaos/a.cpp",
         "line_range": "1303-1320", "function": "activateX", "issue": "mask 不对称",
         "suggestion": "改掉"},
        {"severity": "严重", "repo": "game", "file": "x.cs", "line_range": "30",
         "issue": "越界"},
        {"severity": "建议", "repo": "engine", "file": "chaos/c.cpp", "issue": "缩进"},
    ]


def _findings_en():
    # English severity (HTTP-path findings) — the key bug: must normalize to 中文
    return [
        {"severity": "critical", "repo": "game", "file": "x.cs", "issue": "越界"},
        {"severity": "warning", "repo": "engine", "file": "chaos/a.cpp", "issue": "mask"},
        {"severity": "suggestion", "repo": "engine", "file": "chaos/c.cpp", "issue": "缩进"},
        {"severity": "warning", "repo": "engine", "file": "chaos/d.cpp", "issue": "race"},
    ]


# ── review card: 4-tier + icon + sort + no footer ──────────────────────────

def test_rage_card_4tier_numbering_and_icon():
    t = fn.render_rage_card("CB2N-99", _findings_zh(), doc_url="https://www.feishu.cn/docx/X",
                            triage="complex", round_no=1, mr_url="https://g/1",
                            jira_url="https://j/1")
    # 4-tier #N, severity-sorted (严重 first), with icons
    assert "#1 [🔴 严重] [game] x.cs:30" in t
    assert "#2 [🟠 中] [engine] chaos/a.cpp:1303-1320 activateX" in t
    assert "#3 [🟢 建议] [engine] chaos/c.cpp" in t
    assert "📊 严重1 / 中1 / 轻0 / 建议1" in t


def test_rage_card_normalizes_english_severity():
    # The real bug: English findings showed 严重0/中0/轻0/建议0. Normalize → correct.
    t = fn.render_rage_card("CB2N-100", _findings_en())
    assert "📊 严重1 / 中2 / 轻1 / 建议0" in t, "English severity must normalize to 中文 counts"
    assert "#1 [🔴 严重] [game] x.cs" in t      # critical first
    assert "#2 [🟠 中]" in t                        # warning
    assert "#4 [🟡 轻]" in t                        # suggestion -> 轻


def test_rage_card_doc_link():
    t = fn.render_rage_card("K-1", _findings_zh(), doc_url="https://www.feishu.cn/docx/DOC",
                            triage="complex")
    assert "📄 完整评审文档：https://www.feishu.cn/docx/DOC" in t


def test_rage_card_doc_link_first():
    t = fn.render_rage_card("K-1", _findings_zh(), doc_url="https://www.feishu.cn/docx/DOC",
                            mr_url="https://g/1")
    assert t.index("📄 完整评审文档") < t.index("🔗 MR")


def test_rage_card_has_NO_footer():
    # 方案C: review 卡不内嵌指令 footer(已移到交互卡), 避免语义重复
    t = fn.render_rage_card("K-1", _findings_zh())
    assert "【闭环指令】" not in t and "【自动修改 / 其它指令】" not in t
    assert "回复问题序号" not in t


def test_rage_card_no_findings_clean():
    t = fn.render_rage_card("K-1", [], round_no=0)
    assert "已完成审查，未发现问题" in t
    assert "🔍 K-1 · 审查结果" in t
    assert "📊" not in t


# ── interact card: all commands, no 改码/指引 ──────────────────────────────

def test_interact_card_has_all_commands_no_changma():
    t = fn.build_interact_card(has_findings=True)
    assert "【闭环指令】" in t and "【自动修改 / 其它指令】" in t
    for w in ("回复问题序号", "ok：", "done：", "@bot 同步",
              "优化", "mr", "深入", "质疑", "重新审查", "更新结论", "关闭"):
        assert w in t, f"interact card missing {w!r}"
    assert "改码" not in t and "指引" not in t


def test_interact_card_no_findings_hides_index():
    t = fn.build_interact_card(has_findings=False)
    assert "回复问题序号" not in t
    assert "ok：审查人批准" in t
    assert "关闭" in t


def test_interact_card_closure_style_hides_number_commands():
    # 闭路卡: 不展示 2/4 纯数字命令(闭路下 2/4 是序号不是命令), 避免歧义
    t = fn.build_interact_card(has_findings=True, closure_style=True)
    assert "对应 `2`" not in t and "回复 `4`" not in t
    assert "重新审查" in t and "关闭" in t       # 文本命令仍有
    # 非闭路版仍保留数字命令提示
    t2 = fn.build_interact_card(has_findings=True, closure_style=False)
    assert "对应 `2`" in t2 and "回复 `4`" in t2


# ── closure routing unchanged ──────────────────────────────────────────────

def test_custom_commands_not_intercepted_by_closure():
    for cmd in ("优化", "mr", "深入", "质疑", "重新审查", "更新结论"):
        cls = c.classify(cmd, "dev1", "DEV_TRIAGE", ["apr1"], developer_id="dev1")
        assert cls["intent"] is None and cls["role"] == "ignored"


def test_closure_intents_still_routed():
    r = c.reconcil("1 3", "dev1", "DEV_TRIAGE", ["apr1"], "dev1", issue_count=3)
    assert r["intent"] == "dev_triage"
    r = c.reconcil("ok", "apr1", "AWAITING_APPROVAL", ["apr1"], "dev1", 0)
    assert r["intent"] == "approve"
    r = c.reconcil("done", "dev1", "DEV_TRIAGE", ["apr1"], "dev1", 0)
    assert r["intent"] == "dev_handoff"


def test_simple_no_doc_shows_notice():
    """方案A: simple 审查无 doc 时不显示链接, 但加句轻提示(不是漏了)."""
    t = fn.render_rage_card("K-1", [{"severity": "中", "file": "a.cpp", "issue": "x"}],
                            triage="simple")
    assert "📄 完整评审文档" not in t
    assert "变更较小" in t or "未生成完整文档" in t
    # complex 有 doc 才显示链接
    c = fn.render_rage_card("K-1", [{"severity": "中", "file": "a.cpp", "issue": "x"}],
                            triage="complex", doc_url="https://www.feishu.cn/docx/D")
    assert "📄 完整评审文档" in c
