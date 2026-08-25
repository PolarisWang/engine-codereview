"""Tests for P4 manual-review integration (gitlab_threads_http).

Covers the non-network pure logic: DiffNote normalization, reconcile (verification
preservation + stale drop), and the verification context/prompt builder against a
real temp git repo (before/after ±10 lines + per-file diff).
"""
import os, subprocess, tempfile
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "skills", "rage-review"))
import gitlab_threads_http as g


def _diffnote(did, body="bug", ftype="DiffNote", cls_type_new=True, extra=None):
    n = {"id": 7, "type": ftype, "body": body, "created_at": "t",
         "author": {"username": "zhang"}, "position": {"new_path": "a.cpp",
         "new_line": 4, "base_sha": "abc"}}
    if extra:
        n.update(extra)
    return {"id": did, "notes": [n]}


def test_normalize_diffnote_keeps_human_comment():
    e = g._normalize_diffnote(_diffnote("d1", "内存泄漏"), "https://h/x/-/merge_requests/1")
    assert e is not None
    assert e["file"] == "a.cpp" and e["line_new"] == 4
    assert e["author"] == "zhang" and "内存泄漏" in e["body"]
    assert "discussion_id" in e and "verified_at_sha" in e


def test_normalize_drops_non_diffnote():
    assert g._normalize_diffnote(_diffnote("d9", ftype="SystemNote"), "u") is None


def test_reconcile_preserves_verification():
    e = g._normalize_diffnote(_diffnote("d1"), "u")
    existing = [dict(e, verification="addressed", verification_rationale="已修",
                     verified_at_sha="sha1")]
    fresh = [dict(e)]
    merged = g.reconcile_manual_issues(existing, fresh)
    assert len(merged) == 1
    assert merged[0]["verification"] == "addressed"
    assert merged[0]["verified_at_sha"] == "sha1"


def test_reconcile_drops_stale_thread():
    e = g._normalize_diffnote(_diffnote("d1"), "u")
    stale = [dict(e)]
    # fresh fetch no longer has d1
    merged = g.reconcile_manual_issues(stale, [])
    assert merged == []


def test_verification_context_and_prompt_real_repo():
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "r"); os.makedirs(src)
        def git(*args):
            return subprocess.run(["git", "-C", src, *args],
                                  capture_output=True, text=True)
        git("init", "-q", "-b", "master"); git("config", "user.email", "t@t")
        git("config", "user.name", "t")
        open(os.path.join(src, "f.cpp"), "w").write("int a;\nint b;\n// review\nint c;\n")
        git("add", "."); git("commit", "-qm", "x")
        base = git("rev-parse", "HEAD").stdout.strip()
        open(os.path.join(src, "f.cpp"), "w").write("int a;\nint b;\nint b2;\n// fixed\nint c;\n")
        git("add", "."); git("commit", "-qm", "y")
        head = git("rev-parse", "HEAD").stdout.strip()
        ctx = g.build_verification_context(src, {"file": "f.cpp", "line_new": 3,
                                                 "base_sha": base, "body": "double-free"}, head)
        assert "review" in ctx["original_code"] and "fixed" in ctx["current_code"]
        assert ctx["diff_slice"]
        prompt = g.build_verification_prompt(ctx)
        assert "addressed" in prompt and "unclear" in prompt


def test_repo_slug_from_mr_url():
    assert g.repo_slug_and_iid_from_mr_url(
        "https://gitlab.booming-inc.com/booming/dev/projects/rage/rage/-/merge_requests/7201") \
        == ("booming/dev/projects/rage/rage", "7201")
