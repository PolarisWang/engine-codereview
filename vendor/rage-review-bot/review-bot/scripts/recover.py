"""Reconcile open topics against GitLab MR state.

Implements SKILL.md's `recover` mode as deterministic code instead of an
LLM-walked procedure:
- For each open topic in cfg/topics/*.json, fetch each MR via glab api.
- Map head_pipeline.status to pipeline_status (success/missing -> passed,
  running/pending -> running, failed/canceled -> failed).
- Any MR closed -> topic CLOSED. All MRs merged -> topic MERGED.
- APPROVED + opened MR -> refresh pipeline_status.
- Move topic file to cfg/topics/closed/ on terminal transitions.

Refuses to run while any topic lock is held (would clobber an in-flight
agent's writes). Output is JSON on stdout for the parent session to
display.
"""
import json
import subprocess
import sys
import time
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
CFG_DIR = SKILL_DIR / "cfg"
TOPICS_DIR = CFG_DIR / "topics"
CLOSED_DIR = TOPICS_DIR / "closed"
INDEX_PATH = CFG_DIR / "open_topic_index.json"
DAEMON_PID = CFG_DIR / "daemon.pid"

sys.path.insert(0, str(SCRIPT_DIR))
import topic_store  # noqa: E402
import subprocess_util  # noqa: E402
import terminal_notice  # noqa: E402

TERMINAL = {"MERGED", "CLOSED"}


def _glab_mr(repo_slug, iid):
    encoded = repo_slug.replace("/", "%2F")
    out = subprocess.run(
        ["glab", "api", f"projects/{encoded}/merge_requests/{iid}"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if out.returncode != 0:
        return {"_error": out.stderr.strip() or out.stdout.strip()}
    try:
        return json.loads(out.stdout)
    except Exception as e:
        return {"_error": f"json: {e}"}


def _pipeline_status(mr_json):
    hp = mr_json.get("head_pipeline") or {}
    s = hp.get("status")
    if s in (None, "success"):
        return "passed"
    if s in ("running", "pending"):
        return "running"
    if s in ("failed", "canceled"):
        return "failed"
    return "passed"


def _repo_slug_for(repo_key, mr_entry):
    if repo_key.startswith("3rd_party/"):
        return mr_entry.get("repo_slug", "")
    if repo_key == "rage":
        return "booming/dev/projects/rage/rage"
    if repo_key == "chaos":
        return "booming/dev/projects/rage/chaos"
    return mr_entry.get("repo_slug", "")


def _catch_up_messages():
    """Run one full dispatcher cycle to ingest messages missed while down.

    The GitLab reconcile below only fixes MR-state drift on topics that
    already exist. Topics opened — and mechanical replies (pass / indices /
    ok / close) sent — while the bot was down are Lark messages the dead
    listener never captured; the only way to recover them is the
    history-backfill reconcile inside a dispatcher cycle. We run exactly one
    cycle here so `/review-bot recover` is self-contained.

    Guarded: skip if a live daemon already holds `daemon.pid`, since it
    reconciles every cycle and a concurrent one-shot cycle could double-
    process. Returns a short status dict (never raises).
    """
    pid = subprocess_util.read_pid_file(DAEMON_PID)
    if pid and subprocess_util.is_process_alive(pid):
        return {"ran": False, "reason": "daemon_alive", "daemon_pid": pid}
    try:
        out = subprocess_util.hidden_run(
            ["python", str(SCRIPT_DIR / "dispatcher.py")],
            capture_output=True, text=True, timeout=300,
        )
        if out.returncode != 0:
            return {"ran": False, "reason": "dispatcher_failed",
                    "error": (out.stderr or "")[:200]}
        try:
            return {"ran": True, "cycle": json.loads(out.stdout.strip())}
        except json.JSONDecodeError:
            return {"ran": True, "cycle": "ok"}
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return {"ran": False, "reason": "dispatcher_error", "error": str(e)}


def reconcile():
    rows = []
    # iter_topic_files (NOT a raw om_*.json glob) so we skip sibling
    # `.inbox.json` (a list) / `.tmp-` files — reading one as a topic dict
    # would crash on `.get()` and abort the whole reconcile.
    for path in sorted(topic_store.iter_topic_files(TOPICS_DIR)):
        try:
            with open(path, "r", encoding="utf-8") as f:
                topic = json.load(f)
        except Exception as e:
            rows.append({"file": path.name, "error": f"read: {e}"})
            continue
        if not isinstance(topic, dict):
            rows.append({"file": path.name, "error": "not a topic dict — skipped"})
            continue

        ticket = topic.get("identity", {}).get("ticket_id", "?")
        before_state = topic.get("review", {}).get("state", "?")
        mrs = topic.get("mrs", {})
        per_mr = []
        any_closed = False
        all_merged = bool(mrs)
        changed = False

        for repo_key, entry in list(mrs.items()):
            iid = entry.get("mr_iid")
            slug = _repo_slug_for(repo_key, entry)
            if not iid or not slug:
                per_mr.append({"repo": repo_key, "skip": "missing iid/slug"})
                all_merged = False
                continue
            mr_json = _glab_mr(slug, iid)
            if not mr_json or "_error" in mr_json:
                per_mr.append({"repo": repo_key, "iid": iid,
                               "error": mr_json.get("_error", "fetch failed")})
                all_merged = False
                continue
            gl_state = mr_json.get("state", "unknown")
            if gl_state == "closed":
                any_closed = True
            if gl_state != "merged":
                all_merged = False
            new_pipe = _pipeline_status(mr_json)
            old_pipe = entry.get("pipeline_status")
            if before_state == "APPROVED" and gl_state == "opened":
                if new_pipe != old_pipe:
                    entry["pipeline_status"] = new_pipe
                    changed = True
            per_mr.append({
                "repo": repo_key, "iid": iid, "gl_state": gl_state,
                "old_pipe": old_pipe, "new_pipe": new_pipe,
            })

        new_state = before_state
        action = "no-change"
        now_ms = int(time.time() * 1000)
        if before_state in TERMINAL:
            action = "already-terminal"
        elif any_closed:
            new_state = "CLOSED"
            topic["review"]["state"] = new_state
            topic.setdefault("lifecycle", {})["resolved_at"] = now_ms
            topic["lifecycle"]["closed_reason"] = "recover_detected_closed_mr"
            action = "→ CLOSED"
        elif mrs and all_merged:
            new_state = "MERGED"
            topic["review"]["state"] = new_state
            topic.setdefault("lifecycle", {})["resolved_at"] = now_ms
            topic["lifecycle"]["merge_detected_at"] = now_ms
            action = "→ MERGED"
        elif before_state == "APPROVED":
            action = "pipeline-refresh"

        changed = changed or (new_state != before_state)

        moved = None
        if new_state in TERMINAL and before_state not in TERMINAL:
            # Persist the terminal state, then archive via the canonical
            # helper so the index entry AND the sibling inbox are removed
            # too (a bare os.replace orphaned both — see DESIGN §1.1.3).
            topic.setdefault("lifecycle", {})["updated_at"] = now_ms
            # Reflect the terminal transition in the thread before archiving —
            # recover runs after downtime, so the thread is exactly where a
            # human last saw it. Idempotent + persisted by the write below.
            # See DESIGN §1.6.11.
            try:
                terminal_notice.post_terminal_notice(topic, new_state)
            except Exception as e:  # noqa: BLE001
                pass
            topic_store.write_atomic(str(path), topic)
            # Use the on-disk filename stem (topics are always named
            # {thread_id}.json); archive_topic rebuilds the path from it.
            thread_id = path.stem
            try:
                ok = topic_store.archive_topic(TOPICS_DIR, INDEX_PATH, thread_id)
                moved = str(CLOSED_DIR / path.name) if ok else "archive-failed"
            except Exception as e:
                moved = f"archive-error: {e}"
        elif changed:
            topic.setdefault("lifecycle", {})["updated_at"] = now_ms
            topic_store.write_atomic(str(path), topic)

        rows.append({
            "ticket": ticket,
            "file": path.name,
            "before": before_state,
            "after": new_state,
            "action": action,
            "mrs": per_mr,
            "moved": moved,
        })

    return rows


def main():
    holders = topic_store.active_lock_holders(str(TOPICS_DIR))
    if holders:
        print(json.dumps({
            "status": "refused",
            "reason": "active topic locks — wait for in-flight agents or clear locks",
            "holders": holders,
        }, ensure_ascii=False, indent=2))
        sys.exit(1)
    # D: ingest topics / mechanical replies that arrived while the bot was
    # down (one guarded dispatcher cycle), THEN reconcile GitLab MR state
    # across all topics — including any the catch-up just created.
    catch_up = _catch_up_messages()
    rows = reconcile()
    print(json.dumps({"status": "ok", "catch_up": catch_up, "rows": rows},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
