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
    t = fn.render_rage_card("CB2N-T", [{"severity": "中", "file": "a.cpp", "issue": "x"}])
    for w in ("优化", "mr", "深入", "质疑", "重新审查", "更新结论", "关闭"):
        assert w in t, f"card missing {w}"
    assert "改码" not in t and "指引" not in t, "改码/指引 should be hidden"
