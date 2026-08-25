#!/usr/bin/env python
"""Reconcile missed websocket events by pulling recent chat history.

Fetches the last N messages from the chat via lark-cli, and for any
message that (a) isn't already present in cfg/events/ as a raw file,
and (b) isn't filtered out by the withdrawn / self-message gates,
synthesizes a listener-format event JSON. The router handles routing
on the next step.

v2 changes vs v1:
- No more `state-file` argument. Reconcile is stateless: it just
  writes into cfg/events/ and lets the router's dedupe handle
  double-delivery (router checks per-topic last_processed_event_id
  and pending queue).
- No more `omt_ → root_om_` rewrite: the listener now drops --compact
  so raw events carry real `thread_id` / `root_id`. Reconcile
  preserves those fields verbatim.
- Window: we still need SOME notion of "how far back to scan". Use
  cfg/dispatcher.state.json's `last_reconcile_ts` as the floor. If
  missing, fall back to 1 hour ago.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import subprocess_util


RECONCILE_LOOKBACK_MS = 60 * 60 * 1000  # 1 hour fallback (no --since-ts given)
MAX_RECONCILE_PAGES = 60  # backstop: 60 * 50 = 3000 messages of history


def _resolve_root_id(thread_id, cli_argv):
    """Resolve a thread's root message id (om_) from its thread id (omt_).

    The chat-messages-list bulk API returns thread_id (omt_) but not the
    root message's om_ id for thread replies. `im +threads-messages-list`
    lists a thread's messages; sorted ascending the first message is the
    thread root, whose message_id IS the root_id we key topics on (for the
    root message itself this returns its own id, which is correct).

    NOTE: the previous implementation called `im messages get`, which is
    not a real lark-cli subcommand — it silently returned "" for every
    thread reply, so reconcile marked them all "unresolved" and the
    dispatcher's reconcile floor pinned on the oldest one forever (the
    window then grew until it tripped the 30s dispatcher timeout, stalling
    the whole bot). See the reconcile floor-pin bug.

    Retries once on failure. Returns root om_ id or empty string.
    """
    for attempt in range(2):
        try:
            r = subprocess_util.hidden_run(
                cli_argv + ["im", "+threads-messages-list",
                 "--as", "user", "--thread", thread_id,
                 "--sort", "asc", "--page-size", "1", "--format", "json"],
                capture_output=True, text=True, timeout=15, encoding="utf-8")
            if r.returncode != 0:
                if attempt == 0:
                    time.sleep(1)
                    continue
                return ""
            d = json.loads(r.stdout)
            msgs = d.get("data", {}).get("messages", []) or []
            if msgs:
                return msgs[0].get("message_id", "") or ""
            return ""
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            if attempt == 0:
                time.sleep(1)
                continue
            return ""
    return ""


def parse_create_time(s):
    """lark-cli returns 'YYYY-MM-DD HH:MM' (no seconds, local tz). Return ms."""
    if not s:
        return 0
    s = str(s)
    if s.isdigit():
        return int(s)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return int(datetime.datetime.strptime(s, fmt).timestamp() * 1000)
        except ValueError:
            continue
    return 0


def _fetch_one_page(cli_argv, chat_id, page_size, page_token):
    """Fetch a single chat-history page. Returns the `data` dict or None."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
        out_path = tmp.name
    argv = cli_argv + ["im", "+chat-messages-list",
                       "--as", "user",
                       "--chat-id", chat_id,
                       "--sort", "desc",
                       "--page-size", str(page_size),
                       "--format", "json"]
    if page_token:
        argv += ["--page-token", page_token]
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            subprocess_util.hidden_run(
                argv, stdout=f, stderr=subprocess.DEVNULL,
                check=True, timeout=30)
        with open(out_path, encoding="utf-8") as f:
            return json.load(f).get("data", {}) or {}
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError,
            json.JSONDecodeError, OSError):
        return None  # transient — caller stops; next cycle retries the rest
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def fetch_history(chat_id, cutoff_ms, page_size=50, max_pages=MAX_RECONCILE_PAGES,
                  page_floor_ms=None):
    """Pull chat history newest-first, paginating down to `cutoff_ms`.

    A single chat-messages-list page returns at most `page_size` (<=50)
    messages plus a `page_token` / `has_more` for the next page. Fetching
    one page only covers the ~50 most recent messages, so a bot that was
    offline long enough for >50 messages to accumulate would miss the
    older ones. We page through (sorted desc) and stop as soon as a page's
    oldest message predates the cutoff, `has_more` is false, or `max_pages`
    is reached.

    `page_floor_ms` deepens the pagination target below `cutoff_ms`: the
    bulk list shows thread replies ONLY nested under their root message
    (see DESIGN §1.2.8), and the list is ordered by ROOT create_time — so
    a fresh reply to a days-old root is invisible unless the scan pages
    back to that root. The caller passes the oldest OPEN topic's creation
    time; replies themselves are still gated by `cutoff_ms`.

    Returns (messages, capped). `capped` is True when `max_pages` is hit
    before reaching the cutoff — coverage may be incomplete, and the caller
    surfaces that rather than silently truncating.
    """
    page_target_ms = min(cutoff_ms, page_floor_ms) if page_floor_ms else cutoff_ms
    cli_argv = subprocess_util.lark_cli_argv_prefix()
    all_msgs = []
    page_token = ""
    capped = False
    for _ in range(max_pages):
        data = _fetch_one_page(cli_argv, chat_id, page_size, page_token)
        if data is None:
            break  # transient failure — return what we have so far
        msgs = data.get("messages") or []
        all_msgs.extend(msgs)
        # Messages are desc (newest first), so the last one is the oldest
        # in the page. Once it predates the page target, every later page
        # does too — stop paginating.
        oldest_ts = parse_create_time(msgs[-1].get("create_time", "")) if msgs else 0
        if msgs and oldest_ts and oldest_ts < page_target_ms:
            break
        page_token = data.get("page_token") or ""
        if not data.get("has_more") or not page_token:
            break
    else:
        # Loop ran the full max_pages without a break → still more history
        # below the cutoff that we did not pull.
        capped = True
    return all_msgs, capped


def _existing_message_ids(events_dir):
    """Collect all message_ids currently in cfg/events/ (including
    the corrupt/ subdirectory) so we don't re-synthesize them."""
    ids = set()
    for p in events_dir.iterdir():
        if p.is_dir():
            if p.name == "corrupt":
                for c in p.iterdir():
                    if c.is_file() and c.name.endswith(".json"):
                        try:
                            with open(c, encoding="utf-8") as f:
                                ev = json.load(f)
                            mid = ev.get("message_id") or ev.get("id")
                            if mid:
                                ids.add(mid)
                        except (OSError, ValueError):
                            pass
            continue
        if not p.name.endswith(".json"):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                ev = json.load(f)
        except (OSError, ValueError):
            continue
        mid = ev.get("message_id") or ev.get("id")
        if mid:
            ids.add(mid)
    return ids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat-id", required=True)
    ap.add_argument("--events-dir", required=True)
    ap.add_argument("--lookback-ms", type=int, default=RECONCILE_LOOKBACK_MS,
                    help="How far back to scan messages (ms) when --since-ts "
                         "is not supplied")
    ap.add_argument("--since-ts", type=int, default=None,
                    help="Absolute floor (epoch ms). Scan messages newer than "
                         "this. Overrides --lookback-ms. The dispatcher passes "
                         "the persisted last_reconcile_ts so the window covers "
                         "the whole downtime since the last reconcile.")
    ap.add_argument("--page-floor-ts", type=int, default=None,
                    help="Pagination target (epoch ms), at or below --since-ts. "
                         "The dispatcher passes the oldest OPEN topic's creation "
                         "time so thread replies nested under old roots are "
                         "reachable (DESIGN §1.2.8). Default: the event floor.")
    args = ap.parse_args()

    events_dir = Path(args.events_dir)
    events_dir.mkdir(parents=True, exist_ok=True)

    now_ms = int(time.time() * 1000)
    cutoff_ms = args.since_ts if args.since_ts is not None \
        else now_ms - args.lookback_ms
    existing = _existing_message_ids(events_dir)
    cli_argv = subprocess_util.lark_cli_argv_prefix()

    msgs, capped = fetch_history(args.chat_id, cutoff_ms,
                                 page_floor_ms=args.page_floor_ts)

    # Map each thread reply -> its root message id, harvested from the bulk
    # `thread_replies` arrays (the root entry carries them). This is how we
    # resolve a reply's routing key WITHOUT `_resolve_root_id`, whose
    # `+threads-messages-list` call returns only the thread's REPLIES (not the
    # root) — so its `messages[0]` is the first reply, frequently the bot's own
    # ack, which spawned phantom topics keyed by that ack. See DESIGN §1.2.5.
    reply_to_root = {}
    for _m in msgs:
        _root = _m.get("message_id")
        for _rep in (_m.get("thread_replies") or []):
            _rep_id = _rep.get("message_id")
            if _rep_id and _root:
                reply_to_root[_rep_id] = _root

    counters = {
        "written": 0,
        "skipped_old": 0,
        "skipped_existing": 0,
        "skipped_withdrawn": 0,
        "skipped_self": 0,
        "skipped_system": 0,
    }
    withdrawn_ids = []
    state = {"max_ts": cutoff_ms}

    def _consider(m, root_id):
        """Gate one message (root or nested reply) and synthesize its event.

        `root_id` is the routing key — topic files are keyed by the ROOT
        message's om_ id. For nested replies the caller passes the enclosing
        root's message_id verbatim (no resolution needed).
        """
        ts = parse_create_time(m.get("create_time", ""))
        mid = m.get("message_id", "")
        if not mid:
            return

        if ts and ts < cutoff_ms:
            counters["skipped_old"] += 1
            return

        if mid in existing:
            counters["skipped_existing"] += 1
            return

        # Withdrawn: nothing actionable. Route has its own filter too,
        # but dropping here saves a round trip to disk.
        if m.get("deleted"):
            counters["skipped_withdrawn"] += 1
            withdrawn_ids.append(mid)
            return

        # System messages (group invites/joins like "X invited Y to the
        # group") carry msg_type "system", an omt_ thread but no resolvable
        # root, and an EMPTY sender (so the cli_ self-filter below misses
        # them). They are never routable topics — drop here, or their
        # timestamp pins the persisted floor forever (the reconcile
        # floor-pin). See DESIGN §1.2.4.
        if m.get("msg_type") == "system":
            counters["skipped_system"] += 1
            return

        sender_id = (m.get("sender") or {}).get("id", "") or ""
        if sender_id.startswith("cli_"):
            counters["skipped_self"] += 1
            return

        ev = {
            "chat_id":      args.chat_id,
            "chat_type":    "group",
            "content":      m.get("content", ""),
            "create_time":  str(ts),
            "id":           mid,
            "message_id":   mid,
            "thread_id":    root_id,
            "root_id":      root_id,
            "parent_id":    m.get("parent_id") or "",
            "message_type": m.get("msg_type", "text"),
            "mentions":     m.get("mentions") or [],
            "sender_id":    sender_id,
            "timestamp":    str(int(time.time() * 1000)),
            "type":         "im.message.receive_v1",
            "_source":      "reconcile",
        }
        out = events_dir / f"reconcile_{mid}_{ts}.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(ev, f, ensure_ascii=False, indent=2)
        existing.add(mid)  # guard against double-write within this pass
        counters["written"] += 1
        state["max_ts"] = max(state["max_ts"], ts or 0)

    scanned = 0
    for m in msgs:
        scanned += 1
        mid = m.get("message_id", "")
        if not mid:
            continue

        # Resolve the routing key for a TOP-LEVEL entry (see DESIGN §1.2.5):
        #   1. `root_id` if the API already gave one.
        #   2. `reply_to_root` map (harvested above) resolves a reply -> root.
        #   3. Otherwise it is a thread ROOT or standalone message — its OWN
        #      id is the key. We deliberately do NOT call _resolve_root_id:
        #      `+threads-messages-list` returns only a thread's REPLIES, so
        #      it would return the first reply — usually the bot's own ack —
        #      and spawn a phantom topic keyed by that ack.
        root_id = m.get("root_id") or reply_to_root.get(mid) or mid
        _consider(m, root_id)

        # Thread replies appear ONLY nested here — never as top-level
        # entries — so this walk is the only way reconcile can recover a
        # reply that arrived while the listener was down. The event floor
        # inside _consider still drops old replies. See DESIGN §1.2.8.
        for reply in (m.get("thread_replies") or []):
            scanned += 1
            _consider(reply, mid)

    print(json.dumps({
        "reconciled":          counters["written"],
        "scanned":             scanned,
        "cutoff_ms":           cutoff_ms,
        "max_ts":              state["max_ts"],
        "min_unresolved_ts":   None,
        "capped":              capped,
        "skipped_old":         counters["skipped_old"],
        "skipped_existing":    counters["skipped_existing"],
        "skipped_withdrawn":   counters["skipped_withdrawn"],
        "withdrawn_ids":       withdrawn_ids,
        "skipped_self":        counters["skipped_self"],
        "skipped_system":      counters["skipped_system"],
        "skipped_unresolved":  0,
    }))


if __name__ == "__main__":
    main()
