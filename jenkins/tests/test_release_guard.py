"""Tests for release.py publishing guardrails:
  - idempotency: a tag that already exists refuses re-publish (no duplicate
    send/notify).
  - version bumping is covered in test_release_note.py; here we focus the
    release.py main() guard branches (unclean worktree / unpushed / existing tag).
"""
import os, sys, importlib, importlib.util
_HERE = os.path.dirname(os.path.abspath(__file__))          # .../jenkins/tests
_REPO = os.path.join(_HERE, "..", "..")                      # repo root
_DEP = os.path.join(_REPO, "deploy")                         # repo/deploy
_SCR = os.path.join(_REPO, "jenkins", "scripts")             # repo/jenkins/scripts
for _p in (_DEP, _SCR):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import release_note  # ensure importable for release.py's 'import release_note as rn'
_spec = importlib.util.spec_from_file_location("_rel_mod", os.path.join(_DEP, "release.py"))
release = importlib.util.module_from_spec(_spec)
sys.modules["_rel_mod"] = release
_spec.loader.exec_module(release)


def _git_stub(responses):
    """A callable standing in for release.git: return per-arg responses.

    responses: dict keyed by a substring of the joined args; value (rc, stdout,'')."""
    def _g(args, cwd):
        joined = " ".join(args)
        for needle, resp in sorted(responses.items(), key=lambda kv: -len(kv[0])):
            if needle in joined:
                if callable(resp):
                    return resp(args, cwd)
                return resp
        return (0, "", "")
    return _g


def _fake_version_commits(monkeypatch):
    """Freeze version/commit derivation so main() reaches the guard branches
    with a known tag (v9.9.9) and a couple of commits."""
    import release_note as rn
    monkeypatch.setattr(rn, "list_tags", lambda repo: ["v9.9.8"])
    def _range(prev, repo):
        return [("fix", "a"), ("feat", "b")]   # (type, subject) tuples
    monkeypatch.setattr(rn, "_git_range_commits", _range)
    monkeypatch.setattr(release, "rn", rn)


def test_release_refuses_existing_tag(monkeypatch, capsys):
    """幂等: tag v9.9.9 已存在 -> main 返回 1, 不重复发布."""
    _fake_version_commits(monkeypatch)
    # 前置检查全通过(status 空, unpushed 空), 幂等检查 tag 已存在(rc=0)
    monkeypatch.setattr(release, "git", _git_stub({
        "status --porcelain": (0, "", ""),
        "rev-parse --abbrev-ref": (0, "main", ""),
        "origin/main..HEAD": (0, "", ""),
        "refs/tags/v9.9.9": (0, "d3adb33fc0", ""),   # tag exists
    }))
    # 防 LLM 润色副作用
    monkeypatch.setattr(release, "_polish_release_note", lambda *a, **k: "")
    monkeypatch.setattr(sys, "argv", ["release.py", "--repo-dir", "."])
    rc = release.main()
    assert rc == 1
    out = capsys.readouterr().err
    assert "已存在" in out


def test_release_refuses_unclean_worktree(monkeypatch, capsys):
    """/clean worktree: main 返回 1 且不发布."""
    _fake_version_commits(monkeypatch)
    monkeypatch.setattr(release, "git", _git_stub({
        "status --porcelain": (0, " M foo.py", ""),   # dirty
    }))
    monkeypatch.setattr(sys, "argv", ["release.py", "--repo-dir", "."])
    rc = release.main()
    assert rc == 1
    assert "不干净" in capsys.readouterr().err


def test_release_refuses_unpushed(monkeypatch, capsys):
    """有未推送提交: main 返回 1."""
    _fake_version_commits(monkeypatch)
    monkeypatch.setattr(release, "git", _git_stub({
        "status --porcelain": (0, "", ""),
        "rev-parse --abbrev-ref": (0, "main", ""),
        "origin/main..HEAD": (0, "abc123  fix: x", ""),   # unpushed
    }))
    monkeypatch.setattr(sys, "argv", ["release.py", "--repo-dir", "."])
    rc = release.main()
    assert rc == 1
    assert "未推送" in capsys.readouterr().err


def test_release_dry_run_ok(monkeypatch):
    """dry-run(patch bump) 返回 0 且不打 tag."""
    _fake_version_commits(monkeypatch)
    monkeypatch.setattr(release, "git", _git_stub({
        "status --porcelain": (0, "", ""),
        "rev-parse --abbrev-ref": (0, "main", ""),
        "origin/main..HEAD": (0, "", ""),
    }))
    monkeypatch.setattr(release, "_polish_release_note", lambda *a, **k: "")
    monkeypatch.setattr(sys, "argv", ["release.py", "--repo-dir", ".", "--dry-run"])
    rc = release.main()
    assert rc == 0
