"""Reopen a superseded predecessor topic when its superseding root is withdrawn.

Same-ticket supersede is one-way: the OLD topic gets
``lifecycle.closed_reason = "superseded by new topic om_<new>"`` and the NEW
topic records nothing. When the new (duplicate) root is later withdrawn, the
withdrawn-root close paths close only the duplicate — the predecessor stays
archived, and every reply addressed to its thread dies silently at router
Gate 4a. The RAGE-20032 incident (2026-07-10): a live FULL_REVISION topic was
superseded by a duplicate root, the duplicate was withdrawn, and the
developer's ``ok`` was dropped with zero signal. See DESIGN §1.2.9.

This module is the undo: called from ``dispatcher._close_withdrawn_topics``
right after it closes a withdrawn-root topic, it

1. **purges** any deferred-supersede ledger entry keyed on the withdrawn
   thread — unconditionally, BEFORE any guard, because
   ``_retry_pending_supersedes`` runs later in the same cycle and would
   otherwise close the predecessor on behalf of a topic that just died;
2. finds the predecessor via the ``closed_reason`` supersede pattern;
3. restores it (state from the last ``topic_closed`` audit ``from_state``),
   re-adds the index entry;
4. **backfills** the thread's missed replies as synthesized raw events so the
   normal router/drain pipeline processes them (the reconcile floor has moved
   past them — a targeted re-synthesis is the only recovery, DESIGN §1.2.6);
5. posts the ``topic_reopened`` notice.

Guards fail OPEN on transient errors: the trigger is one-shot (a re-reported
withdrawal finds no open topic to close, so the hook never re-fires). A wrong
reopen is visible (notice) and reversible (``close`` verb / recover.py); a
wrong non-reopen is silent and permanent. Only definitive verdicts block:
predecessor root confirmed withdrawn/dead, or an MR confirmed merged/closed.

Deliberately NOT hooked into ``router._evict_recalled_message``: the router
holds in-memory ``index`` / ``ticket_to_thread`` / ``closed_thread_ids``
snapshots and saves the index wholesale at pass end — an inline reopen there
gets clobbered and the backfilled events are re-dropped. Re-adding that hook
requires patching those snapshots at the call site (DESIGN §1.2.9). Recalls
also demonstrably never arrive on the live listener; the reconcile path this
module hooks is the one that fires in practice.

CLI (manual recovery):
    python topic_reopen.py --withdrawn-thread om_... [--dry-run]
"""

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import mechanical_reply_handler  # noqa: E402  (module attr: _is_message_alive)
import merge_tracker             # noqa: E402  (module attr: check_mr)
import reconcile                 # noqa: E402  (parse_create_time)
import topic_index               # noqa: E402
import topic_store               # noqa: E402
import subprocess_util           # noqa: E402

# States that must never be "restored" — a reopened topic left terminal
# would be silently re-archived by the janitor next cycle.
_TERMINAL_STATES = {"MERGED", "CLOSED"}

# Backfill floor tolerance: thread-listing create_time has MINUTE granularity
# while last_processed_ts is exact ms. Over-inclusion is free (recent-event
# ring + router Gate 5 dedup); under-inclusion loses the recovery event.
_FLOOR_SLACK_MS = 60_000

_BACKFILL_PAGE_SIZE = 50
# The recovery targets are the NEWEST replies, at the tail of the asc
# listing — page to exhaustion (floor filter makes over-fetch cheap); the
# cap only bounds a runaway pagination loop.
_BACKFILL_MAX_PAGES = 40


def _noop_log(level, message):  # pragma: no cover - trivial
    pass


def _read_topic_tolerant(path):
    """`read_or_none` only catches FileNotFoundError; a corrupt/truncated
    JSON raises. One bad file in closed/ must not brick the whole scan
    (the trigger is one-shot — fail-closed here re-creates the incident)."""
    try:
        return topic_store.read(path)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Step 0: deferred-supersede purge
# ---------------------------------------------------------------------------

def _purge_supersede_pending(cfg_dir, withdrawn_thread_id, dry_run=False):
    """Drop ledger entries whose new_thread is the withdrawn thread.

    Must run before any guard/bail: with the supersede deferred (lock-fresh
    predecessor), the predecessor is still OPEN and the finder below sees no
    closed predecessor — but ``_retry_pending_supersedes`` (later, same
    cycle) would still close it for the now-dead duplicate.

    ``dry_run`` counts matches without writing — an operator probing with
    ``--dry-run`` must not disarm a legitimately-armed deferred supersede.
    """
    ledger_path = Path(cfg_dir) / "supersede_pending.json"
    if not ledger_path.exists():
        return 0
    try:
        with open(ledger_path, encoding="utf-8") as f:
            entries = json.load(f)
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(entries, list):
        return 0
    kept = [e for e in entries
            if not (isinstance(e, dict)
                    and e.get("new_thread") == withdrawn_thread_id)]
    purged = len(entries) - len(kept)
    if purged and not dry_run:
        topic_store.write_atomic(ledger_path, kept)
    return purged


# ---------------------------------------------------------------------------
# Predecessor lookup + state restore
# ---------------------------------------------------------------------------

def find_superseded_predecessor(topics_dir, withdrawn_thread_id):
    """Scan closed/ for the topic whose closed_reason names the withdrawn
    thread as its superseder. Returns (path, topic) or (None, None).
    Newest ``lifecycle.resolved_at`` wins if several match."""
    closed_dir = Path(topics_dir) / "closed"
    if not closed_dir.exists():
        return None, None
    best = (None, None, -1)
    for p in closed_dir.iterdir():
        if not (p.is_file() and p.name.endswith(".json")
                and p.stem.startswith("om_")):
            continue
        topic = _read_topic_tolerant(p)
        if not topic:
            continue
        if topic_store.parse_superseded_by(topic) != withdrawn_thread_id:
            continue
        resolved_at = (topic.get("lifecycle") or {}).get("resolved_at") or 0
        if resolved_at > best[2]:
            best = (p, topic, resolved_at)
    return best[0], best[1]


def _last_closed_from_state(topic):
    """The ``from_state`` of the newest ``topic_closed`` audit entry, or None
    when missing / terminal (either way the reopen must refuse — restoring a
    terminal state would just get janitor-archived again)."""
    for entry in reversed(topic.get("audit") or []):
        if entry.get("event") == "topic_closed":
            from_state = entry.get("from_state")
            if from_state and from_state not in _TERMINAL_STATES:
                return from_state
            return None
    return None


# ---------------------------------------------------------------------------
# Thread backfill
# ---------------------------------------------------------------------------

def _default_fetch_thread(thread_id):
    """List the thread's replies via lark-cli (paginated, oldest first).
    Returns [] on any error — backfill is best-effort."""
    messages = []
    page_token = None
    for _ in range(_BACKFILL_MAX_PAGES):
        cmd = subprocess_util.lark_cli_argv_prefix() + [
            "im", "+threads-messages-list",
            "--as", "user", "--thread", thread_id,
            "--sort", "asc", "--page-size", str(_BACKFILL_PAGE_SIZE),
            "--format", "json"]
        if page_token:
            cmd += ["--page-token", page_token]
        try:
            proc = subprocess_util.hidden_run(
                cmd, capture_output=True, text=True,
                timeout=30, encoding="utf-8", errors="replace")
            body = json.loads(proc.stdout or "{}")
        except Exception:  # noqa: BLE001 - best-effort fetch
            return messages
        data = body.get("data") or {}
        messages.extend(data.get("messages") or [])
        page_token = data.get("page_token")
        if not (data.get("has_more") and page_token):
            break
    return messages


def _unwrap_content(raw):
    """Plain text from a thread-listing content payload.

    Synthesized flat events take ``normalize_listener_event``'s passthrough
    branch, which skips the JSON-content unwrap done for nested listener
    payloads — so ``{"text":"ok"}`` left verbatim would never match the
    ``^ok$`` reply grammar. Unwrap here instead.
    """
    obj = raw
    if not isinstance(obj, dict):
        s = str(raw or "")
        try:
            obj = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            return s
        if not isinstance(obj, dict):
            return s
    if isinstance(obj.get("text"), str):
        return obj["text"]
    post = obj.get("zh_cn") if isinstance(obj.get("zh_cn"), dict) else obj
    segments = []
    for paragraph in (post.get("content") or []):
        if not isinstance(paragraph, list):
            continue
        for seg in paragraph:
            if isinstance(seg, dict) and isinstance(seg.get("text"), str):
                segments.append(seg["text"])
    if segments:
        return "\n".join(segments)
    # str(dict) would be a Python repr; keep the original JSON text instead.
    return (json.dumps(obj, ensure_ascii=False) if isinstance(raw, dict)
            else str(raw))


def _synthesize_backfill_events(events_dir, topic, messages):
    """Write reconcile-shaped raw events for the thread replies the pipeline
    missed while the topic sat in closed/. The router routes them next cycle
    (Gate 4a passes — the topic is open again) and the normal drains handle
    classification, ack, and agent spawn.
    """
    events_dir = Path(events_dir)
    events_dir.mkdir(parents=True, exist_ok=True)
    thread_id = topic.get("thread_id", "")
    chat_id = (topic.get("identity") or {}).get("chat_id", "")
    floor = ((topic.get("events") or {}).get("last_processed_ts") or 0) \
        - _FLOOR_SLACK_MS
    written = 0
    for m in messages:
        if not isinstance(m, dict) or m.get("deleted"):
            continue
        mid = m.get("message_id", "")
        if not mid or mid == topic.get("root_message_id"):
            continue
        sender_id = ((m.get("sender") or {}).get("id", "")) or ""
        if sender_id.startswith("cli_"):
            continue  # bot's own posts (ou_ self-events die at router Gate 2)
        ts = reconcile.parse_create_time(m.get("create_time", ""))
        if ts and ts < floor:
            continue
        if topic_store.is_recent_event(topic, f"msg:{mid}"):
            continue  # already processed before the supersede
        content = _unwrap_content(
            (m.get("body") or {}).get("content", m.get("content", "")))
        ev = {
            "chat_id":      chat_id,
            "chat_type":    "group",
            "content":      content,
            "create_time":  str(ts),
            "id":           mid,
            "message_id":   mid,
            "thread_id":    thread_id,
            "root_id":      thread_id,
            "parent_id":    m.get("parent_id") or "",
            "message_type": m.get("msg_type", "text"),
            "mentions":     m.get("mentions") or [],
            "sender_id":    sender_id,
            "timestamp":    str(topic_store.now_ms()),
            "type":         "im.message.receive_v1",
            "_source":      "reopen_backfill",
        }
        out = events_dir / f"reopen_{mid}_{ts}.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(ev, f, ensure_ascii=False, indent=2)
        written += 1
    return written


# ---------------------------------------------------------------------------
# Notice
# ---------------------------------------------------------------------------

def _post_reopen_notice(topic):
    """Best-effort ``topic_reopened`` notice into the reopened thread."""
    try:
        from templates.render import load_template, render, post_to_lark
        root_message_id = topic.get("root_message_id") or topic.get("thread_id")
        if not root_message_id:
            return False
        ticket_id = (topic.get("identity") or {}).get("ticket_id", "")
        rendered = render(load_template("topic_reopened"),
                          {"TICKET_ID": ticket_id})
        ok, _response = post_to_lark(rendered, root_message_id)
        return bool(ok)
    except Exception:  # noqa: BLE001 - notice must never block the reopen
        return False


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def reopen_superseded_predecessor(topics_dir, index_path, withdrawn_thread_id,
                                  *, withdrawn_ids=None, fetch_thread_fn=None,
                                  events_dir=None, dry_run=False, log_fn=None):
    """Undo a supersede whose superseding root was withdrawn.

    Args:
        topics_dir / index_path: production (or sandbox) cfg paths.
        withdrawn_thread_id: thread_id of the just-closed duplicate topic
            (NOT the withdrawn message id — a topic born from a reply can
            have root_message_id != thread_id, and the supersede reason
            embeds the thread_id).
        withdrawn_ids: this reconcile pass's withdrawn message-id set; a
            predecessor whose own root is in it is refused fast (its
            withdrawal may live in an already-processed window, so the
            liveness probe below is the only other reliable block).
        fetch_thread_fn / events_dir / dry_run / log_fn: test seams.

    Returns a status dict; every branch reports ``purged``.
    """
    topics_dir = Path(topics_dir)
    index_path = Path(index_path)
    events_dir = Path(events_dir) if events_dir else topics_dir.parent / "events"
    log = log_fn or _noop_log
    withdrawn_ids = set(withdrawn_ids or ())

    try:
        # Step 0 — unconditional: never leave a dead duplicate's deferred
        # supersede armed (it would re-close the predecessor this same cycle).
        # dry_run only counts (an operator probe must not disarm the ledger).
        matched = _purge_supersede_pending(
            topics_dir.parent, withdrawn_thread_id, dry_run=dry_run)
        purge_info = ({"purged": 0, "would_purge": matched} if dry_run
                      else {"purged": matched})

        pred_path, pred = find_superseded_predecessor(
            topics_dir, withdrawn_thread_id)
        if pred is None:
            return {"status": "no_predecessor", **purge_info}

        pred_thread = pred.get("thread_id", pred_path.stem)
        ticket = (pred.get("identity") or {}).get("ticket_id", "")
        open_path = topics_dir / f"{pred_thread}.json"

        # Guard: another open topic already owns the ticket — the newest
        # root is canonical; do not resurrect (or heal) a rival to it.
        for p in topic_store.iter_topic_files(topics_dir):
            other = _read_topic_tolerant(p)
            if not other:
                continue
            if ((other.get("identity") or {}).get("ticket_id") == ticket
                    and other.get("thread_id") != pred_thread):
                return {"status": "refused_other_open_topic", **purge_info,
                        "predecessor_thread": pred_thread, "ticket": ticket,
                        "open_thread": other.get("thread_id")}

        # Heal: crash between open-copy write and closed-copy unlink leaves
        # both; the open copy is authoritative.
        if open_path.exists():
            if dry_run:
                return {"status": "dry_run", **purge_info,
                        "predecessor_thread": pred_thread, "ticket": ticket,
                        "would_heal_split_copies": True}
            try:
                pred_path.unlink()
            except OSError:
                pass
            topic_index.add(index_path, pred_thread, ticket)
            log("INFO", f"topic_reopen healed split copies for {pred_thread}")
            return {"status": "already_open_healed", **purge_info,
                    "predecessor_thread": pred_thread, "ticket": ticket}

        # Guard: predecessor's own root withdrawn too (double withdrawal).
        pred_root = pred.get("root_message_id") or pred_thread
        if pred_root in withdrawn_ids:
            return {"status": "refused_root_withdrawn", **purge_info,
                    "predecessor_thread": pred_thread, "ticket": ticket}
        alive = mechanical_reply_handler._is_message_alive(pred_root)
        if alive is False:  # None = transient/ambiguous -> fail open
            return {"status": "refused_root_dead", **purge_info,
                    "predecessor_thread": pred_thread, "ticket": ticket}

        # Guard: MRs already merged/closed — a reopened topic would be a
        # zombie nothing terminalizes (_check_approved_topics only watches
        # APPROVED). Definitive verdicts only; glab errors fail open.
        for repo, mr_obj in (pred.get("mrs") or {}).items():
            if not isinstance(mr_obj, dict):
                continue
            verdict = merge_tracker.check_mr(repo, mr_obj)
            if verdict.get("error"):
                log("WARN", f"topic_reopen MR check failed for {repo} "
                            f"({verdict['error']}); proceeding fail-open")
                continue
            if verdict.get("state") in ("merged", "closed"):
                return {"status": "refused_mr_terminal", **purge_info,
                        "predecessor_thread": pred_thread, "ticket": ticket,
                        "repo": repo, "mr_state": verdict.get("state")}

        restored_state = _last_closed_from_state(pred)
        if not restored_state:
            return {"status": "refused_unrestorable_state", **purge_info,
                    "predecessor_thread": pred_thread, "ticket": ticket}

        if dry_run:
            return {"status": "dry_run", **purge_info,
                    "predecessor_thread": pred_thread, "ticket": ticket,
                    "would_restore_state": restored_state}

        # Mutate: open-copy write first, closed-copy unlink second (the
        # reverse order loses the topic on a crash between the two).
        pred.setdefault("review", {})["state"] = restored_state
        lifecycle = pred.setdefault("lifecycle", {})
        lifecycle["closed_reason"] = None
        lifecycle["resolved_at"] = None
        lifecycle.pop("terminal_notice_posted", None)
        if restored_state == "APPROVED":
            # Re-arm the merge queue's pipeline hold: stale pipeline fields
            # would let this same cycle's merge pass beat the NEXT cycle's
            # backfilled replies (e.g. a recovered approver `close`).
            for mr_obj in (pred.get("mrs") or {}).values():
                if isinstance(mr_obj, dict):
                    mr_obj.pop("pipeline_status", None)
                    mr_obj.pop("pipeline_passed_at_ms", None)
        topic_store.append_audit(
            pred, event="topic_reopened",
            withdrawn_thread=withdrawn_thread_id,
            restored_state=restored_state)
        topic_store.write_atomic(open_path, pred)
        try:
            pred_path.unlink()
        except OSError:
            pass
        topic_index.add(index_path, pred_thread, ticket)

        # The reopen has landed — a backfill/notice failure must NOT flip
        # the status to error (the trigger is one-shot; reporting the reopen
        # is what makes a partial failure recoverable).
        backfill_written = 0
        backfill_error = None
        try:
            fetch = fetch_thread_fn or _default_fetch_thread
            backfill_written = _synthesize_backfill_events(
                events_dir, pred, fetch(pred_thread))
        except Exception as exc:  # noqa: BLE001
            backfill_error = str(exc)[:200]
            log("WARN", f"topic_reopen backfill failed for {pred_thread}: "
                        f"{exc} — recover the replies via a manual re-run")
        try:
            notice_posted = _post_reopen_notice(pred)
        except Exception:  # noqa: BLE001 - a stubbed/broken notice
            notice_posted = False

        log("INFO", f"topic_reopened: {ticket} thread={pred_thread} "
                    f"restored={restored_state} backfill={backfill_written} "
                    f"notice={notice_posted}")
        result = {"status": "reopened", **purge_info,
                  "predecessor_thread": pred_thread, "ticket": ticket,
                  "restored_state": restored_state,
                  "backfill_written": backfill_written,
                  "notice_posted": notice_posted}
        if backfill_error:
            result["backfill_error"] = backfill_error
        return result
    except Exception as exc:  # noqa: BLE001 - never break the caller's cycle
        log("WARN", f"topic_reopen error for {withdrawn_thread_id}: {exc}")
        return {"status": "error", "error": str(exc)[:200]}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    cfg_dir = SCRIPT_DIR.parent / "cfg"
    ap = argparse.ArgumentParser(
        description="Reopen the topic a withdrawn duplicate root superseded.")
    ap.add_argument("--withdrawn-thread", required=True,
                    help="thread_id (om_...) of the withdrawn duplicate topic")
    ap.add_argument("--topics-dir", default=str(cfg_dir / "topics"))
    ap.add_argument("--index", default=str(cfg_dir / "open_topic_index.json"))
    ap.add_argument("--events-dir", default=str(cfg_dir / "events"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    result = reopen_superseded_predecessor(
        args.topics_dir, args.index, args.withdrawn_thread,
        events_dir=args.events_dir, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") != "error" else 1


if __name__ == "__main__":
    sys.exit(main())
