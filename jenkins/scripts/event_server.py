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
    cfg = _config()
    return cfg.get("event", {}).get("state_file") or \
        os.environ.get("PIPELINE_STATE_FILE", "/root/.codereview-pipeline-state.json")


def _workspace():
    # The reviewer (Jenkinsfile) writes review-result files to REVIEW_WORKSPACE
    # (= $HOME/codereview-workspace). The interact phase must read findings from
    # the SAME directory, or it finds none and wrongly reports "no findings".
    # Prefer REVIEW_WORKSPACE (matches Jenkinsfile), then the de-facto workspace,
    # and only treat the (possibly-stale) config base_dir as a last resort.
    env_ws = os.environ.get("REVIEW_WORKSPACE", "")
    if env_ws:
        return env_ws
    home_ws = os.path.join(os.path.expanduser("~") or "/root", "codereview-workspace")
    if os.path.isdir(home_ws):
        return home_ws
    cfg = _config()
    return cfg.get("workspace", {}).get("base_dir") or "/root/codereview-workspace"


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
    return bool(common.JIRA_URL_PATTERN.search(text or ""))


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
    _route(msg_id, parent_id, text, sender_id)


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
    Returns (msg_id, parent_id, chat_type, text, sender_id) or None if not group/text."""
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
        if msg_type == "text":
            cd = json.loads(raw_content) if isinstance(raw_content, str) else (raw_content or {})
            text = (cd or {}).get("text", "")
        elif msg_type == "post":
            text = _post_text(raw_content)
        else:
            text = ""
    except Exception as e:
        print(f"[event] parse lark message failed: {e}", file=sys.stderr)
        return None
    return msg_id, parent_id, chat_type, text, sender_id


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
        msg_id, parent_id, chat_type, text, _ = routed
        if chat_type != "group":
            return
        _route(msg_id, parent_id, text, sender_id)
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

# per-topic serial lock (defense-in-depth layer 5): same topic's interact handled one at a time.
_T_LOCKS = {}
_T_LOCKS_GUARD = threading.Lock()
LOCK_TIMEOUT = 120  # seconds


def _topic_lock(topic_key):
    with _T_LOCKS_GUARD:
        if topic_key not in _T_LOCKS:
            _T_LOCKS[topic_key] = threading.Lock()
        return _T_LOCKS[topic_key]


def _route(msg_id, parent_id, text, sender_id=""):
    """Shared single-link routing.

    - New topic (no parent, has Jira URL): NOT reviewed here. The event server
      runs without Jira/GitLab credentials, so an inline review would always fail;
      the Jenkins scanner cron picks up new group topics and reviews with full
      credentials. This keeps review ownership with Jenkins (single-link, no cred
      duplication) and lets the event server act purely as the message/interact hub.
    - Reply / @bot on an existing topic: route to interact (only needs FEISHU +
      ANTHROPIC env, which the event server has). Same-topic replies are
      serialized via a per-topic lock.
    """
    if not parent_id:
        if _is_jira_topic(text):
            # Review is owned by the Jenkins scanner; acknowledge here.
            print(f"[event] NEW TOPIC {msg_id}: {text[:80]} (handing review to scanner)", flush=True)
        else:
            print(f"[event] ignore topic (no Jira URL) {msg_id}", flush=True)
    else:
        print(f"[event] REPLY {msg_id} to parent {parent_id}: {text[:80]} sender={sender_id}", flush=True)
        lock = _topic_lock(parent_id)
        acquired = lock.acquire(timeout=LOCK_TIMEOUT)
        try:
            if not acquired:
                print(f"[event] topic {parent_id} busy, skip this reply", file=sys.stderr)
                return
            base = ["--pipeline-state-file", _state_file(), "--workspace", _workspace()]
            _run_orchestrate(["interact", "--key", parent_id, "--reply", text[:500],
                              "--reply-msg-id", msg_id, "--sender-id", sender_id] + base)
        finally:
            if acquired:
                lock.release()


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
        .build()
    client = WSClient(app_id=app_id, app_secret=app_secret,
                      log_level=lark_oapi.LogLevel.INFO,
                      event_handler=handler)
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