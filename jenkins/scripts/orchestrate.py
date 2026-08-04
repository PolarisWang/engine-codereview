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
    args = parser.parse_args(argv)
    if args.command == "run":
        sys.exit(run(args))


if __name__ == "__main__":
    main()