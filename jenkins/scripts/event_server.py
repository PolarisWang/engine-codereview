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
    cfg = _config()
    return cfg.get("workspace", {}).get("base_dir") or \
        os.environ.get("REVIEW_WORKSPACE", "/root/codereview-workspace")


app = Flask(__name__)


# ── URL verification / secure dispatch ───────────────────────────────────────

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
    _route(msg_id, parent_id, text)


# ── Long-connection (WebSocket) mode — no public callback URL needed ─────────
#
# Feishu's open platform supports receiving events over a persistent WebSocket
# ("长连接") initiated FROM our side, so the server does NOT need a public IP /
# callback URL. We register a handler for im.message.receive_v1 and normalize the
# lark-oapi message dataclass into the same routing used by the webhook mode.

def _lark_message_to_route(lark_message):
    """Map a lark-oapi ws P2ImMessageReceiveV1.message onto the routing fields.
    Returns (msg_id, parent_id, chat_type, text) or None if not group/text."""
    try:
        msg_type = getattr(lark_message, "message_type", "") or ""
        chat_type = getattr(lark_message, "chat_type", "") or ""
        msg_id = getattr(lark_message, "message_id", "") or ""
        parent_id = getattr(lark_message, "parent_id", "") or ""
        raw_content = getattr(lark_message, "content", "") or ""
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
    return msg_id, parent_id, chat_type, text


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
        # data is the P2ImMessageReceiveV1; route via its message payload.
        message = _p2_message_payload(data)
        if not message:
            print("[event] ws event missing message", file=sys.stderr)
            return
        routed = _lark_message_to_route(message)
        if not routed:
            return
        msg_id, parent_id, chat_type, text = routed
        if chat_type != "group":
            return
        _route(msg_id, parent_id, text)
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


def _route(msg_id, parent_id, text):
    """Shared single-link routing: new topic with Jira -> run; reply -> interact."""
    base = ["--pipeline-state-file", _state_file(), "--workspace", _workspace()]
    if not parent_id:
        if _is_jira_topic(text):
            print(f"[event] NEW TOPIC {msg_id}: {text[:80]}", flush=True)
            _run_orchestrate(["run", "--key", msg_id, "--mode", "scan", "--text", text[:200]] + base)
        else:
            print(f"[event] ignore topic (no Jira URL) {msg_id}", flush=True)
    else:
        print(f"[event] REPLY {msg_id} to parent {parent_id}: {text[:80]}", flush=True)
        _run_orchestrate(["interact", "--key", parent_id, "--reply", text[:500],
                          "--reply-msg-id", msg_id] + base)


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