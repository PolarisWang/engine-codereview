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
# 方案C: 用户可见文案从 config.yaml messages 读取(集中配置)
import config as _config
MSG = _config.MSG


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


# ── Interactive action buttons (arch-A) ───────────────────────────────────────
# Feishu interactive cards support clickable buttons. Clicking posts a
# `card.action.trigger` callback carrying `value` + the operator's open_id. We
# route that back into the same interaction/pending logic (see event_server).
#
# Actions are keyed by a short id; each maps to a topic interaction:
#   re_review / apply_patch / close_topic / get_status
ACTION_BUTTONS = [
    # (label, value.action, enabled-when-DONE, tooltip)
    ("🔍 重新审查", "re_review", True, "让 Jenkins 用最新代码重新审查"),
    ("✏️ 修复补丁", "apply_patch", True, "生成/提议修复补丁预览"),
    ("🔒 关闭话题", "close_topic", True, "终止该话题（发起人/管理员）"),
]


def build_action_row(topic_key, actions=ACTION_BUTTONS, enabled=None):
    """Build a Feishu card `action` element (row of buttons) for a topic.

    `enabled` is an optional set of action ids to render; default = all.
    Each button carries value={action, topic} so the callback knows what to do.
    """
    act = enabled if enabled is not None else {a[1] for a in actions}
    buttons = []
    for label, act_id, always, tip in actions:
        if act_id not in act:
            continue
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": "default",
            "value": {"action": act_id, "topic": topic_key},
        })
    if not buttons:
        return None
    return {"tag": "action", "actions": buttons}


def with_action_row(card, topic_key, enabled=None):
    """Return a copy of `card` (dict elements list) with an action-button row
    appended, so the result/state card gains clickable buttons."""
    row = build_action_row(topic_key, enabled=enabled)
    if row is None:
        return card
    card = dict(card)
    card["elements"] = list(card.get("elements") or []) + [row]
    return card


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
            parts.append("🔴 " + MSG.get("sev_critical_label","Critical: "+str(scores["critical"])).format(n=scores["critical"]))
        if scores.get("warning"):
            parts.append("🟡 " + MSG.get("sev_warning_label","Warning").format(n=scores["warning"]))
        if scores.get("suggestion"):
            parts.append("ℹ️ " + MSG.get("sev_suggestion_label","Suggestion").format(n=scores["suggestion"]))
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
        text += " " + MSG.get("review_failed_reason","❌ 审查失败\n原因: "+str(error)).format(msg=error)
    elif branch_exists is False:
        text += " " + MSG.get("skip_branch_missing","⏭️ 跳过审查\n原因: 分支 "+review_branch+" 在远程不存在").format(branch=review_branch)
    elif branch_merged:
        hint = ""
        if mr_state == "merged":
            hint = "（GitLab 显示该 MR 已合并）"
        elif mr_state == "closed":
            hint = "（GitLab 显示该 MR 已关闭）"
        text += " " + MSG.get("skip_branch_merged","").format(branch=review_branch, base=base_branch) + (hint or "")
    elif changed == 0:
        text += " " + MSG.get("skip_no_diff","").format(base=base_branch)
    else:
        text += MSG.get("changed_files_count","\n变更文件: {n} 个").format(n=changed)
        if stats:
            text += f" ({stats})"
        if sev.get("critical"):
            text += MSG.get("sev_critical_count","\n🔴 Critical: {n}").format(n=sev["critical"])
        if sev.get("warning"):
            text += MSG.get("sev_warning_count","\n🟡 Warning: {n}").format(n=sev["warning"])
        if sev.get("suggestion"):
            text += MSG.get("sev_suggestion_count","\nℹ️ Suggestion: {n}").format(n=sev["suggestion"])
        # Evidence chain: expose the diff hash + a few changed files so the result
        # is verifiably tied to a real diff, not a guess.
        diff_hash = (result or {}).get("diff_hash") or ""
        if diff_hash:
            text += MSG.get("diff_preview","\n📄 diff: `{h}…`").format(h=diff_hash[:10])
        changed_files = ((result or {}).get("changed_files") or [])[:5]
        if changed_files:
            names = []
            for cf in changed_files:
                parts = cf.split("\t")
                names.append(parts[-1] if len(parts) > 1 else cf)
            text += MSG.get("files_preview","\n变更文件预览: {names}").format(names=", ".join(names))
            if changed > len(changed_files):
                text += " …"
        if not sev.get("critical") and not sev.get("warning") and not sev.get("suggestion"):
            text += "\n✅ 未发现严重问题"
        if error and review_text:
            # Partial results — batches succeeded but at least one failed.
            text += MSG.get("partial_fail","\n⚠️ 部分批次审查失败: {msg}（已展示成功的部分）").format(msg=error)
    return text


def _sev_mark(sev):
    s = (sev or "").strip().lower()
    if s in ("critical", "high", "error", "blocker"):
        return "🔴"
    if s in ("warning", "warn", "minor"):
        return "🟡"
    return "ℹ️"


def _concise_findings(result, limit=4):
    """Return up to `limit` most important findings as short one-liners (critical first)."""
    rev = (result or {}).get("review") or {}
    fs = rev.get("findings") or []

    def _pri(f):
        s = (f.get("severity") or "").lower()
        return 0 if s in ("critical", "high", "error", "blocker") else (1 if s in ("warning", "warn") else 2)

    fs = sorted(fs, key=_pri)
    out = []
    for f in fs[:limit]:
        file = f.get("file") or f.get("path") or "?"
        issue = (f.get("issue") or "").strip().replace("\n", " ")
        out.append(f"{_sev_mark(f.get('severity'))} `{file}`: {issue[:80]}")
    return out


def _empty_review_reason(engine_result, game_result):
    """当 engine+game 都没有 findings 时, 判定"为什么没有可审内容"并给出人话提示。

    C(优先): MR 已合并(merged) -> 直接报"已合并, 相对 base 无新增改动", 不再看其它判据
             (避免与分支已删导致 branch_exists=False 冲突 —— GitLab merge 后常删源分支)。
    A(兜底, 逐仓库): branch_missing / branch_merged / no_diff / error, 否则 clean。
    合并时去重: 多仓库同因只写一遍; 只要有一个仓库 clean 且其它都无真实问题, 才说"未发现问题"。
    返回用于空 findings 时的提示文本(可能多条, 用 \n 连接)。
    """
    def _mr_state(res):
        return ((res or {}).get("mr_state") or "").strip().lower()

    # C 优先: 任一仓库 MR 已合并
    merged_bases = [res.get("base_branch") or "base" for res in (engine_result, game_result)
                    if res and _mr_state(res) == "merged"]
    if merged_bases:
        base = merged_bases[0]
        return (f"🚫 该分支的 MR 已合并进 `{base}`，相对该 base 没有新增改动，"
                f"因此本次未产生 findings。如需审该段代码，请直接审 `{base}` 上当前实现。")

    # A 兜底: 逐仓库归因
    reasons = []
    for label, res in (("引擎", engine_result), ("游戏", game_result)):
        if not res:
            reasons.append(f"{label}: 无 review 结果")
            continue
        r = res.get("review") or {}
        if res.get("branch_exists") is False:
            reasons.append(f"{label}：分支 `{res.get('branch')}` 在仓库中不存在(可能已删除/未推送)")
        elif res.get("branch_merged"):
            reasons.append(f"{label}：分支已合并到 `{res.get('base_branch')}`，无新改动")
        elif not (res.get("changed_files") or []):
            reasons.append(f"{label}：相对 `{res.get('base_branch')}` 无代码变更(改动可能已合并/分支无新提交)")
        elif r.get("error"):
            reasons.append(f"{label}：review 出错 — {r.get('error')}")
        # 否则 = clean(无 findings 且无上述异常)

    # 若两个仓库都是 clean → 真干净
    clean = all(
        res and res.get("review") and res.get("branch_exists") is not False
        and not res.get("branch_merged")
        and (res.get("changed_files") or [])
        and not (res.get("review") or {}).get("error")
        for res in (engine_result, game_result)
    )
    if clean:
        return "✅ 未发现需要处理的代码问题。"
    # 去重(按"去掉仓库前缀后的原因文本"), 同一原因多仓库只写一遍
    seen = set()
    compact = []
    for line in reasons:
        reason_tail = line.split("：", 1)[-1] if "：" in line else line
        if reason_tail in seen:
            continue
        seen.add(reason_tail)
        compact.append(line)
    tip = "🚫 无可审内容，原因：\n" + "\n".join(compact) if compact else ""
    return tip or "✅ 未发现需要处理的代码问题。"


def build_summary_text(issue_key, project, review_branch, base_branch, jira_url, mr_url,
                       engine_result, game_result):
    """
    Build a CONCISE, FOCUSED Feishu summary: one-line verdict + top findings.
    (The old full-length "详细报告" is dropped; it was noisy — the user wants
    重点突出、言简意赅.)
    """
    def _sev_count(r):
        return (r or {}).get("review") or {}

    ec = _sev_count(engine_result).get("severity_counts") or {}
    gc = _sev_count(game_result).get("severity_counts") or {}
    total_c = (ec.get("critical") or 0) + (gc.get("critical") or 0)
    total_w = (ec.get("warning") or 0) + (gc.get("warning") or 0)
    total_i = (ec.get("suggestion") or 0) + (gc.get("suggestion") or 0)

    summary = f"**🔍 {issue_key}** · 审查结果："
    if total_c:
        summary += f"**{total_c} 个 Critical** / "
    summary += f"{total_w} 个 Warning / {total_i} 个 Suggestion\n\n"

    if mr_url:
        summary += f"MR：{mr_url}\n"
    summary += MSG.get("branch_line","分支：`{rb}` → `{bb}`\n\n").format(rb=review_branch, bb=base_branch)

    # FOCUSED: top findings across engine + game (critical first).
    tops = _concise_findings(engine_result, 3) + _concise_findings(game_result, 3)
    # de-dup by file line, then cap
    seen = set()
    uniq = []
    for line in tops:
        key = line[:40]
        if key in seen:
            continue
        seen.add(key)
        uniq.append(line)
    if uniq:
        summary += "**关键发现：**\n" + "\n".join(uniq[:5]) + "\n\n"
    else:
        # 空 findings: 归因"为什么没可审内容"(已合并/分支缺失/无diff/出错/真干净)
        summary += _empty_review_reason(engine_result, game_result) + "\n\n"

    if jira_url:
        summary += f"📎 {jira_url}"
    return summary


def render_full_findings_text(issue_key, project, review_branch, base_branch, jira_url, mr_url,
                              engine_result, game_result):
    """Render the FULL review as plain-text messages (普通文字消息, 不折叠、不截断).

    需求: review 内容非常重要，必须完整展示。返回一个 chunk 列表，每个 chunk 是一条
    普通文字消息(按 severity 分组: 结论 → Critical → Warning → Suggestion)。
    调用方用 reply-message 逐条发送。
    """
    def _sev_l(f):
        s = (f.get("severity") or "").strip().lower()
        return 0 if s in ("critical", "high", "error", "blocker") else (1 if s in ("warning", "warn", "minor") else 2)
    def _find(res):
        return ((res or {}).get("review") or {}).get("findings") or []
    allf = _find(engine_result) + _find(game_result)
    # repo 来源: 用 (file, issue 前40) 作为稳定 key, 判定该 finding 属于 engine/game。
    # (不能用 dict 作 set key —— unhashable)
    eng_keys = {(f.get("file") or "", (f.get("issue") or "")[:40]) for f in _find(engine_result)}
    gam_keys = {(f.get("file") or "", (f.get("issue") or "")[:40]) for f in _find(game_result)}
    def _repo_of(f):
        k = (f.get("file") or "", (f.get("issue") or "")[:40])
        return "engine" if k in eng_keys else ("game" if k in gam_keys else "?")

    ec = ((engine_result or {}).get("review") or {}).get("severity_counts") or {}
    gc = ((game_result or {}).get("review") or {}).get("severity_counts") or {}
    total_c = (ec.get("critical") or 0) + (gc.get("critical") or 0)
    total_w = (ec.get("warning") or 0) + (gc.get("warning") or 0)
    total_i = (ec.get("suggestion") or 0) + (gc.get("suggestion") or 0)

    header = MSG.get("review_title","🔍 {key} · 审查结果").format(key=issue_key)
    header += "\n**维度覆盖**: 架构 / 安全 / 性能 / 代码质量 / 语言专属（依据 code-review-skill）"
    if total_c: header += f"\n🔴[blocking] 必须修复 {total_c} / "
    header += f"🟡[important] 应处理 {total_w}"
    if total_i: header += f" / 🟢[nit] 可选 {total_i}"
    if mr_url: header += f"\nMR：{mr_url}"
    header += MSG.get("branch_line","\n分支：`{rb}` → `{bb}`").format(rb=review_branch, bb=base_branch)
    if jira_url: header += f"\n📎 {jira_url}"

    # 排序: critical → warning → suggestion; 每条含 repo 来源。
    allf_sorted = sorted(allf, key=_sev_l)
    def _tag(f):
        s = (f.get("severity") or "").strip().lower()
        if s in ("critical","high","error","blocker"): return ("🔴","[blocking]","必须修复")
        if s in ("warning","warn","minor"): return ("🟡","[important]","应处理")
        return ("🟢","[nit]","建议")
    groups = {"critical": [], "warning": [], "suggestion": []}
    for f in allf_sorted:
        key = _sev_l(f)
        grp = "critical" if key == 0 else ("warning" if key == 1 else "suggestion")
        emoji, tag, rank = _tag(f)
        repo = _repo_of(f)
        file = f.get("file") or f.get("path") or "?"
        issue = (f.get("issue") or "").strip()
        groups[grp].append(f"{emoji} {tag} {rank} `{file}` [{repo}]\n  问题：{issue}\n  建议：{f.get('suggestion') or ''}\n")

    chunks = [header + "\n"]
    label = {"critical": "🔴 [blocking] 必须修复", "warning": "🟡 [important] 应处理", "suggestion": "🟢 [nit] 可选/建议"}
    for grp in ("critical", "warning", "suggestion"):
        items = groups[grp]
        if not items:
            continue
        body = f"\n\n---\n{label[grp]}({len(items)})\n\n" + "\n".join(items)
        chunks.append(body)
    # 去掉空结论
    if not allf:
        chunks[0] += "\n" + _empty_review_reason(engine_result, game_result)
    return chunks


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


def cmd_update_reply_card(args):
    """Update a thread reply card with text + interactive action buttons."""
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
    # Enabled action ids come as comma-separated, e.g. "re_review,apply_patch,close_topic"
    enabled = None
    if args.actions:
        enabled = set(a.strip() for a in args.actions.split(",") if a.strip())
    card = with_action_row(card, args.topic, enabled=enabled)
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

    # ── update-reply-card (update a thread reply card with text + action buttons) ──
    p = sub.add_parser("update-reply-card", help="Update a thread reply card with text + interactive action buttons")
    p.add_argument("--app-id", required=True)
    p.add_argument("--app-secret", required=True)
    p.add_argument("--message-id", required=True)
    p.add_argument("--message-base64", help="Base64-encoded card text")
    p.add_argument("--topic", required=False, default="", help="Topic key to bind action buttons")
    p.add_argument("--actions", required=False, default="", help="Comma-separated enabled action ids")

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
    elif args.command == "update-reply-card":
        cmd_update_reply_card(args)
    elif args.command == "render-summary":
        cmd_render_summary(args)
    elif args.command == "render-state":
        cmd_render_state(args)


if __name__ == "__main__":
    main()
