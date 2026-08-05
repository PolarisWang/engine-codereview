#!/usr/bin/env python3
"""
Feishu Scanner — Poll Feishu group chat for new messages containing Jira URLs.

Called by Jenkins cron when no explicit JIRA_URL is provided.
Detects new topic messages with Jira links, then triggers review.

Outputs JSON list of detected Jira URLs with message context.

Usage:
    python3 feishu_scanner.py \
        --app-id "xxx" --app-secret "xxx" \
        --chat-id "oc_xxxx" \
        --state-file /tmp/codereview-feishu-state.json \
        --jira-host "https://jira.boomingtechs.cn" \
        --output /tmp/scan_result.json
"""
import argparse
import json
import os
import re
import sys
import time

# Shared helpers: Jira URL pattern, HTTP with retry
from common import JIRA_URL_PATTERN, http_request


# ── Feishu API helpers ───────────────────────────────────────────────────────

def get_tenant_token(app_id, app_secret):
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    resp = http_request("POST", url, {"app_id": app_id, "app_secret": app_secret})
    if resp and resp.get("code") == 0:
        return resp["tenant_access_token"]
    print(f"[feishu] Failed to get tenant token: {resp}", file=sys.stderr)
    return None


# ── Message polling ───────────────────────────────────────────────────────────

def list_messages(token, chat_id, page_size=50, page_token=None, start_time=None, end_time=None):
    """
    List messages from a group chat using Feishu API.
    GET /open-apis/im/v1/messages?container_id_type=chat&container_id={chat_id}
    """
    params = f"container_id_type=chat&container_id={chat_id}&page_size={page_size}&sort_type=ByCreateTimeDesc"
    if page_token:
        params += f"&page_token={page_token}"
    if start_time:
        params += f"&start_time={start_time}"
    if end_time:
        params += f"&end_time={end_time}"
    url = f"https://open.feishu.cn/open-apis/im/v1/messages?{params}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    resp = http_request("GET", url, headers=headers)
    if resp and resp.get("code") != 0:
        print(f"[feishu] list_messages error: code={resp.get('code')} msg={resp.get('msg')}",
              file=sys.stderr)
    return resp


def extract_jira_urls(text):
    """Extract Jira issue keys from text, return list of (full_url, issue_key)."""
    matches = JIRA_URL_PATTERN.findall(text)
    results = []
    for issue_key in matches:
        # Find the actual URL in the text
        m = re.search(rf'(https?://[\w.-]+/(?:browse|issues)/{re.escape(issue_key)})', text)
        if m:
            results.append((m.group(1), issue_key))
    return results


def main():
    parser = argparse.ArgumentParser(description="Scan Feishu group for Jira URLs")
    parser.add_argument("--app-id", default=os.environ.get("FEISHU_APP_ID", ""))
    parser.add_argument("--app-secret", default=os.environ.get("FEISHU_APP_SECRET", ""))
    parser.add_argument("--chat-id", default=os.environ.get("FEISHU_CHAT_ID",
                        "oc_254e95f0687245b9df82ab8bf823ca54"))
    parser.add_argument("--jira-host", default=os.environ.get("JIRA_HOST", ""))
    parser.add_argument("--state-file", default="/tmp/codereview-feishu-state.json")
    parser.add_argument("--pipeline-state-file", default="",
                        help="Optional path to the topic pipeline state; when set, "
                             "already-CLOSED topic message_ids are filtered out of the "
                             "candidate list (defense-in-depth closure guard).")
    parser.add_argument("--output", default="/tmp/codereview-scan-result.json")
    args = parser.parse_args()

    if not args.app_id or not args.app_secret:
        print(json.dumps({"error": "FEISHU_APP_ID and FEISHU_APP_SECRET required", "items": []}))
        sys.exit(0)

    # ── Load state (time-window cursor) ──
    # The scan window is anchored on the last successful scan time (cursor),
    # with a small overlap so a message posted between runs is not missed even
    # if this cron tick or the API is briefly flaky. The cursor only advances on
    # a successful scan; on failure it is left unchanged so the next run re-scans
    # the same window (no gaps).
    state = {}
    try:
        with open(args.state_file, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}

    # ── Get Feishu token ──
    token = get_tenant_token(args.app_id, args.app_secret)
    if not token:
        print(json.dumps({"error": "Failed to get Feishu token", "items": []}))
        sys.exit(0)

    # ── Calculate time window (cursor-based, with overlap) ──
    #   - First run (no cursor): scan the last INITIAL_WINDOW seconds.
    #   - Subsequent runs: start at `last_scan_time - OVERLAP` to close gaps,
    #     end at now. Repeated messages within the overlap are de-duplicated
    #     downstream by message_id.
    INITIAL_WINDOW = 300    # sec — how far back from first run with no cursor
    OVERLAP = 60            # sec — overlap pulled back from the previous cursor
    MAX_GAP = 86400         # sec — ignore a stale cursor older than 1 day
    now_sec = int(time.time())

    last_scan = state.get("last_scan_time")
    if last_scan and (now_sec - last_scan) < MAX_GAP:
        window_start = last_scan - OVERLAP
        source = "cursor-based"
    else:
        window_start = now_sec - INITIAL_WINDOW
        source = "initial window"

    print(f"[feishu] Scanning messages from {window_start} to {now_sec} ({source})", flush=True)

    all_messages = []
    page_token = None
    scan_succeeded = False
    while True:
        resp = list_messages(token, args.chat_id, page_size=50, page_token=page_token,
                             start_time=window_start, end_time=now_sec)
        if not resp or resp.get("code") != 0:
            err_msg = resp.get("msg", "unknown") if resp else "no response"
            print(f"[feishu] List messages error: {err_msg}", file=sys.stderr)
            break

        data = resp.get("data", {})
        items = data.get("items", [])
        print(f"[feishu] Page: {len(items)} messages (has_more={data.get('has_more')})", flush=True)
        # Detailed per-message sample dropped to avoid PII/noise (message_id,
        # sender ids etc. are not needed in the build log).
        all_messages.extend(items)

        if not data.get("has_more"):
            scan_succeeded = True
            break
        page_token = data.get("page_token")

    print(f"[feishu] Fetched {len(all_messages)} messages", flush=True)

    # ── Extract Jira URLs from all topic messages (no processed_ids dedup) ──
    # We only process topic starters (not thread replies)
    items = []
    for msg in all_messages:
        msg_id = msg.get("message_id", "")

        # Check if it's a thread reply — skip those
        # Feishu sets thread_id on ALL messages in group chats, but only
        # thread replies have parent_id set
        if msg.get("parent_id"):
            continue

        # Get message content
        msg_type = msg.get("msg_type", "")
        if msg_type not in ("text", "post"):
            continue

        body = msg.get("body", {})
        content = body.get("content", "")
        if not content:
            continue

        # Extract text from different message types
        text = ""
        try:
            content_dict = json.loads(content) if isinstance(content, str) else content
            if msg_type == "text":
                text = content_dict.get("text", "")
            elif msg_type == "post":
                paragraphs = []
                if isinstance(content_dict, dict):
                    for locale_key in ("zh_cn", "en_us"):
                        pc = content_dict.get(locale_key)
                        if isinstance(pc, dict) and pc.get("content"):
                            paragraphs = pc["content"]
                            break
                    if not paragraphs and content_dict.get("content"):
                        raw = content_dict["content"]
                        if isinstance(raw, list) and len(raw) > 0:
                            paragraphs = raw
                for paragraph in paragraphs:
                    for seg in paragraph:
                        if seg.get("tag") == "text":
                            text += seg.get("text", "")
                        elif seg.get("tag") == "a":
                            text += seg.get("href", "")
        except (json.JSONDecodeError, TypeError, AttributeError):
            text = str(content)

        if not text:
            continue

        # Check for Jira URLs
        jira_matches = extract_jira_urls(text)
        if not jira_matches:
            continue

        # Found a Jira URL in this message — this is a review candidate
        for jira_url, issue_key in jira_matches:
            sender = msg.get("sender", {})
            items.append({
                "message_id": msg_id,
                "jira_url": jira_url,
                "issue_key": issue_key,
                "text": text[:500],
                "sender_id": sender.get("id", "") if isinstance(sender, dict) else "",
                "sender_name": sender.get("name", "") if isinstance(sender, dict) else "",
                "create_time": msg.get("create_time", ""),
            })

    # ── Save state (advance cursor only on successful scan) ──
    # On failure we keep the previous cursor so the next run re-scans the same
    # window and does not miss messages.
    if scan_succeeded:
        state["last_scan_time"] = now_sec
        with open(args.state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    else:
        print("[feishu] Scan incomplete (API error) — cursor NOT advanced, will retry", file=sys.stderr)

    # ── Defense-in-depth: drop topics already CLOSED in the pipeline state ──
    if args.pipeline_state_file:
        try:
            with open(args.pipeline_state_file, encoding="utf-8") as f:
                pstate = json.load(f)
            topics = pstate.get("topics") or {}
            closed_ids = {k for k, t in topics.items() if (t or {}).get("phase") == "CLOSED"}
            if closed_ids:
                before = len(items)
                items = [it for it in items if it.get("message_id") not in closed_ids]
                if len(items) != before:
                    print(f"[feishu] skipped {before - len(items)} CLOSED topic(s)", flush=True)
        except (FileNotFoundError, json.JSONDecodeError):
            print("[feishu] could not read pipeline state for CLOSED filter; continuing", file=sys.stderr)

    # ── Write output ──
    result = {
        "items": items,
        "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_messages": len(all_messages),
        "jira_found": len(items),
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(json.dumps({"summary": f"Scanned {len(all_messages)} messages, found {len(items)} Jira URLs",
                      "count": len(items)}))
    for item in items:
        print(f"  → {item['issue_key']} ({item['jira_url']})", flush=True)


if __name__ == "__main__":
    main()
