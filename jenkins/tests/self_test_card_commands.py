"""Self-test for 方案C card commands.

Verifies, in a portable way, that every command advertised on the rage card
routes correctly:
- custom commands (优化/mr/深入/质疑/重新审查/更新结论) → NOT intercepted by closure
  (they fall through to the interactive command-word path);
- 关闭 → rage close intent (closure handles it, same action as our 关闭);
- rage closure intents (序号/ok/done/@bot 同步) → correct intent;
- the card body shows the custom commands and HIDES 改码/指引.
"""
import os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "scripts"))
sys.path.insert(0, os.path.join(_HERE, "..", "skills", "rage-review"))

import closure as c
import feishu_notifier as fn


def _route(cmd):
    return c.classify(cmd, "dev1", "DEV_TRIAGE", ["apr1"], developer_id="dev1")


def test_custom_commands_not_closure_intercepted():
    for cmd in ("优化", "mr", "深入", "质疑", "重新审查", "更新结论"):
        cls = _route(cmd)
        assert cls["intent"] is None and cls["role"] == "ignored", f"{cmd} intercepted: {cls}"


def test_close_routes_to_rage_close():
    cls = _route("关闭")
    assert cls["intent"] == "close"


def test_closure_intents():
    cases = [("1 3 5", "dev1", "DEV_TRIAGE", "dev_triage"),
             ("ok", "apr1", "AWAITING_APPROVAL", "approve"),
             ("done", "dev1", "DEV_TRIAGE", "dev_handoff"),
             ("@bot 同步", "dev1", "DEV_TRIAGE", "manual_refresh")]
    for txt, snd, state, exp in cases:
        r = c.reconcil(txt, snd, state, ["apr1"], "dev1", issue_count=3)
        assert r["intent"] == exp, f"{txt} -> {r['intent']} expected {exp}"


def test_card_shows_customs_hides_changma():
    # 方案C: 所有命令在交互卡(build_interact_card), review 结果卡不内嵌
    t = fn.build_interact_card(has_findings=True)
    for w in ("优化", "mr", "深入", "质疑", "重新审查", "更新结论", "关闭"):
        assert w in t, f"interact card missing {w}"
    assert "改码" not in t and "指引" not in t, "改码/指引 should be hidden"


def test_card_doc_link_first_and_review_has_no_footer():
    t = fn.render_rage_card("CB2N-T", [{"severity": "中", "file": "a.cpp", "issue": "x"}],
                            doc_url="https://www.feishu.cn/docx/D", mr_url="https://g/1")
    assert t.index("📄 完整评审文档") < t.index("🔗 MR"), "doc link should precede MR"
    # 方案C: review 卡不内嵌指令(交互在第二张卡)
    assert "【闭环指令】" not in t and "【自动修改 / 其它指令】" not in t
    # 优化需确认提示在交互卡
    ic = fn.build_interact_card(has_findings=True)
    assert "需回复确认" in ic, "优化 should indicate it needs confirmation"


def test_card_no_findings_hides_index():
    # 无 findings 时 review 卡只显示未发现问题；交互卡不显示"回复序号"
    rt = fn.render_rage_card("CB2N-T", [], round_no=0)
    assert "回复问题序号" not in rt and "已完成审查，未发现问题" in rt
    ic = fn.build_interact_card(has_findings=False)
    assert "回复问题序号" not in ic
    assert "ok：审查人批准" in ic and "关闭" in ic

