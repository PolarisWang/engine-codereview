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
import base64

# Shared helper: HTTP with retry
from common import http_request


STDIN_PREFIX = "@stdin"


def read_message_text(args):
    """Resolve message text from --message, --message-file, --message-base64, or stdin."""
    if args.message_base64:
        return base64.b64decode(args.message_base64).decode("utf-8")
    if args.message_file:
        if args.message_file == STDIN_PREFIX:
            return sys.stdin.read()
        with open(args.message_file, encoding="utf-8") as f:
            raw = f.read()
        try:
            return json.loads(raw) if isinstance(json.loads(raw), str) else raw
        except (json.JSONDecodeError, ValueError):
            return raw
    return args.message


# ── Helpers ──────────────────────────────────────────────────────────────────

def _request(method, url, data=None, headers=None, raw_body=None):
    """HTTP request helper (with retry).
    Args:
        raw_body: Pre-serialized JSON string (takes precedence over data)
        data: Dict to serialize as JSON
    """
    if headers is None:
        headers = {}
    resp = http_request(method, url, data=data, headers=headers, raw_body=raw_body,
                        timeout=30)
    if resp is None:
        print(f"[ERROR] HTTP {method} {url[:80]} failed after retries", file=sys.stderr)
    return resp


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
    """Send an interactive card message to a chat."""
    url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Code Review"},
            "template": "blue",
        },
        "elements": [
            {"tag": "markdown", "content": text},
        ],
    }
    content_str = json.dumps(card, ensure_ascii=False)
    body = json.dumps({
        "receive_id": chat_id,
        "msg_type": "interactive",
        "content": content_str,
    }, ensure_ascii=False)
    resp = _request("POST", url, headers=headers, raw_body=body)
    return resp


def reply_in_thread(token, chat_id, parent_message_id, text):
    """Reply to a message thread with an interactive card (supports markdown & emoji)."""
    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{parent_message_id}/reply"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    # Use interactive card format for reliable encoding and markdown rendering
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Code Review"},
            "template": "blue",
        },
        "elements": [
            {"tag": "markdown", "content": text},
        ],
    }
    content_str = json.dumps(card, ensure_ascii=False)
    body = json.dumps({
        "msg_type": "interactive",
        "content": content_str,
    }, ensure_ascii=False)
    resp = _request("POST", url, headers=headers, raw_body=body)
    print(f"[feishu] reply_in_thread response: {json.dumps(resp, ensure_ascii=False)[:200] if resp else 'None'}", file=sys.stderr)
    return resp


def send_card_message(token, chat_id, card):
    """Send a pre-built interactive card message to a chat."""
    url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    content_str = json.dumps(card, ensure_ascii=False)
    body = json.dumps({
        "receive_id": chat_id,
        "msg_type": "interactive",
        "content": content_str,
    }, ensure_ascii=False)
    resp = _request("POST", url, headers=headers, raw_body=body)
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


# ── Summary text rendering (single source of truth for Jenkinsfile) ──────────
#
# This replaces the duplicated markdown-building logic that previously lived in
# the Jenkinsfile's scan-inline and post paths. Jenkins calls `render-summary`
# and just sends whatever it produces — rendering now lives in one testable place.

def _parse_file(path):
    """Load a result JSON file (safely). Returns {} on any failure."""
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def build_repo_section(repo_label, repo_icon, result, review_branch, base_branch):
    """
    Build the markdown section for one repository (engine or game), mirroring the
    skip/fail/merged/no-change/clean logic that previously lived in the pipeline.
    """
    sev = ((result or {}).get("review") or {}).get("severity_counts") or {}
    changed = len((result or {}).get("changed_files") or [])
    stats = (result or {}).get("stats") or ""
    review_text = ((result or {}).get("review") or {}).get("review_text") or ""
    error = (((result or {}).get("review") or {}).get("error")
             or (result or {}).get("error") or "")
    branch_merged = (result or {}).get("branch_merged") or False
    branch_exists = (result or {}).get("branch_exists", True)
    mr_state = (result or {}).get("mr_state") or ""

    text = f"{repo_icon} **{repo_label} 仓库**"
    if error and not review_text:
        # Total failure — no usable review output at all.
        text += f" ❌ 审查失败\n原因: {error}"
    elif branch_exists is False:
        text += f" ⏭️ 跳过审查\n原因: 分支 `{review_branch}` 在远程仓库中不存在（可能已被删除或从未创建）"
    elif branch_merged:
        hint = ""
        if mr_state == "merged":
            hint = "（GitLab 显示该 MR 已合并）"
        elif mr_state == "closed":
            hint = "（GitLab 显示该 MR 已关闭）"
        text += f" ⏭️ 跳过审查\n原因: 分支 `{review_branch}` 已经合并到 `{base_branch}`，没有新的代码变更{hint}"
    elif changed == 0:
        text += f" ⏭️ 跳过审查\n原因: 该分支相对于 {base_branch} 没有代码变更"
    else:
        text += f"\n变更文件: {changed} 个"
        if stats:
            text += f" ({stats})"
        if sev.get("critical"):
            text += f"\n🔴 Critical: {sev['critical']}"
        if sev.get("warning"):
            text += f"\n🟡 Warning: {sev['warning']}"
        if sev.get("suggestion"):
            text += f"\nℹ️ Suggestion: {sev['suggestion']}"
        if not sev.get("critical") and not sev.get("warning") and not sev.get("suggestion"):
            text += "\n✅ 未发现严重问题"
        if error and review_text:
            # Partial results — batches succeeded but at least one failed.
            text += f"\n⚠️ 部分批次的审查失败: {error}（已展示成功的部分）"
    return text


def build_summary_text(issue_key, project, review_branch, base_branch, jira_url, mr_url,
                       engine_result, game_result):
    """
    Build the final Feishu markdown summary, mirroring the pipeline's online format.

    engine_result/game_result are dicts (or file paths resolved by caller).
    Returns the markdown string.
    """
    summary = f"**🔍 Code Review 报告: {issue_key}**\n\n"
    summary += f"**项目:** {project}\n"
    summary += f"**分支:** {review_branch} → {base_branch}\n"
    if mr_url:
        summary += f"**MR:** {mr_url}\n"
    summary += f"\n{build_repo_section('Engine', '🔧', engine_result, review_branch, base_branch)}\n"
    if game_result:
        summary += f"\n{build_repo_section('Game', '🎮', game_result, review_branch, base_branch)}\n"
    summary += f"\n---\n📎 Jira: {jira_url}"

    # Append truncated per-repo review details when they fit within the card limit.
    MAX = 3500
    for label, result in (("🔧 Engine", engine_result), ("🎮 Game", game_result)):
        detail = ((result or {}).get("review") or {}).get("review_text")
        if not detail:
            continue
        if len(summary) >= MAX:
            break
        avail = MAX - len(summary) - 50
        if avail > 200:
            if len(detail) > avail:
                detail = detail[:avail] + "\n\n...（详情见 Jenkins 构建日志）"
            summary += f"\n\n---\n**{label} 详细报告:**\n{detail}"
    return summary


def cmd_render_summary(args):
    """Render the final review summary markdown from result JSON files."""
    engine = _parse_file(getattr(args, "engine_file", None))
    game = _parse_file(getattr(args, "game_file", None))
    # Inject MR state into repo sections so the merged/closed hint renders correctly.
    mr_state = getattr(args, "mr_state", "") or ""
    if mr_state:
        if engine:
            engine["mr_state"] = mr_state
        if game:
            game["mr_state"] = mr_state
    text = build_summary_text(
        args.issue_key, args.project, args.branch, args.base_branch,
        args.jira_url or "", args.mr_url or "", engine, game,
    )
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
    if args.message_base64:
        print(base64.b64encode(text.encode("utf-8")).decode("utf-8"))
    else:
        print(text)


# ── Live status card rendering ───────────────────────────────────────────────
# A compact "state card" shown as the in-flight Feishu thread reply, re-rendered
# in-place at phase transitions (idempotent — one card, edited).

_REPO_BADGE = {
    "PENDING": "⏳ 等待",
    "RUNNING": "🔄 审查中",
    "SUCCESS": "✅ 完成",
    "SKIPPED": "⏭️ 跳过",
    "FAILED": "❌ 失败",
}


def _sev_summary(sev_counts):
    sev = sev_counts or {}
    parts = []
    if sev.get("critical"):
        parts.append(f"🔴 {sev['critical']}")
    if sev.get("warning"):
        parts.append(f"🟡 {sev['warning']}")
    if sev.get("suggestion"):
        parts.append(f"ℹ️ {sev['suggestion']}")
    return " · ".join(parts) if parts else ""


def render_state_card(topic):
    """
    Build the live-update Feishu card markdown from a topic record (the JSON
    produced by pipeline_state.py). Shows overall phase/status and per-repo
    badges + severity counts.
    """
    jira = topic.get("jira_key") or topic.get("message_id") or ""
    phase = topic.get("phase") or ""
    status = topic.get("status") or ""
    repos = topic.get("repos") or {}

    lines = []
    lines.append(f"🔄 **Code Review Pipeline: {jira}**")
    lines.append(f"阶段: **{phase}** · 状态: **{status}**")
    if topic.get("last_error"):
        lines.append(f"⚠️ {topic['last_error']}")
    lines.append("")
    for repo in ("engine", "game"):
        r = repos.get(repo) or {}
        badge = _REPO_BADGE.get(r.get("status"), r.get("status", "?"))
        name = {"engine": "🔧 Engine", "game": "🎮 Game"}.get(repo, repo)
        sev = _sev_summary(r.get("severity_counts"))
        if r.get("skip_reason"):
            lines.append(f"{name}: {badge} — {r['skip_reason']}")
        elif r.get("error"):
            lines.append(f"{name}: {badge} — {r['error']}")
        elif sev:
            lines.append(f"{name}: {badge} ({sev})")
        else:
            lines.append(f"{name}: {badge}")
    if topic.get("render_msg_id"):
        lines.append("")
        lines.append(f"已关联回复卡片: `{topic['render_msg_id']}`")
    return "\n".join(lines)


def cmd_render_state(args):
    """Render a live status card from a pipeline_state topic JSON file."""
    try:
        with open(args.topic_file, encoding="utf-8") as f:
            topic = json.load(f)
    except (OSError, json.JSONDecodeError):
        topic = {}
    text = render_state_card(topic)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
    if args.message_base64:
        print(base64.b64encode(text.encode("utf-8")).decode("utf-8"))
    else:
        print(text)


# ── Main ─────────────────────────────────────────────────────────────────────

def cmd_webhook(args):
    """Send message via webhook."""
    text = read_message_text(args)
    resp = send_webhook(args.webhook_url, text)
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
    text = read_message_text(args)
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
    text = read_message_text(args)
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


def cmd_update_reply(args):
    """Update a thread reply card with new text content (PATCH in-place)."""
    token = get_tenant_token(args.app_id, args.app_secret)
    if not token:
        sys.exit(1)
    text = read_message_text(args)
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Code Review"},
            "template": "blue",
        },
        "elements": [
            {"tag": "markdown", "content": text},
        ],
    }
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
    p.add_argument("--message-base64", help="Base64-encoded message text")

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
    p.add_argument("--message-base64", help="Base64-encoded message text")

    # ── reply-message (reply in topic) ──
    p = sub.add_parser("reply-message", help="Reply in an existing message thread")
    p.add_argument("--app-id", required=True)
    p.add_argument("--app-secret", required=True)
    p.add_argument("--chat-id", required=True)
    p.add_argument("--message-id", required=True)
    p.add_argument("--message", help="Reply text")
    p.add_argument("--message-file", help="Read reply text from JSON file")
    p.add_argument("--message-base64", help="Base64-encoded reply text")

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

    # ── update-reply (update a thread reply card with new text) ──
    p = sub.add_parser("update-reply", help="Update a thread reply card with new text content")
    p.add_argument("--app-id", required=True)
    p.add_argument("--app-secret", required=True)
    p.add_argument("--message-id", required=True)
    p.add_argument("--message-base64", help="Base64-encoded new card text")

    # ── render-summary (build final review markdown; Jenkins sends it) ──
    p = sub.add_parser("render-summary", help="Render the final review summary markdown")
    p.add_argument("--issue-key", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--branch", required=True)
    p.add_argument("--base-branch", required=True)
    p.add_argument("--jira-url", default="")
    p.add_argument("--mr-url", default="")
    p.add_argument("--engine-file", default="", help="Engine result JSON file")
    p.add_argument("--game-file", default="", help="Game result JSON file")
    p.add_argument("--mr-state", default="", help="MR state (merged/closed/opened) for skip hints")
    p.add_argument("--output", default="", help="Write rendered markdown to file")
    p.add_argument("--message-base64", action="store_true",
                   help="Print the rendered markdown as Base64 on stdout")

    # ── render-state (live status card from a pipeline_state topic JSON) ──
    p = sub.add_parser("render-state", help="Render live status card from a topic record")
    p.add_argument("--topic-file", required=True, help="Path to a topic JSON (pipeline_state query single)")
    p.add_argument("--output", default="")
    p.add_argument("--message-base64", action="store_true")

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
    elif args.command == "update-reply":
        cmd_update_reply(args)
    elif args.command == "render-summary":
        cmd_render_summary(args)
    elif args.command == "render-state":
        cmd_render_state(args)


if __name__ == "__main__":
    main()
