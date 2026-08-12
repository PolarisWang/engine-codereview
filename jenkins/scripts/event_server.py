#!/usr/bin/env python3
"""
event_server.py — real-time Feishu event-subscription service (reply-only bot).

This is the SINGLE real-time message intake for the topic group. It receives
Feishu message events and routes them:

  - New topic (no parent_id) containing a Jira URL  -> orchestrate.py run  (review)
  - @bot / topic reply (has parent_id)              -> orchestrate.py interact (reply)
  - anything else                                    -> ignored

It drives the existing Python review logic directly (via orchestrate.py), so it
can reply in the correct topic thread in real time (seconds), unlike the
historical bot.py which pushed to Jenkins (and caused the dual-trigger problem).

Jenkins' cron scan is kept only as a fallback; the `pipeline_state` processed /
terminal markers deduplicate so a message is handled once no matter which link
sees it first.

Security: verifies the Feishu verification token (and supports encrypted events
via Encrypt Key on the /webhook/event route).

Usage:
    python3 event_server.py            # read config from env / config.yaml, serve
    python3 event_server.py --config <path> --state-file <path>
"""

import argparse
import json
import os
import subprocess
import sys

# Ensure scripts dir is importable (for config helpers).
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

try:
    from flask import Flask, request, jsonify
    _HAS_FLASK = True
except ImportError:
    _HAS_FLASK = False

import common


# ── Config ───────────────────────────────────────────────────────────────────

def _env(name, default=""):
    return os.environ.get(name, default)


def _config():
    """Return config.yaml dict (cached)."""
    return common.load_config()


def _state_file():
    # 方案B: 统一从 config.yaml paths.state_file / env PIPELINE_STATE_FILE 解析
    return common.c_state_file()


def _workspace():
    # arch-D shared workspace: the interaction layer and the Jenkins executor must
    # read/write the SAME workspace so a topic's checkout + result files are
    # consistent across processes. Default to paths.workspace (or REVIEW_WORKSPACE).
    return common.c_workspace()


app = None


# ── URL verification / secure dispatch (webhook mode only) ───────────────────
# ws (long-connection) mode does NOT need Flask. To keep the module importable in
# environments without flask (e.g. the CI agent container), we only build the
# Flask app + routes when flask is actually installed.

if _HAS_FLASK:
    app = Flask(__name__)

    @app.route("/webhook/event", methods=["POST"])
    def feishu_event():
        data = request.get_json(silent=True) or {}

        # Feishu sends a URL-verify challenge on first setup.
        if data.get("type") == "url_verify":
            if request.args.get("challenge"):
                return jsonify({"challenge": request.args["challenge"]})
            return jsonify({"challenge": data.get("challenge", "")})

        # Encrypted payload (Encrypt Key configured) — decrypt if present.
        if data.get("encrypt"):
            data = _decrypt(data["encrypt"])
            if data is None:
                return jsonify({"code": 0})

        # Verify request token (Feishu event v1 includes a 'token').
        expected = _env("FEISHU_VERIFICATION_TOKEN", "") or _config().get("event", {}).get("verification_token", "")
        if expected and data.get("token") != expected:
            return jsonify({"error": "invalid token"}), 403

        event = data.get("event", {})
        event_type = event.get("type", data.get("type", ""))
        if event_type == "im.message.receive_v1":
            _handle_message_event(event)

        # Acknowledge immediately (Feishu requires a 200 within 3s).
        return jsonify({"code": 0})


def _decrypt(encrypted_b64):
    """Decrypt a Feishu encrypted event body (AES-CBC, key from Encrypt Key).

    Returns the decrypted JSON dict, or None on any failure (caller acknowledges
    without acting). This is best-effort; if the app doesn't use Encrypt Key this
    is never called.
    """
    import base64
    from Crypto.Cipher import AES  # pycryptodome (optional dep)
    try:
        secret = _env("FEISHU_ENCRYPT_KEY", "") or _config().get("event", {}).get("encrypt_key", "")
        if not secret:
            return None
        key = base64.b64decode(secret)
        aes = AES.new(key, AES.MODE_CBC, key[:16])
        dec = aes.decrypt(base64.b64decode(encrypted_b64))
        # Strip PKCS#7 padding
        pad = dec[-1]
        if isinstance(pad, int) and 1 <= pad <= 16:
            dec = dec[:-pad]
        return json.loads(dec.decode("utf-8"))
    except Exception as e:
        print(f"[event] decrypt failed: {e}", file=sys.stderr)
        return None


# ── Message routing ──────────────────────────────────────────────────────────

def _text_of(message):
    """Extract plain text from a Feishu message body (text/post)."""
    body = message.get("body", {})
    content = body.get("content", "")
    try:
        cd = json.loads(content) if isinstance(content, str) else content
    except json.JSONDecodeError:
        return str(content) if content else ""
    if message.get("message_type") == "text":
        return (cd or {}).get("text", "")
    if message.get("message_type") == "post":
        parts = []
        paragraphs = []
        if isinstance(cd, dict):
            for loc in ("zh_cn", "en_us"):
                pc = cd.get(loc)
                if isinstance(pc, dict) and pc.get("content"):
                    paragraphs = pc["content"]
                    break
            if not paragraphs and isinstance(cd.get("content"), list):
                paragraphs = cd["content"]
            elif not paragraphs and isinstance(cd, dict) and isinstance(cd.get("content"), list):
                paragraphs = cd["content"]
        for para in paragraphs:
            for seg in para:
                if seg.get("tag") == "text":
                    parts.append(seg.get("text", ""))
                elif seg.get("tag") == "a":
                    parts.append(seg.get("href", ""))
        return "".join(parts)
    return ""


def _is_jira_topic(text):
    return bool(common.JIRA_URL_PATTERN.search(text or "") or common.MR_URL_PATTERN.search(text or ""))


def _run_orchestrate(args_list):
    """Run orchestrate.py as a subprocess (keeps Flask responsive)."""
    cmd = [sys.executable, os.path.join(SCRIPTS_DIR, "orchestrate.py")] + args_list
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            print(f"[event] orchestrate rc={r.returncode}: {r.stderr[:500]}", file=sys.stderr)
        return r.returncode
    except subprocess.TimeoutExpired:
        print("[event] orchestrate timed out", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"[event] orchestrate error: {e}", file=sys.stderr)
        return 1


def _handle_message_event(event):
    message = event.get("message", {})
    chat_type = message.get("chat_type", "")
    if chat_type != "group":
        return  # only the topic group
    msg_id = message.get("message_id", "")
    parent_id = message.get("parent_id") or ""   # None/'' -> topic starter
    text = _text_of(message)
    sender = event.get("sender", {}) or {}
    sender_id = (sender.get("sender_id") or {}).get("user_id", "") \
        if isinstance(sender.get("sender_id"), dict) else sender.get("sender_id", "")
    mentions = []   # webhook dict-shape doesn't carry parsed mentions here
    _route(msg_id, parent_id, text, sender_id, mentions)


def on_p2_card_action_trigger(data):
    """WS handler for card.action.trigger (user clicked a button on an interactive
    card). Maps to the same interaction path as an @reply: actor = operator.open_id,
    value={action, topic}. Returns a P2CardActionTriggerResponse (ack)."""
    try:
        ev = getattr(data, "event", None)          # P2CardActionTriggerData
        operator = getattr(ev, "operator", None)
        action = getattr(ev, "action", None)
        actor = (getattr(operator, "open_id", "") or "").strip()
        value = (getattr(action, "value", None) or {}) or {}
        act = str(value.get("action", ""))
        topic = str(value.get("topic", ""))
        print(f"[card] button click action={act} topic={topic} actor={actor}", flush=True)
        if act and topic and actor:
            # 防抖动: 连点/飞书重发同一按钮 -> 丢弃重复(同一窗口内只执行一次)。
            if not _claim_action(act, topic, actor):
                print(f"[card] debounced duplicate click action={act} topic={topic}", flush=True)
            else:
                base = ["--pipeline-state-file", _state_file(), "--workspace", _workspace()]
                _run_orchestrate(["action", "--key", topic, "--action", act,
                                  "--sender-id", actor] + base)
        else:
            print(f"[card] incomplete button payload: action={act!r} topic={topic!r} actor={actor!r}",
                  file=sys.stderr)
    except Exception as e:
        print(f"[card] handler error: {e}", file=sys.stderr)
    try:
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
        return P2CardActionTriggerResponse()
    except Exception:
        return None


# ── Long-connection (WebSocket) mode — no public callback URL needed ─────────
#
# Feishu's open platform supports receiving events over a persistent WebSocket
# ("长连接") initiated FROM our side, so the server does NOT need a public IP /
# callback URL. We register a handler for im.message.receive_v1 and normalize the
# lark-oapi message dataclass into the same routing used by the webhook mode.

def _sd(value):
    """Return a truthy string (strip) or empty. Normalizes None/falsy."""
    return "" if value is None else str(value).strip()


def _sender_id_of(sender):
    """Extract the replier's Feishu user id from a lark-oapi `Sender` object OR a
    dict. Handles the several shapes Feishu can send (open_id is what we store on
    topics as sender_id, so prefer it / the `id` field):
      - Sender dataclass: sender.id (+ friendlier sender.id_type)
      - dict: sender['id'] / sender['sender_id']['user_id'] / sender['owner_id']
    Returns a plain str (possibly empty)."""
    if sender is None:
        return ""
    # Object (lark-oapi Sender / any attr-based)
    if not isinstance(sender, dict):
        raw = getattr(sender, "id", None) or ""
        idt = getattr(sender, "id_type", "") or ""
        if raw:
            s = _sd(raw)
            print(f"[event] sender: id={s!r} id_type={_sd(idt)!r} (attr)", flush=True)
            return s
        # Some events nest the real id under .sender_id(.id)
        nested = getattr(sender, "sender_id", None)
        if nested is not None:
            s = _sd(getattr(nested, "id", None) or getattr(nested, "open_id", ""))
            if s:
                print(f"[event] sender: nested.id={s!r}", flush=True)
                return s
        print(f"[event] sender object has no extractable id; type={type(sender).__name__} "
              f"attrs={sorted(a for a in dir(sender) if not a.startswith('_'))}", flush=True)
        return ""
    # Dict shape (webhook-style / raw JSON event)
    s = ""
    sid = sender.get("id") or sender.get("open_id") or ""
    if not sid and isinstance(sender.get("sender_id"), dict):
        sid = sender["sender_id"].get("user_id") or sender["sender_id"].get("open_id") or ""
    if not sid:
        sid = sender.get("user_id") or sender.get("owner_id") or ""
    s = _sd(sid)
    if not s:
        print(f"[event] sender dict has no id; keys={list(sender.keys())}", flush=True)
    else:
        print(f"[event] sender: id={s!r} (dict)", flush=True)
    return s


def _lark_message_to_route(lark_message):
    """Map a lark-oapi ws P2ImMessageReceiveV1.message onto the routing fields.
    Returns (msg_id, parent_id, chat_type, text, sender_id, mentions) or None if
    not group/text. `mentions` is a list of {key,id,id_type,name} for any Feishu
    @-mentions in the message (used to require the bot be @-ed before replying)."""
    try:
        msg_type = getattr(lark_message, "message_type", "") or ""
        chat_type = getattr(lark_message, "chat_type", "") or ""
        msg_id = getattr(lark_message, "message_id", "") or ""
        parent_id = getattr(lark_message, "parent_id", "") or ""
        raw_content = getattr(lark_message, "content", "") or ""
        sender = getattr(lark_message, "sender", None)
        sender_id = _sender_id_of(sender)
        if not sender_id:
            print("[event] WARN: no sender_id extracted from message (guarded actions "
                  "like re_review/apply/close will be denied)", flush=True)
        text = ""
        mentions = []
        if msg_type == "text":
            cd = json.loads(raw_content) if isinstance(raw_content, str) else (raw_content or {})
            text = (cd or {}).get("text", "")
            # Feishu text mentions: [{"key":"@_user_1","id":{"open_id":"ou_.."},"name":".."}]
            for m in (cd or {}).get("mentions") or []:
                mid = (m.get("id") or {}) or {}
                mentions.append({
                    "key": m.get("key", ""), "name": m.get("name", ""),
                    "open_id": mid.get("open_id", ""), "id": mid,
                })
        elif msg_type == "post":
            text = _post_text(raw_content)
    except Exception as e:
        print(f"[event] parse lark message failed: {e}", file=sys.stderr)
        return None
    return msg_id, parent_id, chat_type, text, sender_id, mentions


def _post_text(raw_content):
    """Extract text from a post content JSON string."""
    try:
        cd = json.loads(raw_content) if isinstance(raw_content, str) else (raw_content or {})
        para = (cd or {}).get("content") or []
        parts = []
        for p in para:
            for seg in p:
                if seg.get("tag") == "text":
                    parts.append(seg.get("text", ""))
                elif seg.get("tag") == "a":
                    parts.append(seg.get("href", ""))
        return "".join(parts)
    except (json.JSONDecodeError, TypeError):
        return ""


def on_p2_im_message_receive(data):
    """WS event handler for im.message.receive_v1 (receives a P2ImMessageReceiveV1)."""
    try:
        event = getattr(data, "event", None)
        message = _p2_message_payload(data)
        if not message:
            print("[event] ws event missing message", file=sys.stderr)
            return
        # The replier's identity may sit on the EVENT (.sender) rather than on the
        # message — check the event first, then fall back to message.sender.
        sender = getattr(event, "sender", None) or getattr(message, "sender", None)
        sender_id = _sender_id_of(sender)
        routed = _lark_message_to_route(message)
        if not routed:
            return
        msg_id, parent_id, chat_type, text, _sender2, mentions = routed
        sender_id = sender_id or _sender2
        if chat_type != "group":
            return
        _route(msg_id, parent_id, text, sender_id, mentions)
    except Exception as e:
        print(f"[event] ws handler error: {e}", file=sys.stderr)


def _p2_message_payload(data):
    """Extract the raw message dict-like from a P2ImMessageReceiveV1.

    lark-oapi wraps the payload; `data.event.message` is the message object. We
    wrap it so _lark_message_to_route can use getattr on it unchanged."""
    try:
        event = getattr(data, "event", None)
        message = getattr(event, "message", None)
        return message
    except Exception:
        return None


import threading
from collections import deque

# per-topic serial lock (defense-in-depth layer 5): same topic's interact handled one at a time.
_T_LOCKS = {}
_T_LOCKS_GUARD = threading.Lock()
LOCK_TIMEOUT = 120  # seconds

# ── Dedup (方案 B): Feishu redelivers an EVENT if the ack is slow; a repeated
# msg_id must not spawn a second interact (duplicate cards in chat_history).
# Bounded in-memory set of recently-seen message ids. Reset on process restart is
# acceptable (retries beyond a restart are caught by nothing, but the ack fix in
# _route (方案 A) makes slow-ack redelivery the exception, not the norm).
_DEDUP_MAX = 200
_SEEN_MSG_IDS = deque(maxlen=_DEDUP_MAX)
_SEEN_GUARD = threading.Lock()


def _claim_msg_id(msg_id):
    """Return True (and record) if this msg_id is NEW; False if it was already
    seen recently (dedup — do not process again). """
    if not msg_id:
        return True  # no id -> cannot dedup; process
    with _SEEN_GUARD:
        if msg_id in _SEEN_MSG_IDS:
            return False
        _SEEN_MSG_IDS.append(msg_id)
        return True


# ── 防抖动: card/button clicks (Feishu may redeliver a card.action.trigger, and a
#  user may double-click a button). The same (action, topic, actor) within the window
#  is a duplicate —— drop it, so we don't enqueue 优化/re_review/apply_patch twice.
_ACTION_DEBOUNCE_SEC = 10
_ACTION_LAST = {}
_ACTION_GUARD = threading.Lock()


def _claim_action(action, topic, actor):
    """Return True if this (action, topic, actor) is NOT a repeat within the debounce
    window; False if it just ran (debounce — skip). Uses wall clock; in-process only
    (a fresh bot process resets the window, acceptable for jitter)."""
    try:
        import time as _t
        now = _t.monotonic()
        key = (action, topic, actor)
        with _ACTION_GUARD:
            last = _ACTION_LAST.get(key)
            _ACTION_LAST[key] = now
            if last is not None and (now - last) < _ACTION_DEBOUNCE_SEC:
                return False
            # bound the dict
            if len(_ACTION_LAST) > 5000:
                _ACTION_LAST.clear()
            return True
    except Exception:
        return True  # never fail-closed on debounce


# Bound concurrent backend interact workers (方案 A): a flood of replies spawns
# one background thread each; cap it so we don't exhaust threads/resources.
MAX_INTERACT_WORKERS = 16
_SPAWN_SEM = threading.BoundedSemaphore(MAX_INTERACT_WORKERS)


def _topic_lock(topic_key):
    with _T_LOCKS_GUARD:
        if topic_key not in _T_LOCKS:
            _T_LOCKS[topic_key] = threading.Lock()
        return _T_LOCKS[topic_key]


def _resolve_topic_key(state_file, parent_id):
    """Map a reply's parent_message_id to the topic's real key.

    Users naturally reply to the review RESULT CARD (a thread reply whose
    message_id == topic.render_msg_id), not to the topic starter. In that case
    parent_id is the card's id, which is NOT a valid topic key — hitting
    interact --key <card_id> silently no-ops because the topic is never found.
    This resolves such ids to the owning topic's key (message_id), or returns
    None when e.g. parent_id already is a valid key or references nothing known."""
    try:
        import pipeline_state as _ps
        topics = _ps.list_topics(state_file)
        for t in topics:
            if t.get("render_msg_id") == parent_id or t.get("message_id") == parent_id:
                return t.get("message_id") or t.get("render_msg_id")
        return None
    except Exception as e:
        print(f"[event] _resolve_topic_key err: {e}", file=sys.stderr)
        return None


def _spawn_interact(topic_key, reply_text, reply_msg_id, sender_id):
    """Run an interact for a reply in a BACKGROUND thread (方案 A).

    The ws message handler must return quickly so the lark SDK can ack Feishu
    before its timeout; if we run the (blocking, up-to-1800s) interact inline,
    Feishu retries the same EVENT -> duplicate cards in chat_history. We instead
    enqueue the interact here and return immediately; the per-topic lock is taken
    inside the worker so same-topic replies stay serialized and the handler never
    blocks. A semaphore bounds concurrent workers to avoid thread exhaustion if
    the group floods.
    """
    _SPAWN_SEM.acquire()
    def _work():
        try:
            lock = _topic_lock(topic_key)
            acquired = lock.acquire(timeout=LOCK_TIMEOUT)
            try:
                if not acquired:
                    print(f"[event] topic {topic_key} busy, skip reply {reply_msg_id}", file=sys.stderr)
                    return
                base = ["--pipeline-state-file", _state_file(), "--workspace", _workspace()]
                _run_orchestrate(["interact", "--key", topic_key, "--reply", reply_text[:500],
                                  "--reply-msg-id", reply_msg_id, "--sender-id", sender_id] + base)
            finally:
                if acquired:
                    lock.release()
        finally:
            _SPAWN_SEM.release()
    threading.Thread(target=_work, daemon=True).start()


def _is_bot_directed(text, mentions):
    """True if a threaded reply is directed at the bot, so the bot only replies when
    addressed and stays silent when users chat among themselves.

    Feishu renders ANY @-mention as an `@_user_N` placeholder in the message text (not
    the bot's display name), so in a review-card thread a reply starting with '@' IS
    the user addressing someone (usually the bot or `@优化`-style). To keep the common
    `@机器人 …` case working while still ignoring plain non-@ small-talk, we treat a
    leading '@' as bot-directed. We ALSO accept:
      - '@' + a known command keyword (@优化/@MR单/@确认/...)
      - mentions matching configured FEISHU_BOT_NAME / FEISHU_BOT_OPEN_ID
      - text starting with the configured bot name
    A reply with NO leading '@' (flat chat, no mention) is NOT handled (no reply)."""
    lo = (text or "").strip().lower()
    if lo.startswith("@"):
        return True            # @-mention (Feishu shows any @ as @_user_N) -> directed
    bot_name = (common.c_feishu_bot_name() or "").strip().lstrip("@").lower()
    bot_open_id = (common.c_feishu_bot_open_id() or "").strip()
    for m in (mentions or []):
        name = (m.get("name") or "").lstrip("@").lower()
        if bot_name and name == bot_name:
            return True
        if bot_open_id and (m.get("open_id") or "").lower() == bot_open_id.lower():
            return True
    if bot_name and lo.lstrip("@").startswith(bot_name):
        return True
    # a command keyword without '@' (e.g. plain "优化")? No — require @ (per user: no @ = no reply)
    return False


def _route(msg_id, parent_id, text, sender_id="", mentions=None):
    """Shared single-link routing.

    - New topic (no parent, has Jira URL): NOT reviewed here. The event server
      runs without Jira/GitLab credentials, so an inline review would always fail;
      the Jenkins scanner cron picks up new group topics and reviews with full
      credentials. This keeps review ownership with Jenkins (single-link, no cred
      duplication) and lets the event server act purely as the message/interact hub.
    - Reply / @bot on an existing topic: route to interact (only needs FEISHU +
      ANTHROPIC env, which the event server has). Same-topic replies are
      serialized via a per-topic lock, taken in the background worker.

    @-gate: a threaded reply only spawns interact if it's DIRECTED at the bot
    (_is_bot_directed) — so a user who chats with others inside the review card's
    thread (without @-ing the bot) is NOT spammed with a bot reply.
    """
    if not parent_id:
        if _is_jira_topic(text):
            # Review is owned by the Jenkins scanner; acknowledge here.
            print(f"[event] NEW TOPIC {msg_id}: {text[:80]} (handing review to scanner)", flush=True)
        else:
            print(f"[event] ignore topic (no Jira URL) {msg_id}", flush=True)
    else:
        print(f"[event] REPLY {msg_id} to parent {parent_id}: {text[:80]} sender={sender_id}", flush=True)
        # 防干扰: 没 @ bot 的普通闲聊(即使落在 card 线程下) 不回复。
        if not _is_bot_directed(text, mentions):
            print(f"[event] ignore reply not directed at bot {msg_id}", flush=True)
            return
        # 方案 B: Feishu redelivers a slow/unacked EVENT as the SAME msg_id. Dedup
        # here so a retry can't spawn a second interact.
        if not _claim_msg_id(msg_id):
            print(f"[event] DEDUP SKIP {msg_id} (already processed)", flush=True)
            return
        # The parent may be the review result CARD (render_msg_id) rather than the
        # topic starter; resolve it to the real topic key so interact() finds the
        # topic. Without this, a reply on the card silently no-ops.
        topic_key = _resolve_topic_key(_state_file(), parent_id) or parent_id
        _spawn_interact(topic_key, text, msg_id, sender_id)


class CardAwareClient:
    """lark-oapi WS client subclass that does NOT drop CARD-frame messages.

    The stock lark-oapi ws client ignores MessageType.CARD (button clicks on
    interactive cards). We override _handle_data_frame so CARD payloads are
    surfaced (callback to _on_card) instead of dropped. EVENT handling is fully
    preserved (delegated to the real implementation). phase-A1: we first only log
    the raw payload so we can learn the exact schema from a real click before
    parsing actions.
    """
    def __init__(self, ws_client):
        self._wsc = ws_client
        self._orig = ws_client._handle_data_frame   # keep the REAL handler (bound)
        self._on_card = None   # optional callback: async def _on_card(payload_bytes)

    async def _handle_data_frame(self, frame):
        import base64 as _b64, time as _time, http as _http
        from lark_oapi.ws.const import HEADER_TYPE
        from lark_oapi.ws.enum import MessageType
        from lark_oapi.core.json import JSON
        from lark_oapi.core.const import UTF_8
        from lark_oapi.ws.model import Response

        hs = frame.headers
        type_ = ""
        for h in hs:
            if h.key == HEADER_TYPE:
                type_ = h.value
                break
        try:
            mt = MessageType(type_)
        except Exception:
            mt = None

        if mt and mt.name == "CARD":
            # Ack so Feishu does not retry; surface raw payload for the phase-A1 probe.
            pl = frame.payload
            print(f"[card] RAW CARD payload ({len(pl) if pl else 0} bytes): {pl!r}", flush=True)
            cb = getattr(self, "_on_card", None)
            if cb is not None and pl:
                try:
                    await cb(pl)
                except Exception as e:
                    print(f"[card] on_card handler error: {e}", file=sys.stderr)
            resp = Response(code=_http.HTTPStatus.OK)
            frame.payload = JSON.marshal(resp).encode(UTF_8)
            await self._wsc._write_message(frame.SerializeToString())
            return
        # Non-CARD: preserve the original EVENT/other handling (delegate to the
        # REAL handler captured before we replaced the attribute).
        return await self._orig(frame)


def _hijack_card(ws_client):
    """Replace the ws client's _handle_data_frame with the CARD-aware wrapper.
    Returns the wrapper (so we can attach an _on_card callback)."""
    wrapper = CardAwareClient(ws_client)
    # bind instance method to the real client
    import asyncio
    real = ws_client
    async def _df(frame):
        return await wrapper._handle_data_frame(frame)
    real._handle_data_frame = _df
    return wrapper


def run_ws():
    """Start the long-connection event listener (no public callback URL needed)."""
    import lark_oapi
    from lark_oapi.ws import Client as WSClient
    app_id = os.environ.get("FEISHU_APP_ID") or _config().get("event", {}).get("app_id", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET") or _config().get("event", {}).get("app_secret", "")
    if not app_id or not app_secret:
        print("[event] FEISHU_APP_ID / FEISHU_APP_SECRET required for ws mode", file=sys.stderr)
        sys.exit(1)
    print(f"[event] long-connection mode (no callback URL needed), state={_state_file()}", flush=True)

    # Register the im.message.receive_v1 handler on an EventDispatcherHandler and
    # pass it into the ws.Client (older lark-oapi builds used client.on_event; the
    # supported path is an event_handler on the Client constructor).
    from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
    handler = EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(on_p2_im_message_receive) \
        .register_p2_card_action_trigger(on_p2_card_action_trigger) \
        .build()
    client = WSClient(app_id=app_id, app_secret=app_secret,
                      log_level=lark_oapi.LogLevel.INFO,
                      event_handler=handler)
    # phase-A1: surface CARD (button-clicks) that the stock SDK drops, by
    # wrapping _handle_data_frame. Logs raw payload until we parse it from real.
    _hijack_card(client)
    client.start()
    # block forever (lark SDK keeps the WS alive / reconnects)
    import time
    while True:
        time.sleep(3600)


if _HAS_FLASK:
    @app.route("/", methods=["GET"])
    def health():
        return jsonify({"status": "ok", "service": "feishu-event-server"})


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Feishu real-time event server")
    parser.add_argument("--mode", choices=["ws", "webhook"], default="ws",
                        help="Event source: 'ws' = long-connection (no public URL, "
                             "default), 'webhook' = Flask callback endpoint")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8085)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.mode == "ws":
        run_ws()
        return

    # webhook mode (requires flask + a public/ reachable callback URL)
    if not _HAS_FLASK:
        print("[event] webhook mode needs flask; install it or use --mode ws", file=sys.stderr)
        sys.exit(1)
    cfg = _config()
    port = args.port or int(cfg.get("event", {}).get("port", 8085))
    print(f"[event] webhook mode listening on {args.host}:{port}, state={_state_file()}", flush=True)
    app.run(host=args.host, port=port, debug=args.debug, threaded=False)


if __name__ == "__main__":
    main()