#!/usr/bin/env python3
"""
Feishu Notifier — Send / update code review results in Feishu group chat.

Can operate in two modes:
  1. Bot API mode (uses Feishu Open API with app_id/app_secret)
  2. Webhook mode (incoming webhook URL, simpler but read-only)

Usage (webhook mode):
    python3 feishu_notifier.py webhook \\
        --webhook-url "https://open.feishu.cn/open-apis/bot/v2/hook/xxx" \\
        --message "Hello from bot"

Usage (initial card):
    python3 feishu_notifier.py send-card \\
        --app-id "xxx" --app-secret "xxx" \\
        --chat-id "oc_xxxx" \\
        --issue-key "EV-123" --project "EV"

Usage (update card):
    python3 feishu_notifier.py update-card \\
        --app-id "xxx" --app-secret "xxx" \\
        --message-id "om_xxxx" \\
        --engine-result '{"severity_counts": {...}}' \\
        --game-result '{"severity_counts": {...}}'
"""
import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
import urllib.error
import base64


# ── Helpers ──────────────────────────────────────────────────────────────────

def _request(method, url, data=None, headers=None):
    """HTTP request helper."""
    if headers is None:
        headers = {}
    headers.setdefault("Content-Type", "application/json")
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"[ERROR] HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
        return None


def get_tenant_token(app_id, app_secret):
    """Get Feishu tenant access token from app credentials."""
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    data = {"app_id": app_id, "app_secret": app_secret}
    resp = _request("POST", url, data)
    if resp and resp.get("code") == 0:
        return resp["tenant_access_token"]
    print(f"[ERROR] Failed to get tenant token: {resp}", file=sys.stderr)
    return None


def send_webhook(webhook_url, content):
    """Send simple message via incoming webhook."""
    payload = {"msg_type": "interactive", "card": content} \
        if isinstance(content, dict) else {"content": content, "msg_type": "text"}
    resp = _request("POST", webhook_url, payload)
    return resp


def send_text_message(token, chat_id, text):
    """Send a post (rich text) message to a chat."""
    url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    data = {
        "receive_id": chat_id,
        "msg_type": "post",
        "content": {"zh_cn": {"content": [[{"tag": "text", "text": text}]]}},
    }
    resp = _request("POST", url, data, headers)
    return resp


def reply_in_thread(token, chat_id, parent_message_id, text):
    """Reply to a message thread with post (rich text) content."""
    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{parent_message_id}/reply"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    data = {
        "msg_type": "post",
        "content": {"zh_cn": {"content": [[{"tag": "text", "text": text}]]}},
    }
    resp = _request("POST", url, data, headers)
    return resp
    """Send an interactive card message to a chat."""
    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    data = {
        "receive_id": chat_id,
        "msg_type": "interactive",
        "content": json.dumps(card),
    }
    resp = _request("POST", url, data, headers)
    return resp


def update_card_message(token, message_id, card):
    """Update (patch) an existing card message."""
    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    data = {
        "content": json.dumps(card),
    }
    resp = _request("PATCH", url, data, headers)
    return resp


# ── Card builders ────────────────────────────────────────────────────────────

def build_processing_card(issue_key, project):
    """Build a card showing review in progress."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"🔄 Code Review: {issue_key}"},
            "template": "blue",
        },
        "elements": [
            {"tag": "markdown", "content": f"**Project:** {project}\n**Issue:** {issue_key}\n\nCode review in progress, please wait..."},
            {"tag": "div", "fields": [
                {"is_short": True, "text": {"tag": "lark_md", "content": "**Engine:** ⏳ Pending"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": "**Game:** ⏳ Pending"}},
            ]},
        ],
    }


def build_result_card(issue_key, project, engine_result, game_result, jira_url):
    """Build a result card after review completes."""
    def sev_text(scores):
        parts = []
        if scores.get("critical"):
            parts.append(f"🔴 Critical: {scores['critical']}")
        if scores.get("warning"):
            parts.append(f"🟡 Warning: {scores['warning']}")
        if scores.get("suggestion"):
            parts.append(f"ℹ️ Suggestion: {scores['suggestion']}")
        return " | ".join(parts) if parts else "✅ No issues found"

    def get_preview(review_text, max_len=800):
        if not review_text:
            return "No review data."
        return review_text[:max_len] + ("..." if len(review_text) > max_len else "")

    engine_sev = (engine_result or {}).get("severity_counts", {})
    game_sev = (game_result or {}).get("severity_counts", {})
    engine_review = (engine_result or {}).get("review_text", "")
    game_review = (game_result or {}).get("review_text", "")

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"✅ Code Review Complete: {issue_key}"},
            "template": "green",
        },
        "elements": [
            {"tag": "markdown", "content": f"**Project:** {project}\n**Jira:** [{jira_url}]({jira_url})"},
            {"tag": "hr"},
            {"tag": "markdown", "content": f"**🔧 Engine Repository**\n{sev_text(engine_sev)}\n\n{get_preview(engine_review)}"},
            {"tag": "hr"},
            {"tag": "markdown", "content": f"**🎮 Game Repository**\n{sev_text(game_sev)}\n\n{get_preview(game_review)}"},
        ],
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def cmd_webhook(args):
    """Send message via webhook."""
    if args.message_file:
        with open(args.message_file) as f:
            raw = f.read()
        try:
            content = json.loads(raw)
        except json.JSONDecodeError:
            content = raw
    else:
        content = args.message
    resp = send_webhook(args.webhook_url, content)
    print(json.dumps(resp, indent=2))


def cmd_send_card(args):
    """Send initial processing card."""
    token = get_tenant_token(args.app_id, args.app_secret)
    if not token:
        sys.exit(1)
    card = build_processing_card(args.issue_key, args.project)
    resp = send_card_message(token, args.chat_id, card)
    if resp and resp.get("code") == 0:
        # Return message_id so Jenkins can update it later
        msg_id = resp.get("data", {}).get("message_id", "")
        print(json.dumps({"message_id": msg_id, "status": "sent"}))
    else:
        print(json.dumps({"error": resp}))
        sys.exit(1)


def cmd_send_message(args):
    """Send a plain text message (topic starter)."""
    token = get_tenant_token(args.app_id, args.app_secret)
    if not token:
        sys.exit(1)
    if args.message_file:
        with open(args.message_file) as f:
            raw = f.read()
        try:
            text = json.loads(raw)
        except json.JSONDecodeError:
            text = raw
    else:
        text = args.message
    resp = send_text_message(token, args.chat_id, text)
    if resp and resp.get("code") == 0:
        msg_id = resp.get("data", {}).get("message_id", "")
        print(json.dumps({"message_id": msg_id, "status": "sent"}))
    else:
        print(json.dumps({"error": resp}))
        sys.exit(1)


def cmd_reply_message(args):
    """Reply in an existing message thread."""
    token = get_tenant_token(args.app_id, args.app_secret)
    if not token:
        sys.exit(1)
    if args.message_file:
        with open(args.message_file) as f:
            raw = f.read()
        try:
            text = json.loads(raw)
        except json.JSONDecodeError:
            text = raw
    else:
        text = args.message
    resp = reply_in_thread(token, args.chat_id, args.message_id, text)
    if resp and resp.get("code") == 0:
        msg_id = resp.get("data", {}).get("message_id", "")
        print(json.dumps({"message_id": msg_id, "status": "replied"}))
    else:
        print(json.dumps({"error": resp}))
        sys.exit(1)


def cmd_update_card(args):
    """Update card with review results."""
    token = get_tenant_token(args.app_id, args.app_secret)
    if not token:
        sys.exit(1)
    engine_result = json.loads(args.engine_json) if args.engine_json else None
    game_result = json.loads(args.game_json) if args.game_json else None
    card = build_result_card(
        args.issue_key, args.project,
        engine_result, game_result,
        args.jira_url,
    )
    resp = update_card_message(token, args.message_id, card)
    print(json.dumps(resp, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Feishu Code Review Notifier")
    sub = parser.add_subparsers(dest="command", required=True)

    # ── webhook ──
    p = sub.add_parser("webhook", help="Send message via incoming webhook")
    p.add_argument("--webhook-url", required=True)
    p.add_argument("--message", help="Message text or JSON string")
    p.add_argument("--message-file", help="Read message from JSON file")

    # ── send-card ──
    p = sub.add_parser("send-card", help="Send initial processing card")
    p.add_argument("--app-id", required=True)
    p.add_argument("--app-secret", required=True)
    p.add_argument("--chat-id", required=True)
    p.add_argument("--issue-key", required=True)
    p.add_argument("--project", required=True)

    # ── send-message (topic starter) ──
    p = sub.add_parser("send-message", help="Send plain text topic starter message")
    p.add_argument("--app-id", required=True)
    p.add_argument("--app-secret", required=True)
    p.add_argument("--chat-id", required=True)
    p.add_argument("--message", help="Message text")
    p.add_argument("--message-file", help="Read message text from JSON file")

    # ── reply-message (reply in topic) ──
    p = sub.add_parser("reply-message", help="Reply in an existing message thread")
    p.add_argument("--app-id", required=True)
    p.add_argument("--app-secret", required=True)
    p.add_argument("--chat-id", required=True)
    p.add_argument("--message-id", required=True)
    p.add_argument("--message", help="Reply text")
    p.add_argument("--message-file", help="Read reply text from JSON file")

    # ── update-card ──
    p = sub.add_parser("update-card", help="Update card with results")
    p.add_argument("--app-id", required=True)
    p.add_argument("--app-secret", required=True)
    p.add_argument("--message-id", required=True)
    p.add_argument("--issue-key", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--jira-url", required=True)
    p.add_argument("--engine-json", help="Engine review result JSON string")
    p.add_argument("--game-json", help="Game review result JSON string")

    args = parser.parse_args()

    if args.command == "webhook":
        cmd_webhook(args)
    elif args.command == "send-card":
        cmd_send_card(args)
    elif args.command == "send-message":
        cmd_send_message(args)
    elif args.command == "reply-message":
        cmd_reply_message(args)
    elif args.command == "update-card":
        cmd_update_card(args)


if __name__ == "__main__":
    main()
