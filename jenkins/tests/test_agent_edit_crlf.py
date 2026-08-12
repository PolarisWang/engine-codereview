"""Regression test: _agent_edit_one must PRESERVE CRLF line endings.

Root cause (MR 7099): the file was rewritten joined with "\n", stripping the "\r"
from a CRLF source file — every line changed, so git diffed the WHOLE file
("encode变了, 全部文件都有 diff"). Fix: detect the original newline style and
re-attach it on write, so only the edited window differs in the diff.
"""
import os
import subprocess

import pytest

from orchestrate import _agent_edit_one, EDIT_MODEL


@pytest.fixture
def crlf_repo(tmp_path):
    """A real temp git repo with a CRLF file containing a distinctive buggy line."""
    repo = str(tmp_path / "co")
    os.makedirs(repo)
    content = (
        "line one\r\n"
        "line two\r\n"
        "line three buggyToken here\r\n"
        "line four\r\n"
        "line five\r\n"
    )
    with open(os.path.join(repo, "a.cpp"), "w", encoding="utf-8", newline="") as f:
        f.write(content)
    subprocess.run(["git", "-C", repo, "init", "-q"])
    # disable autocrlf so git doesn't normalize CRLF->LF and interfere with the
    # byte-level CRLF-preservation assertion (the host git default may be =true)
    subprocess.run(["git", "-C", repo, "config", "core.autocrlf", "false"])
    subprocess.run(["git", "-C", repo, "-c", "user.email=a@b", "-c", "user.name=t",
                    "add", "a.cpp"], capture_output=True)
    subprocess.run(["git", "-C", repo, "-c", "user.email=a@b", "-c", "user.name=t",
                    "commit", "-qm", "base"], capture_output=True)
    return repo


def _mock_claude(monkeypatch, corrected_window):
    """Make _agent_edit_one's claude -p call return a fixed window (only line three
    changed, everything else byte-for-byte)."""
    def fake(prompt, model=EDIT_MODEL):  # noqa: ARG001
        return f"@@START@@\n{corrected_window}\n@@END@@"
    monkeypatch.setattr("orchestrate._claude_p_call", fake)


def test_agent_edit_preserves_crlf(monkeypatch, crlf_repo):
    repo = crlf_repo
    topic = {"review_branch": "feature/x", "jira_key": "T1"}
    # corrected window: line three's token fixed, all other lines identical (CRLF kept)
    corrected = (
        "line one\r\n"
        "line two\r\n"
        "line three fixedToken here\r\n"
        "line four\r\n"
        "line five\r\n"
    )
    _mock_claude(monkeypatch, corrected)
    original_bytes = open(os.path.join(repo, "a.cpp"), "rb").read()

    _file, diff, ok, err = _agent_edit_one(topic, "a.cpp", "buggyToken problem",
                                            "", "", repo, model=EDIT_MODEL)
    assert ok, f"edit failed: {err}"
    # The REAL guarantee: the file must STAY CRLF (not get converted to LF, which was
    # the MR 7099 bug that made every line diff).
    raw = open(os.path.join(repo, "a.cpp"), "rb").read()
    assert b"\r\n" in raw                          # CRLF present after edit
    assert raw.count(b"\r\n") == original_bytes.count(b"\r\n")  # same # of CRLF lines
    assert b"\r" not in raw.replace(b"\r\n", b"")  # no orphan lone \r left
    # Byte-level: EVERY line except the edited one must be byte-identical to the
    # original (only 'buggyToken' -> 'fixedToken'). This is what "no whole-file diff"
    # means in practice.
    orig_lines = original_bytes.split(b"\r\n")
    new_lines = raw.split(b"\r\n")
    assert len(orig_lines) == len(new_lines)
    changed = [i for i, (a, b) in enumerate(zip(orig_lines, new_lines)) if a != b]
    assert changed == [2] or not changed, f"unexpected lines changed: {changed} (want only line index 2)"
