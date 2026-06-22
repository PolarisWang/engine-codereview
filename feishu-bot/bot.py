#!/usr/bin/env python3
"""
Feishu Code Review Bot — lightweight service that receives Feishu group messages,
detects Jira URLs, and triggers Jenkins pipeline for automated code review.

Operation modes:
  1. Event subscription mode: receives Feishu Open Platform event callbacks
     Requires: APP_ID, APP_SECRET, VERIFICATION_TOKEN
  2. Webhook mode: receives incoming webhook messages
     Requires: WEBHOOK_VERIFY_TOKEN

Environment variables (see .env.example):
  FLASK_HOST, FLASK_PORT
  FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_VERIFICATION_TOKEN
  FEISHU_WEBHOOK_VERIFY_TOKEN (for webhook mode)
  JENKINS_URL, JENKINS_USER, JENKINS_TOKEN, JENKINS_JOB_NAME
  FEISHU_WEBHOOK_URL (for sending messages via incoming webhook)
  JIRA_HOST, JIRA_TOKEN
"""
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time
import base64
import urllib.parse
import urllib.request
import urllib.error

import yaml
from flask import Flask, request, jsonify

# ── Config ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("feishu-bot")

app = Flask(__name__)


def load_project_config():
    """Load config.yaml from repo root."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(script_dir, ".."))
    config_path = os.path.join(repo_root, "config.yaml")
    if os.path.exists(config_path):
        with open(config_path) as f:
            return yaml.safe_load(f)
    logger.warning("config.yaml not found at %s", config_path)
    return {"projects": {}}


PROJECT_CONFIG = load_project_config()


# ── Jira URL parsing ─────────────────────────────────────────────────────────

JIRA_URL_PATTERN = re.compile(r'https?://[\w.-]+/(?:browse|issues)/([A-Za-z]+-\d+)')

PROJECT_KEY_MAP = {}
for proj_id, proj_cfg in PROJECT_CONFIG.get("projects", {}).items():
    key = proj_cfg["jira_project_key"].upper()
    PROJECT_KEY_MAP[key] = proj_id


def extract_jira_urls(text):
    """Extract all Jira issue URLs from text."""
    return JIRA_URL_PATTERN.findall(text)


def identify_project(issue_key):
    """Map issue key prefix to project ID."""
    prefix = issue_key.split("-")[0].upper()
    proj_id = PROJECT_KEY_MAP.get(prefix)
    if proj_id:
        return proj_id, PROJECT_CONFIG["projects"].get(proj_id)
    return None, None


# ── Jenkins trigger ──────────────────────────────────────────────────────────

def trigger_jenkins(jira_url):
    """Trigger Jenkins pipeline job by Jira URL.
    Returns (success: bool, queue_item_url: str or None)
    """
    jenkins_url = os.environ.get("JENKINS_URL", "")
    job_name = os.environ.get("JENKINS_JOB_NAME", "code-review-pipeline")
    jenkins_user = os.environ.get("JENKINS_USER", "")
    jenkins_token = os.environ.get("JENKINS_TOKEN", "")

    if not jenkins_url:
        logger.error("JENKINS_URL not set")
        return False, None

    # Build Jenkins job URL
    job_url = f"{jenkins_url.rstrip('/')}/job/{job_name}/buildWithParameters"
    params = urllib.parse.urlencode({"JIRA_URL": jira_url})
    full_url = f"{job_url}?{params}"

    # Basic auth
    auth_str = f"{jenkins_user}:{jenkins_token}"
    auth_bytes = base64.b64encode(auth_str.encode())
    auth = auth_bytes.decode()

    req = urllib.request.Request(full_url, method="POST")
    if auth:
        req.add_header("Authorization", f"Basic {auth}")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            # Jenkins returns 201 with Location header pointing to queue item
            location = resp.headers.get("Location", "")
            logger.info("Jenkins triggered: %s → %s", jira_url, location)
            return True, location
    except urllib.error.HTTPError as e:
        logger.error("Jenkins trigger failed: HTTP %d - %s", e.code, e.read().decode())
        return False, None
    except Exception as e:
        logger.error("Jenkins trigger failed: %s", e)
        return False, None


# ── Feishu Bot API helpers ───────────────────────────────────────────────────

def get_feishu_token(app_id, app_secret):
    """Get tenant access token from Feishu Open API."""
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    data = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("code") == 0:
                return result["tenant_access_token"]
            logger.error("Feishu token error: %s", result)
            return None
    except Exception as e:
        logger.error("Feishu token request failed: %s", e)
        return None


def send_feishu_message(chat_id, card, token):
    """Send a card message to a Feishu chat."""
    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    data = json.dumps({
        "receive_id": chat_id,
        "msg_type": "interactive",
        "content": json.dumps(card),
    }).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            if result.get("code") == 0:
                return result.get("data", {}).get("message_id")
            logger.error("Send message error: %s", result)
            return None
    except Exception as e:
        logger.error("Send message failed: %s", e)
        return None


def send_webhook_message(webhook_url, text):
    """Send simple text to Feishu incoming webhook."""
    data = json.dumps({"msg_type": "text", "content": {"text": text}}).encode()
    req = urllib.request.Request(webhook_url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        logger.error("Webhook message failed: %s", e)
        return False


# ── Card builders ────────────────────────────────────────────────────────────

def build_processing_card(issue_key, project_name):
    """Build a card showing review in progress."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"🔄 Code Review: {issue_key}"},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "markdown",
                "content": f"**Project:** {project_name}\n**Issue:** {issue_key}\n\nCode analysis started, results will be posted shortly..."
            },
        ],
    }


# ── Event handlers ───────────────────────────────────────────────────────────

def handle_message(content_type, content, chat_id, sender_id):
    """
    Handle a received message event.
    """
    if content_type != "text":
        return

    try:
        msg_data = json.loads(content) if isinstance(content, str) else content
        text = msg_data.get("text", "")
    except (json.JSONDecodeError, AttributeError):
        text = str(content) if content else ""

    # Extract Jira URLs
    issue_keys = extract_jira_urls(text)
    if not issue_keys:
        return

    for issue_key in issue_keys:
        project_id, project_cfg = identify_project(issue_key)
        if not project_id:
            logger.info("Unknown project for issue: %s", issue_key)
            continue

        project_name = project_cfg["name"]

        # Build Jira URL from the original text
        jira_url_match = re.search(
            rf'(https?://[\w.-]+/(?:browse|issues)/{re.escape(issue_key)})', text
        )
        jira_url = jira_url_match.group(1) if jira_url_match else ""

        # Send processing notification
        feishu_webhook = os.environ.get("FEISHU_WEBHOOK_URL", "")
        app_id = os.environ.get("FEISHU_APP_ID", "")
        app_secret = os.environ.get("FEISHU_APP_SECRET", "")

        if app_id and app_secret:
            token = get_feishu_token(app_id, app_secret)
            if token:
                card = build_processing_card(issue_key, project_name)
                msg_id = send_feishu_message(chat_id, card, token)
                if msg_id:
                    logger.info("Sent processing card: %s (msg_id=%s)", issue_key, msg_id)

        elif feishu_webhook:
            send_webhook_message(
                feishu_webhook,
                f"🔄 Code Review Started: {issue_key} ({project_name})\n"
                f"Reviewing MR for {jira_url}, please wait..."
            )

        # Trigger Jenkins
        success, queue_url = trigger_jenkins(jira_url)
        if success:
            logger.info("Jenkins triggered for %s → %s", issue_key, queue_url)
        else:
            error_text = f"⚠️ Failed to trigger Jenkins for {issue_key}. Please check Jenkins status."
            if feishu_webhook:
                send_webhook_message(feishu_webhook, error_text)


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "feishu-codereview-bot"})


@app.route("/webhook/event", methods=["POST"])
def feishu_event():
    """
    Handle Feishu Open Platform event subscription callbacks.
    Feishu sends: { "challenge": "...", "token": "...", "type": "url_verify", "event": {...} }
    """
    data = request.get_json(silent=True) or {}
    logger.debug("Received event: %s", json.dumps(data, indent=2)[:500])

    # URL verification (Feishu sends this on setup)
    if data.get("type") == "url_verify":
        challenge = data.get("challenge", "")
        return jsonify({"challenge": challenge})

    # Verify token
    expected_token = os.environ.get("FEISHU_VERIFICATION_TOKEN", "")
    if expected_token and data.get("token") != expected_token:
        logger.warning("Invalid verification token")
        return jsonify({"error": "invalid token"}), 403

    # Handle message event
    event = data.get("event", {})
    event_type = event.get("type", data.get("type", ""))

    if event_type == "im.message.receive_v1":
        message = event.get("message", {})
        chat_type = message.get("chat_type", "")
        if chat_type == "group":  # Only handle group chats (topic groups)
            content_type = message.get("message_type", "")  # e.g. "text"
            content = message.get("content", "")
            chat_id = message.get("chat_id", "")
            sender = event.get("sender", {}).get("sender_id", {})

            try:
                handle_message(content_type, content, chat_id, sender)
            except Exception as e:
                logger.exception("Error handling message: %s", e)

    return jsonify({"code": 0})


@app.route("/webhook/message", methods=["POST"])
def webhook_message():
    """
    Simple incoming webhook endpoint.
    Accepts: { "text": "...jira url...", "chat_id": "optional" }
    Or Feishu card webhook format.
    """
    data = request.get_json(silent=True) or {}

    # Verify webhook token if configured
    expected = os.environ.get("FEISHU_WEBHOOK_VERIFY_TOKEN", "")
    verify_token = data.get("token", "") or request.headers.get("X-Feishu-Webhook-Token", "")
    if expected and verify_token != expected:
        return jsonify({"error": "invalid token"}), 403

    text = data.get("text", data.get("content", ""))
    chat_id = data.get("chat_id", "")

    issue_keys = extract_jira_urls(text)
    if issue_keys:
        for issue_key in issue_keys:
            project_id, project_cfg = identify_project(issue_key)
            if not project_id:
                continue
            # Reconstruct full Jira URL
            jira_url_match = re.search(
                rf'(https?://[\w.-]+/(?:browse|issues)/{re.escape(issue_key)})', text
            )
            jira_url = jira_url_match.group(1) if jira_url_match else ""
            trigger_jenkins(jira_url)

    return jsonify({"status": "ok"})


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes")

    logger.info("Starting Feishu Code Review Bot on %s:%s", host, port)
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()
