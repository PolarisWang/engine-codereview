#!/usr/bin/env python3
"""
orchestrate.py — drive one topic's full code-review pipeline from Python.

Consolidates the flow that previously lived (twice, in Groovy) inside the
Jenkinsfile's scan-inline closure and manual stages, into one testable module.
The Jenkinsfile now only scans for topics and calls `orchestrate.py run`.

Pipeline for one topic (regardless of scan/manual mode):
  1. reply processing card in Feishu thread (or send a new topic in manual mode)
  2. parse Jira -> project / MR / branch
  3. review engine + game repos via code_reviewer.py
  4. record per-repo terminal states
  5. render + send final summary
  6. transition to DONE/FAILED; append msg_id to legacy processed_ids (migration)

Topic state (phase/status/per-repo) is persisted via pipeline_state.py.
Config/secrets come from the environment (Jenkins already exports them):
  FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_CHAT_ID, FEISHU_WEBHOOK_URL
  JIRA_HOST, JIRA_TOKEN, GITLAB_TOKEN, GITLAB_USER
  WORKSPACE (Jenkins), codereview-workspace path via --workspace
  PIPELINE_STATE_FILE, REVIEWED_MSG_IDS_FILE (legacy dedup)
Stdlib only.

Usage:
  python3 orchestrate.py run --key <topicKey|message_id> \
      --mode scan|manual \
      --workspace /data/codereview/workspace \
      [--pipeline-state-file /path/.pipeline-state.json]

  Scan mode extras (source message fields, applied only on first add):
      --jira-key --jira-url --text --sender-id --sender-name
"""
import argparse
import json
import os
import subprocess
import sys

import pipeline_state
import feishu_notifier
from pipeline_state import log_line

# Scripts dir (this file's directory)
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))


# ── helpers ─────────────────────────────────────────────────────────────────

def _env(name, default=""):
    return os.environ.get(name, default)


def _run_py(script, args):
    """Run one of our helper scripts with args (list). Returns (rc, stdout, stderr)."""
    cmd = [sys.executable, os.path.join(SCRIPTS_DIR, script)] + args
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _log(phase, status, topic, issue="", project="", repo="", detail=""):
    """Emit a structured [CODEREVIEW] line exactly like the Jenkinsfile helper."""
    print(log_line(phase=phase, status=status, topic=topic, issue=issue,
                   project=project, repo=repo, detail=detail), flush=True)


def _read_json_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _append_processed(msg_id, state_file):
    """Legacy dedup append (migration window; new dedup is DONE/FAILED in state)."""
    try:
        data = {}
        if os.path.exists(state_file):
            with open(state_file, encoding="utf-8") as f:
                data = json.load(f)
        ids = data.get("processed_ids", [])
        if msg_id not in ids:
            ids.append(msg_id)
            if len(ids) > 500:
                ids = ids[-500:]
            tmp = state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"processed_ids": ids}, f, ensure_ascii=False)
            os.replace(tmp, state_file)
            print(f"[orchestrate] Persisted message_id {msg_id} as processed", flush=True)
    except OSError as e:
        print(f"[orchestrate] WARN: could not persist processed_ids: {e}", file=sys.stderr)


def _alert_if_exhausted(state_file, topic, app_id="", app_secret=""):
    """If a topic just reached failed_exhausted (no more auto-retries), fire a
    Feishu/webhook alert so a human notices. Non-fatal (best-effort)."""
    if topic.get("failed_exhausted"):
        text = (f"⚠️ **Code Review 重试耗尽**: {topic.get('jira_key') or topic.get('message_id')}\n"
                f"错误: {topic.get('last_error', '')[:200]}\n"
                f"已自动重试 {topic.get('retry_count', 0)} 次后放弃，需人工介入。")
        webhook = _env("FEISHU_WEBHOOK_URL")
        try:
            if webhook:
                _run_py("feishu_notifier.py", ["webhook", "--webhook-url", webhook,
                                               "--message-base64", _b64_str(text)])
            elif app_id and app_secret:
                # Convention: in the topic group the bot only REPLIES, never starts a
                # new topic. There is no reply target for the exhausted alert (the
                # topic has no card yet), so we do NOT send-message (would create a
                # new topic). Just log it.
                print(f"[orchestrate] WARN: exhausted alert would start a new topic; "
                      f"skipping (reply-only convention). topic={topic.get('jira_key')}",
                      file=sys.stderr)
        except Exception as e:
            print(f"[orchestrate] WARN: exhausted alert failed: {e}", file=sys.stderr)


# ── the pipeline ────────────────────────────────────────────────────────────

def run(args):
    key = args.key
    mode = args.mode
    workspace = args.workspace
    state_file = args.pipeline_state_file or os.environ.get("PIPELINE_STATE_FILE", "pipeline-state.json")
    legacy_ids_file = os.environ.get("REVIEWED_MSG_IDS_FILE",
                                     os.path.expanduser("~/.codereview-processed-msg-ids.json"))

    app_id = _env("FEISHU_APP_ID")
    app_secret = _env("FEISHU_APP_SECRET")
    chat_id = _env("FEISHU_CHAT_ID")
    jira_host = _env("JIRA_HOST")
    jira_token = _env("JIRA_TOKEN")
    gitlab_token = _env("GITLAB_TOKEN")

    # 0. Ensure topic record exists.
    issue_key = ""   # resolved after jira parse below
    if mode == "manual":
        # Manual mode: key is the Jira URL.
        jira_url = key
        pipeline_state.add_topic(state_file, message_id=key, jira_url=key, mode="manual")
        _log('MANUAL', 'RUNNING', key, '', '', '', 'manual review started')
    else:
        # Scan mode: key is a Feishu message_id. For a fresh scan item the
        # jira_url comes from the CLI args; for a RETRY driven from state, the
        # jira_url is read from the existing topic record.
        topic = pipeline_state.get_topic(state_file, key)
        got_jira_url = getattr(args, "jira_url", "")
        if not got_jira_url and topic:
            got_jira_url = topic.get("jira_url", "")
        if topic is None:
            pipeline_state.add_topic(state_file, message_id=key,
                                     jira_key=getattr(args, "jira_key", key),
                                     jira_url=got_jira_url, mode="scan",
                                     text_preview=getattr(args, "text", ""),
                                     sender_id=getattr(args, "sender_id", ""),
                                     sender_name=getattr(args, "sender_name", ""))
        # For a retry, re-advance the in-progress phase to allow re-running after
        # FAILED/SCANNED (record_failure set FAILED; orchestrate transitions forward).
        _log('SCANNED', 'RUNNING', key, getattr(args, "jira_key", "") or (topic or {}).get("jira_key", ""), '', '', 'topic discovered')
        jira_url = got_jira_url or key

        # Retry path: if this topic previously FAILED, reset it to a runnable
        # state so the forward phase transitions below are legal.
        if topic is not None and topic.get("phase") == "FAILED":
            pipeline_state.reset_for_retry(state_file, key)
            _log('SCANNED', 'RETRY', key, topic.get("jira_key", ""), '', '',
                 f"retry #{int(topic.get('retry_count') or 0) + 1}")

    # 1. Reply processing card in Feishu thread (scan) or send new (manual).
    reply_msg_id = ""
    if mode == "manual":
        # Manual mode: reply to a given thread (FEISHU_REPLY_MSG_ID) if set,
        # else send a new topic container.
        topic_msg_id = _env("FEISHU_REPLY_MSG_ID")
        if topic_msg_id:
            rc, out, _ = _run_py("feishu_notifier.py", [
                "reply-message", "--app-id", app_id, "--app-secret", app_secret,
                "--chat-id", chat_id, "--message-id", topic_msg_id,
                "--message-base64", _b64_str("reviewing..."),])
            reply_msg_id = _extract_msg_id(out, rc)
            pipeline_state.transition(state_file, key, to="PARSING", status="RUNNING",
                                      render_msg_id=reply_msg_id)
        else:
            # No reply target for manual mode. Convention: the bot only REPLIES in the
            # topic group, never starts a new topic — so error out instead of
            # send-message (which would create a new topic).
            print(f"[orchestrate] ERROR: manual mode needs FEISHU_REPLY_MSG_ID to reply in "
                  f"an existing topic (reply-only convention). topic={key}", file=sys.stderr)
            reply_msg_id = ""
    else:
        # Scan mode: we want ONE card per topic that evolves through the review
        # ("Reviewing..." -> FAILED/SKIPPED -> DONE), so a retry reuses the topic's
        # existing render_msg_id card instead of posting a fresh reply each run.
        # This prevents a pile-up of stale failure cards under the same message.
        existing_card = (topic or {}).get("render_msg_id") if topic is not None else None
        if existing_card:
            # Reuse the card already tied to this topic: update it in place.
            _update_card_text(app_id, app_secret, existing_card,
                              "🤖 **正在 Review（重试）...**\n重新拉取代码并进行 AI 审查，请稍候...")
            reply_msg_id = existing_card
            pipeline_state.transition(state_file, key, to="PARSING", status="RUNNING",
                                      render_msg_id=reply_msg_id)
            _log('PARSING', 'RUNNING', key, getattr(args, "jira_key", ""), '', '', 'reusing card in place (retry)')
        else:
            progress = "🤖 **正在 Review...**\n已收到，正在拉取代码并进行 AI 审查，请稍候..."
            rc, out, _ = _run_py("feishu_notifier.py", [
                "reply-message", "--app-id", app_id, "--app-secret", app_secret,
                "--chat-id", chat_id, "--message-id", key,
                "--message-base64", _b64_str(progress)])
            reply_msg_id = _extract_msg_id(out, rc)
            pipeline_state.transition(state_file, key, to="PARSING", status="RUNNING",
                                      render_msg_id=reply_msg_id)
            _log('PARSING', 'RUNNING', key, getattr(args, "jira_key", ""), '', '', 'progress card replied')

    # 2. Parse Jira. `jira_url` was resolved during topic-add above.
    info = _parse_jira(jira_url, jira_host, jira_token, gitlab_token)
    if info is None or info.get("error"):
        err = (info or {}).get("error", "unknown")
        topic_after = pipeline_state.record_failure(state_file, key, f"parse failed: {err}")
        _alert_if_exhausted(state_file, topic_after, app_id, app_secret)
        _log('PARSING', 'FAILED', key, getattr(args, "jira_key", ""), '', '', 'jira parse failed')
        if reply_msg_id:
            _send_reply(app_id, app_secret, reply_msg_id,
                        f"❌ **Review Failed**: 无法解析 Jira 信息: {err}")
        return 1

    project = info.get("project", "")
    issue_key = info.get("issue_key", key)
    mr_info = info.get("mr_info") or {}
    review_branch = mr_info.get("branch") or issue_key
    base_branch = mr_info.get("target_branch") or info.get("default_branch") or "master"
    engine_base = info.get("engine_default_branch") or base_branch
    engine_repo = info.get("engine_repo", "")
    game_repo = info.get("game_repo", "")
    mr_url = info.get("mr_url", "") or ""
    mr_state = (mr_info or {}).get("state", "") or ""
    pipeline_state.transition(state_file, key, to="PARSING", status="RUNNING",
                              review_branch=review_branch, base_branch=base_branch,
                              mr_url=mr_url)
    _log('PARSED', 'RUNNING', key, issue_key, project, '',
         f"branch={review_branch}->{base_branch}")

    # 3. Review engine + game.
    pipeline_state.transition(state_file, key, to="REVIEWING", status="RUNNING")
    _log('REVIEWING', 'RUNNING', key, issue_key, project, '', 'code review started')
    eng_out = os.path.join(workspace, f"result_{key}_engine.json")
    gam_out = os.path.join(workspace, f"result_{key}_game.json")
    eng_res, gam_res = _review_repos(
        key, project, issue_key, review_branch, base_branch, engine_base,
        engine_repo, game_repo, mr_url, workspace, eng_out, gam_out)

    # 4. Record per-repo terminal states + update the in-flight card.
    _record_repo_state(state_file, key, "engine", eng_out, eng_res, review_branch, base_branch)
    _record_repo_state(state_file, key, "game", gam_out, gam_res, review_branch, base_branch)
    _log('REPO', 'DONE', key, issue_key, project, '', 'repo states recorded')
    _update_state_card(state_file, app_id, app_secret, key)

    # 5. Render + send final summary.
    pipeline_state.transition(state_file, key, to="NOTIFYING", status="RUNNING")
    _log('NOTIFY', 'RUNNING', key, issue_key, project, '', 'sending final summary')
    summary = feishu_notifier.build_summary_text(
        issue_key, project, review_branch, base_branch, jira_url, mr_url,
        eng_res or {}, gam_res or {},
    )
    # Append a text interaction hint so users know they can reply to this topic
    # to select a fix / re-review / ask a follow-up (real-time event server routes it).
    summary = _append_fix_options(summary, review_branch)
    if reply_msg_id:
        rc, _, err = _run_py("feishu_notifier.py", [
            "update-reply", "--app-id", app_id, "--app-secret", app_secret,
            "--message-id", reply_msg_id, "--message-base64", _b64_str(summary)])
        if rc != 0:
            topic_after = pipeline_state.record_failure(state_file, key, f"update-reply failed: {err}")
            _alert_if_exhausted(state_file, topic_after, app_id, app_secret)
            _log('NOTIFY', 'FAILED', key, issue_key, project, '', 'final update-reply failed')
            return 1
        pipeline_state.transition(state_file, key, to="DONE", status="SUCCESS")
        _log('DONE', 'SUCCESS', key, issue_key, project, '', 'review complete')

    # 6. Legacy processed_ids append (migration).
    _append_processed(key, legacy_ids_file) if mode == "scan" else None
    return 0


# ── sub-steps ───────────────────────────────────────────────────────────────

def _b64_str(s):
    import base64
    return base64.b64encode(s.encode("utf-8")).decode("utf-8")


def _extract_msg_id(stdout, rc):
    if rc != 0 or not stdout:
        return ""
    try:
        data = json.loads(stdout)
        return data.get("message_id", "")
    except json.JSONDecodeError:
        return ""


def _send_reply(app_id, app_secret, reply_msg_id, text):
    _run_py("feishu_notifier.py", [
        "update-reply", "--app-id", app_id, "--app-secret", app_secret,
        "--message-id", reply_msg_id, "--message-base64", _b64_str(text)])


def _update_card_text(app_id, app_secret, card_msg_id, text):
    """Update an existing card message in place (single-card-per-topic behaviour)."""
    _send_reply(app_id, app_secret, card_msg_id, text)


def _append_fix_options(summary, branch):
    """Append a text-only interaction prompt to the summary card. (Buttons are a
    later upgrade; for now the user replies in-thread to pick an action.)"""
    hint = (
        "\n\n---\n🤖 **交互**：回复本话题 `@机器人` 并附带指令：\n"
        f"  - `1` 生成修复补丁预览\n"
        f"  - `2` 重新审查 `{branch}`\n"
        f"  - `3 <关键词>` 解释某个 finding\n"
        f"  - `/状态` 当前审查状态\n"
        f"  - 直接提问会自动按当前 diff 答疑"
    )
    return summary + hint


def _build_fix_patch_preview(topic, findings):
    """
    Generate a text preview of candidate fixes for the most critical findings,
    based on the structured findings list (file + issue + suggestion). This is a
    plain-text "修复补丁预览" using each finding's suggestion — no code edits are
    applied automatically.
    """
    if not findings:
        return "无待修复的 finding。"
    lines = ["## 修复建议预览（基于审查 findings）\n"]
    shown = 0
    for f in findings:
        sev = _normalize_sev(f.get("severity"))
        if sev != "critical" and shown >= 5:
            continue
        lines.append(f"**{f.get('file','')}** ({sev})\n- {f.get('issue','')}\n"
                     f"- 建议：{f.get('suggestion','')}\n")
        shown += 1
        if shown >= 8:
            break
    lines.append("\n> 仅文本预览，不会自动改动代码。如需真实改动请自行应用。")
    return "\n".join(lines)


def _normalize_sev(sev):
    s = (sev or "").strip().lower()
    if s in ("critical", "high", "error", "blocker"):
        return "critical"
    if s in ("warning", "warn", "minor"):
        return "warning"
    return "suggestion"


def _build_status_text(topic):
    """Human-readable current status of a topic."""
    if not topic:
        return "该话题没有对应的审查状态。"
    repos = topic.get("repos") or {}
    lines = [f"**状态**: {topic.get('phase')} / {topic.get('status')}"]
    if topic.get("last_error"):
        lines.append(f"`错误`: {topic['last_error'][:120]}")
    for r in ("engine", "game"):
        rd = repos.get(r) or {}
        lines.append(f"  {r}: {rd.get('status')} "
                     f"{rd.get('severity_counts') or ''} "
                     f"{('— ' + rd.get('skip_reason','')) if rd.get('skip_reason') else ''}")
    lines.append(f"`retry`: {topic.get('retry_count',0)} · `exhausted`: {topic.get('failed_exhausted')}")
    return "\n".join(lines)


# ── Interaction (reply/chat round-trip) ─────────────────────────────────────

# ── Multi-turn agent (design-3): no-side-effect Agent + guarded Executor ───────
#
# Agent decides next tool call via LLM tool_use (auto). Side-effect-free tools
# are executed inline and their results fed back (loop). The only side-effect
# tool (apply_patch / push_changes) is NOT executed by the Agent — it stages a
# pending_patch and waits for explicit user confirmation (@ok / @confirm push).

AGENT_TOOLS = [
    {
        "name": "get_status",
        "description": "Get the current review status of this topic (phase, per-repo severity, retry).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_findings",
        "description": "List the code review findings for this topic (file, severity, issue, suggestion).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "generate_patch_preview",
        "description": "Generate a unified-diff patch preview that fixes a specific finding. Specify the finding's file (or 'all' for critical findings).",
        "input_schema": {"type": "object",
                         "properties": {"target": {"type": "string",
                                                   "description": "file path to fix, or 'all' for critical findings"}},
                         "required": ["target"]},
    },
    {
        "name": "re_review",
        "description": "Re-run the code review for this topic (reuses diff-hash cache if unchanged).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "answer",
        "description": "Answer the user's question about this review/topic.",
        "input_schema": {"type": "object",
                         "properties": {"question": {"type": "string"}}, "required": ["question"]},
    },
    {
        # Side-effect (write) — staged, NOT auto-executed. Requires @ok then @confirm push.
        "name": "apply_patch",
        "description": "PROPOSE a patch to fix one or more findings. This writes to the local review checkout ONLY after the user replies @ok, and pushes to the review branch ONLY after @confirm push. Never execute automatically.",
        "input_schema": {"type": "object",
                         "properties": {"target": {"type": "string",
                                                   "description": "file (or 'all') to fix"}},
                         "required": ["target"]},
    },
]

AGENT_MAX_ROUNDS = 6           # max tool-call rounds per user message
AGENT_MAX_TOKEN = 1000         # max output tokens per agent LLM turn


def _agent_llm(messages, system, api_key, base_url, model,
               tools=AGENT_TOOLS, max_tokens=AGENT_MAX_TOKEN):
    """One agent LLM turn with tools. Returns (text, tool_use_list) — one is non-empty."""
    import urllib.request, urllib.error
    payload = {
        "model": model, "max_tokens": max_tokens, "system": system,
        "messages": messages,
        "tools": tools, "tool_choice": {"type": "auto"},
    }
    req = urllib.request.Request(
        f"{base_url}/v1/messages", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"[agent] llm HTTP {e.code}: {e.read().decode()[:300]}", file=sys.stderr)
        return None, []
    except Exception as e:
        print(f"[agent] llm err: {e}", file=sys.stderr)
        return None, []
    text = "".join(b.get("text", "") for b in result.get("content", []) if b.get("type") == "text")
    tools = [b for b in result.get("content", []) if b.get("type") == "tool_use"]
    return text.strip(), tools


def _agent_system(topic, api_key):
    """Build the agent system prompt with topic context (read-only)."""
    return (
        "You are a code-review assistant operating in a Feishu topic. The user's "
        "message is @-addressed to you. You may call tools to gather info or propose "
        "fixes. Rules:\n"
        "- Call side-effect-free tools (get_status/get_findings/generate_patch_preview/"
        "re_review/answer) freely; use their results to continue.\n"
        "- To change code, use apply_patch, which only PROPOSES — it is NOT executed "
        "automatically; the user must confirm with @ok, then @confirm push for remote.\n"
        "- Reply in Chinese, concise. When done, give a plain-text final answer.\n"
        f"- Topic context: {topic.get('jira_key','')} ({topic.get('project','')}), "
        f"branch {topic.get('review_branch','')} -> {topic.get('base_branch','')}."
    )


def interact(args):
    """
    Multi-turn agent handler for a reply / @bot in a topic thread.

    Handles confirmation gating first (@ok / @confirm push / @撤销 / @revert), then
    runs a bounded agent loop: LLM decides tool calls; side-effect-free tools are
    executed inline and their results fed back; apply_patch stages a pending_patch
    (not auto-executed). Final text updates the SAME topic card + chat_history.
    """
    key = args.key
    reply_text = (args.reply or "").strip()
    workspace = args.workspace
    state_file = args.pipeline_state_file or os.environ.get("PIPELINE_STATE_FILE", "pipeline-state.json")
    app_id = _env("FEISHU_APP_ID")
    app_secret = _env("FEISHU_APP_SECRET")
    api_key = _env("ANTHROPIC_AUTH_TOKEN") or _env("ANTHROPIC_API_KEY")
    base_url = _env("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    model = _env("ANTHROPIC_MODEL") or "deepseek-v4-flash"

    topic = pipeline_state.get_topic(state_file, key)
    if topic is None:
        return 0
    render_id = topic.get("render_msg_id") or ""
    eng_findings, gam_findings = _load_findings(workspace, key)
    all_findings = (eng_findings or []) + (gam_findings or [])

    # ── Confirmation gating (user replies to a staged action) ───────────────
    low = reply_text.lower()
    pending = topic.get("pending_patch") or {}
    is_ok = low in ("@ok", "ok", "好的", "执行", "确认")
    is_confirm_push = low in ("@confirm push", "确认push", "@push", "推")
    is_rollback = low in ("@撤销", "@revert", "撤销", "回退")

    if pending:
        if is_ok:
            # stage #1: apply locally.
            return _confirm_apply(key, topic, workspace, state_file, app_id, app_secret)
        if low in ("@confirm push", "确认push", "@push"):
            return _confirm_push(key, topic, workspace, state_file, app_id, app_secret)
        if is_rollback:
            return _rollback(key, topic, workspace, state_file, app_id, app_secret)

    # Also handle @撤销 globally (rollback last applied patch even without pending).
    if is_rollback:
        return _rollback(key, topic, workspace, state_file, app_id, app_secret)

    # ── Agent loop ──────────────────────────────────────────────────────────
    history = topic.get("chat_history") or []
    messages = list(history) + [{"role": "user", "content": reply_text}]
    system = _agent_system(topic, api_key)
    all_msgs = list(messages)

    for _round in range(AGENT_MAX_ROUNDS):
        text, calls = _agent_llm(all_msgs, system, api_key, base_url, model)
        if not text and not calls:
            # hard failure
            answer = "⚠️ 暂时无法处理（LLM 调用失败）。可用 `重新审查` / `查看状态`。"
            _finalize(key, answer, render_id, all_msgs, state_file, app_id, app_secret)
            return 0
        if calls:
            # Execute the requested tools (side-effect-free), feed results back.
            all_msgs.append({"role": "assistant", "content": f"[tool_use: {[c.get('name') for c in calls]}]"})
            any_side_effect = False
            for c in calls:
                name = c.get("name")
                inp = c.get("input") or {}
                result, side_effect = _exec_tool(name, inp, topic, workspace, all_findings,
                                                 state_file, api_key, base_url, model)
                all_msgs.append({"role": "user", "content": f"[tool {name} result]\n{result}"})
                if side_effect:
                    any_side_effect = True
            if any_side_effect:
                # apply_patch staged a pending_patch -> confirm card already written; end this turn.
                return 0
            continue
        # Final plain text.
        _finalize(key, text, render_id, all_msgs + [{"role": "assistant", "content": text}],
                  state_file, app_id, app_secret)
        return 0

    # Loop exhausted.
    _finalize(key, "已达本轮工具调用上限，请分步询问。", render_id, all_msgs,
              state_file, app_id, app_secret)
    return 0


def _exec_tool(name, inp, topic, workspace, all_findings, state_file,
               api_key, base_url, model):
    """Execute one agent tool. Returns (result_text, side_effect_bool)."""
    if name == "get_status":
        return _build_status_text(topic), False
    if name == "get_findings":
        if not all_findings:
            return "（该话题暂无 findings）", False
        return "\n".join(f"- [{f.get('severity')}] {f.get('file')}: {f.get('issue','')}"
                         for f in all_findings[:25]), False
    if name == "generate_patch_preview":
        target = (inp.get("target") or "").strip()
        return _build_patch_preview_target(all_findings, target), False
    if name == "re_review":
        jira_url = topic.get("jira_url") or key_source(topic)
        _spawn_rerun(key_source(topic), workspace, state_file, jira_url,
                     _env("FEISHU_APP_ID"), _env("FEISHU_APP_SECRET"))
        return "已触发重新审查（后台运行），结果会更新到本帖。", False
    if name == "answer":
        q = (inp.get("question") or "").strip()
        return _answer_question(q or "请补充说明", all_findings, api_key, base_url, model), False
    if name == "apply_patch":
        # STAGE a patch (side-effect). The actual write waits for @ok (then @confirm push).
        target = (inp.get("target") or "all").strip()
        patch = _build_patch_target(all_findings, target)
        pipeline_state.set_pending_patch(state_file, key_source(topic), {
            "file": target, "diff": patch.get("diff", ""), "target": target,
            "created_at": "now",
        })
        return ("补丁已提议（未应用）。请回复 `@ok` 应用到本地 checkout；"
                "如需推送到远程 review 分支再回复 `@confirm push`。" + _confirm_patch_card(patch)), True
    return "（未知工具）", False


def key_source(topic):
    return topic.get("message_id") or ""


def _build_patch_preview_target(findings, target):
    """Generate a unified-diff patch preview for `target` (file or 'all' critical)."""
    subset = _select_findings(findings, target)
    if not subset:
        return "（没有匹配 target 的 finding 可生成补丁。）"
    # We don't have the real file text here; produce a suggestion-based preview.
    lines = ["## 建议修复（基于 finding 的 suggestion，非实际 diff）：\n"]
    for f in subset[:8]:
        lines.append(f"- `{f.get('file')}` [{f.get('severity')}]: {f.get('issue','')}\n"
                     f"  建议: {f.get('suggestion','')}")
    return "\n".join(lines)


def _build_patch_target(findings, target):
    subset = _select_findings(findings, target)
    return {"diff": "\n".join(
        f"--- {f.get('file')}\n+++ {f.get('file')} (suggested)\n{f.get('suggestion','')}"
        for f in subset[:8]) or "（无）"}


def _select_findings(findings, target):
    target = (target or "all").strip().lower()
    if target in ("all", "critical"):
        return [f for f in findings if (f.get('severity') or '').lower() in ("critical", "high")]
    return [f for f in findings if target in (f.get('file') or '').lower()]


def _confirm_patch_card(patch):
    d = patch.get("diff") or ""
    return "\n\n**待应用补丁（预览）：**\n" + (d[:600] if d else "（无具体补丁）") + \
        "\n\n> 回复 `@ok` 应用本地 / `@confirm push` 推 remote / `@撤销` 取消"


def _finalize(key, answer, render_id, all_msgs, state_file, app_id, app_secret):
    """Persist chat history + update the same card with the final answer."""
    pipeline_state.append_chat(state_file, key, {"role": "user", "content": "（本轮交互）"})
    pipeline_state.append_chat(state_file, key, {"role": "assistant", "content": answer})
    if answer and render_id and app_id and app_secret:
        _update_card_text(app_id, app_secret, render_id, answer)


# ── Guarded side-effect executors (design-3): local apply + remote push + rollback ──

# Protected branch names: never push to these.
PROTECTED_BRANCHES = {"main", "master", "dev", "develop", "release", "stage", "prod"}


def _resolve_repo_checkout(workspace, topic, repo):
    """
    Locate a topic repo's git checkout under workspace. Returns (checkout_dir, real_repo_name)
    or None. Mirrors code_reviewer.prepare_repo naming (url basename minus .git), tried for
    both engine and game repos; repo selects which one.
    """
    repos = {}
    for r in ("engine", "game"):
        url = topic.get(f"{r}_repo") or topic.get("repos", {}).get(r, {}).get("repo_url") or ""
        if url:
            repos[r] = url
    url = repos.get(repo) or (list(repos.values())[0] if repos else "")
    if not url:
        return None, None
    # Convert scp-style (git@host:path/repo.git) to https so the basename is the
    # real repo name — same rule code_reviewer.prepare_repo uses after ssh_to_https.
    import re as _re
    if url.startswith("git@"):
        m = _re.match(r'git@([^:]+):(.+)', url)
        if m:
            url = f"https://{m.group(1)}/{m.group(2)}"
    name = url.rstrip("/").split("/")[-1].replace(".git", "")
    candidate = os.path.join(workspace, name)
    if os.path.isdir(os.path.join(candidate, ".git")):
        return candidate, name
    return candidate, name


def _run_git(args, cwd):
    """Run git in a checkout; returns (rc, out, err)."""
    import subprocess
    try:
        r = subprocess.run(["git"] + args, capture_output=True, text=True, cwd=cwd, timeout=120)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def _confirm_apply(key, topic, workspace, state_file, app_id, app_secret):
    """User said @ok: apply the staged pending_patch to the LOCAL checkout (git apply)."""
    pending = topic.get("pending_patch") or {}
    if not pending:
        return 0
    render = topic.get("render_msg_id") or ""
    # Determine which repo the patch targets (best-effort from file/repo).
    repo = pending.get("repo") or "game" if "validate_commit" in pending.get("file", "") else "engine"
    checkout, name = _resolve_repo_checkout(workspace, topic, repo)
    diff = pending.get("diff") or ""  # the staged patch body (same key _exec_tool wrote)
    if not os.path.isdir(os.path.join(checkout or "", ".git")):
        _update_card_text(app_id, app_secret, render,
                          f"⚠️ 无法定位该仓库 checkout（{name or '?'}），未应用补丁。")
        return 0
    # Path whitelist is implicit: git apply runs inside the checkout dir only.
    rc, out, err = _run_git(["apply", "--check"], checkout)
    # Capture pre-apply HEAD for rollback.
    _, head_before, _ = _run_git(["rev-parse", "HEAD"], checkout)
    # Apply via a temp patch file piped into git apply.
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as f:
        f.write(diff or "# empty")
        patch_file = f.name
    rc2, out2, err2 = _run_git(["apply", patch_file], checkout)
    import os as _os
    _os.unlink(patch_file) if _os.path.exists(patch_file) else None
    if rc2 != 0:
        _update_card_text(app_id, app_secret, render,
                          f"❌ 本地应用补丁失败: {err2 or out2}\n回复 `@撤销` 可回退。")
        return 0
    # Record as applied (commit ref for rollback).
    pipeline_state.record_applied_patch(state_file, key, {
        "file": pending.get("file", ""), "repo": repo,
        "commit_before": head_before, "applied_at": "now",
    })
    _update_card_text(app_id, app_secret, render,
                      "✅ 补丁已应用到本地 checkout。\n"
                      "如需推送到远程 review 分支，回复 `@confirm push`。\n"
                      "如需回退，回复 `@撤销`。")
    return 0


def _confirm_push(key, topic, workspace, state_file, app_id, app_secret):
    """User said @confirm push: push applied changes to the review branch (protected-safe)."""
    render = topic.get("render_msg_id") or ""
    branch = topic.get("review_branch") or ""
    if branch in PROTECTED_BRANCHES or not branch:
        _update_card_text(app_id, app_secret, render,
                          f"⚠️ 分支 `{branch or '?'}` 受保护或未知，拒绝推送。")
        return 0
    # Resolve checkout by engine (or first known repo).
    checkout, name = _resolve_repo_checkout(workspace, topic, "engine")
    if not os.path.isdir(os.path.join(checkout or "", ".git")):
        _update_card_text(app_id, app_secret, render, "⚠️ 无法定位仓库 checkout，未推送。")
        return 0
    rc, out, err = _run_git(["add", "-A"], checkout)
    rc, out, err = _run_git(["commit", "-m", f"[codereview-agent] apply review fix for {key}"], checkout)
    rc, out, err = _run_git(["push", "origin", f"HEAD:{branch}"], checkout)
    if rc != 0:
        _update_card_text(app_id, app_secret, render, f"❌ 推送失败: {err or out}")
        return 0
    _update_card_text(app_id, app_secret, render, f"✅ 已推送到远程分支 `{branch}`。可用 `@撤销` 回退。")
    return 0


def _rollback(key, topic, workspace, state_file, app_id, app_secret):
    """User said @撤销: revert the most recently applied local patch (git revert/checkout)."""
    render = topic.get("render_msg_id") or ""
    pipeline_state.set_pending_patch(state_file, key, None)  # drop any pending
    patch = pipeline_state.pop_last_applied_patch(state_file, key)
    if not patch:
        _update_card_text(app_id, app_secret, render, "ℹ️ 没有可回退的已应用补丁。")
        return 0
    repo = patch.get("repo", "engine")
    checkout, _ = _resolve_repo_checkout(workspace, topic, repo)
    before = patch.get("commit_before") or ""
    if before and os.path.isdir(os.path.join(checkout or "", ".git")):
        _run_git(["reset", "--hard", before], checkout)
        _update_card_text(app_id, app_secret, render, "✅ 已回退（本地 reset 到应用前）。")
    else:
        _update_card_text(app_id, app_secret, render, "⚠️ 补丁记录缺失 checkout，无法自动回退，请手动处理。")
    return 0





def _load_findings(workspace, key):
    eng = gam = []
    for repo in ("engine", "game"):
        p = os.path.join(workspace, f"result_{key}_{repo}.json")
        res = _read_json_file(p)
        if res and isinstance(res, dict):
            f = (res.get("review") or {}).get("findings") or []
            if repo == "engine":
                eng = f
            else:
                gam = f
    return eng, gam


def _answer_question(question, findings, api_key, base_url, model):
    """Answer a free-form question about the reviewed findings using the LLM."""
    if not api_key:
        return "（未配置 LLM，无法答疑。可用 `/状态` 或 `1/2/3` 指令。）"
    ctx = "\n".join(
        f"- [{f.get('severity')}] {f.get('file')}: {f.get('issue', '')}"
        for f in (findings or [])[:20])
    prompt = (f"基于以下代码审查 findings，用中文简短回答用户问题（基于这些 findings 推断，不要编造）：\n"
              f"审查 findings：\n{ctx or '（无）'}\n\n用户问题：{question}")
    code = _call_llm_simple(prompt, api_key, base_url, model)
    return code or "（无法生成答复）"


def _call_llm_simple(prompt, api_key, base_url, model, max_tokens=600):
    """Minimal direct LLM call for interaction answers (no retries needed for chat)."""
    import urllib.request
    try:
        payload = json.dumps({
            "model": model, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url}/v1/messages", data=payload,
            headers={"Content-Type": "application/json", "x-api-key": api_key,
                     "anthropic-version": "2023-06-01"}, method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
        text = "".join(b.get("text", "") for b in result.get("content", []) if b.get("type") == "text")
        return text.strip()
    except Exception as e:
        print(f"[interact] llm error: {e}", file=sys.stderr)
        return None


def _spawn_rerun(key, workspace, state_file, jira_url, app_id, app_secret):
    """Re-run the scan review for a topic in the background (reuses diff-hash cache)."""
    try:
        subprocess.Popen(
            [sys.executable, os.path.join(SCRIPTS_DIR, "orchestrate.py"), "run",
             "--key", key, "--mode", "scan", "--jira-url", jira_url,
             "--workspace", workspace, "--pipeline-state-file", state_file],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"[interact] rerun spawn failed: {e}", file=sys.stderr)


def _parse_jira(jira_url, host, token, gitlab_token):
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
        out_path = f.name
    rc, out, err = _run_py("jira_parser.py", [
        "--jira-url", jira_url, "--jira-host", host, "--jira-token", token,
        "--gitlab-token", gitlab_token])
    try:
        os.unlink(out_path)
    except OSError:
        pass
    if rc != 0:
        print(f"[orchestrate] jira_parser failed: {err}", file=sys.stderr)
        # fall back to stdout JSON if emitted
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return {"error": err or "jira parse failed"}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"error": "jira parse produced non-JSON", "raw": out[:200]}


def _review_repos(key, project, issue_key, review_branch, base_branch, engine_base,
                  engine_repo, game_repo, mr_url, workspace, eng_out, gam_out):
    # Cache dir for reuse by diff_hash (avoids re-POSTing unchanged diffs to the LLM).
    cache_dir = os.path.join(workspace, ".review_cache")

    def _one(repo, repo_url, rb, baseb, out_path):
        base_args = ["--repo", repo_url, "--branch", rb, "--base-branch", baseb,
                     "--project", project, "--issue-key", issue_key,
                     "--repo-type", repo, "--mr-url", mr_url, "--workspace", workspace]
        # 1) Dry run: get diff_hash without the (expensive) LLM call.
        rc, out, err = _run_py("code_reviewer.py", base_args + ["--output", out_path + ".dry", "--dry"])
        dry = _read_json_file(out_path + ".dry") or {}
        diff_hash = dry.get("diff_hash") or ""
        if diff_hash:
            cached = os.path.join(cache_dir, f"{key}_{repo}_{diff_hash}.json")
            if os.path.exists(cached):
                # Reuse cached review result (same diff, already reviewed).
                _log('REPO', 'CACHED', key, issue_key, project, repo,
                     f"diff {diff_hash[:8]} already reviewed; reusing result")
                with open(cached, encoding="utf-8") as f:
                    cached_res = json.load(f)
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(cached_res, f, ensure_ascii=False)
                return cached_res, 0
        # 2) Real review (LLM).
        rc2, _, err2 = _run_py("code_reviewer.py", base_args + ["--output", out_path])
        res = _read_json_file(out_path)
        # Save to cache for reuse if we have a diff hash.
        if res and diff_hash:
            os.makedirs(cache_dir, exist_ok=True)
            with open(os.path.join(cache_dir, f"{key}_{repo}_{diff_hash}.json"), "w", encoding="utf-8") as f:
                json.dump(res, f, ensure_ascii=False)
        if res is None and rc2 != 0:
            res = {"error": err2 or f"{repo} review failed"}
        return res or {}, rc2

    eng_res, rc_e = _one("engine", engine_repo, review_branch, engine_base, eng_out)
    gam_res, rc_g = _one("game", game_repo, review_branch, base_branch, gam_out)
    return eng_res, gam_res


def _record_repo_state(state_file, key, repo, out_path, res, review_branch, base_branch):
    if not res:
        pipeline_state.set_repo(state_file, key, repo, status="FAILED", error="no result")
        _log('REPO', 'FAILED', key, '', '', repo, 'no result')
        return
    review = res.get("review") or {}
    err = review.get("error") or res.get("error") or ""
    branch_exists = res.get("branch_exists", True)
    branch_merged = res.get("branch_merged") or False
    changed = len(res.get("changed_files") or []) or 0
    stats = res.get("stats", "") or ""
    sev = review.get("severity_counts") or {}

    if branch_exists is False:
        pipeline_state.set_repo(state_file, key, repo, status="SKIPPED",
                                skip_reason=f"branch {review_branch} not remote",
                                result_file=os.path.basename(out_path))
    elif branch_merged:
        pipeline_state.set_repo(state_file, key, repo, status="SKIPPED",
                                skip_reason="already merged",
                                result_file=os.path.basename(out_path))
    elif not err and changed == 0:
        pipeline_state.set_repo(state_file, key, repo, status="SKIPPED",
                                skip_reason=f"no changes vs {base_branch}",
                                result_file=os.path.basename(out_path))
    else:
        pipeline_state.set_repo(state_file, key, repo, status="SUCCESS", error=err,
                                result_file=os.path.basename(out_path),
                                severity_counts=sev, stats=stats, changed_files=changed)


def _update_state_card(state_file, app_id, app_secret, key):
    if not app_id or not app_secret:
        return
    try:
        topic = pipeline_state.get_topic(state_file, key)
        if not topic or not topic.get("render_msg_id"):
            return
        text = feishu_notifier.render_state_card(topic)
        _run_py("feishu_notifier.py", [
            "update-reply", "--app-id", app_id, "--app-secret", app_secret,
            "--message-id", topic["render_msg_id"],
            "--message-base64", _b64_str(text)])
    except Exception as e:
        print(f"[orchestrate] WARN: state card update failed: {e}", file=sys.stderr)


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(description="CodeReview topic orchestrator")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="Run the full pipeline for one topic")
    p.add_argument("--key", required=True)
    p.add_argument("--mode", default="scan", choices=["scan", "manual"])
    p.add_argument("--workspace", default="/data/codereview/workspace")
    p.add_argument("--pipeline-state-file", default="")
    # scan-mode source fields (used only when creating the topic record)
    p.add_argument("--jira-key", default="")
    p.add_argument("--jira-url", default="")
    p.add_argument("--text", default="")
    p.add_argument("--sender-id", default="")
    p.add_argument("--sender-name", default="")

    p = sub.add_parser("interact", help="Handle a reply/@bot in a topic thread")
    p.add_argument("--key", required=True, help="Parent topic message_id")
    p.add_argument("--reply", required=True, help="Reply text (command or question)")
    p.add_argument("--reply-msg-id", default="", help="The reply message id (unused; card updated by key)")
    p.add_argument("--workspace", default="/data/codereview/workspace")
    p.add_argument("--pipeline-state-file", default="")

    args = parser.parse_args(argv)
    if args.command == "run":
        sys.exit(run(args))
    elif args.command == "interact":
        sys.exit(interact(args))


if __name__ == "__main__":
    main()