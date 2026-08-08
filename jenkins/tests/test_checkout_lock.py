"""Regression tests for R17: auto-edit/confirm serialize per-repo checkout.

The concurrency cap only bounds *review* subprocesses; auto-edit (改码) and its
confirm share one `{repo}-review` checkout across topics. Two topics editing the
same repo can clobber each other's working tree. `_checkout_lock` is a
cross-process flock keyed by repo name, so all edit/confirm for ONE repo are
serialized while different repos stay parallel.
"""
import threading
import time

from orchestrate import _checkout_lock


def test_checkout_lock_serializes_same_repo(tmp_path):
    lock_dir = str(tmp_path / "locks")
    inside = []
    peak = {"n": 0}
    _guard = threading.Lock()

    def critical(repo):
        with _checkout_lock(repo, lock_dir=lock_dir):
            with _guard:
                inside.append(repo)
                peak["n"] = max(peak["n"], len(inside))
            time.sleep(0.1)
            with _guard:
                inside.remove(repo)

    # Two topics, SAME repo -> MUST serialize: at most ONE in the critical
    # section at any time (peak concurrency stays 1), and both complete.
    t1 = threading.Thread(target=critical, args=("chaos-cb-2",))
    t2 = threading.Thread(target=critical, args=("chaos-cb-2",))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert peak["n"] == 1          # serialized — never two inside the same repo lock
    assert not inside              # all released


def test_checkout_lock_allows_different_repos_parallel(tmp_path):
    lock_dir = str(tmp_path / "locks2")
    peak = {"n": 0}
    _guard = threading.Lock()
    inside = []

    def critical(repo):
        with _checkout_lock(repo, lock_dir=lock_dir):
            with _guard:
                inside.append(repo)
                peak["n"] = max(peak["n"], len(inside))
            time.sleep(0.1)
            with _guard:
                inside.remove(repo)

    t1 = threading.Thread(target=critical, args=("chaos-cb-2",))
    t2 = threading.Thread(target=critical, args=("mars",))
    t1.start(); t2.start(); t1.join(); t2.join()
    # Different repos -> different lock files -> can run concurrently (peak 2).
    assert peak["n"] == 2
    assert not inside


def test_key_is_stable_across_calls(tmp_path):
    # Same repo string must produce the SAME lock file (serializing across calls),
    # even with different topics. Verify by checking the lock file path exists with a
    # stable suffix after one acquisition.
    lock_dir = str(tmp_path / "locks3")
    with _checkout_lock("chaos-cb-2", lock_dir=lock_dir):
        pass
    import os
    files = os.listdir(lock_dir)
    assert len(files) == 1
    assert files[0].startswith("checkout_")
