"""Route raw listener/reconcile events into per-topic inbox files.

Called from dispatcher.py on every poll cycle. Pure library — no
persistent state of its own; all state lives in the topic files,
inbox files, and the open-topic index.

Inbox separation: the router writes events to {thread_id}.inbox.json
(never to the topic file directly). The dispatcher's _drain_inboxes()
step then moves inbox events into the topic's events.pending[]. This
eliminates the race condition where router and topic agent both wrote
to the same topic JSON file.

Crash safety: a raw event file is only deleted after the inbox file
has been written successfully. Re-running is idempotent via
timestamp-based dedup (last_processed_ts) and inbox event_id scan.
"""

import json
from pathlib import Path

import bot_identity
import event_utils
import reply_parser
import topic_index
import topic_revive
import topic_store


# States during which classify_intent is meaningful. New-topic root messages
# are routed before any state exists; TRIAGING messages are still pre-review
# (the topic agent re-reads the root content directly), so we don't stamp
# an intent on them — the inbox flows them straight to the agent.
_REPLY_STATES = {
    "TRIAGE_DECISION", "AWAITING_APPROVAL",
    "DEV_TRIAGE", "ARBITRATION",
    "INLINE_REVIEW", "FULL_REVIEW",
    "SIMPLE_REVISION", "FULL_REVISION",
    "APPROVED",
    # Terminal, but still addressable during the post-merge cherry-pick
    # window — the topic is deliberately left out of closed/ so Gate 4a
    # doesn't drop the approver's branch answer (DESIGN §1.24).
    "MERGED",
}


# ---- Gates ----------------------------------------------------------

def _is_self_message(sender_id, bot_open_id=""):
    """Messages sent by the bot itself. Skip those — otherwise the bot
    re-ingests its own acks/review posts as new topics (phantom topics).

    The SAME bot appears under two sender shapes depending on delivery path:
    - reconcile (chat-history API) delivers the Lark app id `cli_<app>`;
    - the listener (websocket) delivers the bot *user's* open_id (`ou_...`),
      so a `cli_`-only check missed listener-delivered self-messages and let
      the bot's own ack ("…已收到 RAGE-XXXX…") spawn a phantom topic that then
      superseded the real one. Match both shapes."""
    if not sender_id:
        return False
    if sender_id.startswith("cli_"):
        return True
    return bool(bot_open_id) and sender_id == bot_open_id


def _is_withdrawn(ev):
    """Detect messages that have been retracted by their author.

    Lark's chat-messages-list marks these with `deleted: true` and
    sets content to the sentinel string `[Invalid text JSON]`.
    """
    if ev.get("deleted"):
        return True
    content = ev.get("content", "")
    if isinstance(content, str) and content == "[Invalid text JSON]":
        return True
    return False


_ACTIVITY_LOG = Path(__file__).resolve().parent.parent / "cfg" / "activity.log"


def _log_activity(level, message):
    """Best-effort activity.log line (router has no dispatcher _log)."""
    try:
        from datetime import datetime
        _ACTIVITY_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(_ACTIVITY_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [{level}] {message}\n")
    except OSError:
        pass


# ---- Entrypoint ------------------------------------------------------

def route_pending_events(events_dir, topics_dir, index_path, chat_id,
                         approver_open_ids=None, bot_open_id=""):
    """Drain raw events from events_dir into per-topic pending queues.

    Parameters
    ----------
    events_dir : Path-like
        Directory containing listener + reconcile raw event JSONs.
    topics_dir : Path-like
        cfg/topics/ — open-topic state files live here.
    index_path : Path-like
        cfg/open_topic_index.json — refreshed when new topics are
        created.
    chat_id : str
        The Lark chat this bot watches. Events from other chats are
        dropped.
    approver_open_ids : list[str] | None
        Authorized approver namelist. Used by `reply_parser.classify_intent`
        to decide whether an approver verb (`ok`/`close`/indices) is
        accepted from the sender. Pass `None` (or empty) only when no
        intent classification is desired (e.g. legacy callers).
    bot_open_id : str
        The deployed bot's own user-form open_id (`ou_...`). When set,
        a leading Lark @-mention whose `id` matches `bot_open_id` is
        rewritten to the literal token `@bot` before classification, so
        `reply_parser`'s `@bot ...` regexes match across deployments
        with different bot display names. Empty string disables the
        rewrite and auto-learn (see `bot_identity.maybe_learn_bot_open_id`)
        triggers on the next qualifying P2P DM event.

    Returns
    -------
    dict summary: routed, new_topics, skipped, corrupt, ignored_replies.
    """
    approver_open_ids = list(approver_open_ids or [])
    events_dir = Path(events_dir)
    topics_dir = Path(topics_dir)
    events_dir.mkdir(parents=True, exist_ok=True)
    topics_dir.mkdir(parents=True, exist_ok=True)
    (events_dir / "corrupt").mkdir(parents=True, exist_ok=True)

    # Load (or rebuild if stale) the open topic index. We use this to
    # map thread_id -> topic file existence AND ticket_id for supersede
    # detection (same ticket reposted on a new thread).
    index = topic_index.load_or_rebuild(topics_dir, index_path)

    # Build reverse map: ticket_id -> thread_id for supersede detection.
    ticket_to_thread = {}
    for tid, tkt in index.items():
        ticket_to_thread[tkt] = tid

    # Build a set of thread_ids that already reached a terminal state
    # (live under topics/closed/). These MUST NOT be resurrected as new
    # TRIAGING topics just because a stale raw event shows up.
    closed_dir = topics_dir / "closed"
    closed_thread_ids = set()
    if closed_dir.exists():
        for p in closed_dir.iterdir():
            if p.is_file() and p.name.endswith(".json"):
                closed_thread_ids.add(p.stem)

    routed = 0
    new_topics = 0
    skipped = 0
    corrupt = 0
    ignored_replies = 0
    closed_thread_drops = 0

    raw_files = sorted(
        [p for p in events_dir.iterdir()
         if p.is_file() and p.name.endswith(".json")],
        key=lambda p: p.stat().st_mtime,
    )

    for raw_path in raw_files:
        try:
            with open(raw_path, encoding="utf-8") as f:
                ev = json.load(f)
        except (json.JSONDecodeError, OSError):
            dest = events_dir / "corrupt" / raw_path.name
            try:
                if dest.exists():
                    dest.unlink()
                raw_path.rename(dest)
            except OSError:
                pass
            corrupt += 1
            continue

        # Normalize: listener events are nested websocket payloads;
        # reconcile events are already flat. Make both uniform.
        ev = event_utils.normalize_listener_event(ev)

        # Handle recall events: evict the recalled message from any
        # topic's pending queue rather than routing as a new event.
        if ev.get("type") == "im.message.recalled_v1":
            recalled_mid = ev.get("recalled_message_id", "")
            if recalled_mid:
                _evict_recalled_message(topics_dir, index_path, recalled_mid)
            _safe_unlink(raw_path)
            skipped += 1
            continue

        # Gate 1: chat filter
        if ev.get("chat_id") != chat_id:
            _safe_unlink(raw_path)
            skipped += 1
            continue

        # Gate 2: self-message filter (matches both cli_ app id and the
        # bot user's open_id — see _is_self_message)
        sender_id = ev.get("sender_id", "") or ""
        if _is_self_message(sender_id, bot_open_id):
            _safe_unlink(raw_path)
            skipped += 1
            continue

        # Gate 3: withdrawn-message filter
        if _is_withdrawn(ev):
            _safe_unlink(raw_path)
            skipped += 1
            continue

        thread_id = event_utils.get_thread_id_from_event(ev)
        msg_id = ev.get("message_id", "") or ev.get("id", "")
        # Strict invariant: topic files are keyed by the root message's
        # om_ id. Anything else here (empty, omt_, or some other prefix)
        # would create a phantom topic, so drop instead. Normalization
        # and reconcile already enforce this; this is defense in depth.
        if not thread_id or not msg_id or not thread_id.startswith("om_"):
            _safe_unlink(raw_path)
            skipped += 1
            continue

        # Gate 4: find or create the topic
        topic_path = topics_dir / f"{thread_id}.json"
        is_new_topic = False
        # Gate 4a runs on its own: a revive rewrites `index` / `topic_path`,
        # so the create-topic block below re-tests and falls through.
        if thread_id not in index or not topic_path.exists():
            # Gate 4a: if this thread already reached terminal state,
            # drop the stale event rather than re-opening the topic.
            # WARN so the drop is visible — a silent unlink here hid the
            # RAGE-20032 dropped-`ok` incident for two hours (DESIGN §1.2.9).
            if thread_id in closed_thread_ids:
                # …unless the close was an ack-time "go fix this and come
                # back" (missing MR / missing version_3rd bump). Dropping
                # the answer to a question the bot itself asked is a dead
                # end, so revive the topic and let ack re-run (§1.3.7).
                revived, revive_reason = topic_revive.try_revive(
                    topics_dir, index_path, thread_id)
                if revived:
                    closed_thread_ids.discard(thread_id)
                    index[thread_id] = topic_store.read(
                        topic_path).get("identity", {}).get("ticket_id", "")
                    _log_activity("INFO", f"closed_topic_revived "
                                          f"thread={thread_id} msg={msg_id}")
                else:
                    _log_activity("WARN", f"closed_topic_reply_dropped "
                                          f"thread={thread_id} msg={msg_id} "
                                          f"revive={revive_reason}")
                    _safe_unlink(raw_path)
                    skipped += 1
                    closed_thread_drops += 1
                    continue
        if thread_id not in index or not topic_path.exists():
            content = event_utils.extract_text_content(ev)
            ticket_id = event_utils.extract_ticket_id(content)
            if not ticket_id:
                # Unrelated chatter in an unknown thread — drop
                _safe_unlink(raw_path)
                skipped += 1
                continue

            # Supersede check: if this ticket already has an open topic
            # under a different thread_id, close the old one. This
            # handles the case where a developer withdraws a topic and
            # reposts the same ticket quickly (before the next poll).
            #
            # use the lock-aware mutator: if the old topic
            # is mid-agent (fresh lock), skip the close this cycle and
            # let the janitor / next cycle retry. Closing a topic that
            # another process holds corrupts audit trails and races with
            # write_atomic in the agent.
            old_thread = ticket_to_thread.get(ticket_id)
            if old_thread and old_thread != thread_id:
                reason = topic_store.SUPERSEDE_REASON_FMT.format(
                    thread_id=thread_id)
                closed_ok = topic_store.close_topic(
                    topics_dir, index_path, old_thread,
                    reason=reason,
                    require_unlocked=True)
                if closed_ok:
                    index.pop(old_thread, None)
                    ticket_to_thread.pop(ticket_id, None)
                else:
                    # queue for janitor retry. dispatcher
                    # imports router; avoid a cycle by reaching back
                    # through the skill-level helper.
                    try:
                        import dispatcher  # noqa: PLC0415 — lazy to dodge cycle
                        dispatcher.record_pending_supersede(
                            old_thread, thread_id, reason)
                    except Exception:  # noqa: BLE001
                        pass  # ledger is advisory; next router call retries

            topic_path = topic_store.create_topic(
                topics_dir, thread_id, msg_id, ticket_id,
                sender_id, chat_id, initial_state="TRIAGING")
            index[thread_id] = ticket_id
            ticket_to_thread[ticket_id] = thread_id
            new_topics += 1
            is_new_topic = True

        # Gate 5: dedupe — timestamp-based + inbox event_id scan
        try:
            topic = topic_store.read(topic_path)
        except (OSError, ValueError):
            # Topic file vanished or unreadable — leave raw file for next cycle
            skipped += 1
            continue

        event_id = event_utils.derive_event_id(raw_path.name, msg_id)

        # Compute event's message create_time for the inbox record
        # (observability + ordering). We no longer gate on last_processed_ts —
        # Replaced the strict `<=` ts gate with an event_id ring
        # buffer because out-of-order delivery (reconcile backfills older
        # messages) was dropping legitimate late events.
        event_ts_ms = _event_create_time_ms(ev)
        if event_ts_ms is None:
            try:
                event_ts_ms = int(raw_path.stat().st_mtime * 1000)
            except OSError:
                event_ts_ms = topic_store.now_ms()

        # Dedup via the per-topic recent-event ring (plus legacy scalar).
        # Replaces the timestamp gate; survives out-of-order arrival.
        if topic_store.is_recent_event(topic, event_id):
            _safe_unlink(raw_path)
            skipped += 1
            continue

        # Inbox-level dedup: check if event_id already queued in inbox
        inbox_path = topics_dir / f"{thread_id}.inbox.json"
        try:
            with open(inbox_path, encoding="utf-8") as f:
                inbox = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            inbox = []

        if any(p.get("event_id") == event_id for p in inbox):
            _safe_unlink(raw_path)
            skipped += 1
            continue

        # Append to inbox file (NOT the topic file). Stamp phase_at_arrival +
        # state_at_arrival so downstream handlers/agents can skip events whose
        # tag no longer matches the current topic. Tags are
        # observed-only here — skip logic lives in Phase B wiring, not here.
        _review = topic.get("review") or {}
        state = _review.get("state")
        content_text = event_utils.extract_text_content(ev)
        mentions = ev.get("mentions") or []

        # Auto-learn the bot's open_id from an operator P2P DM if we don't
        # have one yet (idempotent / no-op once set). Then normalize a
        # leading bot @-mention to literal `@bot` so reply_parser's
        # `^@bot(\s|$)` regex matches across deployments with different
        # bot display names. See bot_identity.py + DESIGN §1.2.2.
        if not bot_open_id:
            learned = bot_identity.maybe_learn_bot_open_id(
                ev, approver_open_ids,
                Path(__file__).resolve().parent.parent.parent.parent.parent,
                bot_open_id)
            if learned and learned != bot_open_id:
                bot_open_id = learned
        if bot_open_id:
            content_text = bot_identity.normalize_bot_mention(
                content_text, mentions, bot_open_id)

        entry = {
            "event_id": event_id,
            "source": ev.get("_source") or "listener",
            "received_at": event_ts_ms,
            "sender_id": sender_id,
            "message_id": msg_id,
            "content": content_text,
            "mentions": mentions,
            "raw_path": str(raw_path),
            "classified_as": None,
            "phase_at_arrival": _review.get("review_phase"),
            "state_at_arrival": state,
        }

        # Reply-intent classification — only for thread replies on a topic
        # that's already past the initial triage. New-topic root messages
        # and TRIAGING-state messages flow straight through (the topic
        # agent reads the root content directly). For everything else,
        # classify and either drop or stamp role/intent/indices so
        # downstream consumers (mechanical handler, topic agent) read
        # the routing decision instead of re-parsing content.
        if (not is_new_topic and state in _REPLY_STATES
                and approver_open_ids):
            developer_id = (topic.get("identity") or {}).get(
                "creator_open_id") or None
            classification = reply_parser.classify_intent(
                content_text, sender_id, state, approver_open_ids,
                developer_id=developer_id,
                triage=_review.get("triage"))
            role = classification.get("role")
            if role == "ignored":
                topic_store.append_audit(topic,
                    event="reply_intent_ignored",
                    triggered_by_event_id=event_id,
                    sender_id=sender_id,
                    state=state,
                    content_preview=(content_text or "")[:120],
                )
                topic_store.write_atomic(topic_path, topic)
                _safe_unlink(raw_path)
                ignored_replies += 1
                continue
            entry["role"] = role
            entry["intent"] = classification.get("intent")
            indices = classification.get("indices") or []
            if indices:
                entry["indices"] = indices
            # `exclude=True` may carry no indices (the bare `all` case),
            # so stamp it independently of the indices truthy-check.
            # Same for dev_triage's `none` (reject all — also indexless).
            if classification.get("intent") == "revision":
                entry["exclude"] = bool(classification.get("exclude"))
            elif classification.get("intent") == "dev_triage":
                entry["exclude"] = bool(classification.get("exclude"))
                entry["none"] = bool(classification.get("none"))
                # Optional free-text justification typed after the indices
                # (DESIGN §1.23.8). Stamped only when non-empty so existing
                # inbox entries keep their shape.
                reason = (classification.get("reason") or "").strip()
                if reason:
                    entry["reason"] = reason
            elif classification.get("intent") == "cherrypick":
                # Raw tokens (`p1`) — resolved against the offered mapping
                # at drain time, not here (DESIGN §1.24).
                entry["branches"] = list(classification.get("branches") or [])

        inbox.append(entry)
        topic_store.write_atomic(inbox_path, inbox)
        _safe_unlink(raw_path)
        routed += 1

    # Persist the (possibly updated) index.
    if index:
        topic_index.save(index_path, index)

    return {
        "routed": routed,
        "new_topics": new_topics,
        "skipped": skipped,
        "corrupt": corrupt,
        "ignored_replies": ignored_replies,
        "closed_thread_drops": closed_thread_drops,
    }


def _event_create_time_ms(ev):
    """Extract the message's create_time as ms int, or None if not available.

    Both reconcile and normalize_listener_event write create_time as a
    string of ms. This is the message's ORIGINAL timestamp — stable across
    repeated reconcile runs — so it's the correct basis for dedup.
    """
    if not isinstance(ev, dict):
        return None
    raw = ev.get("create_time")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        pass
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _safe_unlink(path):
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _evict_recalled_message(topics_dir, index_path, recalled_message_id):
    """Handle a recalled (withdrawn) message arriving via the listener.

    Two actions:
    1. If the recalled message is an open topic's ROOT, close the topic —
       the developer deleted the whole thread and may repost. This is the
       real-time source for withdrawn-root-close now that reconcile only runs
       on cold start / listener restart (DESIGN §1.2.7); previously it depended
       on `dispatcher._close_withdrawn_topics` cross-referencing reconcile's
       per-cycle `withdrawn_ids`.
    2. Otherwise, evict the recalled message from any topic's pending queue so
       no agent wastes a cycle (or Opus tokens) on a withdrawn message.
    """
    for p in topic_store.iter_topic_files(topics_dir):
        try:
            topic = topic_store.read(p)
        except (OSError, ValueError):
            continue
        if topic.get("root_message_id") == recalled_message_id:
            thread_id = topic.get("thread_id", p.stem)
            topic_store.close_topic(
                topics_dir, index_path, thread_id,
                reason="root message withdrawn (recall event)")
            # The reopen-superseded-predecessor hook lives ONLY on the
            # reconcile path (dispatcher._close_withdrawn_topics) — calling
            # it here would race this pass's in-memory index snapshots
            # (DESIGN §1.2.9). Recalls demonstrably never arrive today; if
            # this WARN ever shows up, that assumption broke and the hook
            # must be revisited.
            _log_activity("WARN", f"recall_path_closed_root_without_reopen "
                                  f"thread={thread_id}")
            continue
        pending = (topic.get("events") or {}).get("pending") or []
        original_len = len(pending)
        pending = [e for e in pending
                   if e.get("message_id") != recalled_message_id]
        if len(pending) < original_len:
            topic.setdefault("events", {})["pending"] = pending
            topic_store.append_audit(topic,
                event="recalled_message_evicted",
                message_id=recalled_message_id)
            topic_store.write_atomic(p, topic)
