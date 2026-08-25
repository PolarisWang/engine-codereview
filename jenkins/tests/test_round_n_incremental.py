"""Tests for P3 round-N incremental diff base in code_reviewer.prepare_repo.

Verifies that --last-review-commit diffs only since that SHA (two-dot) when it is
an ancestor of HEAD, and falls back to the full three-dot diff otherwise, using a
real local temp git repo (no GitLab required).
"""
import os, subprocess, tempfile
import code_reviewer as cr


def _git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                          text=True).stdout.strip()


def _make_repo(tmp):
    src = os.path.join(tmp, "src")
    os.makedirs(src)
    _git(src, "init", "-q", "-b", "master")
    _git(src, "config", "user.email", "t@t")
    _git(src, "config", "user.name", "t")
    with open(os.path.join(src, "f.cpp"), "w") as f:
        f.write("int a;\n")
    _git(src, "add", ".")
    _git(src, "commit", "-qm", "r1")
    base_sha = _git(src, "rev-parse", "HEAD")
    os.system(f'git -C {src} checkout -qb feature/t1')
    with open(os.path.join(src, "f.cpp"), "w") as f:
        f.write("int a;\nint b;\n")
    _git(src, "add", ".")
    _git(src, "commit", "-qm", "add b")
    head_sha = _git(src, "rev-parse", "HEAD")
    return src, base_sha, head_sha


def test_incremental_diff_from_last_review_commit():
    with tempfile.TemporaryDirectory() as tmp:
        src, base_sha, head_sha = _make_repo(tmp)
        ws = os.path.join(tmp, "ws")
        os.makedirs(ws, exist_ok=True)
        # round-1 full diff (no last_review_commit): both a and b lines present
        d_full = cr.prepare_repo(src, "feature/t1", "master", ws, "K-1", cache=False)
        assert "int b" in d_full["diff_text"]
        assert d_full.get("incr_mode") == ""
        # round-N from base_sha: only `int b` is new
        d_incr = cr.prepare_repo(src, "feature/t1", "master", ws, "K-1", cache=False,
                                 last_review_commit=base_sha)
        assert d_incr.get("incr_mode") == "incremental"
        assert d_incr["diff_text"].count("int b") == 1
        assert d_incr["last_review_commit"] == base_sha


def test_invalid_last_review_commit_falls_back_full():
    with tempfile.TemporaryDirectory() as tmp:
        src, _, _ = _make_repo(tmp)
        ws = os.path.join(tmp, "ws")
        os.makedirs(ws, exist_ok=True)
        # a bogus SHA (not an ancestor) -> incr_prev None -> full three-dot
        d = cr.prepare_repo(src, "feature/t1", "master", ws, "K-1", cache=False,
                            last_review_commit="deadbeef0000000000000000000000000000000000")
        assert d.get("incr_mode") == ""
        assert "int b" in d["diff_text"]  # full diff still includes everything
