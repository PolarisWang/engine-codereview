"""Regression tests for R2: close/fix-MR must match by ownership, not bare branch name.

_close_topic_resources formerly closed/deleted any MR whose source_branch == the
topic's {src}-fix-{task} name. A same-named MR owned by someone else could be
closed and its branch deleted. Tests:
  - an MR in the topic's fix_mr_iids ledger IS closed + its branch deleted;
  - a same-branch MR NOT in the ledger, with no jira reference, is LEFT ALONE
    (unclosed, branch not deleted) — the core R2 regression guard;
  - a same-branch MR referencing the jira (legacy fallback) IS treated as owned;
  - with zero owned MRs, the branch is never deleted.
"""
import json

import pytest

from orchestrate import _close_topic_resources


# --- helpers to fake urllib + env ---

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def _monkeypatch_gitlab(monkeypatch, fake_store):
    """fake_store: dict {http_status_code_for_log / or }
    A real fake: urlopen returns OpenMRs for the LIST call and a 204 for close/delete.
    We distinguish by the URL being '?state=opened' (list) vs /merge_requests/{iid}
    (close) vs /repository/branches/ (delete)."""
    import urllib.request
    calls = {"list": 0, "close": [], "delete": []}

    def _urlopen(req, timeout=None):
        u = req.full_url if isinstance(req, urllib.request.Request) else str(req)
        if "merge_requests?state=opened" in u:
            calls["list"] += 1
            return _FakeResp(fake_store["opened_mrs"])
        if "/merge_requests/" in u and req.method == "PUT":
            iid = u.rstrip("/").split("/")[-1]
            calls["close"].append(iid)
            class _NoBody:
                def read(self): return b"{}"
            return _NoBody()
        if "/repository/branches/" in u and req.method == "DELETE":
            calls["delete"].append(u)
            raise urllib.error.HTTPError(u, 204, "", None, None)
        raise AssertionError(f"unexpected url: {u}")

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    return calls


def _fake_topic(**over):
    t = {
        "mr_url": "https://gitlab.booming-inc.com/g/p/-/merge_requests/7",
        "review_branch": "feature/EV-1",
        "jira_key": "EV-1",
        "base_branch": "main",
        "fix_mr_iids": [],
    }
    t.update(over)
    return t


def _own(fake_store, iid, branch="feature/EV-1-fix-EV-1", title="Fix EV-1: code review fixes"):
    fake_store["opened_mrs"] = [{"iid": iid, "source_branch": branch,
                                 "state": "opened", "title": title, "description": "Code review fix for EV-1"}]


def test_close_deletes_when_mr_owned_by_iid(monkeypatch):
    store = {"opened_mrs": []}
    _own(store, 100, branch="feature/EV-1-fix-EV-1", title="Totally unrelated title")  # owned ONLY by iid
    calls = _monkeypatch_gitlab(monkeypatch, store)
    topic = _fake_topic(fix_mr_iids=[100], review_branch="feature/EV-1")
    note = _close_topic_resources(topic)
    # Owned MR closed + branch deleted.
    assert "100" in calls["close"]
    assert len(calls["delete"]) == 1
    assert "已释放" in note


def test_same_named_stranger_mr_is_left_alone(monkeypatch):
    store = {"opened_mrs": []}
    # A stranger's MR: same branch name, different iid, no jira in title/desc.
    _own(store, 999, branch="feature/EV-1-fix-EV-1", title="Someone else's change")
    store["opened_mrs"][0]["description"] = "unrelated"
    calls = _monkeypatch_gitlab(monkeypatch, store)
    topic = _fake_topic(fix_mr_iids=[])  # we never created any MR
    note = _close_topic_resources(topic)
    # No ownership -> nothing closed, branch NOT deleted.
    assert calls["close"] == []
    assert calls["delete"] == []
    assert note == ""


def test_same_named_jira_reference_treated_as_owned_legacy(monkeypatch):
    store = {"opened_mrs": []}
    _own(store, 55, branch="feature/EV-1-fix-EV-1", title="Fix EV-1")  # jira in title
    calls = _monkeypatch_gitlab(monkeypatch, store)
    topic = _fake_topic(fix_mr_iids=[])  # legacy topic, no ledger
    _close_topic_resources(topic)
    assert "55" in calls["close"]
    assert len(calls["delete"]) == 1


def test_no_owned_mr_never_deletes_branch(monkeypatch):
    store = {"opened_mrs": []}
    _own(store, 3, branch="feature/EV-1-fix-EV-1", title="Unrelated")
    store["opened_mrs"][0]["description"] = "unrelated, no jira ref"  # not ours
    calls = _monkeypatch_gitlab(monkeypatch, store)
    topic = _fake_topic(fix_mr_iids=[])
    _close_topic_resources(topic)
    # owned_iids empty and closed==0 => branch delete is skipped.
    assert calls["delete"] == []
    assert calls["close"] == []


# --- A 方案: 孤儿 fix 分支释放(无 MR 也曾删, 仅 bot 创建) ---

def test_branch_is_bot_created_true_when_bot_commit(monkeypatch):
    import urllib.request
    from orchestrate import _branch_is_bot_created
    class _Resp:
        def read(self): return b'{"commit":{"author_name":"codereview-agent","title":"[codereview-agent] auto-fix"}}'
        def __enter__(self): return self
        def __exit__(self,*a): return False
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _Resp())
    assert _branch_is_bot_created("p", "b", "tok") is True


def test_branch_is_bot_created_false_when_foreign(monkeypatch):
    import urllib.request
    from orchestrate import _branch_is_bot_created
    class _Resp:
        def read(self): return b'{"commit":{"author_name":"zhang-san","title":"something else"}}'
        def __enter__(self): return self
        def __exit__(self,*a): return False
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _Resp())
    assert _branch_is_bot_created("p", "b", "tok") is False


def test_branch_is_bot_created_false_when_missing(monkeypatch):
    import urllib.request, urllib.error
    from orchestrate import _branch_is_bot_created
    def _raise(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "", None, None)
    monkeypatch.setattr(urllib.request, "urlopen", _raise)
    assert _branch_is_bot_created("p", "b", "tok") is False
