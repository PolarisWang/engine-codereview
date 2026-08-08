"""Shared pytest fixtures / sys.path setup for the codereview pipeline tests.

The scripts under jenkins/scripts import each other by *sibling* module name
(e.g. `import pipeline_state`, `from config import ...`) — they are NOT a
package. So we must put jenkins/scripts on sys.path for any test that imports
them, and every test must use the `tmp_path` fixture for all state/workspace
paths so nothing ever touches the real production paths
(/var/lib/report-server/daily/*, /root/.codereview-pipeline-state.json, ...).

Run tests (from repo root, or anywhere — paths are repo-root-relative):
    PYTHONPATH=.deps-pytest python3 -m pytest
"""
import os
import sys

import pytest

# Repo root = two levels above this tests/ dir: jenkins/tests -> jenkins -> repo.
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.abspath(os.path.join(TESTS_DIR, "..", "scripts"))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPTS_DIR, "..", ".."))

# Put the scripts dir on sys.path so `import pipeline_state` etc. resolve.
# Insert at front so our modules win over any same-named installed module.
sys.path.insert(0, SCRIPTS_DIR)

# Guard: the environment must never point at production state in tests.
# Any test that needs a state file MUST use the `state_dir` fixture below.
pytest.register_assert_rewrite("conftest_shims") if False else None


@pytest.fixture
def state_dir(tmp_path):
    """Return an ISOLATED directory to serve as a dir-mode (v2) pipeline state
    store. Tests write their state store under here; nothing escapes tmp_path."""
    return tmp_path / "state"


@pytest.fixture
def workspace(tmp_path):
    """Return an ISOLATED workspace dir (checkout + result files)."""
    return tmp_path / "workspace"


@pytest.fixture
def lock_dir(tmp_path):
    """Return an ISOLATED lock dir (ignored if not needed)."""
    return tmp_path / "locks"


@pytest.fixture
def co_fix(tmp_path):
    """A real checkout dir for path-safety tests (R9): a checkout with subdirs
    so join/repath and symlink traversal are exercised for real."""
    co = tmp_path / "checkout"
    co.mkdir(exist_ok=True)
    (co / "src").mkdir(exist_ok=True)
    (co / "sub").mkdir(exist_ok=True)
    return co
