"""Regression tests for the root-caused MR-creation fix.

Root problems fixed in _create_or_get_mr:
  - detect used state=all&per_page=50, truncated by many closed MRs -> missed the
    real open MR -> tried to create -> GitLab 409 "already exists". Now uses
    state=opened + source_branch filter, so an existing open MR is found.
  - create on HTTPError 409 had no fallback -> hard fail. Now re-queries and reuses
    the existing open MR.
  - `MR单` (read-only) wrongly called create_if_missing=True and could create an MR.
    Now MR单 uses create_if_missing=False.
"""
import json

import pytest
from orchestrate import _create_or_get_mr

NEW_BRANCH = "feature/CB2N-27312-clean-rebase3-fix-CB2N-27312"


class _Resp:
    def __init__(self, payload):
        self._p = payload
    def read(self):
        return json.dumps(self._p).encode("utf-8")
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _topic():
    return {"mr_url": "https://gitlab.booming-inc.com/g/p/-/merge_requests/7",
            "review_branch": "feature/CB2N-27312-clean-rebase3",
            "base_branch": "master", "jira_key": "CB2N-27312", "fix_mr_iids": []}


def _mock(monkeypatch, env_token="tok", open_mr=None, create_http=200, compare_diffs=1):
    import urllib.request, urllib.error
    calls = {"create": 0, "queries": []}

    def _urlopen(req, timeout=None):
        u = req.full_url if isinstance(req, urllib.request.Request) else str(req)
        calls["queries"].append(u)
        if "merge_requests?state=opened" in u and "source_branch=" in u:
            # query for existing open MR on the branch
            mrs = [{"iid": 7024, "web_url": "https://x/7024",
                    "source_branch": NEW_BRANCH, "state": "opened"}] if open_mr else []
            return _Resp(mrs)
        if "/repository/compare" in u:
            return _Resp({"diffs": [{"old_path":"a","new_path":"b"}] * compare_diffs})
        if "/merge_requests" in u and req.method == "POST":
            calls["create"] += 1
            if create_http == 200:
                return _Resp({"iid": 999, "web_url": "https://x/999"})
            raise urllib.error.HTTPError(u, create_http, "", None, None)
        raise AssertionError(f"unexpected url: {u}")

    monkeypatch.setattr("orchestrate._env", lambda k, d="": {"GITLAB_TOKEN": env_token}.get(k, d))
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr("orchestrate._project_path", lambda t: "g/p")
    monkeypatch.setattr("orchestrate._new_branch_name", lambda t: NEW_BRANCH)
    return calls


def test_detect_reuses_existing_open_mr(monkeypatch):
    # An open MR on the fix branch exists -> detect it, do NOT create.
    calls = _mock(monkeypatch, open_mr=True)
    iid, url, _, note = _create_or_get_mr(_topic(), [], create_if_missing=True)
    assert iid == 7024 and url == "https://x/7024"
    assert "detected existing" in note
    assert calls["create"] == 0        # never tried to create


def test_409_reuses_existing_mr(monkeypatch):
    # No open MR on first detect (open_mr=None), create -> 409, then re-use.
    # Simulate: first query empty (find_open returns None), create 409,
    # the 409-fallback re-query should now find an open MR.
    call_state = {"count": 0}
    import urllib.request, urllib.error

    def _urlopen(req, timeout=None):
        u = req.full_url
        if "merge_requests?state=opened" in u and "source_branch=" in u:
            # Return empty on the FIRST query, then an MR on the 409-fallback query.
            call_state["count"] += 1
            mrs = [{"iid": 7024, "web_url": "https://x/7024",
                    "source_branch": NEW_BRANCH, "state": "opened"}] if call_state["count"] >= 2 else []
            return _Resp(mrs)
        if "/repository/compare" in u:
            return _Resp({"diffs": [{"old_path":"a"}]})
        if "/merge_requests" in u and req.method == "POST":
            raise urllib.error.HTTPError(u, 409, "", None, None)
        raise AssertionError(u)

    monkeypatch.setattr("orchestrate._env", lambda k, d="": "tok")
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr("orchestrate._project_path", lambda t: "g/p")
    monkeypatch.setattr("orchestrate._new_branch_name", lambda t: NEW_BRANCH)
    iid, url, _, note = _create_or_get_mr(_topic(), [], create_if_missing=True)
    assert iid == 7024
    assert "reused existing" in note


def test_readonly_mr_command_does_not_create(monkeypatch):
    # MR单 uses create_if_missing=False: even if no MR exists, it must NOT create.
    calls = _mock(monkeypatch, open_mr=False)
    iid, url, _, note = _create_or_get_mr(_topic(), [], create_if_missing=False)
    assert iid is None
    assert "no MR for fix branch yet" in note
    assert calls["create"] == 0        # read-only never creates
