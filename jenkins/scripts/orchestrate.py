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
REPO_ROOT = os.path.abspath(os.path.join(SCRIPTS_DIR, "..", ".."))

# Shared workspace (arch-D): both the interaction layer and the Jenkins executor
# only see a CONSISTENT workspace so a topic's checkout + result files are shared.
# The persistent volume /var/lib/report-server/daily is bind-mounted into the agent
# container at the same path, so it survives restarts and is reachable from Jenkins.
_DEFAULT_WORKSPACE = "/var/lib/report-server/daily/cr-workspace"

# ── Load credentials from the persistent env (cr-env) into the process ─────────
# The interaction/executor process may carry an EMPTY GITLAB_TOKEN (key present but
# blank), which silently makes code_reviewer fail to fetch -> empty diff (da39a3ee).
# Ensure non-empty credential values from the persistent env (bind-mounted, secrets
# not in git) are used for any key whose current os.environ value is missing/empty.
_PERSISTENT_ENV = "/var/lib/report-server/daily/cr-env/env.sh"
_CRED_KEYS = ("GITLAB_TOKEN", "GITLAB_USER", "JIRA_HOST", "JIRA_TOKEN",
              "CR_GITLAB_TOKEN", "CR_GITLAB_USER")
for _k in _CRED_KEYS:
    if not os.environ.get(_k):
        try:
            with open(_PERSISTENT_ENV, encoding="utf-8") as _f:
                for _line in _f:
                    _line = _line.strip()
                    if _line.startswith(_k + "="):
                        _v = _line.split("=", 1)[1].strip().strip('"')
                        if _v:
                            os.environ[_k] = _v
                        break
        except Exception:
            pass



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

    # A CLOSED topic must never be re-reviewed (manual reset or scan re-run).
    existing = pipeline_state.get_topic(state_file, key)
    if existing is not None and pipeline_state.is_closed(existing):
        _log('CLOSED', 'SKIP', key, existing.get("jira_key", ""), '', '',
             f'closed (by {existing.get("closed_by","?")}): skip re-review')
        return 0

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

        # Retry/re-review path: if this topic was FAILED (auto-retry) or DONE
        # (explicit re_review), reset it to a runnable state so the forward phase
        # transitions below are legal. CLOSED topics were skipped earlier.
        # Reset any completed OR in-progress (possibly stuck) phase so a re-review
        # can re-run forward from SCANNED. Covers FAILED (auto-retry), DONE (manual
        # re-review) and REVIEWING/PARSING etc. left mid-flight by an earlier run.
        if topic is not None and topic.get("phase") != "CLOSED":
            was_failed = topic.get("phase") == "FAILED"
            reset_kind = "RETRY" if was_failed else "REREVIEW"
            if topic.get("phase") in ("FAILED", "DONE", "REVIEWING", "PARSING", "NOTIFYING"):
                pipeline_state.reset_for_retry(state_file, key)
                _log('SCANNED', reset_kind, key, topic.get("jira_key", ""), '', '',
                     f"{'retry' if was_failed else 're-review'} "
                     f"#{int(topic.get('retry_count') or 0) + 1}")

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
    # arch: when an MR URL is known, the review branch MUST be the MR's real
    # source_branch (authoritative). The jira-guessed branch (bare issue name) is
    # unreliable and yields an empty diff (da39a3ee) when the real branch is
    # feature/.... Re-resolve from GitLab to be safe.
    if mr_url and "merge_requests" in mr_url:
        try:
            import jira_parser as _jp
            mi = _jp.gitlab_get_mr(mr_url, _env("GITLAB_TOKEN"))
            if mi and mi.get("source_branch"):
                review_branch = mi["source_branch"]
                if mi.get("target_branch"):
                    base_branch = mi["target_branch"]
        except Exception as e:
            print(f"[orchestrate] warn: re-resolve MR source failed: {e}", file=sys.stderr)
    # Engine base must follow the MR-resolved base_branch (e.g. master). The config's
    # default_branch may be 'main', but the real repo base for the engine (chaos) is the
    # MR target (master); using 'main' makes the diff empty (da39a3ee). Only an explicit
    # engine_default_branch that differs from the MR target overrides this.
    if mr_url and "merge_requests" in mr_url:
        engine_base = base_branch  # MR target is authoritative for the engine diff base
    else:
        engine_base = info.get("engine_default_branch") or base_branch
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
        # Persist the rendered review summary so later card refreshes (e.g. ci-poll)
        # can append CI status WITHOUT wiping the findings.
        st = pipeline_state.load_state(state_file)
        t2 = st["topics"].get(key)
        if t2 is not None:
            t2["review_summary"] = summary
            st["updated_at"] = pipeline_state._now_iso()
            pipeline_state.save_state(st, state_file)
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


def _update_card_text(app_id, app_secret, card_msg_id, text, topic_key="", actions=""):
    """Update an existing card in place with plain text.

    Interactive button callbacks (arch-A) are retired: verified that this Feishu
    app's server strips the card `action` block (button clicks never produce
    card.action.trigger), so buttons cannot be relied on. Interaction is done via
    @-reply (im.message.receive_v1), guided by the text hint on the card. The
    `topic_key`/`actions` args are kept for signature compatibility but unused.
    """
    _send_reply(app_id, app_secret, card_msg_id, text)


def _append_fix_options(summary, branch):
    """Append a text-only interaction prompt to the summary card. Interaction is via
    @-reply (buttons were retired: the Feishu app server strips card action blocks,
    so button callbacks never fire — see arch-A note)."""
    hint = (
        "\n\n---\n🤖 **交互**：回复本话题 `@机器人` 并附带指令：\n"
        f"  - `指引` 给出每个关键问题的修改指引（用于人工改码）\n"
        f"  - `2` 重新审查 `{branch}`\n"
        f"  - `3 <关键词>` 解释某个 finding\n"
        f"  - `MR单` 生成 MR 描述\n"
        f"  - `/状态` 当前审查状态\n"
        f"  - 改完推到新分支后 `更新MR`\n"
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
    {
        # Side-effect (terminal): closes the topic; only owner/admin may trigger.
        "name": "close_topic",
        "description": "Close this topic (terminate its lifecycle). After closing, the bot ignores further replies and the topic is skipped by scans. Only the topic owner OR an admin may take this action.",
        "input_schema": {"type": "object",
                         "properties": {"reason": {"type": "string",
                                                   "description": "optional reason for closing"}},
                         "required": []},
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
        "- close_topic closes this topic; ONLY the topic owner or an admin may trigger it, "
        "and the system will reject it otherwise.\n"
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
    actor = getattr(args, "sender_id", "") or ""   # the person @-ing / approving
    render_id = topic.get("render_msg_id") or ""
    low0 = (reply_text or "").lower().strip()

    # ── Closed-topic handling (admin/owner close OR auto-silence) ─────────────
    # A closed topic ignores further replies except @审计 (audit stays visible).
    if pipeline_state.is_closed(topic):
        if low0 in ("@审计", "@log", "审计"):
            pass  # fall through to audit branch below
        else:
            reason = topic.get("closed_reason") or "已关闭"
            _finalize(key, f"🔒 本话题已关闭（{reason}），不再处理。如需重新审查请新开话题。",
                      render_id, [], state_file, app_id, app_secret)
            return 0
    else:
        # Lazy auto-silence: DONE/FAILED with no new reply for N days -> auto close.
        N_DAYS = 3
        if topic.get("phase") in ("DONE", "FAILED"):
            try:
                import time as _time
                upd = topic.get("updated_at") or ""
                if upd:
                    ts = _time.mktime(_time.strptime(upd, "%Y-%m-%dT%H:%M:%S"))
                    idle_days = (_time.time() - ts) / 86400.0
                else:
                    idle_days = N_DAYS + 1
            except Exception:
                idle_days = 0
            if idle_days > N_DAYS:
                pipeline_state.close_topic(state_file, key, closed_by="auto",
                                           reason="3天无新回复自动静默")
                _finalize(key, "🔒 本话题长时间无新回复，已自动关闭。如需重新审查请新开话题。",
                          render_id, [], state_file, app_id, app_secret)
                return 0

    eng_findings, gam_findings, findings_status = _load_findings(workspace, key)
    all_findings = (eng_findings or []) + (gam_findings or [])

    # ── Confirmation gating (user replies to a staged action) ───────────────
    low = reply_text.lower()
    pending = topic.get("pending_patch") or {}
    is_ok = low in ("@ok", "ok", "好的", "执行", "确认")
    is_confirm_push = low in ("@confirm push", "确认push", "@push", "推")
    is_rollback = low in ("@撤销", "@revert", "撤销", "回退")

    if pending:
        if is_ok:
            return _confirm_apply(key, topic, workspace, state_file, app_id, app_secret, actor)
        if low in ("@confirm push", "确认push", "@push"):
            return _confirm_push(key, topic, workspace, state_file, app_id, app_secret, actor)
        if is_rollback:
            return _rollback(key, topic, workspace, state_file, app_id, app_secret, actor)

    # Also handle @撤销 globally (rollback last applied patch even without pending).
    if is_rollback:
        return _rollback(key, topic, workspace, state_file, app_id, app_secret, actor)
    # Audit / log query
    if low in ("@审计", "@log", "审计"):
        import json as _json
        log = topic.get("approval_log") or []
        lines = ["## 审计记录（最近）\n"] + [
            f"- [{e.get('time','')}] {e.get('actor','')} -> {e.get('action','')}"
            f" {e.get('target','')} [{e.get('result','')}]" for e in log[-20:]]
        _finalize(key, "\n".join(lines), render_id, [], state_file, app_id, app_secret)
        return 0

    # ── Reliable command routing (arch: 1/2/3/4 + keywords go to FIXED actions, not
    #    LLM guesses). Matches whole-word / prefix so "2" always = re-review, etc. ──
    # Drop a leading @-mention (e.g. "@_user_1 MR ...", "@机器人 1") so the first
    # real command token is used for matching.
    cmd = low.strip()
    import re as _re
    cmd = _re.sub(r'^@[\w.\-]+', '', cmd).strip()
    word = cmd.split()[0] if cmd else ""
    if word in ("1", "补丁", "生成补丁", "修复"):
        # Propose a fix patch for the findings (suggestion-based, staged for later).
        return _cmd_fix_patch(key, topic, all_findings, render_id, workspace, state_file,
                              app_id, app_secret, actor)
    if word in ("2", "重新审查", "重审", "review", "重新review"):
        _cmd_rereview(key, topic, state_file, render_id, app_id, app_secret, actor)
        return 0
    if word in ("3", "解释"):
        rest = low.strip()[1:].strip() if low.strip().startswith("3") else low.strip()[2:].strip()
        answer = _answer_question(rest or "请解释当前发现", all_findings, api_key, base_url, model)
        _finalize(key, answer, render_id, [], state_file, app_id, app_secret)
        return 0
    if word in ("4", "关闭", "关闭话题"):
        _cmd_close(key, topic, state_file, render_id, app_id, app_secret, actor)
        return 0
    if word in ("mr", "生成mr", "出mr单", "mr单", "更新mr", "更新mr单"):
        text = _generate_mr_card(topic, all_findings, workspace, key)
        _finalize(key, text, render_id, [], state_file, app_id, app_secret)
        return 0
    if word in ("预览", "预览补丁", "patch预览"):
        text = _render_patch_preview(topic, all_findings, api_key, base_url, model, workspace)
        _finalize(key, text, render_id, [], state_file, app_id, app_secret)
        return 0
    if word in ("指引", "修改指引", "怎么改"):
        text = _generate_fix_guidance(topic, all_findings, api_key, base_url, model, workspace)
        _finalize(key, text, render_id, [], state_file, app_id, app_secret)
        return 0
    if word in ("应用并提交", "确认提交", "push并建mr"):
        text = _build_patch_preview_target(all_findings, "all")
        _finalize(key, "⏳ 「应用并提交」将 push 新分支 + 建 MR（受控写操作，暂未执行）。\n"
                       "当前先确认补丁预览：\n" + text[:400], render_id, [], state_file, app_id, app_secret)
        return 0
    if word in ("状态", "/状态", "status"):
        _finalize(key, _build_status_text(topic), render_id, [], state_file, app_id, app_secret)
        return 0

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
                                                 findings_status,
                                                 state_file, api_key, base_url, model, actor)
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

    # Loop exhausted without plain text — give a useful wrap-up, not a raw tool_use dump.
    answer = ("已完成多步处理；如需进一步操作，请回复：`1 生成补丁` / `2 重新审查` / "
              "`3 <关键词> 解释` / `4 关闭` / `MR单` 生成 MR 描述。")
    _finalize(key, answer, render_id, all_msgs, state_file, app_id, app_secret)
    return 0


def _exec_tool(name, inp, topic, workspace, all_findings, findings_status,
               state_file, api_key, base_url, model, actor=""):
    """Execute one agent tool. Returns (result_text, side_effect_bool)."""
    if name == "get_status":
        return _build_status_text(topic), False
    if name == "get_findings":
        if not all_findings:
            # Three distinct cases. Only a genuinely clean review (files present,
            # zero findings) may be reported as "no issues".
            if findings_status == "missing":
                return (f"⚠️ 无法读取审查结果文件（workspace 里没有 result_{key_source(topic)}_<engine/game>.json）。"
                        f"审查结果可能在其他 workspace，或文件已被清理。请先 `重新审查` 让 Jenkins 重新生成结果。"), False
            if (topic or {}).get("phase") in ("FAILED", "SCANNED") or (topic or {}).get("last_error"):
                err = (topic or {}).get("last_error") or ""
                return (f"⚠️ 该话题的审查未成功（phase={topic.get('phase')}）"
                        f"{('：' + err[:80]) if err else ''}，因此暂无 findings。"
                        f"发起人可回复 `重新审查` 让 Jenkins 重跑。"), False
            return "（该话题审查通过，确实没有发现代码问题——无 findings。）", False
        return "\n".join(f"- [{f.get('severity')}] {f.get('file')}: {f.get('issue','')}"
                         for f in all_findings[:25]), False
    if name == "generate_patch_preview":
        if not all_findings:
            if findings_status == "missing":
                return "⚠️ 无法读取审查结果文件，无法生成补丁。请先 `重新审查`。", False
            if (topic or {}).get("phase") in ("FAILED", "SCANNED") or (topic or {}).get("last_error"):
                return "⚠️ 该话题审查未成功，无 findings 可生成补丁。请先 `重新审查`。", False
        target = (inp.get("target") or "").strip()
        return _build_patch_preview_target(all_findings, target), False
    if name == "re_review":
        # guarded: only topic_owner may trigger a re-review.
        ok, why = _approve(key_source(topic), topic, actor, "re_review")
        if not ok:
            return f"⛔ {why}", True
        # arch-D: enqueue a re_review for the Jenkins executor (the only role with
        # GitLab/Jira creds to pull real code); do NOT run it here (no creds).
        pipeline_state.set_pending(state_file, key_source(topic), "re_review")
        msg = ("⏳ 已记录重新审查请求，Jenkins 将拉取最新代码重新审查，结果会自动更新到本帖。")
        try:
            _finalize(key_source(topic), msg, topic.get("render_msg_id") or "",
                      [], state_file, _env("FEISHU_APP_ID"), _env("FEISHU_APP_SECRET"))
            return "", True
        except Exception:
            return msg, True
    if name == "answer":
        q = (inp.get("question") or "").strip()
        return _answer_question(q or "请补充说明", all_findings, api_key, base_url, model), False
    if name == "close_topic":
        # guarded: only topic owner OR admin may close (policy.yaml: close_topic ->
        # approver admin_or_owner). Terminal side-effect — stops further processing.
        ok, why = _approve(key_source(topic), topic, actor, "close_topic")
        if not ok:
            return f"⛔ {why}", True
        reason = (inp.get("reason") or "").strip()[:200] or "用户请求关闭"
        pipeline_state.close_topic(state_file, key_source(topic),
                                   closed_by=actor or "unknown", reason=reason)
        return "🔒 本话题已关闭，不再处理。如需重新审查请新开话题。", True
    if name == "apply_patch":
        # guarded: only topic_owner may PROPOSE applying a patch.
        ok, why = _approve(key_source(topic), topic, actor, "apply_patch")
        if not ok:
            return f"⛔ {why}", True
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


def _gitlab_raw_file(repo_project_path, file_path, ref, token=None):
    """Fetch a file's raw content from GitLab (fast; no full clone). Returns text or None."""
    import urllib.request, urllib.error, urllib.parse
    token = token or _env("GITLAB_TOKEN") or ""
    if not token:
        return None
    proj = urllib.parse.quote(repo_project_path, safe="")
    u = (f"https://gitlab.booming-inc.com/api/v4/projects/{proj}/repository/files/"
         f"{urllib.parse.quote(file_path, safe='')}/raw?ref={urllib.parse.quote(ref, safe='')}")
    try:
        r = urllib.request.Request(u, headers={"PRIVATE-TOKEN": token})
        return urllib.request.urlopen(r, timeout=25).read().decode("utf-8", "ignore")
    except Exception as e:
        print(f"[patch] raw read {file_path} err: {e}", file=sys.stderr)
        return None


def _generate_real_patch(topic, all_findings, api_key, base_url, model):
    """Generate REAL, apply-able unified diffs for the topic's critical findings.
    Reads each file from GitLab (raw API), asks the LLM to produce a git apply-able
    diff for that file. Returns a list of {file, diff} (diff may be empty if LLM
    failed). No push. Suggestion-based `_build_patch_preview_target` remains the
    fallback when we lack token / raw files.
    """
    repo_proj = ""
    # Prefer the MR URL's project path (authoritative, independent of empty project field).
    mr_url = topic.get("mr_url") or topic.get("jira_url") or ""
    if mr_url and "merge_requests" in mr_url:
        import jira_parser as _jp
        pp, _iid = _jp.parse_gitlab_mr_url(mr_url)
        if pp:
            repo_proj = pp
    # Fallback: match config project by name if still empty.
    if not repo_proj:
        import common as _common
        for pid, pc in _common.get_projects().items():
            if str(pid).lower() == str(topic.get("project", "")).lower():
                eng = pc.get("engine_repo", "")
                if "git@" in eng:
                    repo_proj = eng.split(":")[-1].lstrip("/").rstrip(".git")
                break
    if not repo_proj:
        return []
    ref = topic.get("review_branch") or topic.get("base_branch") or ""
    if not api_key:
        return []
    crit = [f for f in (all_findings or [])
            if (f.get("severity") or "").lower() in ("critical", "high")]
    show = crit or (all_findings or [])[:3]
    out = []
    for f in show[:3]:
        file = f.get("file") or f.get("path") or ""
        if not file:
            continue
        content = _gitlab_raw_file(repo_proj, file, ref)
        if content is None:
            continue
        issue = (f.get("issue") or "")[:600]
        sys_prompt = ("You are fixing a code review finding. Output a SINGLE minimal, CORRECT, "
                      "git-apply-able unified diff that fixes the issue. Constraints: "
                      "start immediately with 'diff --git', include 'index', '--- a/...', "
                      "'+++ b/...', and each hunk with correct header '@@ -L,C +L,C @@' and "
                      "context/minus/plus lines. Do NOT wrap in code fences. Do NOT add any "
                      "explanatory prose before or after. If you cannot safely produce a "
                      "correct diff, reply exactly: NO_SAFE_FIX.")
        prompt = f"FILE: {file}\n\n```\n{content[:6000]}\n```\n\nISSUE: {issue}\n\nUnified diff:"
        raw = _call_llm_simple(prompt, api_key, base_url, model, max_tokens=1800)
        clean = _extract_clean_diff(raw)
        if clean:
            out.append({"file": file, "diff": clean})
    return out


def _extract_clean_diff(raw):
    """Extract a clean git-apply-able unified diff from LLM output.

    Strips any prose, keeps only from the first 'diff --git' onward, and validates
    the essential unified-diff markers exist (--- a/, +++ b/, @@ hunks). Returns the
    cleaned diff string, or "" if it is not a well-formed diff (strict validation so
    we never push a broken patch)."""
    if not raw or not raw.strip():
        return ""
    text = raw.strip()
    if text.strip() == "NO_SAFE_FIX":
        return ""
    # Drop anything before the first 'diff --git'
    idx = text.find("diff --git")
    if idx < 0:
        return ""
    diff = text[idx:]
    # Cut at the first code fence if LLM wrapped it anyway.
    f1 = diff.find("```")
    if f1 >= 0:
        diff = diff[:f1]
    # Validation: must look like a unified diff.
    if "+++" not in diff or "---" not in diff or "@@" not in diff:
        return ""
    return diff.strip()


def _ensure_checkout(topic, workspace):
    """Clone (or reuse) the topic's review branch to a deterministic checkout dir in
    workspace. Returns (checkout_dir, err) or (None, err). Uses oauth2 token auth."""
    import subprocess as _sp
    src = topic.get("review_branch") or topic.get("base_branch") or ""
    if not src:
        return None, "no review branch"
    mr_url = topic.get("mr_url") or ""
    import jira_parser as _jp
    pp, _ = _jp.parse_gitlab_mr_url(mr_url) if "merge_requests" in mr_url else (None, None)
    if not pp:
        return None, "no mr project path"
    repo_name = pp.rstrip("/").split("/")[-1]
    dest = os.path.join(workspace, f"{repo_name}-review")
    tok = _env("GITLAB_TOKEN")
    if not tok or os.path.isdir(os.path.join(dest, ".git")):
        if os.path.isdir(os.path.join(dest, ".git")):
            return dest, None
        return None, "no GITLAB_TOKEN for checkout"
    repo_url = f"https://oauth2:{tok}@gitlab.booming-inc.com/{pp}.git"
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        r = _sp.run(["git", "clone", "--quiet", "--single-branch", "--branch", src,
                     "--depth", "2", repo_url, dest], capture_output=True, text=True,
                    env=env, timeout=600)
        if r.returncode != 0 or not os.path.isdir(os.path.join(dest, ".git")):
            return None, f"clone failed: {r.stderr[:200]}"
        return dest, None
    except Exception as e:
        return None, f"clone err: {e}"


def _git_apply_check(checkout, diff_text):
    """Run `git apply --check` on a diff. Returns (ok, err)."""
    import subprocess as _sp, tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as f:
        f.write(diff_text or "")
        pf = f.name
    try:
        r = _sp.run(["git", "-C", checkout, "apply", "--check", pf],
                    capture_output=True, text=True, timeout=120)
        return r.returncode == 0, r.stderr or r.stdout
    except Exception as e:
        return False, str(e)
    finally:
        try: os.unlink(pf)
        except OSError: pass


def _auto_fix_in_checkout(topic, all_findings, api_key, base_url, model, workspace,
                          max_rounds=4):
    """Y: generate LLM diffs in a REAL checkout and validate with `git apply --check`,
    iterating per finding until the patch applies (or rounds exhausted). Returns
    (ok_diffs, failed, branch_name, checkout). Never pushes."""
    src = topic.get("review_branch") or ""
    task = topic.get("jira_key") or "task"
    branch_name = f"{src}-fix-{task}" if src else f"fix-{task}"
    checkout, err = _ensure_checkout(topic, workspace)
    if err:
        return [], [], branch_name, err
    crit = [f for f in (all_findings or [])
            if (f.get("severity") or "").lower() in ("critical", "high")]
    show = crit or (all_findings or [])[:3]
    ok_diffs, failed = [], []
    for f in show[:3]:
        file = f.get("file") or f.get("path") or ""
        if not file or not os.path.isfile(os.path.join(checkout, file)):
            failed.append((file, "checkout 缺失该文件"))
            continue
        issue = (f.get("issue") or "")[:500]
        attained = False
        for rnd in range(max_rounds):
            content = open(os.path.join(checkout, file), encoding="utf-8", errors="ignore").read()
            sysp = ("You fix a code review finding. Output ONE git-apply-able unified diff "
                    "for the file, starting with 'diff --git'. Exact hunks with correct "
                    "line numbers/context from the CURRENT file. No prose, no fences. "
                    "If cannot safely fix, reply NO_SAFE_FIX.")
            prompt = (f"FILE: {file}\n\n```\n{content[:6000]}\n```\n\nISSUE: {issue}\n\n"
                      f"Round {rnd+1}/{max_rounds}. Produce the unified diff.")
            raw = _call_llm_simple(prompt, api_key, base_url, model, max_tokens=1800)
            clean = _extract_clean_diff(raw)
            if not clean:
                failed.append((file, "LLM 未能生成干净 diff"))
                break
            ok, apply_err = _git_apply_check(checkout, clean)
            if ok:
                ok_diffs.append({"file": file, "diff": clean})
                attained = True
                break
            # feed error back into issue text for next round
            issue = issue + f"\n[apply failed round {rnd+1}: {apply_err[:200]}. Produce a corrected diff.]"
        if not attained:
            failed.append((file, "apply --check 始终失败"))
    return ok_diffs, failed, branch_name, None


def _auto_edit_preview(topic, all_findings, api_key, base_url, model, workspace=None, file_target=None):
    """Auto-edit ONE file in the real checkout per the fix guidance; apply the LLM diff
    for real and return the resulting `git diff` as a preview (NOT committed/pushed).

    This gives the user a concrete look at what the bot would change before any push.
    Returns (file, diff_text, error)."""
    import subprocess as _sp
    ws = workspace or _DEFAULT_WORKSPACE
    checkout, err = _ensure_checkout(topic, ws)
    if err:
        return None, "", f"checkout: {err}"
    crit = [f for f in (all_findings or [])
            if (f.get("severity") or "").lower() in ("critical", "high")]
    show = crit or (all_findings or [])[:3]
    file = file_target
    issue = ""
    for f in show:
        if file_target and (f.get("file") or "") != file_target:
            continue
        file = f.get("file") or file
        issue = f.get("issue") or ""
        break
    if not file or not issue:
        return None, "", "no finding for target"
    src = topic.get("review_branch") or ""
    task = topic.get("jira_key") or "task"
    new_branch = f"{src}-fix-{task}" if src else f"fix-{task}"
    # ensure on the new branch (create if absent)
    _sp.run(["git", "-C", checkout, "checkout", "-B", new_branch],
            capture_output=True, text=True, timeout=60)
    p = os.path.join(checkout, file)
    if not os.path.isfile(p):
        return None, "", f"file not in checkout: {file}"
    content = open(p, encoding="utf-8", errors="ignore").read()
    sysp = ("You produce ONE git-apply-able unified diff that fixes the issue in this "
            "file, given the CURRENT content. Output diff only, starting 'diff --git', "
            "exact hunks with correct line numbers from the current file. No prose/fences. "
            "If cannot safely fix, reply NO_SAFE_FIX.")
    prompt = (f"FILE: {file}\n\n```\n{content[:6000]}\n```\n\nISSUE: {issue}\n\nUnified diff:")
    raw = _call_llm_simple(prompt, api_key, base_url, model, max_tokens=2000)
    diff = _extract_clean_diff(raw)
    if not diff:
        return None, "", "LLM 未能生成干净 diff"
    # reset any partial apply then apply for real
    _sp.run(["git", "-C", checkout, "checkout", "--", "."], capture_output=True, text=True, timeout=60)
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as f:
        f.write(diff)
        pf = f.name
    r = _sp.run(["git", "-C", checkout, "apply", pf], capture_output=True, text=True, timeout=120)
    os.path.exists(pf) and os.unlink(pf)
    if r.returncode != 0:
        return None, "", f"git apply failed: {r.stderr[:200]}"
    # show the resulting diff for this file
    d = _sp.run(["git", "-C", checkout, "diff", "--", file], capture_output=True, text=True, timeout=60)
    return file, d.stdout, None


EDIT_MODEL = "deepseek-v4-flash[1m]"  # as configured for Claude Code


def _locate_context(content, needle):
    """Programmatically find a ~20-line context window around the first occurrence of
    `needle` in `content`. Locating is done by code (reliable), not by the model
    recalling the snippet, so the model only edits a small, correct window."""
    idx = content.find(needle)
    if idx < 0:
        return None, None
    lines = content.split("\n")
    # find line containing needle
    li = next((i for i, l in enumerate(lines) if needle in l), len(lines))
    start = max(0, li - 6)
    end = min(len(lines), li + 16)
    ctx = "\n".join(lines[start:end])
    return (start, end), ctx


def _agent_edit_one(topic, file, issue, api_key, base_url, checkout, model=EDIT_MODEL, max_rounds=4):
    """Edit one file's exact problem window with the model, apply, and iterate on
    `git apply --check` feedback. Returns (file, git_diff, ok, error)."""
    import subprocess as _sp, re as _re
    p = os.path.join(checkout, file)
    if not os.path.isfile(p):
        return None, "", False, f"file missing: {file}"
    # a distinctive needle from the issue to locate context (prefer an alphanumeric token)
    needle = ""
    for tok in _re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", issue or ""):
        if tok and tok in open(p, encoding="utf-8", errors="ignore").read():
            needle = tok
            break
    if not needle:
        return None, "", False, "no locator token in issue"
    content = open(p, encoding="utf-8", errors="ignore").read()
    (start, end), ctx = _locate_context(content, needle)
    if ctx is None:
        return None, "", False, f"locator '{needle}' not found in file"
    prev_bad = ""
    for rnd in range(max_rounds):
        sysp = ("You fix a bug in a file. I show the RELEVANT window of the file. "
                "Output EXACTLY:\n@@START@@\n<verbatim corrected full window (everything from the window, with the fix applied)>\n@@END@@\n"
                "Preserve all other lines byte-for-byte; only change the buggy part. No other text.")
        prompt = (f"FILE: {file}\nRELEVANT WINDOW:\n```\n{ctx}\n```\n\nISSUE: {issue[:400]}\n\n"
                  f"Round {rnd+1}. Output @@START@@...@@END@@ (corrected window).")
        if prev_bad:
            prompt += f"\n[previous fix failed to apply, error: {prev_bad[:200]}. Correct the window so git apply succeeds.]"
        raw = _call_llm_simple(prompt, api_key, base_url, model, max_tokens=4000)
        m = _re.search(r"@@START@@(.*?)@@END@@", raw or "", re.S)
        if not m:
            prev_bad = "no @@START@@ block"
            continue
        new_window = m.group(1).strip("\n")
        new_content = content.split("\n"); new_content[start:end] = new_window.split("\n")
        new_content = "\n".join(new_content)
        if new_content == content:
            prev_bad = "no change produced"
            continue
        # apply to working tree only (not committed)
        backup = p + ".__bak"
        open(backup, "w", encoding="utf-8").write(content)
        open(p, "w", encoding="utf-8").write(new_content)
        # validate the edit is still syntactically near-identical (diff sane)
        _sp.run(["git", "-C", checkout, "checkout", "--", file], capture_output=True, text=True)  # restore? no
        # restore our edit back
        open(p, "w", encoding="utf-8").write(new_content)
        os.path.exists(backup) and os.unlink(backup)
        d = _sp.run(["git", "-C", checkout, "diff", "--", file], capture_output=True, text=True, timeout=60)
        return file, d.stdout, True, None
    return None, "", False, "max rounds without an applyable edit"


def _agent_edit_preview(topic, all_findings, api_key, base_url, workspace=None, model=EDIT_MODEL):
    """Agent edit preview: use the model to fix ONE finding in the real checkout, return
    the git diff for the user to review. Does NOT commit/push."""
    ws = workspace or _DEFAULT_WORKSPACE
    checkout, err = _ensure_checkout(topic, ws)
    if err:
        return None, "", f"checkout: {err}"
    crit = [f for f in (all_findings or [])
            if (f.get("severity") or "").lower() in ("critical", "high")]
    show = crit or (all_findings or [])[:3]
    for f in show[:3]:
        file = f.get("file") or f.get("path") or ""
        issue = f.get("issue") or ""
        file, diff, ok, e = _agent_edit_one(topic, file, issue, api_key, base_url, checkout, model)
        if ok:
            return file, diff, None
        # failure: report, try next
    return show[0].get("file") if show else None, "", "agent edit failed on all candidate findings"


def _render_patch_preview(topic, all_findings, api_key, base_url, model, workspace=None):
    """Build a human preview using the Y auto-fix (real checkout + apply --check)."""
    ok, failed, branch_name, err = _auto_fix_in_checkout(
        topic, all_findings, api_key, base_url, model,
        workspace or _DEFAULT_WORKSPACE)
    lines = [f"✏️ **补丁预览（严格校验，可 apply 才展示）**\n",
             f"建议新分支：`{branch_name}`（基于源分支，不覆盖原始）\n"]
    if err:
        lines.append(f"\n⚠️ {err}")
    if ok:
        for r in ok:
            lines.append(f"\n✅ **{r['file']}**\n```diff\n{r['diff'][:1200]}\n```")
    if failed:
        lines.append("\n❌ **自动修复失败的文件**：")
        for file, why in failed:
            lines.append(f"- `{file}`: {why}")
        lines.append("\n（失败文件请人工修改；机器人不会 push 未通过 `git apply --check` 的补丁。）")
    if not ok and not failed:
        lines.append("\n（没有可自动修复的 findings。）")
    lines.append("\n> 确认后回复 `@机器人 应用并提交` 才会 push 新分支 + 建 MR。")
    return "\n".join(lines)


def _generate_fix_guidance(topic, all_findings, api_key, base_url, model, workspace=None):
    """Route-P: give PRECISE per-critical modification guidance ("which file / which
    location / how to change") so a developer can implement the fix reliably. LLM is
    good at this; producing apply-able diffs is not reliable ("Y" failed). Returns
    human guidance text."""
    ws = workspace or _DEFAULT_WORKSPACE
    src = topic.get("review_branch") or ""
    task = topic.get("jira_key") or "task"
    branch = f"{src}-fix-{task}" if src else f"fix-{task}"
    crit = [f for f in (all_findings or [])
            if (f.get("severity") or "").lower() in ("critical", "high")]
    show = crit or (all_findings or [])[:3]
    checkout, _err = _ensure_checkout(topic, ws)
    lines = [f"🛠 **修改指引（供人工实现）**\n",
             f"建议新分支：`{branch}`\n"]
    for f in show[:3]:
        file = f.get("file") or f.get("path") or ""
        issue = (f.get("issue") or "").strip()
        suggestion = (f.get("suggestion") or "").strip()
        ctx = ""
        if checkout and file:
            p = os.path.join(checkout, file)
            if os.path.isfile(p):
                ctx = open(p, encoding="utf-8", errors="ignore").read()
        lines.append(f"\n**🔴 {file}**")
        lines.append(f"- 问题：{issue[:200]}")
        if suggestion:
            lines.append(f"- 建议：{suggestion[:200]}")
        if ctx:
            sysp = ("You give CONCISE, PRECISE code-change instructions to a developer "
                    "for one file. Output: (1) the exact location (function/block/line "
                    "range) to change, (2) what to change it to (specific). No full file "
                    "rewrite; no diff required; no markdown fences. Keep under 200 chars.")
            prompt = (f"FILE: {file}\n\n```\n{ctx[:5000]}\n```\n\nISSUE: {issue}\n\n"
                      f"Give the precise change instructions.")
            try:
                g = _call_llm_simple(prompt, api_key, base_url, model, max_tokens=400)
                if g:
                    lines.append(f"- 改动位置/方式：{g[:400]}")
            except Exception:
                pass
        else:
            lines.append("- （无法读取真实文件，请按上述问题/建议自查）")
    lines.append("\n> 改完后把新分支推到 GitLab，回复 `@机器人 更新MR` 生成/更新 MR。")
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
    """Persist chat history + post the answer as an INDEPENDENT reply in the topic
    thread (reply-message), so it does NOT overwrite the review result card.
    (Confirmation-gate actions still update the shared card via _update_card_text.)"""
    pipeline_state.append_chat(state_file, key, {"role": "user", "content": "（本轮交互）"})
    pipeline_state.append_chat(state_file, key, {"role": "assistant", "content": answer})
    if not answer:
        return
    if not (app_id and app_secret):
        _log("CHAT", "SKIP", key, "", "", "", "no feishu creds; reply skipped")
        return
    chat_id = _env("FEISHU_CHAT_ID")
    _run_py("feishu_notifier.py", [
        "reply-message", "--app-id", app_id, "--app-secret", app_secret,
        "--chat-id", chat_id, "--message-id", key, "--message-base64", _b64_str(answer)])


# ── Guarded side-effect executors (design-3): local apply + remote push + rollback ──

# Protected branch names: never push to these.
PROTECTED_BRANCHES = {"main", "master", "dev", "develop", "release", "stage", "prod"}


# ── Policy layer (explicit "can/cannot", defense-in-depth layer 2) ──────────
#
# Reads policy.yaml at repo root (or falls back to this default). The executor
# only follows this table: only topic_owner may approve guarded actions; push is
# further restricted to the topic's own review_branch (layer 3 + 4).

DEFAULT_POLICY = {
    "agent": {
        "invoke_anyone": [
            "get_status", "get_findings", "generate_patch_preview", "answer"],
        "guarded": {
            "re_review": {"approver": "topic_owner"},
            "apply_patch": {"approver": "topic_owner"},
            "apply_local": {"approver": "topic_owner"},
            "push_remote": {"approver": "topic_owner", "branch": "{topic.review_branch}"},
            "rollback": {"approver": "topic_owner"},
        },
    },
    "lock": {"mode": "serial"},
    "scope": {"repo_checkout_only": True},
}

_POLICY_CACHE = None


def _load_policy():
    """Load policy.yaml from repo root, fallback to DEFAULT_POLICY."""
    global _POLICY_CACHE
    if _POLICY_CACHE is not None:
        return _POLICY_CACHE
    try:
        import yaml
        path = os.path.join(REPO_ROOT, "policy.yaml")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                p = yaml.safe_load(f) or {}
            if isinstance(p, dict) and p.get("agent"):
                _POLICY_CACHE = p
                return _POLICY_CACHE
    except Exception as e:
        print(f"[policy] load failed, using default: {e}", file=sys.stderr)
    _POLICY_CACHE = DEFAULT_POLICY
    return _POLICY_CACHE


def _is_invoke_anyone(policy, tool):
    anyone = (policy.get("agent") or {}).get("invoke_anyone") or []
    return tool in anyone


def _approve(key, topic, actor, action, branch=""):
    """
    Defense-in-depth approval check (layers 2/3/4):
      - guarded actions require actor == topic.sender_id (topic owner)
      - push_remote additionally requires branch == topic.review_branch, not protected
    Writes an approval_log entry (audit). Returns (allowed, why).
    """
    policy = _load_policy()
    guarded = (policy.get("agent") or {}).get("guarded") or {}
    rule = guarded.get(action)
    if rule is None:
        # not a guarded action -> allowed (subject to other layers)
        return True, "not-guarded"

    approver = rule.get("approver", "topic_owner")
    owner = (topic or {}).get("sender_id") or ""
    actor = actor or ""

    if approver == "topic_owner":
        if not owner:
            pipeline_state.append_approval(
                os.environ.get("PIPELINE_STATE_FILE", state_file_default(key, topic)), key,
                actor, action, branch, "denied", "no topic owner recorded (fail-closed)")
            return False, "话题未记录发起人，操作已拒绝（fail-closed）"
        if actor != owner:
            pipeline_state.append_approval(
                os.environ.get("PIPELINE_STATE_FILE", state_file_default(key, topic)), key,
                actor, action, branch, "denied", f"actor!=owner ({actor}!={owner})")
            return False, "仅话题发起人可执行此操作"

    if approver == "admin_or_owner":
        # Allowed if actor is the topic owner OR is in the policy admins list.
        admins = (policy.get("agent") or {}).get("admins") or []
        if owner and actor == owner:
            pass  # owner allowed
        elif actor in admins:
            pass  # admin allowed
        else:
            pipeline_state.append_approval(
                os.environ.get("PIPELINE_STATE_FILE", state_file_default(key, topic)), key,
                actor, action, branch, "denied",
                f"not owner nor admin (owner={'yes' if owner else 'no'}, admin={'yes' if actor in admins else 'no'})")
            return False, "仅有话题发起人或管理员可执行此操作"

    if action == "push_remote":
        want_branch = (rule.get("branch") or "").replace("{topic.review_branch}", topic.get("review_branch") or "")
        if branch not in (want_branch, topic.get("review_branch")) or branch in PROTECTED_BRANCHES or not branch:
            pipeline_state.append_approval(
                os.environ.get("PIPELINE_STATE_FILE", state_file_default(key, topic)), key,
                actor, action, branch, "denied", "branch not allowed/protected")
            return False, "仅话题的 review 分支可 push，且不可推受保护分支"

    return True, "approved"


def state_file_default(key, topic):
    return os.environ.get("PIPELINE_STATE_FILE", "pipeline-state.json")


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


def _confirm_apply(key, topic, workspace, state_file, app_id, app_secret, actor=""):
    """@ok — approve applying the staged patch. In arch-D this does NOT run git
    locally: it writes a pending 'apply' action for the Jenkins executor (the
    only role with GitLab creds / the shared checkout), then reports 'recorded'.
    Approval still happens here (defense-in-depth), so only the owner/admin can
    enqueue an apply."""
    render = topic.get("render_msg_id") or ""
    ok, why = _approve(key, topic, actor, "apply_local")
    if not ok:
        pipeline_state.append_approval(state_file, key, actor, "apply_local", "", "denied", why)
        _update_card_text(app_id, app_secret, render, f"⛔ {why}")
        return 0
    pending = topic.get("pending_patch") or {}
    if not pending:
        _update_card_text(app_id, app_secret, render, "ℹ️ 没有待应用的补丁，无需操作。")
        return 0
    # Record intent; the Jenkins executor applies it to the shared checkout.
    pipeline_state.append_approval(state_file, key, actor, "apply_local", pending.get("file", ""), "ok", "@ok enqueued")
    pipeline_state.set_pending(state_file, key, "apply", patch={
        "file": pending.get("file", ""), "repo": pending.get("repo", "engine"), "diff": pending.get("diff", ""),
    })
    pipeline_state.set_pending_patch(state_file, key, None)  # now owned by the executor
    _update_card_text(app_id, app_secret, render,
                      "⏳ 已记录应用请求，Jenkins 将把补丁应用到共享 checkout。\n"
                      "完成后会更新本帖。如需推送远程，届时再回复 `@confirm push`。")
    return 0


def _confirm_push(key, topic, workspace, state_file, app_id, app_secret, actor=""):
    """@confirm push — approve pushing applied changes to the review branch.
    arch-D: records a pending 'push' for the Jenkins executor (only role with
    GitLab creds), rather than pushing locally. Approval still enforced here."""
    render = topic.get("render_msg_id") or ""
    branch = topic.get("review_branch") or ""
    ok, why = _approve(key, topic, actor, "push_remote", branch=branch)
    if not ok:
        pipeline_state.append_approval(state_file, key, actor, "push_remote", branch, "denied", why)
        _update_card_text(app_id, app_secret, render, f"⛔ {why}")
        return 0
    pipeline_state.append_approval(state_file, key, actor, "push_remote", branch, "ok", "@confirm push enqueued")
    pipeline_state.set_pending(state_file, key, "push")
    _update_card_text(app_id, app_secret, render,
                      "⏳ 已记录推送请求，Jenkins 将把已应用的改动推到远程分支 `{branch}`。".format(branch=branch))
    return 0


def _rollback(key, topic, workspace, state_file, app_id, app_secret, actor=""):
    """@撤销 — revert the most recently applied local patch. Owner-only.
    arch-D: enqueues a pending 'rollback' for the Jenkins executor."""
    render = topic.get("render_msg_id") or ""
    ok, why = _approve(key, topic, actor, "rollback")
    if not ok:
        pipeline_state.append_approval(state_file, key, actor, "rollback", "", "denied", why)
        _update_card_text(app_id, app_secret, render, f"⛔ {why}")
        return 0
    pipeline_state.set_pending(state_file, key, "rollback")
    pipeline_state.append_approval(state_file, key, actor, "rollback", "", "ok", "@撤销 enqueued")
    _update_card_text(app_id, app_secret, render,
                      "⏳ 已记录回退请求，Jenkins 将回退最近一次应用的补丁。")
    return 0


# ── Async executor (arch-D): Jenkins consumes topic.pending and executes it ────
#
# The interaction layer only enqueues intents (re_review/apply/push/rollback); the
# Jenkins scan/scheduled job calls consume_pending() for each topic with a pending
# action. It is the ONLY place that touches the git checkout / runs the review,
# because it has the full GitLab/Jira creds and a shared, consistent checkout.
# Returns a human-readable result per action.

def _ensure_shared_checkout(topic, repo, workspace):
    """Locate (or lazily create) the shared repo checkout for a topic using the
    engine/game repo URLs recorded on the topic. Returns (dirname, None) or
    (None, error). Uses `workspace` as the base; both interaction & executor must
    point at the SAME workspace (shared bind) so state/checkout stay consistent."""
    url = ""
    for r in ("engine", "game"):
        if repo == r:
            url = topic.get(f"{r}_repo") or topic.get("repos", {}).get(r, {}).get("repo_url") or ""
            break
    if not url:
        return None, f"no repo_url recorded for '{repo}'"
    checkout, name = _resolve_repo_checkout(workspace, topic, repo)
    # If not present, clone it (executor may need credentials via git env).
    if not os.path.isdir(os.path.join(checkout or "", ".git")):
        try:
            rc, out, err = _run_py("code_reviewer.py", [
                "--repo", url, "--branch", topic.get("review_branch") or "",
                "--base-branch", topic.get("base_branch") or "", "--dry",
                "--workspace", workspace, "--output", os.path.join(workspace, f"dry_{repo}.json")])
            if rc != 0:
                return None, f"checkout prep failed: {err[:150]}"
        except Exception as e:
            return None, f"checkout prep error: {e}"
    return checkout, None


def consume_pending(key, state_file, workspace, app_id, app_secret, actor="jenkins"):
    """Execute a topic's pending action. Returns (ok, message). Caller (Jenkins)
    is expected to hold the per-topic cross-process lock already."""
    topic = pipeline_state.get_topic(state_file, key)
    if topic is None:
        return False, "topic not found"
    pending = topic.get("pending")
    if not pending:
        return False, "no pending action"
    action = pending.get("action")
    render = topic.get("render_msg_id") or ""

    if action == "re_review":
        # Re-run the full review (Jenkins has creds). Reuse the run() pipeline.
        jira_url = topic.get("jira_url") or key
        _log('EXEC', 'REREVIEW', key, topic.get("jira_key", ""), '', '', 'consuming pending re_review')
        # Reset terminal phase so run() can re-run, then run the review.
        if topic.get("phase") in ("DONE", "FAILED"):
            pipeline_state.reset_for_retry(state_file, key)
        rc = _run_review_subprocess(key, jira_url, workspace, state_file)
        ok = rc == 0
        pipeline_state.clear_pending(state_file, key)
        # Feedback: refresh the result card (with buttons). If the diff hash is the
        # SAME as the previous result, the outcome intentionally did not change —
        # surface that so the user does not read "no reaction" as a failure.
        try:
            if ok:
                fresh = pipeline_state.get_topic(state_file, key)
                render = fresh.get("render_msg_id") or render
                note = ""
                if render:
                    eng_res, gam_res, st = _load_findings(workspace, key)
                    all_f = (eng_res or []) + (gam_res or [])
                    if not all_f:
                        note = "\n\nℹ️ 本次审查未发现代码问题（无 findings）。"
            text = feishu_notifier.render_state_card(pipeline_state.get_topic(state_file, key))
            if note:
                text = text + note
            if render and app_id and app_secret:
                _run_py("feishu_notifier.py", [
                    "update-reply", "--app-id", app_id, "--app-secret", app_secret,
                    "--message-id", render, "--message-base64", _b64_str(text)])
        except Exception as e:
            print(f"[executor] re_review feedback update failed: {e}", file=sys.stderr)
        return ok, "re_review executed, review complete" if ok else f"re_review failed rc={rc}"

    if action == "apply":
        patch = pending.get("patch") or topic.get("pending_patch") or {}
        diff = patch.get("diff") or ""
        repo = patch.get("repo") or "engine"
        file = patch.get("file") or ""
        if not diff:
            pipeline_state.clear_pending(state_file, key)
            return False, "apply: no diff recorded"
        checkout, err = _ensure_shared_checkout(topic, repo, workspace)
        if err:
            pipeline_state.clear_pending(state_file, key)
            return False, f"apply: {err}"
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as f:
            f.write(diff)
            patch_file = f.name
        _, head_before, _ = _run_git(["rev-parse", "HEAD"], checkout)
        rc2, out2, err2 = _run_git(["apply", patch_file], checkout)
        os.path.exists(patch_file) and os.unlink(patch_file)
        if rc2 != 0:
            pipeline_state.clear_pending(state_file, key)
            return False, f"apply: git apply failed: {err2 or out2}"
        pipeline_state.record_applied_patch(state_file, key, {
            "file": file, "repo": repo, "commit_before": head_before, "applied_at": "now",
        })
        pipeline_state.append_approval(state_file, key, actor, "apply_local", file, "ok", "executor applied")
        pipeline_state.clear_pending(state_file, key)
        return True, f"applied patch to {file} (commit {head_before[:8]})"

    if action == "push":
        branch = topic.get("review_branch") or ""
        if not branch:
            pipeline_state.clear_pending(state_file, key)
            return False, "push: no review_branch"
        checkout, err = _ensure_shared_checkout(topic, "engine", workspace)
        if err:
            pipeline_state.clear_pending(state_file, key)
            return False, f"push: {err}"
        _run_git(["add", "-A"], checkout)
        _run_git(["commit", "-m", f"[codereview-agent] apply review fix for {key}"], checkout)
        rc, out, err = _run_git(["push", "origin", f"HEAD:{branch}"], checkout)
        if rc != 0:
            pipeline_state.clear_pending(state_file, key)
            return False, f"push: failed: {err or out}"
        pipeline_state.append_approval(state_file, key, actor, "push_remote", branch, "ok", "executor pushed")
        pipeline_state.clear_pending(state_file, key)
        return True, f"pushed to {branch}"

    if action == "rollback":
        patch = pipeline_state.pop_last_applied_patch(state_file, key)
        if not patch:
            pipeline_state.clear_pending(state_file, key)
            return False, "rollback: nothing to revert"
        repo = patch.get("repo", "engine")
        checkout, err = _ensure_shared_checkout(topic, repo, workspace)
        if err:
            pipeline_state.clear_pending(state_file, key)
            return False, f"rollback: {err}"
        before = patch.get("commit_before") or ""
        if before and os.path.isdir(os.path.join(checkout or "", ".git")):
            _run_git(["reset", "--hard", before], checkout)
            pipeline_state.append_approval(state_file, key, actor, "rollback", patch.get("file",""), "ok", "executor rolled back")
        pipeline_state.clear_pending(state_file, key)
        return True, ("rolled back to " + before[:8]) if before else "rolled back"

    pipeline_state.clear_pending(state_file, key)
    return False, f"unknown pending action: {action}"


def _run_review_subprocess(key, jira_url, workspace, state_file):
    """Run the full review pipeline for a topic in a subprocess (reviewer has the
    creds needed). Returns rc."""
    try:
        import subprocess as _sp
        r = _sp.run(
            [sys.executable, os.path.join(SCRIPTS_DIR, "orchestrate.py"), "run",
             "--key", key, "--mode", "scan", "--jira-url", jira_url,
             "--workspace", workspace, "--pipeline-state-file", state_file],
            capture_output=True, text=True, timeout=1800)
        return r.returncode
    except Exception as e:
        print(f"[executor] re_review subprocess error: {e}", file=sys.stderr)
        return 1


def consume_all_pending(state_file, workspace, app_id, app_secret, lock_dir=None):
    """Jenkins entry: consume every topic's pending action, serialized per topic
    with a cross-process lock. Returns a dict {key: (ok, message)}."""
    lock_dir = lock_dir or pipeline_state.DEFAULT_LOCK_DIR()
    results = {}
    for key, _pending in pipeline_state.list_pending_topics(state_file):
        try:
            with pipeline_state.topic_lock_context(lock_dir, key):
                ok, msg = consume_pending(key, state_file, workspace, app_id, app_secret)
                results[key] = (ok, msg)
        except Exception as e:
            results[key] = (False, f"executor error: {e}")
    return results


def poll_ci_all(state_file, workspace, refresh_minutes=15):
    """arch-C: refresh MR CI status onto topic cards for recently-active topics
    that have an mr_url. Deduped by cmd_ci (only posts on change). Returns a dict
    {key: (posted_bool, status)}. Called by the Jenkins scan tick."""
    state = pipeline_state.load_state(state_file)
    posted = {}
    for key, t in (state.get("topics") or {}).items():
        if not t.get("mr_url"):
            continue
        # Only poll topics someone did something with recently (bounded API load).
        import time
        try:
            upd = t.get("updated_at") or ""
            ts = time.mktime(time.strptime(upd, "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            ts = 0
        if time.time() - ts > refresh_minutes * 60:
            continue
        try:
            rc = cmd_ci_silent(key, state_file, workspace)
            posted[key] = (rc == 0, "ok" if rc == 0 else "err")
        except Exception as e:
            posted[key] = (False, f"err: {e}")
    return posted


def cmd_ci_silent(key, state_file, workspace):
    """Thin wrapper to run cmd_ci via its args-object."""
    class _A:
        pass
    a = _A(); a.key = key; a.workspace = workspace; a.pipeline_state_file = state_file
    return cmd_ci(a)





def cmd_action(args):
    """arch-A: apply a card-button action (re_review/apply_patch/close_topic) to a
    topic, with the button clicker's open_id as the actor. Reuses the guarded
    logic so approval + audit + arch-D pending enqueue all behave as if via @回复."""
    key = args.key
    action = args.action
    actor = getattr(args, "sender_id", "") or ""
    state_file = args.pipeline_state_file or os.environ.get("PIPELINE_STATE_FILE", "pipeline-state.json")
    workspace = args.workspace
    topic = pipeline_state.get_topic(state_file, key)
    if topic is None:
        print(f"[action] topic not found: {key}", flush=True)
        return 1
    render = topic.get("render_msg_id") or ""
    app_id, app_secret = _env("FEISHU_APP_ID"), _env("FEISHU_APP_SECRET")

    if action == "re_review":
        ok, why = _approve(key, topic, actor, "re_review")
        if not ok:
            _update_card_text(app_id, app_secret, render, f"⛔ {why}", topic_key=key)
            return 1
        pipeline_state.set_pending(state_file, key, "re_review")
        _update_card_text(app_id, app_secret, render,
                          "⏳ 已记录重新审查请求，Jenkins 将拉取最新代码重新审查。", topic_key=key)
        print(f"[action] re_review enqueued by {actor}", flush=True)
    elif action == "close_topic":
        ok, why = _approve(key, topic, actor, "close_topic")
        if not ok:
            _update_card_text(app_id, app_secret, render, f"⛔ {why}", topic_key=key)
            return 1
        pipeline_state.close_topic(state_file, key, closed_by=actor, reason="用户点击关闭按钮")
        _update_card_text(app_id, app_secret, render,
                          "🔒 本话题已关闭，不再处理。", topic_key=key)
        print(f"[action] closed by {actor}", flush=True)
    elif action == "apply_patch":
        ok, why = _approve(key, topic, actor, "apply_patch")
        if not ok:
            _update_card_text(app_id, app_secret, render, f"⛔ {why}", topic_key=key)
            return 1
        print(f"[action] apply_patch: propose patch (findings-based) staged by {actor}", flush=True)
        # phase-C1: propose a patch preview from existing findings (not applied).
        eng, gam, status = _load_findings(workspace, key)
        all_f = (eng or []) + (gam or [])
        patch = _build_patch_target(all_f, "all")
        if patch.get("diff", "").strip() and all_f:
            pipeline_state.set_pending_patch(state_file, key, {
                "file": "all", "target": "all", "repo": "engine", "diff": patch.get("diff", ""),
            })
            _update_card_text(app_id, app_secret, render,
                              "✏️ 已提议修复补丁预览（未应用）。如确认请回复 `@ok` 让 Jenkins 应用，再 `@confirm push` 推送。",
                              topic_key=key)
        else:
            _update_card_text(app_id, app_secret, render,
                              "ℹ️ 当前没有可生成补丁的 findings。", topic_key=key)
    else:
        print(f"[action] unknown action: {action}", flush=True)
        return 1
    return 0


# ── Reliable interaction commands (routed from 1/2/3/4 + keywords) ───────────

def _cmd_rereview(key, topic, state_file, render_id, app_id, app_secret, actor=""):
    """指令 `2/重新审查`: owner/admin 校验后入队 re_review（Jenkins 执行）。"""
    ok, why = _approve(key, topic, actor, "re_review")
    if not ok:
        _update_card_text(app_id, app_secret, render_id, f"⛔ {why}")
        return
    pipeline_state.set_pending(state_file, key, "re_review")
    _update_card_text(app_id, app_secret, render_id,
                      "⏳ 已记录重新审查请求，Jenkins 将拉取最新代码重新审查。")


def _cmd_close(key, topic, state_file, render_id, app_id, app_secret, actor=""):
    """指令 `4/关闭`: owner/admin 关闭话题。关闭时会一并关闭该话题创建的 OPEN MR。"""
    ok, why = _approve(key, topic, actor, "close_topic")
    if not ok:
        _update_card_text(app_id, app_secret, render_id, f"⛔ {why}")
        return
    closed = _close_topic_created_mrs(topic)
    pipeline_state.close_topic(state_file, key, closed_by=actor, reason="用户指令关闭")
    note = f"🔒 本话题已关闭。{closed} 不处理。" if closed else "🔒 本话题已关闭，不再处理。"
    _update_card_text(app_id, app_secret, render_id, note)


def _close_topic_created_mrs(topic):
    """Find the topic's fix-branch MRs (source_branch == {src}-fix-{task}) that are still
    OPEN and close them. Returns a human note ('' if none). Only closes MRs created for
    THIS topic's fix branch; leaves the original review MR and unrelated MRs untouched."""
    import urllib.request, urllib.error, urllib.parse, json as _json
    pp = _project_path(topic)
    tok = _env("GITLAB_TOKEN")
    if not pp or not tok:
        return ""
    new_branch = _new_branch_name(topic)
    proj = urllib.parse.quote(pp, safe="")
    try:
        r = urllib.request.Request(
            f"https://gitlab.booming-inc.com/api/v4/projects/{proj}/merge_requests?state=opened&per_page=100",
            headers={"PRIVATE-TOKEN": tok})
        mrs = _json.loads(urllib.request.urlopen(r, timeout=20).read())
    except Exception:
        return ""
    closed = 0
    for m in mrs:
        if m.get("source_branch") == new_branch:
            try:
                data = _json.dumps({"state_event": "close"}).encode()
                req = urllib.request.Request(
                    f"https://gitlab.booming-inc.com/api/v4/projects/{proj}/merge_requests/{m.get('iid')}",
                    data=data, method="PUT", headers={"PRIVATE-TOKEN": tok, "Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=20)
                closed += 1
            except Exception:
                pass
    return f"（已关闭本轮创建的 {closed} 个 OPEN MR）" if closed else ""


def _cmd_fix_patch(key, topic, all_findings, render_id, workspace, state_file,
                   app_id, app_secret, actor=""):
    """指令 `1/补丁`: 生成修复补丁方案（基于 findings 的建议，供确认后应用）。"""
    ok, why = _approve(key, topic, actor, "apply_patch")
    if not ok:
        _update_card_text(app_id, app_secret, render_id, f"⛔ {why}")
        return 0
    patch = _build_patch_target(all_findings, "all")
    if not all_findings or not (patch.get("diff") or "").strip():
        _update_card_text(app_id, app_secret, render_id,
                          "ℹ️ 当前没有可生成补丁的 findings，或 `@ok` 后再应用。")
        return 0
    pipeline_state.set_pending_patch(state_file, key, {
        "file": "all", "target": "all", "repo": "engine", "diff": patch.get("diff", ""),
        "created_at": "now",
    })
    _update_card_text(app_id, app_secret, render_id,
                      "✏️ 已生成修复补丁方案（基于 findings 建议，未应用）。\n"
                      "回复 `@ok` 让 Jenkins 应用，`@confirm push` 推送，`@撤销` 取消。")
    return 0


def _new_branch_name(topic):
    src = topic.get("review_branch") or ""
    task = topic.get("jira_key") or "task"
    return f"{src}-fix-{task}" if src else f"fix-{task}"


def _project_path(topic):
    mr_url = topic.get("mr_url") or ""
    if "merge_requests" in mr_url:
        import jira_parser as _jp
        pp, _ = _jp.parse_gitlab_mr_url(mr_url)
        return pp
    return ""


def _create_or_get_mr(topic, all_findings, create_if_missing=False):
    """Detect an MR whose source_branch == the fix's new branch; optionally create it.
    Returns (mr_iid, mr_web_url, source_branch, note)."""
    import urllib.request, urllib.error, urllib.parse, json as _json
    pp = _project_path(topic)
    tok = _env("GITLAB_TOKEN")
    if not pp or not tok:
        return None, None, "", "no project path or token"
    new_branch = _new_branch_name(topic)
    proj = urllib.parse.quote(pp, safe="")
    def _get(url):
        r = urllib.request.Request(url, headers={"PRIVATE-TOKEN": tok})
        return _json.loads(urllib.request.urlopen(r, timeout=20).read())
    # 1) detect existing MR on the fix branch
    try:
        mrs = _get(f"https://gitlab.booming-inc.com/api/v4/projects/{proj}"
                   f"/merge_requests?state=all&per_page=50")
    except Exception:
        mrs = []
    for m in mrs:
        # Only treat an OPENED MR as "the current MR". A closed/merged MR on this
        # branch is stale (e.g. a prior empty MR) — do not surface it as yours.
        if m.get("source_branch") == new_branch and (m.get("state") or "").lower() == "opened":
            return m.get("iid"), m.get("web_url", ""), new_branch, "detected existing"
    if not create_if_missing:
        return None, None, new_branch, "no MR for fix branch yet"
    # 2) Only create if the fix branch actually has changes vs target — never create an
    #    empty/meaningless MR (a real defect we hit: an MR with 0 diffs and wrong intent).
    target = topic.get("base_branch") or "master"
    try:
        cmp = _get(f"https://gitlab.booming-inc.com/api/v4/projects/{proj}"
                   f"/repository/compare?from={urllib.parse.quote(target, safe='')}"
                   f"&to={urllib.parse.quote(new_branch, safe='')}")
        diffs = cmp.get("diffs") or []
    except Exception:
        diffs = []
    if not diffs:
        return None, None, new_branch, "fix 分支相对 " + target + " 无改动，先推送修复再更新MR"
    # 3) create
    title = f"Fix {topic.get('jira_key') or topic.get('message_id') or ''}: code review fixes"
    # NOTE: must not call _generate_mr_card here (it calls back into _create_or_get_mr
    # -> infinite recursion). Build a simple description instead.
    desc_lines = [f"Code review fix for {topic.get('jira_key') or topic.get('message_id') or ''}"]
    crit = [f for f in (all_findings or []) if (f.get('severity') or '').lower() in ('critical', 'high')]
    for f in (crit or (all_findings or []))[:5]:
        desc_lines.append(f"- {(f.get('file') or '?')}: {(f.get('issue') or '').strip()[:90]}")
    desc = "\n".join(desc_lines)
    payload = _json.dumps({"source_branch": new_branch, "target_branch": target,
                           "title": title, "description": desc}).encode()
    try:
        r = urllib.request.Request(
            f"https://gitlab.booming-inc.com/api/v4/projects/{proj}/merge_requests",
            data=payload, method="POST", headers={"PRIVATE-TOKEN": tok, "Content-Type": "application/json"})
        m = _json.loads(urllib.request.urlopen(r, timeout=30).read())
        return m.get("iid"), m.get("web_url", ""), new_branch, "created"
    except urllib.error.HTTPError as e:
        return None, None, new_branch, f"create failed HTTP {e.code}: {e.read()[:150]}"


def _generate_mr_card(topic, all_findings, workspace, key):
    """指令 `mr/生成MR单/更新MR`: 基于 findings + 已应用修改生成 MR 描述。区分
    评审 MR（原始）与本次修复 MR（新分支）。检测/创建新 MR 后填真实新 URL。"""
    jira = topic.get("jira_key") or key
    branch = topic.get("review_branch") or ""
    new_branch = _new_branch_name(topic)
    # detect/create fix-branch MR to get its REAL url
    mriid, murl, nbranch, mrnote = _create_or_get_mr(topic, all_findings, create_if_missing=True)
    mr_url = topic.get("mr_url") or ""
    c = (topic.get("applied_patches") or [])
    lines = [f"# MR 单：{jira}", ""]
    lines.append(f"**评审 MR**（原始）: {mr_url if mr_url else '（无）'}")
    lines.append(f"**本次修复分支**: `{nbranch or new_branch}`")
    if murl:
        lines.append(f"**本次修复 MR**: {murl}　({mrnote})")
    elif mrnote:
        lines.append(f"**本次修复 MR**: 未创建 — {mrnote}")
    lines.append("")
    lines.append("**变更要点：**")
    if all_findings:
        crit = [f for f in all_findings if (f.get('severity') or '').lower() in ('critical', 'high')]
        shows = (crit or all_findings)[:5]
        for f in shows:
            file = f.get("file") or "?"
            lines.append(f"- {file}: {(f.get('issue') or '').strip()[:90]}")
    else:
        lines.append("- （无 findings）")
    lines.append("")
    lines.append(f"**已应用补丁**：{len(c)} 个。" if c else "**已应用补丁**：无。")
    lines.append("")
    lines.append("> 本次修复 MR 为机器人通过 GitLab API 创建/检测得到；请人工核对后合并。")
    return "\n".join(lines)


def cmd_ci(args):
    """arch-C: refresh a topic's MR GitLab CI status onto its card (read-only)."""
    key = args.key
    state_file = args.pipeline_state_file or os.environ.get("PIPELINE_STATE_FILE", "pipeline-state.json")
    topic = pipeline_state.get_topic(state_file, key)
    if topic is None:
        print(f"[ci] topic not found: {key}", flush=True)
        return 1
    mr_url = topic.get("mr_url") or ""
    app_id, app_secret = _env("FEISHU_APP_ID"), _env("FEISHU_APP_SECRET")
    if not mr_url:
        print(f"[ci] topic {key} has no mr_url", flush=True)
        return 1
    import gitlab_ci
    block = gitlab_ci.render_ci_card_block(mr_url)
    # Dedup: only push a card update when the CI status changed since last poll,
    # so an idle pipeline doesn't re-post an identical card every scan tick.
    summary = gitlab_ci.pipeline_summary(mr_url)
    new_status = f"{summary.get('status')}::{summary.get('pipeline_id')}"
    if topic.get("ci_status") == new_status:
        print(f"[ci] {key}: unchanged ({new_status})", flush=True)
        return 0
    print(f"[ci] {key}: {block.replace(chr(10), ' ')[:150]}", flush=True)
    render = topic.get("render_msg_id")
    if render and app_id and app_secret:
        # Build on the REVIEW SUMMARY (findings), not the sparse state card, so the
        # findings stay visible and CI status is appended — NOT replacing them.
        base = (topic.get("review_summary") or
                feishu_notifier.render_state_card(pipeline_state.get_topic(state_file, key)))
        _run_py("feishu_notifier.py", [
            "update-reply", "--app-id", app_id, "--app-secret", app_secret,
            "--message-id", render, "--message-base64",
            _b64_str(base + "\n\n---\n" + block)])
    # record last reported status to prevent repeat posts
    st = pipeline_state.load_state(state_file)
    topic2 = st["topics"].get(key)
    if topic2 is not None:
        topic2["ci_status"] = new_status
        st["updated_at"] = pipeline_state._now_iso()
        pipeline_state.save_state(st, state_file)
    return 0







def _load_findings(workspace, key):
    """Load findings for both repos from the workspace result files.

    Returns (eng, gam, status) where status is one of:
      - "ok":      at least one result file was read and parsed (findings may still
                   legitimately be empty if the review was clean).
      - "missing": no result file could be read — the caller must NOT report the
                   review as "clean", because we simply could not see the results.
    """
    eng = gam = []
    found_any = False
    for repo in ("engine", "game"):
        p = os.path.join(workspace, f"result_{key}_{repo}.json")
        res = _read_json_file(p)
        if res and isinstance(res, dict):
            found_any = True
            f = (res.get("review") or {}).get("findings") or []
            if repo == "engine":
                eng = f
            else:
                gam = f
    return eng, gam, ("ok" if found_any else "missing")


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
    # If the "jira_url" is actually a GitLab MR link, resolve the MR's source/
    # target branch directly (this powers "send an MR link -> review its branch").
    import common as _common
    if _common.MR_URL_PATTERN.search(jira_url or ""):
        try:
            import jira_parser
            mi = jira_parser.gitlab_get_mr(jira_url, gitlab_token)
            if mi:
                # Normalize: run() reads mr_info.branch as the review source branch.
                if not mi.get("branch"):
                    mi["branch"] = mi.get("source_branch") or ""
                info = {
                    "project": "MR",
                    "issue_key": jira_url,
                    "mr_info": mi,
                    "mr_url": jira_url,
                    "default_branch": "master",
                }
                # Resolve engine/game repos for the MR's project by matching the
                # MR URL path against config project repo URLs.
                for pid, pc in (_common.get_projects() or {}).items():
                    eng = pc.get("engine_repo", "")
                    if eng and jira_parser.repo_matches_mr_url(eng, jira_url):
                        info["engine_repo"] = eng
                        info["game_repo"] = pc.get("game_repo", "")
                        info["project"] = pid
                        if not info.get("default_branch"):
                            info["default_branch"] = pc.get("default_branch") or "master"
                        break
                return info
        except Exception as e:
            print(f"[orchestrate] MR resolve err: {e}", file=sys.stderr)
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
        # DEBUG (finding empty-diff vs sim): log env + dry outcome
        import logging as _lg
        _dbg = (f"[deepdebug] repo={repo} branch={rb} base={baseb} "
                f"rc={rc} GITLAB_TOKEN_len={len(_env('GITLAB_TOKEN'))} "
                f"diff_hash={diff_hash[:8]} changed={len(dry.get('changed_files') or [])} stderr={err[:80]}")
        print(_dbg, flush=True)
        # Guard: an empty diff (sha1 of "") must never be cached/reused as a valid
        # review — it means the branch/base produced no diff. Only cache real diffs.
        EMPTY_DIFF_HASHES = {"da39a3ee5e6b4b0d3255bfef95601890afd80709", ""}
        if diff_hash not in EMPTY_DIFF_HASHES:
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
        # Save to cache for reuse only for a REAL (non-empty) diff.
        if res and diff_hash and diff_hash not in EMPTY_DIFF_HASHES:
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
            "--message-id", topic["render_msg_id"], "--message-base64", _b64_str(text)])
    except Exception as e:
        print(f"[orchestrate] WARN: state card update failed: {e}", file=sys.stderr)


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(description="CodeReview topic orchestrator")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="Run the full pipeline for one topic")
    p.add_argument("--key", required=True)
    p.add_argument("--mode", default="scan", choices=["scan", "manual"])
    p.add_argument("--workspace", default=_DEFAULT_WORKSPACE)
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
    p.add_argument("--sender-id", default="", help="The user who sent the @/reply (approver for guarded actions)")
    p.add_argument("--workspace", default=_DEFAULT_WORKSPACE)
    p.add_argument("--pipeline-state-file", default="")

    p = sub.add_parser("consume", help="[arch-D executor] consume pending actions for all topics")
    p.add_argument("--workspace", default=_DEFAULT_WORKSPACE)
    p.add_argument("--pipeline-state-file", default="")
    p.add_argument("--lock-dir", default="")

    p = sub.add_parser("action", help="[arch-A] apply a card-button action to a topic (with actor)")
    p.add_argument("--key", required=True, help="topic message_id")
    p.add_argument("--action", required=True, help="re_review | apply_patch | close_topic")
    p.add_argument("--sender-id", default="", help="the button clicker's open_id (actor for approval)")
    p.add_argument("--workspace", default=_DEFAULT_WORKSPACE)
    p.add_argument("--pipeline-state-file", default="")

    p = sub.add_parser("ci", help="[arch-C] refresh a topic's MR GitLab CI status onto its card")
    p.add_argument("--key", required=True, help="topic message_id")
    p.add_argument("--workspace", default=_DEFAULT_WORKSPACE)
    p.add_argument("--pipeline-state-file", default="")

    p = sub.add_parser("ci-poll", help="[arch-C] refresh CI status for recently-active topics with mr_url")
    p.add_argument("--workspace", default=_DEFAULT_WORKSPACE)
    p.add_argument("--pipeline-state-file", default="")
    p.add_argument("--refresh-minutes", type=int, default=15)

    args = parser.parse_args(argv)
    if args.command == "run":
        sys.exit(run(args))
    elif args.command == "interact":
        sys.exit(interact(args))
    elif args.command == "consume":
        lock_dir = getattr(args, "lock_dir", "") or pipeline_state.DEFAULT_LOCK_DIR()
        results = consume_all_pending(args.pipeline_state_file, args.workspace,
                                      _env("FEISHU_APP_ID"), _env("FEISHU_APP_SECRET"),
                                      lock_dir=lock_dir)
        for k, (ok, msg) in results.items():
            print(f"[executor] {k}: {'OK' if ok else 'FAIL'} — {msg}", flush=True)
        sys.exit(0 if results else 1)
    elif args.command == "action":
        sys.exit(cmd_action(args))
    elif args.command == "ci":
        sys.exit(cmd_ci(args))
    elif args.command == "ci-poll":
        res = poll_ci_all(args.pipeline_state_file, args.workspace,
                          refresh_minutes=getattr(args, "refresh_minutes", 15))
        for k, (posted, st) in res.items():
            print(f"[ci-poll] {k}: {'posted' if posted else 'skip'} {st}", flush=True)
        sys.exit(0)


if __name__ == "__main__":
    main()