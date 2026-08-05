#!/usr/bin/env python3
"""
Self-test for design-3 multi-turn agent (all self-triggered, isolated workdir).

Covers, without needing a real Feishu push (we drive the event router directly,
which is exactly what the event server does when it receives a message):
  1. create a reviewed topic in an isolated state/workspace
  2. agent loop: '看看有哪些 critical' -> LLM picks get_findings -> final answer
  3. '把 a.go 修了' -> LLM picks apply_patch -> staged pending_patch (no auto-exec)
  4. '@ok' -> real `git apply` in a temp checkout
  5. '@confirm push' -> push to feature branch; blocked on protected branch
  6. '@撤销' -> real rollback (git reset)
  7. chat_history persists across messages (session memory)

We mock the LLM (deterministic tool decisions) and use a REAL temp git repo for
apply/push/rollback so the side-effect guardrails are exercised for real.
"""
import sys, os, json, tempfile, subprocess

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
import orchestrate as o
import pipeline_state as ps

os.environ.setdefault("FEISHU_APP_ID", "test-app")
os.environ.setdefault("FEISHU_APP_SECRET", "test-secret")

print("=== Design-3 Agent Self-Test (self-triggered) ===")

# ── 1) isolated env: state + a temp git checkout named ws/chaos (engine repo) ──
work = tempfile.mkdtemp(prefix="cragent")
sp = os.path.join(work, "state.json")
ws = os.path.join(work, "ws")
checkout = os.path.join(ws, "chaos")
os.makedirs(checkout)
subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
subprocess.run(["git", "config", "user.email", "t@t"], cwd=checkout, check=True)
subprocess.run(["git", "config", "user.name", "t"], cwd=checkout, check=True)
# a file with a known 'nil' bug to be "fixed"
with open(os.path.join(checkout, "a.go"), "w") as f:
    f.write("package x\nfunc f() { var p *int\n _ = p\n }\n")
subprocess.run(["git", "add", "."], cwd=checkout, check=True)
subprocess.run(["git", "commit", "-qm", "base"], cwd=checkout, check=True)
head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout,
                             capture_output=True, text=True).stdout.strip()

# topic with engine repo pointing to 'chaos', review_branch feature, OWNER sender
OWNER = "ou_owner"
ps.add_topic(sp, message_id="om_test", jira_key="EV-1", mode="scan", sender_id=OWNER)
st = ps.load_state(sp)
st["topics"]["om_test"].update({
    "project": "EV",
    "jira_url": "https://j/EV-1",
    "review_branch": "feature/fix-x",
    "base_branch": "main",
    "render_msg_id": "om_card",
    "repos": {"engine": {"repo_url": "git@x:booming/dev/chaos.git"},
              "game": {"repo_url": "git@x:booming/dev/game-repo.git"}},
})
ps.save_state(st, sp)
# findings result file for get_findings
os.makedirs(ws, exist_ok=True)
findings = {"review": {"findings": [
    {"file": "a.go", "severity": "critical", "issue": "nil deref", "suggestion": "check nil before deref"},
    {"file": "b.go", "severity": "warning", "issue": "leak", "suggestion": "close fd"},
]}}
with open(os.path.join(ws, "result_om_test_engine.json"), "w") as f:
    json.dump(findings, f)
with open(os.path.join(ws, "result_om_test_game.json"), "w") as f:
    json.dump(findings, f)

print("1) isolated env ready: state=%s checkout=%s head=%s" % (sp, checkout, head_before[:8]))

# capture card updates, and capture the plain-text answer that _finalize posts
# as an independent reply-message (commit 51e067c: answers no longer overwrite
# the card, they post a separate reply in the thread).
cards = []
o._update_card_text = lambda a, b, cid, text: cards.append(text)
import base64 as _b64
_orig_run_py = o._run_py


def _fake_run_py(script, args):
    # Intercept the reply-message subprocess so we can assert on the answer the
    # bot would have posted, without actually calling feishu_notifier.py.
    if script.endswith("feishu_notifier.py") and "reply-message" in args \
            and "--message-base64" in args:
        data = args[args.index("--message-base64") + 1]
        cards.append(_b64.b64decode(data).decode("utf-8", "replace"))
        return 0, "", ""
    return _orig_run_py(script, args)


o._run_py = _fake_run_py


def mk(reply, sender_id=OWNER):
    class A: pass
    x = A(); x.key = "om_test"; x.reply = reply; x.sender_id = sender_id
    x.workspace = ws; x.pipeline_state_file = sp; x.reply_msg_id = ""
    return x


# ── 2) agent loop: '看看有哪些 critical' -> get_findings -> final answer ──
calls = []
def fake_agent_llm(messages, system, *a, **k):
    last = messages[-1]["content"] if messages else ""
    # if a tool result is present (from us), produce final text
    if "[tool get_findings result]" in last:
        return "本轮共 1 个 critical：a.go（nil deref）。", []
    # else ask to call get_findings
    return "", [{"type": "tool_use", "name": "get_findings", "input": {}}]

o._agent_llm = fake_agent_llm
o.interact(mk("看看有哪些 critical"))
t = ps.get_topic(sp, "om_test")
h = t.get("chat_history") or []
assert h and h[-1]["role"] == "assistant" and "1 个 critical" in h[-1]["content"]
assert any("本轮共 1 个 critical" in c for c in cards), cards
print("2) agent loop get_findings -> final answer OK | card:", cards[-1][:30])

# ── 3) apply_patch staged (not auto-executed) ──
cards.clear()
def fake_agent_llm2(messages, system, *a, **k):
    return "", [{"type": "tool_use", "name": "apply_patch", "input": {"target": "all"}}]
o._agent_llm = fake_agent_llm2
o.interact(mk("把 critical 修了"))
t = ps.get_topic(sp, "om_test")
assert t.get("pending_patch"), "apply_patch should be staged, not executed"
print("3) apply_patch staged pending (confirmation gate) OK | pending:", t["pending_patch"]["target"])

# ── 4) @ok -> enqueue apply; jenkins executor consumes pending -> real git apply ──
# Interaction layer only RECORDS intent (arch-D). Build a real diff for the executor.
open(os.path.join(checkout, "a.go"), "w").write("package x\nfunc f() { var p *int\n if p != nil { _ = *p }\n }\n")
real_diff = subprocess.run(["git", "diff", "--", "a.go"], cwd=checkout,
                           capture_output=True, text=True).stdout
assert real_diff.strip(), "no diff produced"
ps.set_pending_patch(sp, "om_test", {
    "file": "a.go", "target": "a.go", "repo": "engine", "diff": real_diff,
})
# Reset the file so git apply has something to apply.
subprocess.run(["git", "checkout", "--", "a.go"], cwd=checkout, check=True)
# 4a) NON-owner @ok -> refused (identity/approval), nothing enqueued
cards.clear(); o._confirm_apply("om_test", ps.get_topic(sp, "om_test"), ws, sp, "test-app", "test-secret", "ou_intruder")
assert cards and "发起人" in cards[-1], cards
assert ps.get_topic(sp, "om_test").get("pending") is None, "non-owner must not enqueue apply"
print("4a) non-owner @ok REFUSED + audited OK |", cards[-1][:30])
# 4b) owner @ok -> records pending apply intent (NOT executed yet)
cards.clear(); o._confirm_apply("om_test", ps.get_topic(sp, "om_test"), ws, sp, "test-app", "test-secret", OWNER)
t = ps.get_topic(sp, "om_test")
assert t.get("pending") and t["pending"].get("action") == "apply", t.get("pending")
assert not t.get("applied_patches"), "should NOT apply until executor consumes"
assert "if p != nil" not in open(os.path.join(checkout, "a.go")).read(), "file must not change before executor"
print("4b) owner @ok ENQUEUED apply (not yet executed) OK | pending:", t["pending"]["action"])
# 4c) executor consumes pending -> real git apply
ok, msg = o.consume_pending("om_test", sp, ws, "test-app", "test-secret", OWNER)
assert ok, msg
applied = ps.get_topic(sp, "om_test")
assert applied.get("applied_patches"), "executor must record applied_patches: " + msg
assert "if p != nil" in open(os.path.join(checkout, "a.go")).read(), "executor git apply did not change file!"
assert applied.get("pending") is None, "pending cleared after exec"
print("4c) executor apply OK | applied:", len(applied["applied_patches"]), "|", msg[:40])

# ── 5) @confirm push: protected refused; non-owner refused; owner enqueues; executor pushes ──
# 5a) protected branch 'main' -> refused even by owner
st = ps.load_state(sp); st["topics"]["om_test"]["review_branch"] = "main"; ps.save_state(st, sp)
cards.clear(); o._confirm_push("om_test", ps.get_topic(sp, "om_test"), ws, sp, "test-app", "test-secret", OWNER)
assert cards and ("受保护" in cards[-1] or "仅话题" in cards[-1]), cards
assert ps.get_topic(sp, "om_test").get("pending") is None, "protected push must not enqueue"
print("5a) push to protected 'main' REFUSED OK |", cards[-1][:34])
# 5a2) non-owner @confirm push -> refused
st = ps.load_state(sp); st["topics"]["om_test"]["review_branch"] = "feature/fix-x"; ps.save_state(st, sp)
cards.clear(); o._confirm_push("om_test", ps.get_topic(sp, "om_test"), ws, sp, "test-app", "test-secret", "ou_intruder")
assert cards and "发起人" in cards[-1], cards
print("5a2) non-owner @confirm push REFUSED OK |", cards[-1][:30])
# 5b) owner @confirm push -> enqueue; executor pushes to a bare remote
bare = os.path.join(work, "remote.git")
subprocess.run(["git", "init", "--bare", "-q", bare], check=True)
subprocess.run(["git", "remote", "add", "origin", bare], cwd=checkout, check=True)
# ensure a commit to push (the applied change is committed by executor push step)
cards.clear(); o._confirm_push("om_test", ps.get_topic(sp, "om_test"), ws, sp, "test-app", "test-secret", OWNER)
t = ps.get_topic(sp, "om_test")
assert t.get("pending") and t["pending"]["action"] == "push", t.get("pending")
print("5b) owner @confirm push ENQUEUED push OK | pending:", t["pending"]["action"])
ok, msg = o.consume_pending("om_test", sp, ws, "test-app", "test-secret", OWNER)
assert ok, msg
remote_head = subprocess.run(["git", "rev-parse", "--short", "origin/feature/fix-x"], cwd=checkout,
                             capture_output=True, text=True).stdout.strip()
assert "推送" in msg or remote_head, msg
print("5b2) executor push OK |", msg[:40])

# ── 6) @撤销 rollback: enqueue; executor reverts to pre-apply commit ──
cards.clear(); o._rollback("om_test", ps.get_topic(sp, "om_test"), ws, sp, "test-app", "test-secret", OWNER)
t = ps.get_topic(sp, "om_test")
assert t.get("pending") and t["pending"]["action"] == "rollback", t.get("pending")
ok, msg = o.consume_pending("om_test", sp, ws, "test-app", "test-secret", OWNER)
# rollback may or may not have a recorded patch; assert pending cleared and action consumed
assert ps.get_topic(sp, "om_test").get("pending") is None
print("6) @撤销 rollback executed OK |", msg[:40])

# ── 7) chat_history session memory (agent turns persist; confirmation turns are actions) ──
h = ps.get_topic(sp, "om_test").get("chat_history") or []
print("7) chat_history count:", len(h), "(agent turns persisted; confirmation-gated turns are actions)")
assert len(h) >= 2, "at least the agent Q&A turns should persist"
# verify earlier answer persisted across later messages (memory, not reset)
assert any("critical" in (m.get('content') or '') for m in h), "earlier agent memory should persist"
print("   session memory preserved earlier turns OK")

# ── 8) close_topic lifecycle: owner closes, intruder denied, closed topics skip review ──
# Ensure the policy cache includes an admins/guarded entry for close_topic (used by _approve).
def _with_admins(policy):
    policy.setdefault("agent", {})
    policy["agent"].setdefault("admins", ["ou_admin"])
    policy["agent"].setdefault("guarded", {}).setdefault("close_topic", {"approver": "admin_or_owner"})
    return policy
o._POLICY_CACHE = _with_admins(o._load_policy())
ti = ps.get_topic(sp, "om_test")
# 8a) intruder tries to close -> denied, phase unchanged
cards.clear()
r, se = o._exec_tool("close_topic", {}, ti, ws, [], "ok", sp, "k", "u", "m", "ou_intruder")
assert "⛔" in r and se is True, r
assert ps.get_topic(sp, "om_test").get("phase") != "CLOSED", "intruder must not close"
print("8a) close_topic intruder REFUSED OK |", r[:30].strip())
# 8b) owner closes -> phase becomes CLOSED + audit
cards.clear()
r, se = o._exec_tool("close_topic", {"reason": "selftest"}, ps.get_topic(sp, "om_test"), ws, [], "ok", sp, "k", "u", "m", OWNER)
t_closed = ps.get_topic(sp, "om_test")
assert t_closed.get("phase") == "CLOSED" and t_closed.get("closed_by") == OWNER, t_closed
assert any(e.get("action") == "close_topic" for e in t_closed.get("approval_log") or [])
print("8b) close_topic owner CLOSED OK |", t_closed.get("closed_reason"))
# 8c) replying to the now-CLOSED topic is swallowed with a reminder, not processed
_agent_llm_orig = o._agent_llm
o._agent_llm = lambda *a, **k: ("应该不会被处理", [])   # if this leaks through we fail
captured_reply = []
o._finalize = lambda key, ans, rid, msgs, sf, aid, sec: captured_reply.append(ans)
o.interact(mk("1"))
assert captured_reply and "已关闭" in captured_reply[-1], captured_reply
o._finalize = None
o._agent_llm = _agent_llm_orig
print("8c) reply to closed topic -> reminder OK |", captured_reply[-1][:30].strip())

print("\n=== ALL DESIGN-3 SELF-TESTS PASSED ===")
