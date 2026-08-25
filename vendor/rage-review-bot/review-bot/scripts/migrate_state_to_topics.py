"""One-shot migration from the v1 monolithic cfg/state.json to v2
per-topic files under cfg/topics/.

Idempotent: safe to re-run. Already-migrated topic files are not
touched. The original state.json is copied to state.json.bak on the
first run (kept for a week as a rollback safety net).

Usage:
    python scripts/migrate_state_to_topics.py
"""

import json
import os
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import topic_store


SKILL_DIR = SCRIPT_DIR.parent
CFG_DIR = SKILL_DIR / "cfg"
STATE_FILE = CFG_DIR / "state.json"
STATE_BACKUP = CFG_DIR / "state.json.bak"
TOPICS_DIR = CFG_DIR / "topics"
CLOSED_DIR = TOPICS_DIR / "closed"
INDEX_PATH = CFG_DIR / "open_topic_index.json"


# In v2 the terminal set is {MERGED, CLOSED}. APPROVED is transient.
TERMINAL_IN_V2 = {"MERGED", "CLOSED"}


def _pick_repo_and_mrs(topic_v1):
    """Normalize the v1 game_mr_iid / chaos_mr_iid split into v3 mrs dict."""
    mrs = {}
    if topic_v1.get("chaos_mr_iid"):
        mrs["chaos"] = {
            "mr_iid":     topic_v1.get("chaos_mr_iid"),
            "branch":     topic_v1.get("chaos_branch"),
            "branch_sha": topic_v1.get("chaos_branch_sha"),
            "web_url":    None,
        }
    if topic_v1.get("game_mr_iid"):
        mrs["rage"] = {
            "mr_iid":     topic_v1.get("game_mr_iid"),
            "branch":     topic_v1.get("game_branch"),
            "branch_sha": topic_v1.get("game_branch_sha"),
            "web_url":    None,
        }
    return mrs


def _convert(thread_id, topic_v1, chat_id, last_ts):
    """Build a v2 topic dict from a v1 entry."""
    # Lifecycle timestamps — v1 has no created_at. Use last_processed_ts
    # as a loose upper bound for anything that didn't record resolved_at.
    created = topic_v1.get("created_at") or last_ts or 0
    updated = topic_v1.get("updated_at") or created
    resolved = topic_v1.get("resolved_at")

    return {
        "schema_version": 3,
        "thread_id": thread_id,
        "root_message_id": topic_v1.get("message_id", thread_id),
        "identity": {
            "ticket_id":       topic_v1.get("ticket_id"),
            "creator_open_id": topic_v1.get("creator_open_id"),
            "developer":       topic_v1.get("developer"),
            "chat_id":         chat_id,
        },
        "mrs": _pick_repo_and_mrs(topic_v1),
        "review": {
            "state":              topic_v1.get("state", "TRIAGING"),
            "triage":             topic_v1.get("triage"),
            "review_round":       topic_v1.get("review_round", 0),
            "last_review_commit": topic_v1.get("last_review_commit"),
            "issues_found":       topic_v1.get("issues_found", 0),
            "flagged_issues":     topic_v1.get("flagged_issues", []),
            "lark_doc_token":     topic_v1.get("lark_doc_token"),
            "review_history":     [],
        },
        "events": {
            "pending": [],
            "last_processed_event_id": None,
            "last_processed_ts": last_ts or 0,
        },
        "lifecycle": {
            "created_at":        created,
            "updated_at":        updated,
            "resolved_at":       resolved,
            "merge_sha":         None,
            "merge_detected_at": None,
            "closed_reason":     topic_v1.get("note"),
        },
        "audit": [
            {
                "ts": updated,
                "event": "migrated_from_v1",
                "state": topic_v1.get("state", "TRIAGING"),
            }
        ],
    }


def main():
    if not STATE_FILE.exists():
        print(f"error: {STATE_FILE} not found — nothing to migrate", file=sys.stderr)
        return 1

    # Backup
    if not STATE_BACKUP.exists():
        shutil.copy2(STATE_FILE, STATE_BACKUP)

    with open(STATE_FILE, encoding="utf-8") as f:
        v1 = json.load(f)
    chat_id = os.environ.get("REVIEW_BOT_CHAT_ID", "")
    last_ts = int(v1.get("last_processed_timestamp", 0) or 0)

    TOPICS_DIR.mkdir(parents=True, exist_ok=True)
    CLOSED_DIR.mkdir(parents=True, exist_ok=True)

    open_index = {}
    migrated = 0
    skipped_existing = 0
    open_count = 0
    closed_count = 0

    for thread_id, topic_v1 in (v1.get("topics") or {}).items():
        v2 = _convert(thread_id, topic_v1, chat_id, last_ts)
        state = v2["review"]["state"]
        is_terminal = state in TERMINAL_IN_V2
        dest_dir = CLOSED_DIR if is_terminal else TOPICS_DIR
        dest = dest_dir / f"{thread_id}.json"

        if dest.exists():
            skipped_existing += 1
            if not is_terminal:
                open_index[thread_id] = (
                    (v2["identity"].get("ticket_id")) or "")
            continue

        topic_store.write_atomic(dest, v2)
        migrated += 1
        if is_terminal:
            closed_count += 1
        else:
            open_count += 1
            open_index[thread_id] = v2["identity"].get("ticket_id") or ""

    # Index is a snapshot of currently-open topics (including
    # already-migrated ones that we skipped-existing above).
    topic_store.write_atomic(INDEX_PATH, open_index)

    # v2 -> v3 upgrade: any existing topic files with schema_version == 2
    # are upgraded in-place using the same logic as topic_store auto-migrate.
    v2_upgraded = 0
    for scan_dir in (TOPICS_DIR, CLOSED_DIR):
        if not scan_dir.exists():
            continue
        for fp in scan_dir.iterdir():
            if fp.is_dir() or not fp.name.endswith(".json"):
                continue
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(data, dict) and data.get("schema_version") == 2:
                topic_store._migrate_v2_to_v3(data)
                topic_store.write_atomic(fp, data)
                v2_upgraded += 1

    print(json.dumps({
        "migrated":         migrated,
        "skipped_existing": skipped_existing,
        "v2_upgraded":      v2_upgraded,
        "open":             open_count,
        "closed":           closed_count,
        "index_path":       str(INDEX_PATH),
        "backup":           str(STATE_BACKUP),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
