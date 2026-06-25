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
import urllib.request
import urllib.error

# ── Jira URL pattern ──────────────────────────────────────────────────────────

JIRA_URL_PATTERN = re.compile(r'https?://[\w.-]+/(?:browse|issues)/([A-Za-z]+-\d+)')


# ── Feishu API helpers (same pattern as feishu_notifier.py) ───────────────────

def _request(method, url, data=None, headers=None):
    if headers is None:
        headers = {}
    headers.setdefault("Content-Type", "application/json")
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"[feishu] HTTP {e.code}: {e.read().decode()[:200]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[feishu] Request error: {e}", file=sys.stderr)
        return None


def get_tenant_token(app_id, app_secret):
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    resp = _request("POST", url, {"app_id": app_id, "app_secret": app_secret})
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
    resp = _request("GET", url, headers=headers)
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


def is_thread_reply(message):
    """Check if a message is a thread reply (not a topic starter)."""
    # Feishu thread replies have thread_id or parent_id in message body
    msg_body = message.get("body", {})
    content = msg_body.get("content", "")
    if not content:
        return False
    try:
        content_dict = json.loads(content) if isinstance(content, str) else content
        # thread replies in Feishu have different content structure
        # Check by message type and the absence of thread info in root
        return False  # will refine based on actual data
    except (json.JSONDecodeError, TypeError):
        return False


def main():
    parser = argparse.ArgumentParser(description="Scan Feishu group for Jira URLs")
    parser.add_argument("--app-id", default=os.environ.get("FEISHU_APP_ID", ""))
    parser.add_argument("--app-secret", default=os.environ.get("FEISHU_APP_SECRET", ""))
    parser.add_argument("--chat-id", default=os.environ.get("FEISHU_CHAT_ID",
                        "oc_254e95f0687245b9df82ab8bf823ca54"))
    parser.add_argument("--jira-host", default=os.environ.get("JIRA_HOST", ""))
    parser.add_argument("--state-file", default="/tmp/codereview-feishu-state.json")
    parser.add_argument("--output", default="/tmp/codereview-scan-result.json")
    args = parser.parse_args()

    if not args.app_id or not args.app_secret:
        print(json.dumps({"error": "FEISHU_APP_ID and FEISHU_APP_SECRET required", "items": []}))
        sys.exit(0)

    # ── Load state (last scan timestamps and processed message IDs) ──
    state = {}
    if os.path.exists(args.state_file):
        try:
            with open(args.state_file) as f:
                state = json.load(f)
        except Exception:
            state = {}

    processed_ids = set(state.get("processed_ids", []))
    last_start_time = state.get("last_start_time", 0)

    # ── Get Feishu token ──
    token = get_tenant_token(args.app_id, args.app_secret)
    if not token:
        print(json.dumps({"error": "Failed to get Feishu token", "items": []}))
        sys.exit(0)

    # ── Calculate time window ──
    # Use 10-digit Unix seconds for Feishu API
    now_sec = int(time.time())
    # On first scan (no state), look back 24h to catch any existing topic starters
    window_start = now_sec - 86400 if not last_start_time else max(last_start_time // 1000, now_sec - 86400)
    # Also cap at 70s going forward for incremental scans
    window_start = max(window_start, now_sec - 70) if last_start_time else window_start

    print(f"[feishu] Scanning messages from {window_start} to {now_sec}", flush=True)

    all_messages = []
    page_token = None
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
        if items and not page_token and len(all_messages) < 2:
            # Print first message as debug sample
            sample = items[0]
            print(f"[feishu] Sample: id={sample.get('message_id','')} type={sample.get('msg_type','')} "
                  f"chat_id={sample.get('chat_id','')} thread_id={sample.get('thread_id','')} "
                  f"parent_id={sample.get('parent_id','')} "
                  f"sender_type={sample.get('sender',{}).get('sender_type','') if isinstance(sample.get('sender'), dict) else '?'}", flush=True)
        all_messages.extend(items)

        if not data.get("has_more"):
            break
        page_token = data.get("page_token")

    print(f"[feishu] Fetched {len(all_messages)} messages", flush=True)

    # ── Extract Jira URLs from unprocessed messages ──
    # We only process topic starters (not thread replies)
    items = []
    new_processed = set(processed_ids)
    for msg in all_messages:
        msg_id = msg.get("message_id", "")
        if not msg_id or msg_id in processed_ids:
            continue

        # Check if it's a thread reply — skip those
        # Feishu sets thread_id on ALL messages in group chats, but only
        # thread replies have parent_id set (indicating they're replies in a topic)
        if msg.get("parent_id"):
            new_processed.add(msg_id)
            continue

        # Get message content
        msg_type = msg.get("msg_type", "")
        if msg_type not in ("text", "post"):
            new_processed.add(msg_id)
            continue

        body = msg.get("body", {})
        content = body.get("content", "")
        if not content:
            new_processed.add(msg_id)
            continue

        # Extract text from different message types
        text = ""
        try:
            content_dict = json.loads(content) if isinstance(content, str) else content
            if msg_type == "text":
                text = content_dict.get("text", "")
            elif msg_type == "post":
                # Post content: {"zh_cn": {"content": [[{"tag": "text", "text": "..."}]]}}
                post_content = content_dict.get("zh_cn", {})
                for paragraph in post_content.get("content", []):
                    for segment in paragraph:
                        if segment.get("tag") == "text":
                            text += segment.get("text", "")
        except (json.JSONDecodeError, TypeError, AttributeError):
            text = str(content)

        if not text:
            new_processed.add(msg_id)
            continue

        # Check for Jira URLs
        jira_matches = extract_jira_urls(text)
        if not jira_matches:
            new_processed.add(msg_id)
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
            new_processed.add(msg_id)

        # Mark message as processed (even if we already added it above)
        new_processed.add(msg_id)

    # ── Save state ──
    # Keep last 1000 processed IDs, trim oldest
    processed_list = list(new_processed)
    if len(processed_list) > 1000:
        processed_list = processed_list[-1000:]

    state = {
        "processed_ids": processed_list,
        "last_scan_time": now_sec,
        "last_start_time": window_start,
    }
    with open(args.state_file, "w") as f:
        json.dump(state, f, indent=2)

    # ── Write output ──
    result = {
        "items": items,
        "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_messages": len(all_messages),
        "jira_found": len(items),
    }
    with open(args.output, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(json.dumps({"summary": f"Scanned {len(all_messages)} messages, found {len(items)} Jira URLs",
                      "count": len(items)}))
    for item in items:
        print(f"  → {item['issue_key']} ({item['jira_url']})", flush=True)


if __name__ == "__main__":
    main()
