"""Regression tests for R1: confirm-agent-edit refuses when the checkout drifted.

After 改码 (agent_edit) stages the diffs, the user later replies @确认. Both
confirm paths re-ensure the (shared/reused) checkout, which does a
fetch+reset --hard origin/{src}, so if another tick re-used it meanwhile the
tree is no longer on the base the edit was built on. R1 records the edit-time
base SHA (checkout_sha) and refuses to commit/push unless HEAD still matches —
so we never replay stale diffs onto a drifted base.

We test the decision point via _cmd_confirm_agent_edit (interact path), mocking
subprocess git calls and the Feishu/state side effects. The core assertion:
with a mismatched HEAD, it returns BEFORE commit/push (no 'push' git call).
"""
import pytest
from orchestrate import _cmd_confirm_agent_edit

# --- mock scaffolding ---

class _R:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def _make_topic(**over):
    t = {
        "render_msg_id": "card1",
        "review_branch": "feature/EV-1",
        "jira_key": "EV-1",
        "base_branch": "main",
        "mr_url": "https://gitlab.booming-inc.com/g/p/-/merge_requests/7",
        "fix_mr_iids": [],
        "pending_patch": {
            "state": "staged_agent_edit",
            "branch": "feature/EV-1-fix-EV-1",
            "files": [{"file": "a.py", "diff": "--- a/a.py\n+++ b/a.py\n"}],
            "checkout_sha": "AAAA1111",
        },
    }
    t.update(over)
    return t


def _mock_confirm(monkeypatch, topic, head_sha):
    """Wire _cmd_confirm_agent_edit's dependencies so it runs to the R1 decision
    and no further. `head_sha` controls what `git rev-parse HEAD` returns.
    Returns a dict of observed git commands. `_sp` is a local `import subprocess
    as _sp` inside the function, so we patch the real subprocess.run."""
    import subprocess
    calls = []

    def fake_sp_run(cmd, **kw):
        calls.append(cmd)
        c = " ".join(cmd)
        if "rev-parse" in c and "HEAD" in c:
            return _R(stdout=head_sha)
        if "checkout" in c and "-B" in c:
            return _R()
        if "config" in c:
            return _R()
        if "add" in c and "-A" in c:
            return _R()
        if "commit" in c:
            return _R(stdout="nothing to commit")  # force replay branch if reached
        if "apply" in c:
            return _R()
        if "push" in c:
            return _R(stderr="push-not-expected")
        return _R()

    monkeypatch.setattr("orchestrate._ensure_checkout", lambda topic, ws: ("/fake/co", None))
    monkeypatch.setattr(subprocess, "run", fake_sp_run)
    monkeypatch.setattr("orchestrate._approve", lambda *a, **k: (True, "approved"))
    monkeypatch.setattr("orchestrate._update_card_text", lambda *a, **k: None)
    monkeypatch.setattr("orchestrate.pipeline_state.set_pending_patch", lambda *a, **k: None)
    # Block _create_or_get_mr so it never actually runs if we somehow reach push+MR.
    monkeypatch.setattr("orchestrate._create_or_get_mr",
                        lambda *a, **k: (None, "", "", "blocked"))
    return calls


def test_confirm_refuses_when_head_drifted(monkeypatch):
    topic = _make_topic()  # checkout_sha AAAA1111
    calls = _mock_confirm(monkeypatch, topic, head_sha="BBBB2222")  # drifted
    rc = _cmd_confirm_agent_edit("om_1", topic, [], "/state", "/ws", "app", "secret",
                                 actor="u1")
    assert rc == 0
    # Must NOT have reached git push (refused at the R1 guard).
    assert not any("push" in c for c in calls)


def test_confirm_refuses_when_baseline_missing(monkeypatch):
    topic = _make_topic()
    topic["pending_patch"]["checkout_sha"] = ""  # legacy, no baseline
    calls = _mock_confirm(monkeypatch, topic, head_sha="AAAA1111")
    _cmd_confirm_agent_edit("om_1", topic, [], "/state", "/ws", "app", "secret", actor="u1")
    assert not any("push" in c for c in calls)


def test_confirm_proceeds_when_head_matches(monkeypatch):
    topic = _make_topic()  # checkout_sha AAAA1111
    calls = _mock_confirm(monkeypatch, topic, head_sha="AAAA1111")  # matches
    rc = _cmd_confirm_agent_edit("om_1", topic, [], "/state", "/ws", "app", "secret", actor="u1")
    # It passes the guard; later it would hit commit("nothing to commit") -> replay ->
    # push. We assert the guard did not short-circuit at R1 (i.e. it got past the early
    # return). Concretely: _approved was reached and we attempted commit/push.
    # Because the fake returns "nothing to commit", it proceeds to replay (apply) then
    # push. Assert a push git command appears.
    assert any("push" in c for c in calls)
