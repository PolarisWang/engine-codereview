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

# Central runtime configuration (lifecycle / concurrency / LLM / workspace).
from config import (IDLE_CLOSE_DAYS, AUTO_CLOSE_MR, MAX_CONCURRENT_REVIEWS,
                    DEFAULT_WORKSPACE, CHECKOUT_RESET_ON_REUSE, EDIT_MODEL,
                    AGENT_MAX_ROUNDS, AGENT_MAX_TOKEN, AUTO_CLOSE_HOURS,
                    MAX_OPEN_CHECKOUT_DIRS, DISK_FREE_MIN_BYTES)
import config as _config          # 方案C: 经 config 模块读 MSG/CMD(集中 config.yaml)
MSG = _config.MSG
CMD = _config.CMD
M = _config.M

# Scripts dir (this file's directory)
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPTS_DIR, "..", ".."))

# Shared workspace (arch-D): both the interaction layer and the Jenkins executor
# only see a CONSISTENT workspace so a topic's checkout + result files are shared.
# The persistent volume /var/lib/report-server/daily is bind-mounted into the agent
# container at the same path, so it survives restarts and is reachable from Jenkins.
# Value centralized in config.py (DEFAULT_WORKSPACE); this is a compat alias.
_DEFAULT_WORKSPACE = DEFAULT_WORKSPACE

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

def _apply_repo_override(engine_repo, game_repo, mr_url):
    """Given the project's configured engine_repo / game_repo and a GitLab MR URL,
    return (engine_repo, game_repo) with the mr_url-based repo override applied.

    只覆盖 ENGINE repo：当 MR 挂在与项目 engine_repo 不同的项目时（例 ENG-30314：
    MR 在 chaos-cb-2 但 config 的 ENG.engine_repo=chaos.git），把 engine_repo 改为 MR
    所在仓库，否则引擎 review 会去错误的仓库找分支 -> 空diff -> "无改动"。

    GAME repo 绝不能被 mr_url 覆盖（方案A）：否则一个纯引擎 MR（如 CB2N-27312，MR 在
    chaos-cb-2、分支是 mempool 引擎改动、游戏仓库根本不存在该分支）会把 game_repo 也
    改成引擎仓库，导致"游戏 review 复用引擎 diff、冒充游戏 review"。游戏 review 必须走
    真实游戏仓库——分支/改动不存在时 code_reviewer 置 branch_exists=False，_record_repo_state
    记为 SKIPPED（无游戏改动）。
    """
    if not (mr_url and "merge_requests" in mr_url):
        return engine_repo, game_repo
    import jira_parser as _jp2
    _mr_pp, _ = _jp2.parse_gitlab_mr_url(mr_url)
    if not _mr_pp:
        return engine_repo, game_repo
    _mr_repo = f"git@gitlab.booming-inc.com:{_mr_pp}.git"
    if not _jp2.repo_matches_mr_url(engine_repo, mr_url):
        print(f"[orchestrate] MR repo ({_mr_pp}) != engine_repo; overriding engine_repo -> {_mr_repo}", flush=True)
        engine_repo = _mr_repo
    # game_repo 保持配置的真实游戏仓库；不因 MR 覆盖。
    return engine_repo, game_repo


def _send_review_segments(reply_msg_id, segs, app_id, app_secret, chat_id, key, issue_key, project):
    """把 review 结果写到群里的第一张卡, 处理多段(超长>45000字符)的情况。

    方案A(修复 old bug): update-reply 把整张卡 PATCH 覆盖; 旧代码对每段依次 update-reply,
    导致多段时每段各自覆盖、第一张卡只剩最后一段、前面的 review 全丢。
    现改为:
      - 首段(Seg0)用 update-reply 覆盖进度卡成 review 头部/完整结果(普通 review 只有 1 段 -> 单次, 不变)
      - 后续段(Seg1..)用 reply-message 追加新消息(不覆盖, 完整保留)
    返回 rc_all(0=全成功; 非0=有失败, 调用方据此记 FAILED 并 return 1)。
    """
    rc_all = 0
    if segs:
        rc, _, err = _run_py("feishu_notifier.py", [
            "update-reply", "--app-id", app_id, "--app-secret", app_secret,
            "--message-id", reply_msg_id, "--message-base64", _b64_str(segs[0])])
        if rc != 0:
            rc_all = rc
            _log('NOTIFY', 'FAILED', key, issue_key, project, '', f'update reply failed: {err}')
    for seg in segs[1:]:
        rc, _, err = _run_py("feishu_notifier.py", [
            "reply-message", "--app-id", app_id, "--app-secret", app_secret,
            "--chat-id", chat_id, "--message-id", key, "--message-base64", _b64_str(seg)])
        if rc != 0:
            rc_all = rc
            _log('NOTIFY', 'FAILED', key, issue_key, project, '', f'append reply failed: {err}')
    return rc_all


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

    # Concurrency admission: cap how many independent review subprocesses run at
    # once. Acquiring a slot is process-scoped (flock auto-released on exit); if
    # the cap is reached, keep the topic pending for the next scan tick, post a
    # queue notice, and do not start another review now.
    was_queued = (existing or {}).get("queued") if existing is not None else False
    qcard = (existing or {}).get("render_msg_id") if existing is not None else ""
    if not _acquire_review_slot():
        if qcard and app_id and app_secret:
            _update_card_text(app_id, app_secret, qcard,
                              "🤖 **正在排队...**\n" + _queue_notice(app_id, app_secret, qcard))
        queue_pos = _queue_position(state_file, key)
        pipeline_state.set_topic_fields(state_file, key, queued=True,
                                        queued_at=_now_iso(), queued_position=queue_pos)
        _log('SCANNED', 'QUEUED', key, (existing or {}).get("jira_key", ""), '', '',
             f'concurrency cap {MAX_CONCURRENT_REVIEWS} reached; deferred')
        return 0
    if was_queued:
        # A previously queued topic that now has a slot: clear the queue marker so a
        # later review completion does not try to release it again, and tell the user
        # their turn came up.
        pipeline_state.set_topic_fields(state_file, key, queued=False, queued_position=None)
        if qcard and app_id and app_secret:
            _update_card_text(app_id, app_secret, qcard, "🤖 " + _started_notice())

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
            # (review 进度卡复用,非命令反馈,故 PATCH 保留而非新起卡)
            _update_card_text(app_id, app_secret, existing_card,
                              M("review_retry_progress"))
            reply_msg_id = existing_card
            pipeline_state.transition(state_file, key, to="PARSING", status="RUNNING",
                                      render_msg_id=reply_msg_id)
            _log('PARSING', 'RUNNING', key, getattr(args, "jira_key", ""), '', '', 'reusing card in place (retry)')
        else:
            # 进度卡显示易读标识: jira_key(如 CB2N-25256) > 短 message_id(截断避免一长串)。
            _disp = getattr(args, "jira_key", "") or issue_key or (key[-12:] if key else "")
            progress = M("review_progress", key=_disp)
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
    # 当有 MR URL 时，MR 所在仓库才是 review 实际要 clone 的（而非项目配置的 engine_repo/
    # game_repo）。只覆盖 ENGINE repo（见 _apply_repo_override / 方案A）；game_repo 保持配置的
    # 真实游戏仓库，避免纯引擎 MR 把游戏 review 带成"复用引擎 diff 的假游戏 review"。
    engine_repo, game_repo = _apply_repo_override(engine_repo, game_repo, mr_url)
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
    # rage-style per-topic agent review (Path A): spawn claude-opus-5 subprocess
    # agent so findings bind to real code with whole-file verification. Read
    # config.yaml review.agent_enabled OR env so the gray-switch works both when
    # the executor sets env and when it only relies on config. HTTP fallback kept.
    _rev_cfg = {}
    try:
        import config as _cfg
        _rev_cfg = (_cfg.load_config().get("review") or {})
    except Exception:
        pass
    use_agent = bool(_rev_cfg.get("agent_enabled")) or \
        _env("REVIEW_AGENT", "").lower() in ("1", "true", "yes") or \
        _env("REVIEW_AGENT_MODEL", "") != ""
    # round-N incremental: pass the previous review SHA + carried (already-settled)
    # issue indices so the agent diffs only since last review and does NOT re-raise
    # issues earlier rounds confirmed fixed (rage incr_base / review_rounds).
    _tcur = pipeline_state.get_topic(state_file, key) or {}
    last_review_commit = (_tcur.get("last_review_commit") or "") or \
        _env("REVIEW_LAST_COMMIT", "")
    carried = (_tcur.get("carried") or []) or []
    if isinstance(carried, str):
        carried = [int(x) for x in carried.split(",") if x.strip().isdigit()]
    eng_res, gam_res = _review_repos(
        key, project, issue_key, review_branch, base_branch, engine_base,
        engine_repo, game_repo, mr_url, workspace, eng_out, gam_out,
        use_agent=use_agent, last_review_commit=last_review_commit, carried=carried)

    # 4. Record per-repo terminal states + update the in-flight card.
    _record_repo_state(state_file, key, "engine", eng_out, eng_res, review_branch, base_branch,
                       repo_url=engine_repo)
    _record_repo_state(state_file, key, "game", gam_out, gam_res, review_branch, base_branch,
                       repo_url=game_repo)
    _log('REPO', 'DONE', key, issue_key, project, '', 'repo states recorded')
    # (不再提前 update 进度卡 —— 让最终 review 结果在下方一次性刷新进度卡, 避免中途变稀疏状态卡)

    # P2: 记录 review 闭环状态(供 closure 驱动 round-2+ 交互)。合并两仓 findings,
    # 计入 review_issues / issue_count / review_state / review_triage / creator_open_id。
    try:
        _set_review_closure_fields(state_file, key, eng_res, gam_res)
    except Exception as _ce:
        print(f"[closure] set fields err: {_ce}", file=sys.stderr)


    # 5. Render + send final summary as PLAIN-TEXT messages (普通文字消息, 全量不折叠).
    pipeline_state.transition(state_file, key, to="NOTIFYING", status="RUNNING")
    _log('NOTIFY', 'RUNNING', key, issue_key, project, '', 'sending final summary')
    # 方案C: 直接用 code_reviewer 产出的 skill 模板 review_text(含 Summary/Strengths/
    # 架构性能/严重度), 而非 feishu_notifier 旧的分组重渲染 —— 保证群里显示 skill 模板。
    # engine/game 两份分开、各加一行短标(引擎仓库 / 游戏仓库), 否则合并后分不清来源。
    # ── 方案C(交互卡): 统一用 rage 标准卡 render_rage_card (4级 #N + doc + 双段指令) ──
    merged_findings = []
    for repo, res in (("engine", eng_res or {}), ("game", gam_res or {})):
        for f in feishu_notifier._findings_of(res):
            merged_findings.append({"repo": repo, **f})

    doc_url = ""
    try:
        # 复杂审查生成完整评审文档(仅 triage=complex 时), 否则 doc_url 留空(卡上不显示)。
        _t_after = pipeline_state.get_topic(state_file, key) or {}
        if _t_after.get("review_triage") == "complex" and merged_findings:
            okd, _tok, _url, _err = _maybe_create_review_doc(
                key, issue_key, project, _t_after, merged_findings,
                review_branch, app_id, app_secret)
            if okd and _url:
                doc_url = _url
                pipeline_state.set_topic_fields(state_file, key, review_doc_url=_url)
            if _err and not okd:
                print(f"[doc] complex doc skipped: {_err}", file=sys.stderr)
    except Exception as _dce:
        print(f"[doc] create review doc err: {_dce}", file=sys.stderr)

    _round_no = int((pipeline_state.get_topic(state_file, key) or {}).get("review_round") or 1)
    # R7-C4 (统一卡): 一律走 rage 标准卡(4 级 #N + doc 链接 + 双段指令), 不再跳旧 3 级卡。
    # 空 findings 由 render_rage_card 渲染"已完成审查, 未发现问题"。
    full_text = feishu_notifier.render_rage_card(
        issue_key, merged_findings, doc_url=doc_url,
        triage=(pipeline_state.get_topic(state_file, key) or {}).get("review_triage") or "",
        round_no=_round_no, mr_url=mr_url, jira_url=jira_url,
    )
    # 全量 review 单条普通消息发出(≤45000字符); 超大才拆第二段。
    segs = _split_text(full_text)
    # 记录渲染后的完整 review(供 ci-poll 追加, 不覆盖 findings)
    pipeline_state.set_topic_fields(state_file, key, review_summary=full_text)
    if reply_msg_id:
        # 方案D: 用 update-reply 把进度卡(reply_msg_id)刷新成最终 review 结果(不另发),
        # 这样 thread 里只有 1 条 review 结果卡(由进度卡演进), 不再出现"两条 review 结果"。
        #
        # 方案A(修复): update-reply 是把整张卡 PATCH 覆盖。旧代码对每段依次 update-reply,
        # 导致超长 review(>45000字符, 分段)时每段各自覆盖, 第一张卡只剩最后一段、前面的
        # review 全丢。现改为: 首段 update-reply(卡=review 头部), 后续段用 reply-message
        # 追加新消息(不覆盖, 完整保留)。普通 review 只有 1 段 → 仍单次 update-reply 不变。
        rc_all = _send_review_segments(
            reply_msg_id, segs, app_id, app_secret, chat_id, key, issue_key, project)
        # 交互指引卡: review 结果后单独发一张, 教用户下一步(重点是 `优化` 自动改码+提MR)。
        if rc_all == 0:
            _run_py("feishu_notifier.py", [
                "reply-message", "--app-id", app_id, "--app-secret", app_secret,
                "--chat-id", chat_id, "--message-id", key, "--message-base64", _b64_str(M("interact_hint"))])
        if rc_all != 0:
            topic_after = pipeline_state.record_failure(state_file, key, "update reply failed")
            _alert_if_exhausted(state_file, topic_after, app_id, app_secret)
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


def _split_text(text, max_chars=45000):
    """Split long plain-text into ≤max_chars segments on line boundaries, so each
    Feishu plain-text message is fully delivered without truncation (review 全量展示).
    Preserves content; never drops or truncates findings.
    Default 45000: a full review fits in ONE Feishu plain-text message (verified
    ~30k chars sends ok), so it isn't split into a multiple-message "duplicate"
    pile; only an unusually large review breaks into a second message."""
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    parts = []
    lines = text.split("\n")
    cur = ""
    for line in lines:
        if cur and len(cur) + len(line) + 1 > max_chars:
            parts.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
        # handle a single over-long line (unlikely) by hard-cut
        while len(cur) > max_chars:
            parts.append(cur[:max_chars])
            cur = cur[max_chars:]
    if cur:
        parts.append(cur)
    return parts


def _extract_msg_id(stdout, rc):
    if rc != 0 or not stdout:
        return ""
    try:
        data = json.loads(stdout)
        return data.get("message_id", "")
    except json.JSONDecodeError:
        return ""


def _send_reply(app_id, app_secret, reply_msg_id, text):
    rc, out, err = _run_py("feishu_notifier.py", [
        "update-reply", "--app-id", app_id, "--app-secret", app_secret,
        "--message-id", reply_msg_id, "--message-base64", _b64_str(text)])
    if rc != 0:
        print(f"[send_reply] update-reply failed rc={rc} msg_id={reply_msg_id} err={err[:200]}",
              file=sys.stderr)


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

# 方案C (C4-1): agent loop is strictly READ-ONLY Q&A. It ONLY exposes read-only
# tools — it can never re-run reviews, propose patches (write), or close topics.
# All write operations (改码/确认/推送/重审/关闭/建MR) go through the FIXED command
# router (2/4/改码/确认/MR单...), which calls the _cmd_* / _create_or_get_mr
# functions directly — those do NOT depend on the agent loop choosing a tool. This
# removes the "agent claims it will push/close" hallucination at the root: the LLM
# has no write tool and no capability to act.
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
        "description": "Generate a text-only patch PREVIEW that fixes a specific finding (read-only; does NOT stage or apply anything). Specify the finding's file (or 'all' for critical findings).",
        "input_schema": {"type": "object",
                         "properties": {"target": {"type": "string",
                                                   "description": "file path to fix, or 'all' for critical findings"}},
                         "required": ["target"]},
    },
    {
        "name": "answer",
        "description": "Answer the user's question about this review/topic. Input can be a question or '请补充说明'. This tool does NOT perform any write/push operation.",
        "input_schema": {"type": "object",
                         "properties": {"question": {"type": "string"}}, "required": ["question"]},
    },
    {
        "name": "deep_dive",
        "description": "深入分析指定的若干条 review 发现（用 #序号 或 文件名 指定，如 '#2'/'#2,5'/'scene.cpp'/'all'）。返回对每条发现的更深入解释：根因、涉及代码范围、更具体的修复建议。只读，不改任何结论。",
        "input_schema": {"type": "object",
                         "properties": {"targets": {"type": "string",
                                                   "description": "要深入的发现引用：#序号 / 文件名子串 / all"}},
                         "required": ["targets"]},
    },
    {
        "name": "challenge",
        "description": "质疑/复核指定的 review 发现（用 #序号 或 文件名 指定），给出「成立/存疑/不成立」的再核结论及依据。只读，不改任何结论。",
        "input_schema": {"type": "object",
                         "properties": {"targets": {"type": "string",
                                                   "description": "要质疑的发现引用：#序号 / 文件名子串 / all"},
                                       "reason": {"type": "string",
                                                  "description": "用户的质疑理由（若有）"}},
                         "required": ["targets"]},
    },
]

# Runtime limits centralized in config.py (AGENT_MAX_ROUNDS / AGENT_MAX_TOKEN).


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
    """Build the agent system prompt with topic context (READ-ONLY).

    方案C: the agent loop is strictly a Q&A assistant. It MUST NOT and CANNOT
    perform any write/push/close/edit operation — it has no such tools and must
    never claim to. Write operations are only reachable via the fixed command
    router (改码/确认/关闭/重审/MR单). This prevents the "agent claims it will
    push/merge but does nothing" hallucination."""
    return (
        "You are a READ-ONLY code-review Q&A assistant in a Feishu topic. The user's "
        "message is @-addressed to you.\n"
        "You can ONLY call the read-only tools: get_status / get_findings / "
        "generate_patch_preview / answer / deep_dive / challenge.\n"
        "deep_dive(targets) 深入分析指定发现(#序号/文件名/all); challenge(targets,reason) "
        "复核指定发现(成立/存疑/不成立)。两者都只读。\n"
        "HARD RULE: You have NO write, push, merge, close, or edit ability — never "
        "claim you pushed, merged, applied, closed, or started anything. If the user "
        "asks you to actually change code / push / create an MR / close the topic, "
        "DO NOT attempt it and do NOT pretend to. Instead answer the analysis you can, "
        "then tell the user to use the exact fixed command that can do it:\n"
        "  - 改码 / @确认提交并建mr : auto-fix the code and push a fix branch + create an MR\n"
        "  - 关闭 / 4 关闭         : close the topic\n"
        "  - 2 重审 / MR单 / 指引 : re-review / MR description / precise fix guidance\n"
        "- Reply in Chinese, concise, grounded in the findings. When done, give a "
        "plain-text final answer. Never emit a [tool_use] placeholder.\n"
        f"- Topic context: {topic.get('jira_key','')} ({topic.get('project','')}), "
        f"branch {topic.get('review_branch','')} -> {topic.get('base_branch','')}."
    )


def _refresh_last_user_activity(state_file, key):
    """F2: record a real user reply as the topic's last_user_activity. Extracted so
    it's independently testable and so the state_file is passed in (previously a
    before-use bug silently disabled this refresh — see #P0). No-op on a closed or
    missing topic; swallows storage errors with a logged warning (never breaks the
    user-facing reply)."""
    try:
        cur = pipeline_state.get_topic(state_file, key)
        if cur is not None and cur.get("phase") != "CLOSED":
            pipeline_state.set_topic_fields(state_file, key,
                                             last_user_activity=pipeline_state._now_iso())
            return True
    except Exception as _e:
        print(f"[interact] refresh last_user_activity error: {_e}", file=sys.stderr)
    return False


def _autoclose_message(topic, note=""):
    """Build a CORRECT, topic-specific group notice for an auto-close. States the real
    idle threshold (AUTO_CLOSE_HOURS小时), identifies the topic (jira key / short id) so
    the group knows WHICH topic was closed, and appends what was released (note)."""
    hrs = AUTO_CLOSE_HOURS
    if hrs >= 24:
        window = f"{hrs/24:.0f}天" if hrs % 24 == 0 else f"{hrs}小时"
    else:
        window = f"{hrs}小时"
    label = (topic or {}).get("jira_key") or (topic or {}).get("message_id") or ""
    msg = f"🔒 话题 {label} 因 {window} 无新回复，已自动关闭。如需重新审查可新开话题。"
    if note:
        msg += f"\n（已释放：{note}）"
    return msg


def _should_auto_close(topic, now_ts=None):
    """R7: whether a topic is due for lazy auto-close (idle > IDLE_CLOSE_DAYS and
    not already CLOSED). Applies to ANY non-CLOSED phase — including DONE/FAILED —
    so finished reviews that stay ignored release their fix MR/branch. Extracted
    from interact() for testability. `now_ts` is injectable (seconds) for tests.

    F2/B: idle 判定用 `last_user_activity`(仅真实用户回复刷新); 若无用户回复
    (last_user_activity 缺失)则用 `created_at` —— 而非 `updated_at`(会被 ci-poll/consume/
    review 等 background 写入刷新, 导致活跃话题永不超时)。created_at 缺失的历史话题
    回退 updated_at 兜底(避免误关)。"""
    if not topic:
        return False
    if topic.get("phase") == "CLOSED":
        return False
    try:
        import time as _time
        upd = (topic.get("last_user_activity") or topic.get("created_at")
               or topic.get("updated_at") or "")
        if upd:
            ts = _time.mktime(_time.strptime(upd, "%Y-%m-%dT%H:%M:%S"))
            idle_hours = ((now_ts if now_ts is not None else _time.time()) - ts) / 3600.0
        else:
            idle_hours = AUTO_CLOSE_HOURS + 1
    except Exception:
        idle_hours = 0
    return idle_hours > AUTO_CLOSE_HOURS


# First-token command keywords recognized by the reliable router (orchestrate.py
# "Reliable command routing" block + the confirmation gating handled earlier).
# Used by _strip_mention so an @-mention that IS itself a command keyword (e.g.
# `@指引 ...`) is treated as the command, not stripped away into an agent loop.
# 方案C: 命令词表从 config.CMD 读取(集中在 config.yaml commands:), 业务代码不再 hardcode。
# 追加数字/数字别名等固定路由词。
_COMMAND_FIRST_WORDS = set()
for _wlist in _config.CMD.values():
    if isinstance(_wlist, list):
        _COMMAND_FIRST_WORDS.update(w for w in _wlist if isinstance(w, str))
_COMMAND_FIRST_WORDS |= {
    "1", "补丁", "生成补丁", "修复",
    "2", "重新审查", "重审", "review", "重新review",
    "3", "解释",
    "预览", "预览补丁", "patch预览",
    "应用并提交", "确认提交", "push并建mr",
}


# ── A) 可扩展命令注册表 ────────────────────────────────────────────────
# 每条命令通过 @command(*关键词, guarded=..., requires_findings=...) 注册一个 handler。
# handler 统一签名:
#     handler(ctx)  -> int
# ctx = {key, topic, all_findings, findings_status, render_id, workspace, state_file,
#        app_id, app_secret, actor, api_key, base_url, model, low, word, arg}
# 这样加一个新交互 = 写一个 handler + 一行 @command 注册, 不再改 interact() 主函数。
_HANDLER_REGISTRY = {}     # word -> (fn, needs_findings, guarded)


def command(*words, needs_findings=True, guarded=None):
    """Register a handler for one or more command keywords (extensible table)."""
    def _reg(fn):
        for w in words:
            if w:  # guard empty-string keyword
                _HANDLER_REGISTRY[w] = (fn, needs_findings, guarded)
                _COMMAND_FIRST_WORDS.add(w)
        return fn
    return _reg


def _dispatch_command(ctx):
    """Look up a registered handler by the leading word; returns its rc, or None if
    no command matched (caller falls through to operation-guard / agent loop)."""
    entry = _HANDLER_REGISTRY.get(ctx["word"])
    if not entry:
        return None
    fn, needs_findings, _g = entry
    if needs_findings and not (ctx.get("all_findings") or ctx.get("findings_status") == "ok"):
        # findings missing/failed handled inside each handler; pass through
        pass
    return fn(ctx)


# 现有 11 条命令迁到注册表(结构A): 每个 handler 薄封装原 _cmd_* / _finalize 路径。
# 统一 ctx 注入, 不改变原行为; 新增命令复用同样的 @command 注册即可。
def _register_builtin_commands():
    import config as _c  # noqa: F401  (already imported at module top as _config)
    C = _config.CMD

    @command("1", "补丁", "生成补丁", "修复")
    def _h_fix_patch(ctx):
        return _cmd_fix_patch(ctx["key"], ctx["topic"], ctx["all_findings"],
                              ctx["render_id"], ctx["workspace"], ctx["state_file"],
                              ctx["app_id"], ctx["app_secret"], ctx["actor"])

    @command("2", "重新审查", "重审", "review", "重新review")
    def _h_rereview(ctx):
        _cmd_rereview(ctx["key"], ctx["topic"], ctx["state_file"], ctx["render_id"],
                      ctx["app_id"], ctx["app_secret"], ctx["actor"])
        return 0

    @command("3", "解释")
    def _h_explain(ctx):
        low = ctx["low"] or ""
        rest = low.strip()[1:].strip() if low.strip().startswith("3") else low.strip()[2:].strip()
        answer = _answer_question(rest or "请解释当前发现", ctx["all_findings"],
                                  ctx["api_key"], ctx["base_url"], ctx["model"])
        _finalize(ctx["key"], answer, ctx["render_id"], [], ctx["state_file"],
                  ctx["app_id"], ctx["app_secret"])
        return 0

    @command(*C["close"], guarded="close_topic")
    def _h_close(ctx):
        return _cmd_close(ctx["key"], ctx["topic"], ctx["state_file"], ctx["render_id"],
                          ctx["app_id"], ctx["app_secret"], ctx["actor"],
                          workspace=ctx["workspace"])

    @command(*C["mr"])
    def _h_mr(ctx):
        text = _generate_mr_card(ctx["topic"], ctx["all_findings"], ctx["workspace"],
                                 ctx["key"], state_file=ctx["state_file"])
        _finalize(ctx["key"], text, ctx["render_id"], [], ctx["state_file"],
                  ctx["app_id"], ctx["app_secret"])
        return 0

    @command("预览", "预览补丁", "patch预览")
    def _h_preview(ctx):
        text = _render_patch_preview(ctx["topic"], ctx["all_findings"], ctx["api_key"],
                                     ctx["base_url"], ctx["model"], ctx["workspace"])
        _finalize(ctx["key"], text, ctx["render_id"], [], ctx["state_file"],
                  ctx["app_id"], ctx["app_secret"])
        return 0

    @command(*C["guidance"])
    def _h_guidance(ctx):
        text = _generate_fix_guidance(ctx["topic"], ctx["all_findings"], ctx["api_key"],
                                      ctx["base_url"], ctx["model"], ctx["workspace"])
        _finalize(ctx["key"], text, ctx["render_id"], [], ctx["state_file"],
                  ctx["app_id"], ctx["app_secret"])
        return 0

    @command(*C["optimize"], guarded="auto_edit")
    def _h_optimize(ctx):
        return _cmd_optimize(ctx["key"], ctx["topic"], ctx["all_findings"], ctx["render_id"],
                             ctx["workspace"], ctx["state_file"], ctx["app_id"],
                             ctx["app_secret"], ctx["actor"])

    @command(*C["autofix"], guarded="auto_edit")
    def _h_autofix(ctx):
        return _cmd_auto_edit(ctx["key"], ctx["topic"], ctx["all_findings"], ctx["render_id"],
                              ctx["workspace"], ctx["state_file"], ctx["app_id"],
                              ctx["app_secret"], ctx["actor"])

    @command("应用并提交", "确认提交", "push并建mr")
    def _h_apply_submit(ctx):
        text = _build_patch_preview_target(ctx["all_findings"], "all")
        _finalize(ctx["key"], M("apply_submit_pending") + text[:400], ctx["render_id"], [],
                  ctx["state_file"], ctx["app_id"], ctx["app_secret"])
        return 0

    @command(*C["status"])
    def _h_status(ctx):
        _finalize(ctx["key"], _build_status_text(ctx["topic"]), ctx["render_id"], [],
                  ctx["state_file"], ctx["app_id"], ctx["app_secret"])
        return 0

    # ── 交互增强(C3): 更新结论(guarded)——修订指定 findings 并重渲染方案C 卡 ──
    @command("更新结论", "更新review结论", "修订结论", guarded="update_conclusion")
    def _h_update_conclusion(ctx):
        return _cmd_update_conclusion(ctx["key"], ctx["topic"], ctx["all_findings"],
                                      ctx["render_id"], ctx["workspace"], ctx["state_file"],
                                      ctx["app_id"], ctx["app_secret"], ctx["actor"], ctx["arg"])

    @command("深入", "deepdive", "深入分析", needs_findings=True)
    def _h_deep(ctx):
        return _cmd_deep_dive(ctx["key"], ctx["topic"], ctx["all_findings"], ctx["render_id"],
                              ctx["workspace"], ctx["state_file"], ctx["app_id"],
                              ctx["app_secret"], ctx["actor"], ctx["arg"], ctx["api_key"],
                              ctx["base_url"], ctx["model"])

    @command("质疑", "challenge")
    def _h_challenge(ctx):
        return _cmd_challenge(ctx["key"], ctx["topic"], ctx["all_findings"], ctx["render_id"],
                              ctx["workspace"], ctx["state_file"], ctx["app_id"],
                              ctx["app_secret"], ctx["actor"], ctx["arg"], ctx["api_key"],
                              ctx["base_url"], ctx["model"])


_register_builtin_commands()



def _strip_mention(text):
    """Strip a leading @-mention so the first real command token is used for
    routing. Handles BOTH ASCII mentions ("@_user_1 指引 ...") and Chinese ones
    ("@机器人 改码 ..."). Crucially, if the token after '@' is itself a known
    command keyword ("@指引 ...", "@改码 ..."), we do NOT strip it — the keyword
    IS the command. Without this, `@指引 修复所有问题` was mis-routed into the
    agent loop (producing raw [tool_use] history) instead of the fixed 指引 path."""
    import re as _re
    s = (text or "").strip()
    if not s.startswith("@"):
        return s
    # token immediately after '@' (run of non-whitespace)
    m2 = _re.match(r'^@([^\s]+)', s)
    if not m2:
        return s
    token = m2.group(1)
    if token in _COMMAND_FIRST_WORDS:
        # e.g. `@指引 修复...` — the @-token IS the command keyword; strip only the
        # leading '@' so `word = split()[0]` yields `指引` and routes correctly.
        return s[1:].strip()  # drop the '@' before the keyword, keep the rest
    # a genuine mention placeholder (bot id / user id / bot's display name) -> strip
    return s[m2.end():].strip()


# 方案C (C4-3): operation-intent words. If a reply that fell through the FIXED
# command router contains any of these, the user is almost certainly trying to make
# the bot DO something (push/merge/close/apply) rather than ask a review question.
# We intercept BEFORE the agent loop so the write-capable intent never reaches the
# read-only assistant, and we guide the user to the exact fixed command instead.
# Phrases (not single chars) keep false-positives low (e.g. "逻辑该不该改" won't trigger).
_OPERATION_WORDS = [
    "确认", "push", "推送", "提交", "合并", "merge", "关闭", "改码", "自动修复", "优化", "自动优化",
    "重审", "重新审查", "生成mr", "建mr", "更新mr", "应用", "apply", "commit", "建立mr",
]


def _looks_like_operation(text):
    """True if the (already @-stripped) reply probably requests a write/operation,
    so we can keep it out of the read-only agent loop."""
    s = (text or "").lower()
    for w in _OPERATION_WORDS:
        if w in s:
            return True
    return False


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
    # 先解析出 state_file/workspace(它们若不在此处赋值, 手记处会 UnboundLocalError,
    # 被裸 except 吞掉, 导致 last_user_activity 永不刷新 —— F2 闲置判定失效)。
    workspace = args.workspace
    state_file = args.pipeline_state_file or os.environ.get("PIPELINE_STATE_FILE", "pipeline-state.json")
    # F2: 真实用户回复刷新 last_user_activity(独立闲置判定), 而非 updated_at。
    _refresh_last_user_activity(state_file, key)
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

    # ── P2: rage-style review closure (self-service dev loop). When the topic is
    # in an active review state (review_state set by the agent path), a thread
    # reply like `1 3` / `ok` / `done` / `close` drives the closure mechanically.
    # Anything non-review falls through to the existing command-word path below.
    # Ported from rage DESIGN §1.5 / §1.23 (closure.py). See
    # jenkins/skills/rage-review/closure.py.
    if not pipeline_state.is_closed(topic):
        rcl = _try_handle_closure(key, topic, reply_text, actor, workspace, state_file)
        if rcl is not _CLOSURE_NO_MATCH:
            return rcl

    # ── Closed-topic handling (admin/owner close OR auto-silence) ─────────────
    # A closed topic ignores further replies except @审计 (audit stays visible).
    if pipeline_state.is_closed(topic):
        if low0 in ("@审计", "@log", "审计"):
            pass  # fall through to audit branch below
        else:
            reason = topic.get("closed_reason") or "已关闭"
            _finalize(key, M("closed_with_reason", reason=reason),
                      render_id, [], state_file, app_id, app_secret)
            return 0
    else:
        # Lazy auto-close: any non-fresh topic with no new reply for IDLE_CLOSE_DAYS.
        # Runs on every scan of an idle topic so resources (fix-branch MR + branch +
        # checkout via phase=CLOSED) are released without needing a manual `4 关闭`.
        # R7: applies to DONE/FAILED too, not just in-progress topics — a finished
        # review that stays ignored for IDLE_CLOSE_DAYS releases its fix MR/branch.
        if _should_auto_close(topic):
            # Release fix-branch MRs + delete the fix branch (best-effort).
            if AUTO_CLOSE_MR:
                try:
                    _close_topic_resources(topic, workspace)
                except Exception as _e:
                    print(f"[autoclose] cleanup resources error: {_e}", file=sys.stderr)
            pipeline_state.close_topic(state_file, key, closed_by="auto",
                                       reason=f"{AUTO_CLOSE_HOURS}小时无新回复自动关闭")
            pipeline_state.set_topic_fields(state_file, key, phase="CLOSED")
            _finalize(key, _autoclose_message(topic), render_id, [], state_file, app_id, app_secret)
            return 0

    eng_findings, gam_findings, findings_status = _load_findings(workspace, key)
    all_findings = (eng_findings or []) + (gam_findings or [])

    # ── Confirmation gating (user replies to a staged action) ───────────────
    low = reply_text.lower()
    import re as _re3
    # Normalize away @, spaces, commas, full-width punctuation so "确认 提交并建mr",
    # "确认，提交" etc. collapse for reliable confirmation matching — the old
    # exact whole-string match rejected natural inputs with spaces (falling into
    # the agent loop with a confusing reply).
    low_norm = _re3.sub(r'[@\s，,。.、：:]+', '', low)
    # 确认 means: strip a leading @-mention first relative to confirmation intent.
    confirm_intent = _re3.search(r'(确认|confirm|push|提交|合并|应用)', low_norm)
    pending = topic.get("pending_patch") or {}
    is_ok = ("确认" in low_norm and len(low_norm) <= 6) or low in ("@ok", "ok", "好的", "执行", "确认")
    # push-confirm intent: contains push/推送 with 确认/confirm (or exact push words)
    is_confirm_push = ("push" in low_norm and confirm_intent) or low in ("@confirm push", "确认push", "@push", "推") \
                      or ("推送" in low and any(k in low for k in ("确认", "confirm")))
    # edit-confirm intent: contains 确认 + 提交/建mr (any grouping, spaces ok)
    is_confirm_edit = (("确认" in low_norm) and
                       any(k in low_norm for k in ("提交", "建mr", "提交并建"))) \
                      or low in ("@确认", "确认提交", "确认并建mr", "git提交") \
                      or any(t in low for t in ("@确认", "确认提交", "确认并建mr", "git提交"))
    is_rollback = low in ("@撤销", "@revert", "撤销", "回退") or any(t in low for t in ("@撤销", "@revert"))

    if pending:
        if pending.get("state") == "staged_agent_edit":
            # auto-edit closure: 确认/提交 -> enqueue commit+push+建MR ; 撤销 -> cancel
            if is_confirm_edit or is_ok:
                pipeline_state.set_pending(state_file, key, "agent_edit_confirm",
                                           patch={"actor": actor})
                pipeline_state.append_approval(state_file, key, actor, "push_fix_branch",
                                               pending.get("branch", ""), "ok", "@确认改码 enqueued")
                _proc_reply(key, topic,
                            "⏳ 已记录确认，Jenkins 将推送修复分支 `{0}` 并自动建 MR。".format(pending.get("branch", "")),
                            render_id, state_file, app_id, app_secret, intent="确认并推送修复分支、创建修复 MR")
                return 0
            if is_rollback:
                pipeline_state.set_pending_patch(state_file, key, None)
                _proc_reply(key, topic, M("edit_cancelled"), render_id, state_file, app_id, app_secret)
                return 0
        else:
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
    cmd = _strip_mention(low)
    word = cmd.split()[0] if cmd else ""
    # ── A) 数据驱动命令注册表路由(替换原 if 链) ──
    ctx = {
        "key": key, "topic": topic, "all_findings": all_findings,
        "findings_status": findings_status, "render_id": render_id,
        "workspace": workspace, "state_file": state_file,
        "app_id": app_id, "app_secret": app_secret, "actor": actor,
        "api_key": api_key, "base_url": base_url, "model": model,
        "low": low, "word": word,
        "arg": cmd[len(word):].strip() if cmd and word else "",
    }
    _dispatched = _dispatch_command(ctx)
    if _dispatched is not None:
        return _dispatched

    # ── 方案C (C4-3): operation-intent interception ────────────────────────
    # Nothing above matched a fixed command. If the message still looks like the
    # user is asking us to DO something (push/merge/close/改码/确认...), do NOT send
    # it to the read-only agent loop (which would hallucinate "I'll push" without
    # doing it). Guide them to the exact fixed command instead.
    if _looks_like_operation(_strip_mention(low)):
        hint = M("qa_hint")
        _finalize(key, hint, render_id, [], state_file, app_id, app_secret)
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
            answer = M("llm_failed")
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
    answer = M("loop_wrapup")
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
        # 带稳定序号, 便于用户用 `#N` 引用具体一条(深入/质疑/更新结论)。
        return "\n".join(f"- [#{i}] [{f.get('severity')}] {f.get('file')}: {f.get('issue','')}"
                         for i, f in _findings_indexed(all_findings[:25])), False
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
    if name == "deep_dive":
        # 只读: 对指定发现做更深入分析(不改结论)。不读真实文件(事件侧无 GitLab 凭证),
        # 基于原 finding 的 issue/suggestion + LLM 推理给出根因/范围/更细建议。
        targets = (inp.get("targets") or "all").strip()
        picks, hint = resolve_findings(all_findings, targets)
        if not picks:
            return f"⚠️ 未解析到可深入的发现。{hint}\n可用 `#序号` / 文件名 / `all`。", False
        _txt = "\n".join(
            f"- [#{i}] {f.get('file')} [{f.get('severity')}]: {f.get('issue')} "
            f"→ 建议 {f.get('suggestion')}".rstrip()
            for i, f in _findings_indexed(all_findings) if f in picks)
        _pr = (f"请对以下 {len(picks)} 条 review 发现做**更深入的针对性分析**(每条给出: "
               f"可能根因、影响的代码范围/调用链、以及更具体的修复步骤)。仅作分析, 不改结论。\n\n{_txt}")
        deep = _call_llm_simple(_pr, api_key, base_url, model, max_tokens=1200)
        return f"🔍 深入分析（{targets}）：\n" + (deep or "（未能生成）"), False
    if name == "challenge":
        # 只读: 复核一条发现成立/存疑/不成立(不改结论)。
        targets = (inp.get("targets") or "all").strip()
        reason = (inp.get("reason") or "").strip()
        picks, hint = resolve_findings(all_findings, targets)
        if not picks:
            return f"⚠️ 未解析到可质疑的发现。{hint}", False
        _txt = "\n".join(
            f"- [#{i}] {f.get('file')} [{f.get('severity')}]: {f.get('issue')} "
            f"→ 建议 {f.get('suggestion')}".rstrip()
            for i, f in _findings_indexed(all_findings) if f in picks)
        _pr = (f"用户质疑以下 review 发现(reason: {reason or '未给出'})。请基于代码审查常识复核,"
               f"并对每条给出结论: **成立 / 存疑 / 不成立** + 一句话依据。仅复核, 不改结论。\n\n{_txt}")
        verdict = _call_llm_simple(_pr, api_key, base_url, model, max_tokens=900)
        return f"⚖️ 复核（{targets}）：\n" + (verdict or "（未能生成）"), False
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


def _safe_checkout_path(checkout, file):
    """Map an LLM-provided `file` (untrusted) onto a path that is GUARANTEED to
    stay inside the topic's checkout dir (R9).

    Finding `file` values come from model output and the code-review prompt does
    not constrain them. A crafted/errant value could use `..`, an absolute path,
    or a symlink to escape the checkout and let the auto-edit write files outside
    the topic's tree (scope: repo_checkout_only is *declared* in policy.yaml but
    was not enforced in code). `realpath` resolves any `..`/symlinks, and
    `commonpath` asserts the resolved target still lives under the checkout.

    Raises ValueError on any escape so the caller can mark that finding as
    failed (refuse-to-write), never silently writing outside the checkout.
    """
    base = os.path.realpath(checkout)
    target = os.path.realpath(os.path.join(base, file or ""))
    if os.path.commonpath([base, target]) != base:
        raise ValueError(f"path escapes checkout: {file!r}")
    return target


# Checkout-scoped cross-process lock (R17): auto-edit (改码) and its confirm run
# against a SHARED `{repo}-review` checkout that multiple topics may reuse. The
# per-topic lock serializes only the SAME topic; two topics editing the same repo
# can still clobber each other's working tree. This lock is keyed by the repo
# name (not topic), so all auto-edit/confirm for one repo are serialized across
# processes, while different repos stay parallel. Scope is ONE execution (we
# must NOT hold it across the user's @确认 wait).
def _checkout_lock(repo_name, lock_dir=None):
    """Return a context manager holding a cross-process flock for a repo's review
    checkout. repo_name is the repo basename (lock key). Must only be held during
    a single edit/confirm mutation, never across the user-confirm wait."""
    import hashlib
    lock_dir = lock_dir or os.environ.get("CHECKOUT_LOCK_DIR") or pipeline_state.DEFAULT_LOCK_DIR()
    key = hashlib.sha1((repo_name or "?").encode("utf-8")).hexdigest()[:16]
    return pipeline_state.topic_lock_context(lock_dir, f"checkout_{key}")


def _auth_git_env(extra=None):
    """Build a git subprocess env that can AUTHENTICATE via git_askpass (GITLAB_TOKEN
    delivered through env, never on argv/URL). Use for ANY git read/write that may
    need to hit GitLab (commit/push/fetch), matching _ensure_checkout's clone auth.
    Without this, a push with only GIT_TERMINAL_PROMPT=0 fails with "could not read
    Username ... terminal prompts disabled"."""
    import os as _os
    askpass = _os.path.join(SCRIPTS_DIR, "git_askpass.sh")
    env = dict(_os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS=askpass,
               CR_GITLAB_USER=_env("GITLAB_USER", "gitlab-ci-token"),
               CR_GITLAB_TOKEN=_env("GITLAB_TOKEN"))
    if isinstance(extra, dict):
        env.update(extra)
    return env


# ── 方案B: 按-topic 隔离 checkout 目录 ────────────────────────────────
# 同一 git 仓库的每个 topic 拥有自己独立的 checkout 目录
#   {workspace}/{repo_name}-review/{slug}
# 而非所有 topic 挤在一个 {repo_name}-review 里。这消除跨 topic 的 checkout
# 状态串线(mempool 历史泄漏进 LOD fix MR 的根因之一)。目录路径由 topic 确定性
# 推导(branch 名), 因此 _ensure_checkout 与 _close_topic_resources 都能算出
# 同一个目录来做"建 clone"与"关闭即删"配对。
#
# 磁盘资源保护(三个保证)：
#   1) open 话题上限  —— MAX_OPEN_CHECKOUT_DIRS 封顶现有目录数, 超出则不再新建
#      (topic 保持 pending/排队, 待有关口释放), 避免目录数随累积话题无界增长。
#   2) 关闭即释放     —— topic CLOSED 时 _close_topic_resources 删除它自己的目录。
#   3) 磁盘容量保护   —— 新建/复用前检查 statvfs 剩余空间, 低于 DISK_FREE_MIN_BYTES
#      则拒绝新建(复用或返回错误), 不把共享盘写爆。

def _checkout_slug(topic):
    """Deterministic, filesystem-safe per-topic slug for the checkout subdir.
    Derives from the review branch (stable across the topic's lifetime) so close-time
    can re-derive the same dir. Falls back to the message_id / a hash if no branch."""
    import re as _re
    branch = (topic.get("review_branch") or topic.get("base_branch") or "") \
        if isinstance(topic, dict) else ""
    slug = _re.sub(r"[^A-Za-z0-9_.-]", "_", branch).strip("._") if branch else ""
    if not slug:
        import hashlib
        src = str(topic.get("message_id")) or str(topic) or "untracked"
        slug = "topic_" + hashlib.sha1(src.encode("utf-8")).hexdigest()[:12]
    return slug[:64]


def _checkout_dir_for(workspace, repo_name, topic):
    """Return the per-topic checkout dir: {workspace}/{repo}-review/{slug}. Parent
    {workspace}/{repo}-review is the repo's dir pool (all topics of that repo)."""
    return os.path.join(workspace, f"{repo_name}-review", _checkout_slug(topic))


def _repo_name_from_checkout(checkout):
    """Robustly recover the repo lock-key from a checkout path under either layout:
       flat  {workspace}/chaos-cb-2-review        -> 'chaos-cb-2'
       nested{workspace}/chaos-cb-2-review/{slug} -> 'chaos-cb-2' (parent has -review)
    Used so the cross-process lock keys stay per-repo regardless of per-topic subdirs."""
    base = os.path.basename(checkout or "")
    parent = os.path.basename(os.path.dirname(checkout or "") or "")
    for cand in (parent, base):
        if cand.endswith("-review"):
            return cand[: -len("-review")]
    return parent or base


def _list_checkout_dirs(workspace, repo_name):
    """List existing per-topic checkout subdir paths for a repo pool.

    Only counts dirs that are REAL per-topic checkouts (contain a `.git`).
    A legacy pre-方案B pool had the whole repo tree flat at {repo}-review/, so its
    top-level content dirs (_source/, _content/, ...) are NOT per-topic checkouts
    and must be ignored — otherwise the open-dir ceiling is miscounted (18 'dirs'
    when only 1 is a real checkout) and 优化/改码 wrongly refuses on an existing
    pool. Per-topic slug subdirs always contain `.git` (they're git-clone targets)."""
    pool = os.path.join(workspace, f"{repo_name}-review")
    try:
        out = []
        for n in os.listdir(pool):
            d = os.path.join(pool, n)
            if os.path.isdir(d) and os.path.isdir(os.path.join(d, ".git")):
                out.append(d)
        return out
    except FileNotFoundError:
        return []
    except OSError:
        return []


def _disk_free_bytes(directory):
    """Free bytes on the filesystem holding `directory` (best-effort; -1 on error).
    Used so the bot never fills the shared disk (statvfs guard before new clone)."""
    import shutil
    try:
        usage = shutil.disk_usage(os.path.abspath(directory or "."))
        return usage.free
    except Exception:
        return -1


def _sweep_orphan_checkout_dirs(workspace, state_file):
    """C#3: reclaim per-topic checkout dirs whose owning topic no longer exists.

    A topic that crashes before close can leave its `{repo}-review/{slug}` dir behind,
    which never enters the open-dir ceiling and never gets cleaned -> enough crashes
    exhaust MAX_OPEN_CHECKOUT_DIRS and block new topics. This sweep, run opportunistically
    every autoclose tick, computes the set of slugs still owned by any live (non-CLOSED)
    topic and deletes every `{workspace}/*-review/*` subdir whose slug is not in that set.

    Conservative by design: a dir is only removed if its slug matches NO live topic, so
    we can never delete a live topic's checkout. (A cross-repo slug collision keeps an
    orphan but never touches a live dir — safe, just slower to reclaim.)"""
    try:
        topics = pipeline_state.list_topics(state_file)
    except Exception as _e:
        print(f"[sweep] list_topics error: {_e}", file=sys.stderr)
        return
    live = set()
    for t in topics:
        if isinstance(t, dict) and t.get("phase") != "CLOSED":
            live.add(_checkout_slug(t))
    try:
        entries = os.listdir(workspace or ".")
    except OSError:
        return
    import shutil as _shutil
    removed = 0
    for entry in entries:
        if not entry.endswith("-review"):
            continue
        pool = os.path.join(workspace, entry)
        if not os.path.isdir(pool):
            continue
        try:
            for slug in os.listdir(pool):
                if slug in live:
                    continue
                d = os.path.join(pool, slug)
                if os.path.isdir(d) and os.path.isdir(os.path.join(d, ".git")):
                    _shutil.rmtree(d, ignore_errors=True)
                    removed += 1
        except OSError:
            continue
    if removed:
        print(f"[sweep] removed {removed} orphaned checkout dir(s) under {workspace}", flush=True)


def _ensure_checkout(topic, workspace):
    """Clone (or reuse) the topic's OWN review-branch checkout to a per-topic dir in
    workspace. Returns (checkout_dir, err) or (None, err). Authenticates via the
    GIT_ASKPASS helper (token delivered through env, never on argv or in the clone
    URL) so the token is not persisted into <checkout>/.git/config — same rule the
    reviewer's prepare_repo uses.

    方案B: each topic gets its OWN dir ({repo}-review/{slug}), so a prior topic's
    git state (e.g. a mempool branch) can never leak into another topic's fix branch.
    Guards: MAX_OPEN_CHECKOUT_DIRS ceiling + DISK_FREE_MIN_BYTES statvfs protection.
    The top-level repo basename is also the reason we keep the parent named
    '{repo}-review' — existing lock/branch logic keys off it."""
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
    dest = _checkout_dir_for(workspace, repo_name, topic)
    tok = _env("GITLAB_TOKEN")
    if not tok:
        return None, "no GITLAB_TOKEN for checkout"
    # 方案B 磁盘资源保护(3 个保证)：
    #   a) 磁盘容量 —— 剩余空间低于阈值时绝不再新建 clone(复用已存在目录或拒绝)。
    if not os.path.isdir(os.path.join(dest, ".git")):
        free = _disk_free_bytes(workspace)
        if free >= 0 and free < DISK_FREE_MIN_BYTES:
            print(f"[checkout] disk low: {free//(1024*1024)}MiB free < "
                  f"{DISK_FREE_MIN_BYTES//(1024*1024)}MiB; refuse new clone", file=sys.stderr)
            return None, "磁盘空间不足，稍后再试"
        #   b) open 目录上限 —— 该 repo 的目录数已达 MAX_OPEN_CHECKOUT_DIRS 且本 topic
        #      尚未建目录: 不新建, 保持 pending/排队, 等关闭话题释放后再重试(严格隔离,
        #      不复用他人目录)。这样磁盘上每个 topic 目录数被封顶。
        existing = _list_checkout_dirs(workspace, repo_name)
        if len(existing) >= MAX_OPEN_CHECKOUT_DIRS:
            print(f"[checkout] per-topic dir ceiling "
                  f"{MAX_OPEN_CHECKOUT_DIRS} reached for {repo_name}; defer", file=sys.stderr)
            return None, f"当前 open 话题目录数已达上限({MAX_OPEN_CHECKOUT_DIRS})，请先关闭旧话题"
    if os.path.isdir(os.path.join(dest, ".git")):
        # Reuse the tree but force-align it to the latest remote HEAD and drop any
        # residual working changes/untracked files left by an earlier topic or a
        # previous fix attempt. Without this, the reusable checkout silently
        # carries a stale SHA (and stale haves) — which caused oversized push packs
        # (HTTP 413) and foreign/corrupt file states to leak across reviews.
        # P3: 任一复位失败都必须可见 —— 静默失败会留下陈旧基线(MR7091 的串线向量)。
        _fr = _sp.run(["git", "-C", dest, "fetch", "--quiet", "origin", src],
                      capture_output=True, text=True, timeout=300)
        _rr = _sp.run(["git", "-C", dest, "reset", "--hard", f"origin/{src}"],
                      capture_output=True, text=True, timeout=60)
        _cr = _sp.run(["git", "-C", dest, "clean", "-fdx"],
                      capture_output=True, text=True, timeout=60)
        for _nm, _rc, _err in (("fetch", _fr, _fr.stderr[:300]),
                               ("reset", _rr, _rr.stderr[:300]),
                               ("clean", _cr, _cr.stderr[:300])):
            if _rc != 0:
                print(f"[checkout] {_nm} failed rc={_rc} in {dest}: {_err}", file=sys.stderr)
        # P3: reset 是基线完整性的关键 —— 复位失败意味着 checkout 可能停留在陈旧/串线基线
        # (MR7091 根因), 此时绝不复用该树, 返回错误让调用方重新拉取而非在坏基线上改码。
        if _rr.returncode != 0:
            return None, f"checkout 复位到 origin/{src} 失败(基线不可信), 请重试"
        return dest, None
    # Clean URL — no token; git_askpass.sh supplies credentials via env.
    repo_url = f"https://gitlab.booming-inc.com/{pp}.git"
    # Ensure the repo's pool parent exists so git clone can create the per-topic leaf.
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
    except OSError:
        pass
    _askpass = os.path.join(SCRIPTS_DIR, "git_askpass.sh")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS=_askpass,
               CR_GITLAB_USER=_env("GITLAB_USER", "gitlab-ci-token"),
               CR_GITLAB_TOKEN=tok)
    try:
        r = _sp.run(["git", "clone", "--quiet", "--single-branch", "--branch", src,
                     "--depth", "2", repo_url, dest], capture_output=True, text=True,
                    env=env, timeout=600)
        if r.returncode != 0 or not os.path.isdir(os.path.join(dest, ".git")):
            return None, f"clone failed: {r.stderr[:200]}"
        return dest, None
    except Exception as e:
        return None, f"clone err: {e}"


def _ensure_checkout_preserve(topic, workspace, repo="engine"):
    """Locate THIS topic's per-repo checkout WITHOUT resetting/cleaning it,
    preserving the working-tree edits that _agent_edit_all wrote (agent_edit →
    staged_agent_edit → agent_edit_confirm runs across separate consume ticks.)

    Root cause of "创建MR失败 / fix 分支无改动": the confirm step used _ensure_checkout,
    which does fetch + reset --hard origin/{src} + clean -fdx, WIPING _agent_edit_all's
    working-tree edits. The stored-diff replay then failed (context mismatch) and the
    old code silently pushed an empty branch → fix branch == review tip → no MR.

    `repo` selects engine vs game: each repo has ITS OWN per-topic dir
    ({repo}-review/{slug}), so confirm reuses the same dir the edit ran in (safe with
    per-topic isolation). Only clone if the dir is missing (rare — edit created it)."""
    import subprocess as _sp
    src = topic.get("review_branch") or topic.get("base_branch") or ""
    # Resolve this repo's real dir + name + project (engine from mr_url, game from
    # game_repo URL) so confirm lands on the SAME checkout the edit used.
    dest, repo_name = _resolve_repo_checkout(workspace, topic, repo)
    if not dest:
        return None, "no checkout dir for " + repo
    pp = _repo_project(topic, repo)
    if os.path.isdir(os.path.join(dest, ".git")):
        # PRESERVE working tree + index (the staged diffs). Do NOT fetch/reset/clean.
        return dest, None
    if not pp:
        return None, "no project path for " + repo
    # Not present: clone it fresh (edited branch state is gone anyway).
    tok = _env("GITLAB_TOKEN")
    if not tok:
        return None, "no GITLAB_TOKEN for checkout"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    _askpass = os.path.join(SCRIPTS_DIR, "git_askpass.sh")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS=_askpass,
               CR_GITLAB_USER=_env("GITLAB_USER", "gitlab-ci-token"),
               CR_GITLAB_TOKEN=tok)
    r = _sp.run(["git", "clone", "--quiet", "--single-branch", "--branch", src,
                 "--depth", "2", f"https://gitlab.booming-inc.com/{pp}.git", dest],
                capture_output=True, text=True, env=env, timeout=600)
    if r.returncode != 0 or not os.path.isdir(os.path.join(dest, ".git")):
        return None, f"checkout clone failed: {r.stderr[:200]}"
    return dest, None


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
        try:
            fpath = _safe_checkout_path(checkout, file)
        except ValueError as _e:
            failed.append((file, f"不安全路径: {_e}"))
            continue
        if not file or not os.path.isfile(fpath):
            failed.append((file, "checkout 缺失该文件"))
            continue
        issue = (f.get("issue") or "")[:500]
        attained = False
        for rnd in range(max_rounds):
            content = open(fpath, encoding="utf-8", errors="ignore").read()
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
    try:
        p = _safe_checkout_path(checkout, file)
    except ValueError as _e:
        return None, "", f"file escapes checkout: {file}"
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


# local `claude -p` CLI 用的模型。值集中配置在 config.py (EDIT_MODEL)。


def _find_claude():
    """Locate the claude CLI. MUST use an absolute path: the persistent env
    (cr-env/env.sh) sets PATH=/usr/bin:/bin which strips /usr/local/bin, so a bare
    `claude` lookup can fail even when claude is installed. Candidates are ordered
    by how claude is actually laid out on this host / peer containers."""
    for c in ("/usr/local/bin/claude",
              "/root/.hermes/node/bin/claude",
              "/usr/local/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe"):
        if os.path.exists(c):
            return c
    return None


def _claude_p_call(prompt, model=EDIT_MODEL, timeout=180):
    """Run the local `claude -p` CLI (the exact tool that can reach the [1m] model on
    this machine). Falls back to None on failure (caller retries elsewhere). Uses an
    absolute path so a PATH override (cr-env/env.sh) cannot hide the binary."""
    import subprocess as _sp
    exe = _find_claude()
    if not exe:
        print("[claude-p] claude CLI not found (need absolute path scan)", file=sys.stderr)
        return None
    try:
        r = _sp.run([exe, "-p", prompt, "--model", model],
                    capture_output=True, text=True, timeout=timeout,
                    stdin=_sp.PIPE)
        if r.returncode != 0:
            print(f"[claude-p] rc={r.returncode} err={r.stderr[:200]}", file=sys.stderr)
            return None
        return r.stdout.strip()
    except Exception as e:
        print(f"[claude-p] err: {e}", file=sys.stderr)
        return None



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
    try:
        p = _safe_checkout_path(checkout, file)
    except ValueError as _e:
        return None, "", False, f"file escapes checkout: {file}"
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
    # NOTE: read/write must PRESERVE the file's original line endings. Game-engine
    # source is often CRLF; if we read as text and rewrite joined with "\n", every
    # line loses its "\r" -> the WHOLE file diffs as changed ("encode变了, 全部文件
    # 都有 diff" — seen on MR 7099 where a 177-line file showed all lines modified).
    raw = open(p, "rb").read()
    have_crlf = b"\r\n" in raw
    content = raw.decode("utf-8", errors="replace")
    (start, end), ctx = _locate_context(content, needle)
    if ctx is None:
        return None, "", False, f"locator '{needle}' not found in file"
    newline = "\r\n" if have_crlf else "\n"   # keep the file's own newline style
    prev_bad = ""
    for rnd in range(max_rounds):
        sysp = ("You fix a bug in a file. I show the RELEVANT window of the file. "
                "Output EXACTLY:\n@@START@@\n<verbatim corrected full window (everything from the window, with the fix applied)>\n@@END@@\n"
                "Preserve all other lines byte-for-byte (including whitespace); only change the buggy part. No other text.")
        prompt = (f"FILE: {file}\nRELEVANT WINDOW:\n```\n{ctx}\n```\n\nISSUE: {issue[:400]}\n\n"
                  f"Round {rnd+1}. Output @@START@@...@@END@@ (corrected window).")
        if prev_bad:
            prompt += f"\n[previous fix failed to apply, error: {prev_bad[:200]}. Correct the window so git apply succeeds.]"
        raw = _claude_p_call(prompt, model)
        if raw is None:
            prev_bad = "claude -p call failed"
            continue
        m = _re.search(r"@@START@@(.*?)@@END@@", raw or "", _re.S)
        if not m:
            prev_bad = "no @@START@@ block"
            continue
        new_window = m.group(1).strip("\n")
        # operate on lines WITHOUT the trailing \r so the window math is clean,
        # then re-attach the file's own newline style on write (preserves CRLF).
        content_lines = [l[:-1] if l.endswith("\r") else l for l in content.split("\n")]
        _orig_normal = "\n".join(content_lines)          # CRLF-stripped baseline
        had_trailing_nl = content_lines[-1:] == [""]     # original ended with a newline
        new_lines = [l[:-1] if l.endswith("\r") else l for l in new_window.split("\n")]
        content_lines[start:end] = new_lines
        # preserve trailing newline exactly as the original had it: a file that ended
        # with "\n" (or "\r\n") must keep it, or git diffs the final line too.
        if had_trailing_nl and content_lines[-1:] != [""]:
            content_lines.append("")
        elif not had_trailing_nl and content_lines[-1:] == [""]:
            content_lines.pop()
        new_content = newline.join(content_lines)
        if new_content == _orig_normal:
            prev_bad = "no change produced"
            continue
        # apply to working tree only (not committed): ensure the file is clean
        # first, then write the corrected window with the file's ORIGINAL newline
        # style (CRLF if it had CRLF) — otherwise the whole file diffs as changed.
        _sp.run(["git", "-C", checkout, "checkout", "--", file], capture_output=True, text=True, timeout=60)
        open(p, "w", encoding="utf-8", newline="").write(new_content)
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


def _agent_edit_all(topic, all_findings, api_key, base_url, workspace=None, model=EDIT_MODEL, repo="engine"):
    """Multi-file agent edit (closure #4): iterate over critical/high findings (up to 3)
    and fix each in the SAME working tree on the fix branch `{src}-fix-{task}`. Changes are
    cumulative (each later file is edited against the tree mutated by earlier fixes), so the
    returned diffs form one coherent change set. Returns (ok_diffs, failed, branch, checkout,
    err, checkout_sha). checkout_sha is the fix-branch base commit at edit time (R1): the
    confirm step checks the tree is still on this SHA before pushing, so a reused/reset
    checkout can't silently replay stale diffs onto a drifted base.

    方案(双MR): `repo` selects engine or game; each repo runs on ITS OWN checkout and
    produces its own fix branch {src}-fix-{task}(-game), so a repo with findings gets its
    own MR. `all_findings` must already be scoped to `repo` (engine findings only / game
    findings only)."""
    import subprocess as _sp
    ws = workspace or _DEFAULT_WORKSPACE
    # Per-topic-unique fix branch (_new_branch_name appends a message_id hash) so two
    # topics on the same Jira do NOT collide on the same fix branch (previously the
    # inline {src}-fix-{task} reused the LAST topic's branch -> push "fetch first" reject).
    # Add -game for the game repo so engine/game don't collide either.
    base = _new_branch_name(topic)
    branch = f"{base}-{repo}" if repo == "game" else base
    # Engine/game each have their own checkout carrying their own files. _ensure_shared_checkout
    # resolves the correct repo's URL+dir and resets it to origin/{src} (clean edit base).
    checkout, err = _ensure_shared_checkout(topic, repo, ws)
    if err:
        return [], [], branch, None, err, ""
    # R17: serialize edits on this repo's shared checkout across processes.
    repo_name = _repo_name_from_checkout(checkout)
    with _checkout_lock(repo_name):
        # _ensure_checkout already reset the reused tree to origin/{src} (fetch + reset +
        # clean), so the fix branch below is carved from the latest remote HEAD, not a
        # stale/foreign one. Work on the new fix branch (never touch review_branch).
        _sp.run(["git", "-C", checkout, "checkout", "-B", branch],
                capture_output=True, text=True, timeout=60)
        # Baseline SHA: the commit this edit set will be built on. Confirm re-checks this
        # before push (R1) so we never commit onto a drifted tree.
        _sha_r = _sp.run(["git", "-C", checkout, "rev-parse", "HEAD"],
                         capture_output=True, text=True, timeout=30)
        checkout_sha = (_sha_r.stdout or "").strip()
        crit = [f for f in (all_findings or [])
                if (f.get("severity") or "").lower() in ("critical", "high")]
        show = crit or (all_findings or [])[:3]
        ok_diffs, failed = [], []
        for f in show[:3]:
            file = f.get("file") or f.get("path") or ""
            issue = f.get("issue") or ""
            _file, diff, ok, e = _agent_edit_one(topic, file, issue, api_key, base_url, checkout, model)
            if ok and diff:
                ok_diffs.append({"file": _file, "diff": diff})
            else:
                failed.append((file, e or "edit failed"))
    return ok_diffs, failed, branch, checkout, None, checkout_sha


def _edit_already_pending(state_file, key):
    """防抖动: True if the topic already has an in-flight edit that would collide with a
    new 优化/改码 — either a pending agent_edit / agent_edit_confirm action, or a staged
    change set awaiting confirm. Prevents repeated button clicks from enqueueing two
    auto-edits (which produced duplicate MRs / lost push state earlier)."""
    try:
        t = pipeline_state.get_topic(state_file, key) or {}
        pend = t.get("pending") or {}
        if pend.get("action") in ("agent_edit", "agent_edit_confirm"):
            return True
        pp = t.get("pending_patch") or {}
        if pp.get("state") in ("staged_agent_edit", "pending"):
            return True
    except Exception:
        pass
    return False


def _cmd_auto_edit(key, topic, all_findings, render_id, workspace, state_file,
                   app_id, app_secret, actor=""):
    """指令 `改码/自动修复`: enqueue an agent_edit intent for the Jenkins executor,
    which runs claude -p to auto-fix critical/high findings in the checkout, stages
    the change set (pending_patch, state="staged_agent_edit"), and posts the diff
    preview. Async: the preview arrives on the next scan tick, not in this reply.
    Approval happens here (enqueue side); the executor does not re-gate owner."""
    ok, why = _approve(key, topic, actor, "auto_edit")
    if not ok:
        _proc_reply(key, topic, M("denied_why", why=why), render_id, state_file, app_id, app_secret,
                    intent="自动改码", prefix="🛑")
        return 0
    if not _find_claude():
        _proc_reply(key, topic,
                    "⚠️ 无法执行自动改码：未在本机找到 claude CLI（自动改码需 `claude` 可执行）。",
                    render_id, state_file, app_id, app_secret, intent="自动改码")
        return 0
    # 防抖动(优化/按钮连点): 已有待执行的改码或已 staged 待确认 -> 不再重复入队。
    if _edit_already_pending(state_file, key):
        _proc_reply(key, topic,
                    "⏳ 已有待执行的自动改码/待确认修改，正在处理中。请稍候或先 `@确认`/`@撤销`。",
                    render_id, state_file, app_id, app_secret, intent="自动改码")
        return 0
    pipeline_state.set_pending(state_file, key, "agent_edit", patch={"actor": actor})
    pipeline_state.append_approval(state_file, key, actor, "auto_edit", "", "ok", "@改码 enqueued")
    _proc_reply(key, topic,
                "⏳ 已记录自动改码，Jenkins 将稍后调用 AI 修改代码并展示 diff 待你确认。\n"
                "（结果可能延迟到下一轮扫描；完成后回复 `@确认 提交并建mr` 推送并建 MR，`@撤销` 取消。）",
                render_id, state_file, app_id, app_secret, intent="自动改码（改码→staged→待确认）")
    return 0


def _cmd_optimize(key, topic, all_findings, render_id, workspace, state_file,
                  app_id, app_secret, actor=""):
    """指令 `优化`: 全自动修复 —— 改码 staged 后自动 push + 创建/更新 MR，无需手动确认。
    仅 owner 触发；用 close 托底清理。

    与 `改码`(手动) 的区别: 设 topic.auto_confirm=True, consume 改码完成后自动入队
    agent_edit_confirm(commit+push+建/更新MR)。"""
    ok, why = _approve(key, topic, actor, "auto_edit")
    if not ok:
        _proc_reply(key, topic, M("denied_why", why=why), render_id, state_file, app_id, app_secret,
                    intent="优化（全自动修复并建 MR）", prefix="🛑")
        return 0
    if not _find_claude():
        _proc_reply(key, topic,
                    "⚠️ 无法执行优化：未在本机找到 claude CLI（自动修复需 `claude` 可执行）。",
                    render_id, state_file, app_id, app_secret, intent="优化")
        return 0
    # 防抖动(优化/按钮连点): 已有待执行改码或待确认 -> 不重复入队, 避免重复改码/建MR。
    if _edit_already_pending(state_file, key):
        _proc_reply(key, topic,
                    "⏳ 已有待执行的自动改码/待确认修改，正在处理中。请稍候或先 `@确认`/`@撤销`。",
                    render_id, state_file, app_id, app_secret, intent="优化")
        return 0
    pipeline_state.set_topic_fields(state_file, key, auto_confirm=True)
    pipeline_state.set_pending(state_file, key, "agent_edit", patch={"actor": actor})
    pipeline_state.append_approval(state_file, key, actor, "auto_edit", "", "ok", "@优化 enqueued(auto)")
    _proc_reply(key, topic,
                (MSG.get("optimize_started") or "⏳ 已开始优化：AI 将自动修复关键问题，改码完成后自动推送修复分支并创建/更新 MR。\n") +
                (MSG.get("optimize_note") or "（可再次 `优化` 更新已有 MR，无需单独重申。）"),
                render_id, state_file, app_id, app_secret, intent="优化：改码→自动推送→创建/更新MR")
    return 0


def _commit_push_create_mr_for_repo(key, topic, repo_set, state_file, workspace):
    """Confirm ONE repo's staged fix set: commit on its fix branch in its per-repo
    checkout (preserving the working-tree edits _agent_edit_all wrote), push, auto-create
    the fix MR on THAT repo's project. Returns (mriid, murl, mrnote, err) — on err
    (mriid None) the MR was not created and a reply must be posted by the caller.

    `repo_set` = {repo, branch, files, checkout_sha}. Wired for the per-repo (双MR)
    design: engine and game each get their own fix branch + MR on their own project."""
    import subprocess as _sp
    repo = repo_set.get("repo") or "engine"
    branch = repo_set.get("branch") or ""
    files = repo_set.get("files") or []
    expected_sha = repo_set.get("checkout_sha") or ""
    if not branch or not files:
        return None, "", "", "no staged branch/files"
    # 根因修复: confirm 复用 edit 时的工作树(不改写), 否则 reset+clean 清掉改动 ->
    # 重放失败 -> 空提交 -> push 空分支 -> MR"无改动"创建失败。
    checkout, err = _ensure_checkout_preserve(topic, workspace, repo=repo)
    if err:
        return None, "", "", f"checkout: {err}"
    _git_env = _auth_git_env({"LC_ALL": "C"})
    _sp.run(["git", "-C", checkout, "config", "user.email", "codereview-agent@booming-inc.com"],
            capture_output=True, text=True, timeout=30, env=_git_env)
    _sp.run(["git", "-C", checkout, "config", "user.name", "codereview-agent"],
            capture_output=True, text=True, timeout=30, env=_git_env)
    repo_name = _repo_name_from_checkout(checkout)
    with _checkout_lock(repo_name):
        _sp.run(["git", "-C", checkout, "checkout", "-B", branch],
                capture_output=True, text=True, timeout=60, env=_git_env)
        if expected_sha:
            _sha_r = _sp.run(["git", "-C", checkout, "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=30, env=_git_env)
            cur = (_sha_r.stdout or "").strip()
            if not cur or cur != expected_sha:
                return None, "", "", "R1: checkout drifted from baseline"
        else:
            return None, "", "", "R1: missing baseline"
        _sp.run(["git", "-C", checkout, "add", "-A"], capture_output=True, text=True, timeout=60, env=_git_env)
        _commit = _sp.run(["git", "-C", checkout, "commit", "-m", f"[codereview-agent] auto-fix {key} ({len(files)} files)"],
                          capture_output=True, text=True, timeout=60, env=_git_env)
        _commit_out = (_commit.stdout or "") + (_commit.stderr or "")
        if _commit.returncode != 0 and "nothing to commit" in _commit_out:
            import tempfile
            apply_errs = []
            for d in files:
                diff = d.get("diff") or ""
                if not diff:
                    apply_errs.append("(empty diff)")
                    continue
                with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as _f:
                    _f.write(diff)
                    _pf = _f.name
                try:
                    _ap = _sp.run(["git", "-C", checkout, "apply", "--3way", _pf],
                                  capture_output=True, text=True, timeout=60, env=_git_env)
                    if _ap.returncode != 0:
                        apply_errs.append((d.get("file") or "?") + ": " +
                                          ((_ap.stderr or _ap.stdout) or "apply failed").strip()[:120])
                finally:
                    try:
                        os.unlink(_pf)
                    except OSError:
                        pass
            if apply_errs:
                return None, "", "\n".join("· " + e for e in apply_errs[:5]), "replay-apply-failed"
            _sp.run(["git", "-C", checkout, "add", "-A"], capture_output=True, text=True, timeout=60, env=_git_env)
            _commit = _sp.run(["git", "-C", checkout, "commit", "-m", f"[codereview-agent] auto-fix {key} ({len(files)} files)"],
                              capture_output=True, text=True, timeout=60, env=_git_env)
        if _commit.returncode != 0 and "nothing to commit" not in (_commit_out):
            return None, "", (_commit.stderr or _commit.stdout)[:200], "commit-failed"
        # safety net: HEAD must advance past the baseline (a real fix commit exists)
        if expected_sha:
            _after = _sp.run(["git", "-C", checkout, "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=30, env=_git_env)
            if ((_after.stdout or "").strip() or "") == (expected_sha or "").strip():
                return None, "", "", "no-fix-commit"
        push = _sp.run(["git", "-C", checkout, "push", "origin", f"HEAD:{branch}"],
                       capture_output=True, text=True, timeout=180, env=_git_env)
        if push.returncode != 0:
            return None, "", (push.stderr or push.stdout)[:200], "push-failed"
    # Auto-create/detect the fix-branch MR on the repo's project.
    import jira_parser as _jpx
    _mriid, murl, _nb, mrnote = _create_or_get_mr(topic, [], create_if_missing=True, branch=branch, repo=repo)
    return _mriid, murl, mrnote, (None if _mriid else "mr-create-failed")


def _cmd_confirm_agent_edit(key, topic, all_findings, state_file, workspace, app_id, app_secret, actor=""):
    """确认自动改码(双MR): iterate the per-repo staged change sets, each on its own
    checkout/fix-branch/project, commit+push+create its MR, then post one merged reply."""
    render = topic.get("render_msg_id") or ""
    pending = (topic.get("pending_patch") or {})
    if pending.get("state") != "staged_agent_edit":
        _proc_reply(key, topic,
                    "⛔ 当前没有待确认的自动改码。请先回复 `改码` 生成修改。",
                    render, state_file, app_id, app_secret, intent="确认并推送修复分支", prefix="🛑")
        return 0
    repos = pending.get("repos") or []
    if not repos:
        files = pending.get("files") or []
        branch = pending.get("branch") or ""
        if not files or not branch:
            pipeline_state.set_pending_patch(state_file, key, None)
            _proc_reply(key, topic, M("confirm_no_staged"),
                        render, state_file, app_id, app_secret, intent="确认并推送修复分支", prefix="🛑")
            return 0
        repos = [{"repo": "engine", "branch": branch, "files": files,
                  "checkout_sha": pending.get("checkout_sha") or ""}]
    for rs in repos:
        if (rs.get("branch") or "").split("/")[-1] in PROTECTED_BRANCHES or (rs.get("branch") or "") in PROTECTED_BRANCHES:
            _proc_reply(key, topic, M("confirm_protected_branch", branch=rs.get("branch")),
                        render, state_file, app_id, app_secret, intent="确认并推送修复分支", prefix="🛑")
            return 0
    ok, why = _approve(key, topic, actor, "push_fix_branch",
                       branch=",".join((rs.get("branch") or "") for rs in repos))
    if not ok:
        _proc_reply(key, topic, M("denied_why", why=why),
                    render, state_file, app_id, app_secret, intent="确认并推送修复分支", prefix="🛑")
        return 0
    results = []
    hard_fail = None
    for rs in repos:
        mriid, murl, mrnote, err = _commit_push_create_mr_for_repo(key, topic, rs, state_file, workspace)
        if err:
            hard_fail = (rs.get("repo"), err, mrnote or "")
            results.append({"repo": rs.get("repo"), "branch": rs.get("branch"),
                            "ok": False, "murl": "", "mrnote": mrnote or "", "files": rs.get("files") or []})
            continue
        results.append({"repo": rs.get("repo"), "branch": rs.get("branch"), "ok": True,
                        "murl": murl, "mrnote": mrnote or "", "files": rs.get("files") or []})
        if mriid:
            pipeline_state.record_fix_mr(state_file, key, mriid)
        pipeline_state.append_approval(state_file, key, actor, "push_fix_branch",
                                       rs.get("branch"), "ok", "pushed + MR " + (murl or ""))
        for f in (rs.get("files") or []):
            pipeline_state.record_applied_patch(state_file, key, {
                "file": f.get("file", ""), "repo": rs.get("repo") or "engine",
                "branch": rs.get("branch", ""), "applied_at": "now", "mode": "agent_edit",
            })
    pipeline_state.set_pending_patch(state_file, key, None)
    if hard_fail:
        note = "🛑 确认阶段失败(" + str(hard_fail[0]) + "): " + str(hard_fail[1])
        if hard_fail[2]:
            note += "\n" + str(hard_fail[2])
        note += "\n\n请重新回复 `优化` 生成新的修复。"
        _proc_reply(key, topic, note, render, state_file, app_id, app_secret,
                    intent="确认并推送修复分支", prefix="🛑")
        return 0
    out_lines = []
    outcomes = pending.get("repo_outcomes") or {}
    for r in results:
        if r["ok"] and r["murl"]:
            out_lines.append("✅ **自动改码已推送** `" + r["branch"] + "`（" + r["repo"] + "，" +
                            str(len(r["files"])) + " 个文件）。\n" +
                            "- 推送分支：`" + r["branch"] + "`\n" +
                            "- 本次修复 MR：" + r["murl"])
        else:
            out_lines.append("⚠️ " + r["repo"] + " 修复 MR 未创建（" + (r["mrnote"] or "未知") + "）")
    # 未提交修复的仓库也要明确提示（"没提"不能静默）——告诉用户该仓库无发现/无需修复/处理失败。
    done = {r["repo"] for r in results}
    for repo, oc in sorted(outcomes.items()):
        if repo in done:
            continue
        if oc == "no_findings":
            out_lines.append("💤 " + repo + "：本次审查无发现，无需修复")
        elif oc == "no_fix":
            out_lines.append("💤 " + repo + "：有发现但未能自动生成修复（可手动处理）")
        elif oc == "error":
            out_lines.append("⚠️ " + repo + "：修复处理失败")
    out_lines.append("- 原评审 MR：" + (topic.get("mr_url") or "") +
                     "\n\n> 修复 MR 由机器人创建，请到 GitLab 自行 review 后合并；`4` 可关闭话题（会连带关闭本轮 fix 分支的 OPEN MR）。")
    _finalize(key, "\n".join(out_lines), render, [], state_file, app_id, app_secret)
    return 0
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
            try:
                p = _safe_checkout_path(checkout, file)
            except ValueError:
                p = ""
            if p and os.path.isfile(p):
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


# ── 可引用 finding(B) ────────────────────────────────────────────────
# findings 本身无 id。为让用户能按 "#3 / 文件名 / all / critical" 定位单条, 用
# 稳定的位置序号 + 一个指纹(基于 file+issue)作 id。engine+game 拼接顺序固定,
# 同一批 result 下序号稳定(重审会重新生成则序号随之变化, 可接受)。

def _finding_id(finding, idx):
    """1-based index-based id; fallback fingerprint if no index given."""
    import hashlib
    key = (str(finding.get("file") or "") + "|" + (str(finding.get("issue") or "")[:40])).\
        encode("utf-8")
    return "#" + str(idx), hashlib.sha1(key).hexdigest()[:6]


def _findings_indexed(all_findings):
    """Return a list of (idx, finding) with a stable 1-based positional index over
    the ordered (engine then game) findings list."""
    return list(enumerate(all_findings or [], start=1))


def resolve_findings(all_findings, refs):
    """Resolve a user's finding reference(s) to concrete findings.
    refs: e.g. "#3", "#1,5", "scene_manager.cpp", "all", "critical".
    Returns (resolved_findings, unresolved_hint). Never raises.
    `all` = all critical/high (keeps old _select_findings(all) behavior for the
    patch-preview path); explicitly numbered refs return exactly those."""
    if not all_findings:
        return [], "（无 findings）"
    import re as _re
    refs = (refs or "all").strip().lower()
    if refs in ("all", "critical"):
        return [f for f in all_findings
                if (f.get('severity') or '').lower() in ("critical", "high")], ""
    # numeric refs like "3" or "#3"
    nums = _re.findall(r'#?(\d+)', refs)
    if nums:
        idx = {int(n) for n in nums}
        out = [f for i, f in _findings_indexed(all_findings) if i in idx]
        missing = [str(n) for n in sorted(idx) if n < 1 or n > len(all_findings)]
        hint = f"（无第 {', '.join(missing)} 条）" if missing else ""
        return out, hint
    # file / substring match
    sub = refs.lstrip("#").strip()
    if not sub:
        return [], "（引用为空，可用 `#序号` / 文件名 / all）"
    return [f for f in all_findings if sub in (f.get('file') or '').lower()], \
        "" if any(sub in (f.get('file') or '').lower() for f in all_findings) else f"（找不到包含 {sub} 的发现）"


# ── 交互增强(C3): review 结论覆盖 + 重渲染 ─────────────────────────────
# 底版 findings 不可变; 用户"更新结论"后, 在 topic.review_overrides 里记录修订,
# 渲染时叠加成最终方案C 卡并原地 PATCH render_msg_id 那张卡。

def apply_review_overrides(all_findings, overrides):
    """Merge review_overrides (visible amendments) onto the immutable findings:
      - by "#N": amend/reclassify/resolve that finding in place (returns a NEW list,
        leaving the base dicts untouched so result_*.json stays intact).
    Returns (merged_findings, applied_notes) where applied_notes describe what changed."""
    base = [dict(f) for f in (all_findings or [])]
    applied = []
    for ov in (overrides or []):
        action = (ov.get("action") or "amend").strip().lower()
        ref = str(ov.get("ref") or "").strip()
        idx = None
        if ref.startswith("#"):
            try:
                idx = int(ref[1:])
            except ValueError:
                idx = None
        if idx is not None and 1 <= idx <= len(base):
            f = base[idx - 1]
            if action == "resolve":
                f["_resolved"] = True
                applied.append(f"#{idx} 已关闭(resolve)")
            elif action == "reclassify":
                if ov.get("severity"):
                    f["severity"] = ov["severity"]
                applied.append(f"#{idx} 重新定级 -> {f.get('severity')}")
            else:  # amend / add text
                for k in ("issue", "suggestion", "severity"):
                    if ov.get(k):
                        f[k] = ov[k]
                applied.append(f"#{idx} 已修订")
        else:
            # add a brand-new finding (action=='add', no ref)
            if action == "add":
                base.append({"file": ov.get("file") or "?", "severity": ov.get("severity") or "suggestion",
                             "issue": ov.get("issue") or "(新增)", "suggestion": ov.get("suggestion") or ""})
                applied.append(f"新增 {ov.get('file') or '?'}")
            else:
                applied.append(f"未定位 {ref or '(空引用)'}")
    return base, applied


def render_findings_with_overrides(topic, all_findings):
    """Produce the final 方案C card text = findings + review_overrides, and update
    topic.review_summary so later in-place re-renders (e.g. cmd_ci) don't clobber it."""
    from code_reviewer import _build_markdown_from_findings
    merged, applied = apply_review_overrides(all_findings,
                                             (topic or {}).get("review_overrides") or [])
    text = _build_markdown_from_findings(merged)
    if applied:
        text += "\n\n📝 结论修订：" + "；".join(applied)
    return text


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
    rc, out, err = _run_py("feishu_notifier.py", [
        "reply-message", "--app-id", app_id, "--app-secret", app_secret,
        "--chat-id", chat_id, "--message-id", key, "--message-base64", _b64_str(answer)])
    if rc != 0:
        print(f"[finalize] reply-message failed rc={rc} key={key} err={err[:200]} out={out[:120]}",
              file=sys.stderr)


def _proc_reply(key, topic, text, render_id, state_file, app_id, app_secret,
                intent="", prefix="🤖"):
    """方案C 过程通道: 每次交互 NEW 一张卡（reply-message），并声明"我将做什么"，
    不覆盖之前的卡片（结果卡 PATCH 保留给 review summary / CI）。

    intent 为动作说明（如 '确认并推送修复分支、创建修复 MR'）；会在文案前渲染成
    '{prefix} 准备执行：{intent}'。每次都新发一条（走 reply-message + 记 chat），
    让用户看到"准备 → 执行中 → 结果"的完整过程，而非卡片被反复覆盖。
    """
    if intent:
        text = f"{prefix} **准备执行：{intent}**\n\n{text}"
    _finalize(key, text, render_id, [], state_file, app_id, app_secret)


# Sentinel: the reply was not a closure intent — caller should continue with the
# normal command-word path.
_CLOSURE_NO_MATCH = object()


def _try_handle_closure(key, topic, reply_text, actor, workspace, state_file):
    """rage-style closure handling for a reply in an active review state.

    When the topic's `review_state` is one of the actionable review states, classify
    the reply via closure.py (P2) and handle it mechanically — persist dev_triage /
    approve/close / handoff, transition review_state, post a template response, and
    (for next-round) signal a re-review. Returns 0 when the reply WAS consumed as a
    review intent, or _CLOSURE_NO_MATCH when not (caller falls through).

    This is the decision layer only; it does NOT spawn the review agent — a round-N
    review is triggered by the caller (orchestrate run) via REVIEW_AGENT=1 the same
    way round 1 is.
    """
    import sys as _sys, os as _os
    rdir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                         "..", "skills", "rage-review")
    _sys.path.insert(0, rdir)
    try:
        import closure as _cl
        app_id = _env("FEISHU_APP_ID")
        app_secret = _env("FEISHU_APP_SECRET")
        render_id = topic.get("render_msg_id") or key

        review_state = topic.get("review_state") or ""
        if review_state not in _cl.REVIEW_STATES:
            return _CLOSURE_NO_MATCH

        # Resolve per-project approvers (config) → fallback policy.yaml admins.
        import config as _cfg
        project_id = topic.get("project") or ""
        projs = _cfg.load_config().get("projects") or {}
        proj_cfg = projs.get(project_id) or {}
        policy_admins = []
        try:
            import policy  # policy.yaml loader if present
            policy_admins = _policy_admins()
        except Exception:
            policy_admins = []
        approvers = _cl.approver_ids_for(proj_cfg, policy_admins)

        triage = topic.get("review_triage") or "simple"
        dev_triage = topic.get("dev_triage") or {}
        issue_count = int(topic.get("issue_count") or len(topic.get("review_issues") or []))
        developer = (topic.get("creator_open_id") or
                     topic.get("sender_id") or "")

        res = _cl.reconcil(reply_text, actor, review_state, approvers, developer,
                           issue_count, triage=triage, dev_triage=dev_triage)
        if not res or res.get("intent") in (None, "dev_question", "manual_refresh"):
            return _CLOSURE_NO_MATCH  # question/sync → normal agent path

        # Persist closure result + transition review_state.
        updates = {}
        if res.get("persist", {}).get("dev_triage") is not None:
            updates["dev_triage"] = res["persist"]["dev_triage"]
        if res.get("persist", {}).get("flagged_issues"):
            updates["flagged_issues"] = res["persist"]["flagged_issues"]
        if res.get("next_state"):
            updates["review_state"] = res["next_state"]
        if res.get("persist", {}).get("approved"):
            updates["review_approved"] = True
        if res.get("persist", {}).get("closed"):
            updates["review_state"] = "CLOSED"
        if updates:
            pipeline_state.set_topic_fields(state_file, key, **updates)

        # Post the appropriate template response.
        post = res.get("post") or {}
        tpl = post.get("template") or "revision_request"
        msg = _closure_human_text(post, res, issue_count, topic)
        _finalize(key, msg, render_id, [], state_file, app_id, app_secret)

        # Next-round review trigger for dev_reply (dev pushed fixes, re-review).
        if res.get("intent") == "dev_reply" and res.get("persist", {}).get("re_review"):
            # Actually schedule the round-N review (P3): the Jenkins executor drains
            # this re_review pending and re-runs orchestrate run, which now uses
            # topic.last_review_commit (incremental) + carried (skip settled) from P3.
            try:
                import pipeline_state as _psc
                _psc.set_pending(state_file, key, "re_review")
                _log('CLOSURE', 'REREVIEW', key, topic.get("jira_key", ""), '', '',
                     'dev pushed fixes — queued round-N incremental review')
            except Exception as _err:
                _log('CLOSURE', 'REREVIEW', key, topic.get("jira_key", ""), '', '',
                     f'failed to queue re_review: {_err}')
        return 0
    except Exception as e:
        print(f"[closure] err: {e}", file=_sys.stderr)
        return _CLOSURE_NO_MATCH


def _maybe_create_review_doc(key, issue_key, project, topic, findings,
                             review_branch, app_id, app_secret):
    """Complex review → create the full-review Feishu doc (PlanA) and return
    (ok, doc_token, url, err). Falls back to (False, ..., reason) when doc scope
    unavailable or creation fails (caller then renders the card without doc link;
    PlanB long-post is the fallback at the render layer)."""
    try:
        import os as _os, sys as _sys2
        rdir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                             "..", "skills", "rage-review")
        _sys2.path.insert(0, rdir)
        import build_review_doc_http as _brd
        # build issues in doc schema + files overview
        issues = []
        for f in findings or []:
            issues.append({
                "severity": f.get("severity") or "建议",
                "repo": f.get("repo") or "engine",
                "file": (f.get("file") or "").strip(),
                "line_range": (f.get("line_range") or "").strip(),
                "function": (f.get("function") or "").strip(),
                "description": (f.get("issue") or f.get("description") or "").strip(),
                "suggestion": (f.get("suggestion") or "").strip(),
            })
        files = [{"repo": f.get("repo") or "engine", "path": (f.get("file") or "").strip(),
                  "insertions": 0, "deletions": 0, "description": ""} for f in findings or []]
        md = _brd.build_doc_markdown(issue_key, "复杂审查完整报告。", issues, files)
        # R7-C3 (加固): grant_view 从 config projects.<project>.approver_open_ids 读
        # （rage 要求授权审查人+开发者，topic 上不一定有 approver 字段）。回退 topic 字段。
        import common as _common
        _grant = []
        try:
            _proj_cfg = (_common.load_config().get("projects") or {}).get(project) or {}
            _grant += [a for a in (_proj_cfg.get("approver_open_ids") or []) if a]
        except Exception:
            pass
        _grant += [a for a in (topic.get("approver_open_ids") or []) if a]
        _grant += [a for a in ([topic.get("creator_open_id") or ""]) if a]
        # de-dup preserving order
        grant, _seen = [], set()
        for g in _grant:
            if g and g not in _seen:
                _seen.add(g)
                grant.append(g)
        title = f"代码审查 {issue_key}"
        ok, tok, url, err = _brd.create_lark_doc_http(app_id, app_secret, title, md,
                                                       grant_view=grant)
        return ok, tok, url, err
    except Exception as e:
        import traceback as _tb
        _tb.print_exc()
        return False, "", "", f"_maybe_create_review_doc: {e}"


def _set_review_closure_fields(state_file, key, eng_res, gam_res):
    """Persist the rage-style closure state on a topic after a review run.

    Merges engine+game findings into `review_issues[]` (severity-sorted, #N),
    sets `issue_count`, `review_triage`, and `review_state` so `_try_handle_closure`
    can drive the developer/approver loop on round-2+ replies. Marked the topic's
    opener as the developer (`creator_open_id`) when not already set.

    review_state after a round:
      - issues found → DEV_TRIAGE (dev triages first)
      - zero issues  → TRIAGE_DECISION (simple) / AWAITING_APPROVAL (complex)
    """
    issues = []
    for repo, res in (("engine", eng_res or {}), ("game", gam_res or {})):
        rv = (res or {}).get("review") or {}
        for f in rv.get("findings") or []:
            issues.append({
                "repo": repo,
                "file": (f.get("file") or "").strip(),
                "severity": (f.get("severity") or "").strip(),
                "line_range": (f.get("line_range") or "").strip(),
                "function": (f.get("function") or "").strip(),
                "issue": (f.get("issue") or "").strip(),
                "suggestion": (f.get("suggestion") or "").strip(),
            })
    # severity-sort then assign indices (rage: 严重>中>轻>建议, #N after sorting)
    _order = {"严重": 0, "中": 1, "轻": 2, "建议": 3}
    issues.sort(key=lambda x: (_order.get(x["severity"], 3), x["file"]))
    for i, it in enumerate(issues, start=1):
        it["index"] = i

    # triage: complex if >5 files/100 lines across repos (rage rule), else simple.
    # R7-C1 (加固): changed_files may be absent on the agent path, so ALSO count
    # unique files from the findings (agent always yields findings). Max of the
    # two, so a 30-file MR with findings never mis-sorts to simple → skips doc.
    _cf_files = set()
    for res in (eng_res or {}, gam_res or {}):
        for _cf in (res.get("changed_files") or []):
            p = _cf.split("\t", 1)[-1] if "\t" in _cf else _cf
            if p:
                _cf_files.add(p)
    _finding_files = {i["file"] for i in issues if i.get("file")}
    total_files = len(_cf_files | _finding_files)
    # crude line heuristic from stats strings
    def _lines(res):
        try:
            s = (res or {}).get("stats") or ""
            a = int(s.split("insertions")[0].split("+")[-1].strip())
            return a
        except Exception:
            return 0
    total_lines = _lines(eng_res) + _lines(gam_res)
    triage = "complex" if (total_files > 5 or total_lines > 100) else "simple"

    topic = pipeline_state.get_topic(state_file, key) or {}
    if issues:
        next_state = "DEV_TRIAGE"
    else:
        next_state = "AWAITING_APPROVAL" if triage == "complex" else "TRIAGE_DECISION"

    # Record the reviewed head SHA so the next (round-N) run can diff incrementally
    # (incr_base). Best-effort: resolve via the engine checkout's repo_dir already on
    # disk; on failure leave empty so the next run degrades to the full diff.
    review_sha = ""
    try:
        eng_repo_dir = (eng_res or {}).get("repo_dir") or (gam_res or {}).get("repo_dir") or ""
        review_branch = topic.get("review_branch") or "HEAD"
        if eng_repo_dir and review_branch:
            import subprocess as _sp
            p = _sp.run(["git", "-C", eng_repo_dir, "rev-parse", review_branch],
                        capture_output=True, text=True, timeout=20)
            if p.returncode == 0:
                review_sha = p.stdout.strip()[:40]
    except Exception:
        review_sha = ""

    pipeline_state.set_topic_fields(state_file, key,
                                    review_issues=issues,
                                    issue_count=len(issues),
                                    review_triage=triage,
                                    review_state=next_state,
                                    creator_open_id=(topic.get("sender_id") or ""),
                                    review_approved=False,
                                    last_review_commit=review_sha)


def _policy_admins():
    """policy.yaml admins fallback (best-effort)."""
    try:
        import common as _c
        raw = _c.load_config() or {}
        return _c.load_config().get("policy", {}).get("agent", {}).get("admins", [])
    except Exception:
        return []


def _closure_human_text(post, res, issue_count, topic):
    """Rage-standard human copy for a closure reply (Chinese)."""
    tpl = post.get("template")
    if tpl == "dev_triage" or tpl == "revision_request":
        acc = post.get("vars", {}).get("accepted", [])
        rej = post.get("vars", {}).get("rejected", [])
        if rej:
            return (f"✅ 已确认修复：{acc or '无'}；有异议（将交审查人）：{rej}。\n"
                    f"请修改后在话题回复 `ok` 触发下一轮审查；异议理由可选 @bot 说明。")
        return (f"✅ 已确认修复：{acc or len(list(range(1, issue_count + 1)))} 项。\n"
                f"请修改后在话题回复 `ok` 触发下一轮审查。")
    if tpl == "approval":
        return "✅ 已批准。若已启用自动合入将由合并队列处理；否则请手动 merge 后话题自动进入已合并。"
    if tpl == "closed":
        return "🔒 已关闭本话题与对应 MR。"
    if tpl == "escalated":
        return "↗️ 已升级为完整审查。"
    if tpl == "handoff_summary":
        dt = post.get("vars", {}).get("dev_triage") or {}
        rej = dt.get("rejected_indices") or []
        notes = ""
        if rej:
            reasons = dt.get("reasons") or {}
            notes = f"\n\n开发者异议：{rej}" + (f"（{reasons}）" if reasons else "")
        return f"🤝 开发者已完成，已交审查人裁决。{notes}"
    if tpl == "re_review":
        return "⏳ 已收到修复，将进行下一轮增量审查（仅复核未解决项）。"
    if tpl == "manual_refresh":
        return "🔄 正在同步人工审查评论并核对修复状态…"
    if tpl == "dev_question":
        return "🤖 已收到提问，正在查证…"
    return "ok"



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
            "get_status", "get_findings", "generate_patch_preview", "answer",
            "deep_dive", "challenge"],
        "guarded": {
            "re_review": {"approver": "topic_owner"},
            "apply_patch": {"approver": "topic_owner"},
            "auto_edit": {"approver": "topic_owner"},
            "apply_local": {"approver": "topic_owner"},
            "push_remote": {"approver": "topic_owner", "branch": "{topic.review_branch}"},
            "push_fix_branch": {"approver": "topic_owner"},
            "rollback": {"approver": "topic_owner"},
            "update_conclusion": {"approver": "topic_owner"},
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


def _repo_url_from_mr(topic):
    """For legacy topics with no persisted repo_url, derive the git https URL from
    the recorded MR url (mirrors _ensure_checkout's project-path parsing, but with a
    clean https URL — the executor authenticates via git_askpass, not token-in-URL)."""
    mr_url = topic.get("mr_url") or ""
    if "merge_requests" not in mr_url:
        return ""
    import jira_parser as _jp
    pp, _ = _jp.parse_gitlab_mr_url(mr_url)
    return f"https://gitlab.booming-inc.com/{pp}.git" if pp else ""


def _resolve_repo_checkout(workspace, topic, repo):
    """
    Locate THIS topic's repo checkout dir under workspace. Returns (checkout_dir, real_repo_name)
    or (None, name when no url). Mirrors code_reviewer.prepare_repo naming (url basename minus .git)
    tried for both engine and game repos; repo selects which one.

    方案B: like autofix, the apply/push/rollback executor now uses THIS topic's isolated
    per-topic dir {workspace}/{repo}-review/{slug} instead of a shared flat {workspace}/{repo},
    so a different topic's applied patch / branch state can never leak into these ops
    (C#2: closes the last shared-checkout serial-baseline gap)."""
    repos = {}
    for r in ("engine", "game"):
        url = (topic.get(f"{r}_repo")
               or topic.get("repos", {}).get(r, {}).get("repo_url")
               or (_repo_url_from_mr(topic) if r == repo else "") or "")
        if url:
            repos[r] = url
    url = repos.get(repo) or (list(repos.values())[0] if repos else "")
    if not url:
        return None, ""
    # Convert scp-style (git@host:path/repo.git) to https so the basename is the
    # real repo name — same rule code_reviewer.prepare_repo uses after ssh_to_https.
    import re as _re
    if url.startswith("git@"):
        m = _re.match(r'git@([^:]+):(.+)', url)
        if m:
            url = f"https://{m.group(1)}/{m.group(2)}"
    name = url.rstrip("/").split("/")[-1].replace(".git", "")
    return _checkout_dir_for(workspace, name, topic), name


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
        _proc_reply(key, topic, M("denied_why", why=why), render, state_file, app_id, app_secret)
        return 0
    pending = topic.get("pending_patch") or {}
    if not pending:
        _proc_reply(key, topic, M("apply_nothing_pending"), render, state_file, app_id, app_secret)
        return 0
    # Record intent; the Jenkins executor applies it to the shared checkout.
    pipeline_state.append_approval(state_file, key, actor, "apply_local", pending.get("file", ""), "ok", "@ok enqueued")
    pipeline_state.set_pending(state_file, key, "apply", patch={
        "file": pending.get("file", ""), "repo": pending.get("repo", "engine"), "diff": pending.get("diff", ""),
    })
    pipeline_state.set_pending_patch(state_file, key, None)  # now owned by the executor
    _proc_reply(key, topic,
                "⏳ 已记录应用请求，Jenkins 将把补丁应用到共享 checkout。\n"
                "完成后会更新本帖。如需推送远程，届时再回复 `@confirm push`。",
                render, state_file, app_id, app_secret, intent="应用补丁到共享 checkout")
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
        _proc_reply(key, topic, M("denied_why", why=why), render, state_file, app_id, app_secret)
        return 0
    pipeline_state.append_approval(state_file, key, actor, "push_remote", branch, "ok", "@confirm push enqueued")
    pipeline_state.set_pending(state_file, key, "push")
    _proc_reply(key, topic, M("push_enqueued", branch=branch), render, state_file, app_id, app_secret)
    return 0


def _rollback(key, topic, workspace, state_file, app_id, app_secret, actor=""):
    """@撤销 — revert the most recently applied local patch. Owner-only.
    arch-D: enqueues a pending 'rollback' for the Jenkins executor."""
    render = topic.get("render_msg_id") or ""
    ok, why = _approve(key, topic, actor, "rollback")
    if not ok:
        pipeline_state.append_approval(state_file, key, actor, "rollback", "", "denied", why)
        _proc_reply(key, topic, M("denied_why", why=why), render, state_file, app_id, app_secret)
        return 0
    pipeline_state.set_pending(state_file, key, "rollback")
    pipeline_state.append_approval(state_file, key, actor, "rollback", "", "ok", "@撤销 enqueued")
    _proc_reply(key, topic, M("rollback_enqueued"), render, state_file, app_id, app_secret)
    return 0


# ── Async executor (arch-D): Jenkins consumes topic.pending and executes it ────
#
# The interaction layer only enqueues intents (re_review/apply/push/rollback); the
# Jenkins scan/scheduled job calls consume_pending() for each topic with a pending
# action. It is the ONLY place that touches the git checkout / runs the review,
# because it has the full GitLab/Jira creds and a shared, consistent checkout.
# Returns a human-readable result per action.

def _ensure_shared_checkout(topic, repo, workspace):
    """Locate (or lazily create) THIS topic's isolated repo checkout dir under
    workspace. Returns (dirname, None) or (None, error). Uses `workspace` as the base;
    both interaction & executor must point at the SAME workspace (shared bind) so
    state/checkout stay consistent.

    方案B(C#2): like autofix, the apply/push/rollback executor operates on the topic's
    per-topic dir {repo}-review/{slug} (not a shared flat dir), so one topic's applied
    patch / branch switch can never leak into another topic's apply/push/rollback."""
    import subprocess as _sp
    import re as _re
    url = ""
    for r in ("engine", "game"):
        if repo == r:
            url = (topic.get(f"{r}_repo")
                   or topic.get("repos", {}).get(r, {}).get("repo_url")
                   or _repo_url_from_mr(topic) or "")
            break
    if not url:
        return None, f"no repo_url recorded for '{repo}'"
    checkout, name = _resolve_repo_checkout(workspace, topic, repo)
    if not checkout:
        return None, f"no checkout dir for '{repo}'"
    # If present, reuse it but force-align to the latest remote HEAD of the topic's
    # review branch, and drop any working-tree residue (stale fix patches, foreign
    # branch switches, leftover build artifacts). Same "never trust a reused checkout"
    # rule as _ensure_checkout. Log + hard-fail on a failed reset (stale baseline).
    if os.path.isdir(os.path.join(checkout, ".git")):
        src = topic.get("review_branch") or ""
        if src:
            _fr = _sp.run(["git", "-C", checkout, "fetch", "--quiet", "origin", src],
                          capture_output=True, text=True, timeout=300)
            _rr = _sp.run(["git", "-C", checkout, "reset", "--hard", f"origin/{src}"],
                          capture_output=True, text=True, timeout=60)
            _sp.run(["git", "-C", checkout, "clean", "-fdx"],
                    capture_output=True, text=True, timeout=60)
            for _nm, _rc, _err in (("apply-fetch", _fr, _fr.stderr[:200]),
                                   ("apply-reset", _rr, _rr.stderr[:200])):
                if _rc != 0:
                    print(f"[apply] {_nm} failed rc={_rc}: {_err}", file=sys.stderr)
            if _rr.returncode != 0:
                return None, f"checkout 复位到 origin/{src} 失败(基线不可信)"
        return checkout, None
    # If not present, clone into the per-topic dir directly (token via git_askpass env).
    os.makedirs(os.path.dirname(checkout), exist_ok=True)
    _askpass = os.path.join(SCRIPTS_DIR, "git_askpass.sh")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS=_askpass,
               CR_GITLAB_USER=_env("GITLAB_USER", "gitlab-ci-token"),
               CR_GITLAB_TOKEN=_env("GITLAB_TOKEN"))
    # project path from the topic's MR url (same as _ensure_checkout); clean https,
    # no token in the URL.
    import jira_parser as _jp
    pp, _ = _jp.parse_gitlab_mr_url(topic.get("mr_url") or "") \
        if "merge_requests" in (topic.get("mr_url") or "") else (None, None)
    # Use THIS repo's own URL (which already resolves to engine or game correctly) —
    # do NOT override with the engine MR's project path, or the GAME repo would clone
    # the ENGINE repo (seen live: game dir got chaos-cb-2.git instead of conquerors-blade-2).
    # Normalize ssh:// to https:// so git_askpass token auth works.
    if url.startswith("git@"):
        _m = _re.match(r'git@([^:]+):(.+)', url)
        if _m:
            url = f"https://{_m.group(1)}/{_m.group(2)}"
    repo_url = url.rstrip("/").removesuffix(".git") + ".git"
    src = topic.get("review_branch") or topic.get("base_branch") or ""
    r = _sp.run(["git", "clone", "--quiet", "--single-branch", "--branch", src,
                 "--depth", "2", repo_url, checkout],
                capture_output=True, text=True, env=env, timeout=600)
    if r.returncode != 0 or not os.path.isdir(os.path.join(checkout, ".git")):
        return None, f"apply checkout clone failed: {r.stderr[:200]}"
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
    # For auto-edit actions we need the findings the review produced.
    _eng_f, _gam_f, _fs = _load_findings(workspace, key)
    all_findings = (_eng_f or []) + (_gam_f or [])

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
            note = ""
            if ok:
                fresh = pipeline_state.get_topic(state_file, key)
                render = fresh.get("render_msg_id") or render
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
        try:
            rc2, out2, err2 = _run_git(["apply", patch_file], checkout)
        finally:
            try:
                os.unlink(patch_file)
            except OSError:
                pass
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

    if action == "agent_edit":
        # 改码: run claude -p to auto-fix findings per repo, stage the diffs for @确认.
        # Approval was done at enqueue time in interact (owner-gated); the real
        # actor rides in pending.patch.actor (consume_all_pending hardcodes "jenkins").
        act = (pending.get("patch") or {}).get("actor") or "jenkins"
        api_key = _env("ANTHROPIC_AUTH_TOKEN") or _env("ANTHROPIC_API_KEY")
        base_url = _env("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
        # 方案(双MR): engine 与 game 各自跑 _agent_edit_all（各自 checkout+fix 分支），
        # 有改动的仓库各得到一个修复 MR。findings 已按仓库分开(_eng_f/_gam_f)。
        repo_findings = {"engine": _eng_f or [], "game": _gam_f or []}
        staged_repos = []          # list of {repo, branch, files, checkout_sha}
        repo_outcomes = {}         # {repo: "fix" | "no_findings" | "no_fix" | "error"}
        merged_failed = []
        for repo in ("engine", "game"):
            rf = repo_findings.get(repo) or []
            if not rf:
                repo_outcomes[repo] = "no_findings"   # reviewed clean -> nothing to fix
                continue
            try:
                ok_diffs, failed, branch, _co, err, checkout_sha = _agent_edit_all(
                    topic, rf, api_key, base_url, workspace, repo=repo)
            except Exception as e:
                repo_outcomes[repo] = "error"
                pipeline_state.append_approval(state_file, key, act, "auto_edit", "", "fail", str(e))
                pipeline_state.clear_pending(state_file, key)
                _proc_reply(key, topic, M("edit_err", msg=str(e)[:200]), render, state_file, app_id, app_secret)
                return False, f"agent_edit error: {e}"
            if err:
                repo_outcomes[repo] = "error"
                merged_failed.append(f"{repo}: {err}")
                continue
            if ok_diffs:
                repo_outcomes[repo] = "fix"
                staged_repos.append({"repo": repo, "branch": branch,
                                     "files": ok_diffs, "checkout_sha": checkout_sha})
            else:
                repo_outcomes[repo] = "no_fix"
            merged_failed.extend((f"{repo}: {ff}" for ff in (failed or [])))
        if not staged_repos:
            pipeline_state.append_approval(state_file, key, act, "auto_edit", "", "fail",
                                           "no fixed finding" if not merged_failed else "; ".join(merged_failed[:2]))
            pipeline_state.clear_pending(state_file, key)
            _proc_reply(key, topic, M("edit_no_diff"), render, state_file, app_id, app_secret)
            return False, "agent_edit: no usable diff"
        # store the per-repo staged sets (confirm iterates them -> per-repo push+MR)
        pipeline_state.set_pending_patch(state_file, key, {
            "file": "all", "target": "agent_edit",
            "state": "staged_agent_edit",
            "diff": "", "created_at": "now",
            "repos": staged_repos,                  # list of {repo, branch, files, checkout_sha}
            "repo_outcomes": repo_outcomes,         # {repo: fix|no_findings|no_fix|error}
        })
        pipeline_state.append_approval(state_file, key, act, "auto_edit",
                                       ",".join(r["branch"] for r in staged_repos), "ok",
                                       f"staged {sum(len(r['files']) for r in staged_repos)} files across "
                                       f"{', '.join(r['repo'] for r in staged_repos)}")
        pipeline_state.clear_pending(state_file, key)
        # 优化 全自动: 若用户触发的是 `优化`(auto_confirm=True), 自动入队确认(commit+push+建/更新MR),
        # 无需手动 `@确认`。否则(手动 `改码`)仍展示待确认。
        if topic.get("auto_confirm"):
            pipeline_state.set_topic_fields(state_file, key, auto_confirm=False)
            pipeline_state.set_pending(state_file, key, "agent_edit_confirm",
                                       patch={"actor": act})
            _proc_reply(key, topic,
                        M("auto_optimizing", n=sum(len(r["files"]) for r in staged_repos)),
                        render, state_file, app_id, app_secret, intent="优化：改码→自动推送→创建/更新MR")
            return True, f"agent_edit: staged {sum(len(r['files']) for r in staged_repos)} files; auto-confirm enqueued"
        lines = [f"## ⚠️ 自动改码完成，请确认\n"]
        for sr in staged_repos:
            lines.append(f"将修复推送到**新分支** `{sr['branch']}`（{sr['repo']}，不覆盖原始 "
                         f"`{topic.get('review_branch') or ''}`），确认后自动创建对应 MR。\n")
            for d in sr["files"]:
                lines.append(f"\n### {d['file']}\n```diff\n{d['diff'][:1500]}\n```")
        if merged_failed:
            lines.append("\n---\n**未能自动修复的文件**：")
            for ff in merged_failed[:5]:
                lines.append(f"- {ff}")
        lines.append("\n> 回复 `@确认 提交并建mr` 推送并建 MR；回复 `@撤销` 取消。")
        _finalize(key, "\n".join(lines), render, [], state_file, app_id, app_secret)
        return True, f"agent_edit: staged {sum(len(r['files']) for r in staged_repos)} files for confirm"

    if action == "agent_edit_confirm":
        # 确认改码(双MR): commit+push+create MR for each per-repo staged set in
        # pending_patch.repos, then post one merged reply listing every MR.
        act = (pending.get("patch") or {}).get("actor") or "jenkins"
        pp = topic.get("pending_patch") or {}
        repos = pp.get("repos") or []
        if pp.get("state") != "staged_agent_edit" or not repos:
            pipeline_state.set_pending_patch(state_file, key, None)
            pipeline_state.clear_pending(state_file, key)
            _proc_reply(key, topic, M("consume_no_staged"), render, state_file, app_id, app_secret)
            return False, "agent_edit_confirm: no staged change set"
        for rs in repos:
            if (rs.get("branch") or "").split("/")[-1] in PROTECTED_BRANCHES or (rs.get("branch") or "") in PROTECTED_BRANCHES:
                pipeline_state.clear_pending(state_file, key)
                _proc_reply(key, topic, M("confirm_protected_branch", branch=rs.get("branch")),
                            render, state_file, app_id, app_secret)
                return False, f"agent_edit_confirm: protected branch {rs.get('branch')}"
        results = []
        hard_fail = None
        for rs in repos:
            mriid, murl, mrnote, err = _commit_push_create_mr_for_repo(key, topic, rs, state_file, workspace)
            if err:
                hard_fail = (rs.get("repo"), err, mrnote or "")
                results.append({"repo": rs.get("repo"), "branch": rs.get("branch"),
                                "ok": False, "murl": "", "mrnote": mrnote or "", "files": rs.get("files") or []})
                continue
            results.append({"repo": rs.get("repo"), "branch": rs.get("branch"), "ok": True,
                            "murl": murl, "mrnote": mrnote or "", "files": rs.get("files") or []})
            if mriid:
                pipeline_state.record_fix_mr(state_file, key, mriid)  # R2 ownership ledger
            pipeline_state.append_approval(state_file, key, act, "push_fix_branch",
                                           rs.get("branch"), "ok", "pushed + MR " + (murl or ""))
            for f in (rs.get("files") or []):
                pipeline_state.record_applied_patch(state_file, key, {
                    "file": f.get("file", ""), "repo": rs.get("repo") or "engine",
                    "branch": rs.get("branch", ""), "applied_at": "now", "mode": "agent_edit",
                    "commit_before": "",
                })
        pipeline_state.set_pending_patch(state_file, key, None)
        pipeline_state.clear_pending(state_file, key)
        if hard_fail:
            note = "🛑 确认阶段失败(" + str(hard_fail[0]) + "): " + str(hard_fail[1])
            if hard_fail[2]:
                note += "\n" + str(hard_fail[2])
            note += "\n\n请重新回复 `优化` 生成新的修复。"
            _proc_reply(key, topic, note, render, state_file, app_id, app_secret,
                        intent="确认并推送修复分支", prefix="🛑")
            return False, "agent_edit_confirm: failed for " + str(hard_fail[0])
        out_lines = []
        outcomes = pp.get("repo_outcomes") or {}
        for r in results:
            if r["ok"] and r["murl"]:
                out_lines.append("✅ **自动改码已推送** `" + r["branch"] + "`（" + r["repo"] + "，" +
                                str(len(r["files"])) + " 个文件）。\n" +
                                "- 推送分支：`" + r["branch"] + "`\n" +
                                "- 本次修复 MR：" + r["murl"])
            else:
                out_lines.append("⚠️ " + r["repo"] + " 修复 MR 未创建（" + (r["mrnote"] or "未知") + "）")
        done = {r["repo"] for r in results}
        for repo, oc in sorted(outcomes.items()):
            if repo in done:
                continue
            if oc == "no_findings":
                out_lines.append("💤 " + repo + "：本次审查无发现，无需修复")
            elif oc == "no_fix":
                out_lines.append("💤 " + repo + "：有发现但未能自动生成修复（可手动处理）")
            elif oc == "error":
                out_lines.append("⚠️ " + repo + "：修复处理失败")
        out_lines.append("- 原评审 MR：" + (topic.get("mr_url") or "") +
                         "\n\n> 请到 GitLab 人工核对后合并；也可 `4` 关闭话题（同时关闭本轮 fix 分支的 OPEN MR）。")
        _finalize(key, "\n".join(out_lines), render, [], state_file, app_id, app_secret)
        return True, "agent_edit_confirm: pushed " + ",".join(r["branch"] for r in results) + " + MR(s)"
    pipeline_state.clear_pending(state_file, key)
    return False, f"unknown pending action: {action}"


# ── Concurrency admission (flock slot lease) ─────────────────────────────────
# Each topic's review runs as its OWN independent subprocess (separate clone,
# separate result files) — the 6 "slots" below simply bound how many of those
# review subprocesses may run at once. A slot is held by an exclusive flock on
# one of MAX_CONCURRENT_REVIEWS lockfiles; the flock is released automatically
# when the process exits (normal or crash), so we never leak a slot and never
# need a finally to free it. Exceeding the cap keeps the topic pending and posts
# a queue notice; the next scan tick retries and sends a "started" notice.
_held_slot = {"file": None, "idx": -1}
_now_iso = pipeline_state._now_iso  # local alias for timestamps


def _queue_position(state_file, key):
    """1-based position of this queued topic among all topics currently queued
    (those with queued=True and not yet running). Returns an int; falls back to
    a best-effort count if the state can't be read."""
    try:
        state = pipeline_state.load_state(state_file)
        queued = [(k, t) for k, t in (state.get("topics") or {}).items()
                  if t.get("queued") and not t.get("phase") in ("CLOSED", "DONE", "FAILED")]
        queued.sort(key=lambda kv: kv[1].get("queued_at") or "")
        for i, (k, _t) in enumerate(queued):
            if k == key:
                return i + 1
        return len(queued) or 1
    except Exception:
        return 1


def _acquire_review_slot():
    """Try to lease one of MAX_CONCURRENT_REVIEWS slots. Returns True on success
    (lease is process-scoped and auto-released on exit), False when all busy."""
    global _held_slot
    import fcntl
    lock_dir = pipeline_state.DEFAULT_LOCK_DIR()
    try:
        os.makedirs(lock_dir, exist_ok=True)
    except Exception:
        pass
    for i in range(MAX_CONCURRENT_REVIEWS):
        try:
            f = open(os.path.join(lock_dir, f"review_slot_{i}"), "a+")
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                f.close()
            except Exception:
                pass
            continue
        _held_slot = {"file": f, "idx": i}
        return True
    return False


def _release_review_slot():
    """Explicitly release the slot (used by in-process drains; also safe to skip
    because flock auto-frees on process exit)."""
    global _held_slot
    import fcntl
    f = _held_slot.get("file")
    if f is not None:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            f.close()
        except Exception:
            pass
    _held_slot = {"file": None, "idx": -1}


def _queue_notice(app_id, app_secret, reply_msg_id):
    """Return the Feishu 'queued' notice text for a topic that hit the concurrency cap.
    方案C: 文案来自 config.yaml messages.queued(可配置)。"""
    from config import MAX_CONCURRENT_REVIEWS as _M
    return (MSG.get("queued") or f"⚠️ 并发 Review 已达上限（{_M} 个并行任务），本话题已进入排队。\n"
            f"当前繁忙，稍后会按顺序自动开始审查，请勿重复触发。")


def _started_notice():
    """方案C: 文案来自 config.yaml messages.started."""
    return MSG.get("started") or "↗️ 轮到本话题了，开始自动 Review..."



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
    with a cross-process lock. Returns a dict {key: (ok, message)}.
    Starvation guard: agent_edit(_confirm) runs claude -p and can take ~minutes,
    blocking this single serialized Jenkins tick; do at most ONE such long action
    per tick so message scanning / other topics aren't starved out."""
    lock_dir = lock_dir or pipeline_state.DEFAULT_LOCK_DIR()
    results = {}
    AGENT_ACTIONS = {"agent_edit", "agent_edit_confirm"}
    agent_done = False
    for key, _pending in pipeline_state.list_pending_topics(state_file):
        action = (_pending or {}).get("action") if isinstance(_pending, dict) else ""
        if action in AGENT_ACTIONS and agent_done:
            # skip a second long-running auto-edit this tick; it stays pending
            continue
        try:
            with pipeline_state.topic_lock_context(lock_dir, key):
                ok, msg = consume_pending(key, state_file, workspace, app_id, app_secret)
                results[key] = (ok, msg)
                if action in AGENT_ACTIONS:
                    agent_done = True
        except Exception as e:
            results[key] = (False, f"executor error: {e}")
    return results


def run_autoclose(state_file, workspace, app_id, app_secret, lock_dir=None, chat_id=""):
    """F1: INDEPENDENT auto-close driver — scans ALL topics on each Jenkins tick and
    closes any IDLE (past AUTO_CLOSE_HOURS) non-CLOSED topic, releasing its fix MR +
    branch. Previously _should_auto_close ran ONLY inside interact() (i.e. only when a
    user happened to reply), so an ignored topic was never actually auto-closed.
    On close it also posts a short group message (已自动关闭 + 释放) so the group sees it.
    Returns {key: (ok, note)} for topics actually closed this tick."""
    lock_dir = lock_dir or pipeline_state.DEFAULT_LOCK_DIR()
    closed = {}
    try:
        topics = pipeline_state.list_topics(state_file)
    except Exception as e:
        print(f"[autoclose] list_topics err: {e}", file=sys.stderr)
        return closed
    # C#3: opportunistically reclaim orphans left by topics that crashed before close,
    # so the open-dir ceiling (MAX_OPEN_CHECKOUT_DIRS) can't be exhausted by them.
    try:
        _sweep_orphan_checkout_dirs(workspace, state_file)
    except Exception as _e:
        print(f"[autoclose] orphan sweep error: {_e}", file=sys.stderr)
    for t in topics:
        key = t.get("message_id") or ""
        if not key or t.get("phase") == "CLOSED":
            continue
        if not _should_auto_close(t):   # 用独立 last_user_activity(F2)
            continue
        # F3: 有 open fix MR 时不自动删分支(防误删未合并 MR), 只关话题并提示;
        #     无 open fix MR 的孤儿分支才自动释放。
        try:
            with pipeline_state.topic_lock_context(lock_dir, key):
                note = _close_topic_resources_if_safe(t, state_file, key, app_id, app_secret, workspace)
                pipeline_state.close_topic(state_file, key, closed_by="auto",
                                           reason=f"{AUTO_CLOSE_HOURS}小时无新回复自动关闭")
                pipeline_state.set_topic_fields(state_file, key, phase="CLOSED")
                closed[key] = (True, note)
                print(f"[autoclose] closed {key[:20]} — {note}", flush=True)
                # 群里发一条"已自动关闭"消息, 让用户明确看到(auto-close 不额外建卡)。
                if app_id and app_secret and chat_id:
                    _run_py("feishu_notifier.py", [
                        "reply-message", "--app-id", app_id, "--app-secret", app_secret,
                        "--chat-id", chat_id, "--message-id", key,
                        "--message-base64", _b64_str(_autoclose_message(t, note))])
        except Exception as e:
            print(f"[autoclose] close {key[:20]} err: {e}", file=sys.stderr)
            closed[key] = (False, str(e))
    return closed


def run_queued_topics(state_file, workspace, app_id, app_secret, limit=3):
    """F: 排队复入健壮性加固 —— 独立重跑 queued=True 的 topic。

    并发满时被排队(queued=True)的 topic, 原依赖 scanner 在下个 tick 重新从群消息
    选中它复入。若该话题的群消息被删除/不可见, scanner 不再返回它 → topic 永久滞留。
    这里在 Jenkins consume tick 里, 独立枚举所有 queued=True 的 topic, 为其重跑
    orchestrato run()(内部 _acquire_review_slot 会在有槽时自动放行), 不依赖群消息可见。
    limit 每 tick 最多重试几个, 避免一下子放太多。返回 {key: last_phase}."""
    import subprocess as _sp, os as _os
    replayed = {}
    try:
        topics = pipeline_state.list_topics(state_file)
        queued = [t for t in topics if t.get("queued")]
        for t in queued[:limit]:
            key = t.get("message_id") or ""
            if not key or t.get("phase") in ("CLOSED", "DONE", "FAILED"):
                continue
            jira_url = t.get("jira_url") or key
            jira_key = t.get("jira_key") or ""
            try:
                r = _sp.run(
                    [sys.executable, _os.path.join(SCRIPTS_DIR, "orchestrate.py"), "run",
                     "--key", key, "--mode", "scan",
                     "--jira-key", jira_key, "--jira-url", jira_url,
                     "--workspace", workspace, "--pipeline-state-file", state_file],
                    capture_output=True, text=True, timeout=300)
                replayed[key] = ("re-run" if r.returncode == 0 else "fail")
            except Exception as e:
                replayed[key] = f"err:{str(e)[:40]}"
    except Exception as e:
        print(f"[queued] run_queued_topics err: {e}", file=sys.stderr)
    return replayed


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
            _update_card_text(app_id, app_secret, render, M("denied_why", why=why), topic_key=key)
            return 1
        pipeline_state.set_pending(state_file, key, "re_review")
        _update_card_text(app_id, app_secret, render,
                          "⏳ 已记录重新审查请求，Jenkins 将拉取最新代码重新审查。", topic_key=key)
        print(f"[action] re_review enqueued by {actor}", flush=True)
    elif action == "close_topic":
        ok, why = _approve(key, topic, actor, "close_topic")
        if not ok:
            _update_card_text(app_id, app_secret, render, M("denied_why", why=why), topic_key=key)
            return 1
        pipeline_state.close_topic(state_file, key, closed_by=actor, reason="用户点击关闭按钮")
        _update_card_text(app_id, app_secret, render,
                          "🔒 本话题已关闭，不再处理。", topic_key=key)
        print(f"[action] closed by {actor}", flush=True)
    elif action == "apply_patch":
        ok, why = _approve(key, topic, actor, "apply_patch")
        if not ok:
            _update_card_text(app_id, app_secret, render, M("denied_why", why=why), topic_key=key)
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
        _proc_reply(key, topic, M("denied_why", why=why), render_id, state_file, app_id, app_secret)
        return
    pipeline_state.set_pending(state_file, key, "re_review")
    _proc_reply(key, topic, M("rerereview_enqueued"), render_id, state_file, app_id, app_secret)


def _cmd_close(key, topic, state_file, render_id, app_id, app_secret, actor="", workspace=None):
    """指令 `4/关闭`: owner/admin 关闭话题。关闭时会一并关闭该话题创建的 OPEN MR
    （fix-branch）并删除 fix 分支，release 资源；原 review MR 不受影响。
    成功后发一条新的群回复（reply-message）让用户明确看到"已关闭"——PATCH 改卡
    不会推新消息，用户可能看不到。"""
    ok, why = _approve(key, topic, actor, "close_topic")
    if not ok:
        _proc_reply(key, topic, M("denied_why", why=why), render_id, state_file, app_id, app_secret)
        return
    closed = _close_topic_resources(topic, workspace)
    pipeline_state.close_topic(state_file, key, closed_by=actor, reason="用户指令关闭")
    pipeline_state.set_topic_fields(state_file, key, phase="CLOSED")
    note = f"🔒 本话题已关闭。{closed} 不处理。" if closed else "🔒 本话题已关闭，不再处理。"
    # 1) keep the review card in sync
    _update_card_text(app_id, app_secret, render_id, note)
    # 2) ALSO post a NEW thread reply so the close is visible in the group (PATCH
    #    does not create a message). _finalize appends chat history + reply-message.
    _finalize(key, note, render_id, [], state_file, app_id, app_secret)


def _close_topic_resources_if_safe(topic, state_file, key, app_id, app_secret, workspace=None):
    """F3: auto-close 版资源释放 —— 防误删未合并 MR。

    与 _close_topic_resources 不同: 仅当话题**没有 open 的 owned fix MR** 时才释放
    该 fix 分支(孤儿分支才自动删); 若有 open fix MR(用户可能还没合并), 则**不自动删
    分支**, 只关闭话题并提示人工处理, 避免 auto-close 误删在途合并。返回 human note。
    `workspace`: 与 _ensure_checkout 一致的工作区(缺省用配置默认); 删除本 topic 的
    checkout 目录必须用真实创建时的工作区, 否则会删错位置/删不到(见方案B磁盘保护)。"""
    import urllib.request, urllib.error, urllib.parse, json as _json
    pp = _project_path(topic)
    tok = _env("GITLAB_TOKEN")
    if not pp or not tok:
        return ""
    new_branch = _fix_branch(topic)
    proj = urllib.parse.quote(pp, safe="")
    owned_iids = set(int(x) for x in (topic.get("fix_mr_iids") or []) if str(x).isdigit())
    # 是否有 open fix MR 还开着
    open_mr_on_branch = False
    try:
        r = urllib.request.Request(
            f"https://gitlab.booming-inc.com/api/v4/projects/{proj}/merge_requests?state=opened&per_page=100",
            headers={"PRIVATE-TOKEN": tok})
        mrs = _json.loads(urllib.request.urlopen(r, timeout=20).read())
        for m in mrs:
            if m.get("source_branch") == new_branch or m.get("iid") in owned_iids:
                open_mr_on_branch = True
                break
    except Exception:
        pass
    # 方案B: 无论是否有 open fix MR, 关闭即删本地按-topic checkout 目录(释放磁盘,
    # 并让 open 目录上限回落)。MR 在服务端, 本地 clone 仅工作副本, 删除安全。
    use_ws = workspace or _DEFAULT_WORKSPACE
    try:
        import shutil as _shutil
        repo_name = pp.rstrip("/").split("/")[-1]
        cdir = _checkout_dir_for(use_ws, repo_name, topic)
        if os.path.isdir(cdir):
            _shutil.rmtree(cdir, ignore_errors=True)
            note_dir = "、已删本地 checkout 目录" if not os.path.isdir(cdir) else "、本地 checkout 目录删除失败(占用)"
        else:
            note_dir = ""
    except Exception as _e:
        print(f"[autoclose] local checkout cleanup error: {_e}", file=sys.stderr)
        note_dir = ""
    if open_mr_on_branch:
        return "有 open fix MR，保留分支待人工合并（未自动删除）" + note_dir
    # 无 open MR -> 孤儿分支, 安全释放(删分支)
    if _branch_is_bot_created(proj, new_branch, tok):
        try:
            req = urllib.request.Request(
                f"https://gitlab.booming-inc.com/api/v4/projects/{proj}/repository/branches/"
                f"{urllib.parse.quote(new_branch, safe='')}",
                method="DELETE", headers={"PRIVATE-TOKEN": tok})
            urllib.request.urlopen(req, timeout=20)
            return f"删除孤儿 fix 分支 {new_branch}" + note_dir
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return "分支已不存在" + note_dir
            return f"删分支失败 HTTP {e.code}" + note_dir
        except Exception:
            return "删分支失败(网络)" + note_dir
    return "无 open MR 也无 bot 分支，无需释放" + note_dir


def _is_bot_fix_commit(title):
    """True if a commit title is a bot-generated fix commit (`[codereview-agent] ...`).
    Used by 方案A to ensure a fix MR only carries the bot's own fix commits — if a
    compare range includes any non-bot commit, the fix branch did not sit cleanly on
    the review branch (serial-baseline pollution, see MR7091) and we refuse to create."""
    return (title or "").startswith("[codereview-agent]")


def _branch_is_bot_created(proj, branch, tok):
    """True if a GitLab branch exists AND its HEAD commit is authored by the bot
    (commit message contains '[codereview-agent]' or author name 'codereview-agent').
    Used to safely release orphan fix branches (pushed but MR- was never created)
    at close time — never deletes a branch we didn't create. A missing branch or any
    API error returns False (won't attempt delete of something unknown)."""
    import urllib.request, urllib.parse, json as _json
    try:
        u = (f"https://gitlab.booming-inc.com/api/v4/projects/{proj}/repository/branches/"
             f"{urllib.parse.quote(branch, safe='')}")
        r = urllib.request.Request(u, headers={"PRIVATE-TOKEN": tok})
        with urllib.request.urlopen(r, timeout=20) as resp:
            d = _json.loads(resp.read())
        c = d.get("commit") or {}
        author = (c.get("author_name") or "")
        msg = (c.get("title") or "") + " " + (c.get("message") or "")
        return ("codereview-agent" in author.lower()) or ("[codereview-agent]" in msg)
    except Exception:
        return False  # branch missing / API error -> do not delete


def _close_topic_resources(topic, workspace=None):
    """Release all remote resources owned by THIS topic's fix branch:
      1) close every OPEN fix-branch MR this bot created for the topic (R2), and
      2) delete the fix branch itself (if it still exists AND we own it).
      `workspace`: 与 _ensure_checkout 一致的工作区(缺省用配置默认), 用于删除本 topic
      的 checkout 目录时定位到真实创建路径。
    Returns a human note ('' if nothing done). Only touches MRs attributed to
    THIS topic; leaves the original review MR (source == {src}) and any
    unrelated MR/branch untouched.

    R2 ownership: we match by the `fix_mr_iids` ledger recorded when the MR was
    created (authoritative), never by bare branch name alone, so a same-named
    MR owned by someone else is never closed. For legacy topics without a
    ledger, we fall back to branch-name AND title/description containing the
    jira_key as an ownership proxy."""
    import urllib.request, urllib.error, urllib.parse, json as _json
    pp = _project_path(topic)
    tok = _env("GITLAB_TOKEN")
    if not pp or not tok:
        return ""
    # 加固3: 关闭删除用的也是同一个修复分支(staged 优先), 与 push/建 MR 一致。
    new_branch = _fix_branch(topic)
    jira = topic.get("jira_key") or ""
    proj = urllib.parse.quote(pp, safe="")
    owned_iids = set(int(x) for x in (topic.get("fix_mr_iids") or []) if str(x).isdigit())
    notes = []
    closed = 0
    owned_branches = set()   # actual source branches of MRs we own/close
    try:
        # 1) close this fix branch's OPEN MRs — only ones WE own (by iid) or, for
        #    legacy topics, a same-branch MR whose title/desc references our jira.
        r = urllib.request.Request(
            f"https://gitlab.booming-inc.com/api/v4/projects/{proj}/merge_requests?state=opened&per_page=100",
            headers={"PRIVATE-TOKEN": tok})
        mrs = _json.loads(urllib.request.urlopen(r, timeout=20).read())
        for m in mrs:
            iid = m.get("iid")
            same_branch = m.get("source_branch") == new_branch
            owned = (iid in owned_iids) or (
                same_branch and jira and (
                    (m.get("title") or "") + " " + (m.get("description") or "")
                ).find(jira) >= 0
            )
            if not owned:
                continue
            # record the branch this owned MR actually lives on (may differ from
            # _fix_branch for older MRs — we must delete the real one, not a guess).
            if m.get("source_branch"):
                owned_branches.add(m["source_branch"])
            try:
                data = _json.dumps({"state_event": "close"}).encode()
                req = urllib.request.Request(
                    f"https://gitlab.booming-inc.com/api/v4/projects/{proj}/merge_requests/{iid}",
                    data=data, method="PUT", headers={"PRIVATE-TOKEN": tok, "Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=20)
                closed += 1
            except Exception:
                pass
        if closed:
            notes.append(f"关闭 {closed} 个 OPEN MR")
    except Exception:
        pass
    # 2) delete the fix branch(es).
    # Candidates: (a) the computed fix branch (per-topic) if it's bot-created or owned,
    #    and (b) the ACTUAL source branches of the MRs we own/closed — for older MRs the
    #    real branch may differ from _fix_branch, so we must delete THAT one, not a guess.
    candidates = set(owned_branches)
    if new_branch:
        candidates.add(new_branch)
    deleted = 0
    for br in candidates:
        if not br:
            continue
        # Only delete if we own it (bot-created branch OR an MR we closed lives on it).
        if br in owned_branches or _branch_is_bot_created(proj, br, tok):
            try:
                req = urllib.request.Request(
                    f"https://gitlab.booming-inc.com/api/v4/projects/{proj}/repository/branches/"
                    f"{urllib.parse.quote(br, safe='')}",
                    method="DELETE", headers={"PRIVATE-TOKEN": tok})
                urllib.request.urlopen(req, timeout=20)
                notes.append(f"删除分支 {br}")
                deleted += 1
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    pass  # already gone
                else:
                    notes.append(f"删分支失败 HTTP {e.code} ({br})")
            except Exception:
                notes.append(f"删分支失败(网络) ({br})")
    # 3) 释放本地按-topic checkout 目录(方案B): topic 关闭即删, 释放磁盘并保证
    #    open 目录上限能回落。目录不存在则幂等跳过; 删除失败仅记录, 不阻断关闭。
    use_ws = workspace or _DEFAULT_WORKSPACE
    try:
        import shutil as _shutil
        repo_name = pp.rstrip("/").split("/")[-1]
        cdir = _checkout_dir_for(use_ws, repo_name, topic)
        if os.path.isdir(cdir):
            _shutil.rmtree(cdir, ignore_errors=True)
            if not os.path.isdir(cdir):
                notes.append("删除本地 checkout 目录")
            else:
                notes.append("删除本地 checkout 目录失败(占用)")
    except Exception as _e:
        print(f"[close] local checkout cleanup error: {_e}", file=sys.stderr)
    return "（已释放：{}）".format("、".join(notes)) if notes else ""


def _cmd_update_conclusion(key, topic, all_findings, render_id, workspace, state_file,
                           app_id, app_secret, actor, arg=""):
    """指令 `更新结论 <ref> <动作>`: 修订指定 findings 并把新的方案C 卡原地 PATCH 回
    render_msg_id 那张卡。全量重审语义：先经 LLM 重新核对该 finding 的结论，把修订写进
    topic.review_overrides(叠加层)，再重渲染。底版 result_*.json 不变(可回溯)。

    用法示例:
      更新结论 #2 降为 suggestion
      更新结论 #3 关闭              (resolve)
      更新结论 新增 scene.cpp issue=... suggestion=...
    """
    ok, why = _approve(key, topic, actor, "update_conclusion")
    if not ok:
        _proc_reply(key, topic, M("denied_why", why=why), render_id, state_file, app_id,
                    app_secret, intent="更新审查结论", prefix="🛑")
        return 0
    arg = (arg or "").strip()
    if not arg:
        _proc_reply(key, topic,
                    "用法：`更新结论 #2 降为 suggestion` / `更新结论 #3 关闭` / "
                    "`更新结论 新增 <文件> @issue... @suggestion...`。",
                    render_id, state_file, app_id, app_secret, intent="更新审查结论")
        return 0
    # 解析引用 + 动作
    import re as _re
    ref_m = _re.match(r'^(#?\d+)\s+(.*)$', arg)
    action = ""
    ref = ref_m.group(1) if ref_m else (arg if arg.startswith("新增") else "")
    rest = ref_m.group(2) if ref_m else ""
    if rest:
        if "关闭" in rest or "resolve" in rest.lower():
            action = "resolve"
        elif "降" in rest or "suggestion" in rest.lower() or "改" in rest:
            action = "amend"
            sev = "suggestion" if ("suggestion" in rest.lower() or "建议" in rest) else \
                  ("warning" if "warning" in rest.lower() or "警告" in rest else "critical")
        else:
            action = "amend"
    elif arg.startswith("新增"):
        action = "add"
    # 干净地解析引用序号：ref 形如 "#3" 或 "3"
    if ref:
        _digits = ref.lstrip("#")
        idx = int(_digits) if _digits.isdigit() and 1 <= int(_digits) <= (len(all_findings) or 1) else None
    else:
        idx = None
    if idx is not None:
        ref = f"#{idx}"
    # 追加 override
    ov = {"ref": ref, "action": action}
    if action == "amend" and ref_m and ref_m.group(2):
        ov["severity"] = sev
        ov["note"] = rest
    elif action == "add":
        ov["file"] = (arg.split("新增", 1)[1] or "?").strip() or "?"
        ov["issue"] = "由用户追加的补充发现"
    overrides = list((topic or {}).get("review_overrides") or []) + [ov]
    pipeline_state.set_topic_fields(state_file, key, review_overrides=overrides)
    # 重渲染 + 原地更新卡(reuse cmd_ci precedent: review_summary + render_msg_id PATCH)
    text = render_findings_with_overrides(topic, all_findings)
    pipeline_state.set_topic_fields(state_file, key, review_summary=text)
    render = topic.get("render_msg_id") or render_id
    if render and app_id and app_secret:
        _run_py("feishu_notifier.py", [
            "update-reply", "--app-id", app_id, "--app-secret", app_secret,
            "--message-id", render, "--message-base64", _b64_str(text)])
    _finalize(key, f"📝 已更新审查结论：{ov.get('action')} {ref or ''}\n请在话题卡上查看修订后的结论。若需撤销，可 `重新审查` 重新生成。",
              render_id, [], state_file, app_id, app_secret)
    return 0


def _cmd_deep_dive(key, topic, all_findings, render_id, workspace, state_file,
                   app_id, app_secret, actor, arg, api_key, base_url, model):
    """指令 `深入 <ref>`: 与 agent 工具 deep_dive 同逻辑的直连入口(便捷使用)。"""
    picks, hint = resolve_findings(all_findings, arg or "all")
    if not picks:
        _finalize(key, f"⚠️ 未解析到可深入的发现。{hint}\n可用 `#序号` / 文件名 / `all`。",
                  render_id, [], state_file, app_id, app_secret)
        return 0
    _txt = "\n".join(f"- [#{i}] {f.get('file')} [{f.get('severity')}]: {f.get('issue')} "
                     f"→ 建议 {f.get('suggestion')}".rstrip()
                     for i, f in _findings_indexed(all_findings) if f in picks)
    _pr = (f"请对以下 {len(picks)} 条 review 发现做更深入的针对性分析(每条给出: 可能根因、"
           f"影响的代码范围/调用链、更具体的修复步骤)。仅分析, 不改结论。\n\n{_txt}")
    deep = _call_llm_simple(_pr, api_key, base_url, model, max_tokens=1200)
    _finalize(key, f"🔍 深入分析（{arg or 'all'}）：\n" + (deep or "（未能生成）"),
              render_id, [], state_file, app_id, app_secret)
    return 0


def _cmd_challenge(key, topic, all_findings, render_id, workspace, state_file,
                   app_id, app_secret, actor, arg, api_key, base_url, model):
    """指令 `质疑 <ref> [理由]`: 与 agent 工具 challenge 同逻辑的直连入口。"""
    parts = arg.split(None, 1) if arg else ["all"]
    ref, reason = (parts[0], parts[1] if len(parts) > 1 else "")
    picks, hint = resolve_findings(all_findings, ref)
    if not picks:
        _finalize(key, f"⚠️ 未解析到可质疑的发现。{hint}", render_id, [], state_file,
                  app_id, app_secret)
        return 0
    _txt = "\n".join(f"- [#{i}] {f.get('file')} [{f.get('severity')}]: {f.get('issue')} "
                     f"→ 建议 {f.get('suggestion')}".rstrip()
                     for i, f in _findings_indexed(all_findings) if f in picks)
    _pr = (f"用户质疑 review 发现(reason: {reason or '未给出'})。请复核并对每条给出结论: "
           f"**成立 / 存疑 / 不成立** + 一句话依据。仅复核, 不改结论。\n\n{_txt}")
    verdict = _call_llm_simple(_pr, api_key, base_url, model, max_tokens=900)
    _finalize(key, f"⚖️ 复核（{ref}）：\n" + (verdict or "（未能生成）"),
              render_id, [], state_file, app_id, app_secret)
    return 0


def _cmd_fix_patch(key, topic, all_findings, render_id, workspace, state_file,
                   app_id, app_secret, actor=""):
    """指令 `1/补丁`: 生成修复补丁方案（基于 findings 的建议，供确认后应用）。"""
    ok, why = _approve(key, topic, actor, "apply_patch")
    if not ok:
        _proc_reply(key, topic, M("denied_why", why=why), render_id, state_file, app_id, app_secret)
        return 0
    # 流程②: never let a suggestion patch (1/补丁) overwrite an in-progress 改码
    # change set (staged_agent_edit). Otherwise a stray `补丁` reply kills the real
    # auto-edit waiting for confirmation.
    cur_pp = topic.get("pending_patch") or {}
    if cur_pp.get("state") == "staged_agent_edit":
        _proc_reply(key, topic, M("fix_patch_staged_exists"), render_id, state_file, app_id, app_secret)
        return 0
    patch = _build_patch_target(all_findings, "all")
    if not all_findings or not (patch.get("diff") or "").strip():
        _proc_reply(key, topic, M("no_findings_patch"), render_id, state_file, app_id, app_secret)
        return 0
    pipeline_state.set_pending_patch(state_file, key, {
        "file": "all", "target": "all", "repo": "engine", "diff": patch.get("diff", ""),
        "created_at": "now",
    })
    _proc_reply(key, topic,
                "✏️ 已生成修复补丁方案（基于 findings 建议，未应用）。\n"
                "回复 `@ok` 让 Jenkins 应用，`@confirm push` 推送，`@撤销` 取消。",
                render_id, state_file, app_id, app_secret, intent="生成修复补丁方案")
    return 0


def _new_branch_name(topic):
    """Per-topic-unique fix branch name (root-cause fix for cross-topic pollution).

    Previously `{src}-fix-{task}` — SAME for every topic sharing the same Jira, so
    different topics pushed commits to the SAME GitLab fix branch, advancing its
    HEAD and making R1's checkout_sha drift (confirm would silently reject: "checkout
    drifted"). Now append a stable short hash of the topic's message_id, so each
    topic gets an INDEPENDENT fix branch. Deterministic across calls for one topic
    (same hash), and all callers (_create_or_get_mr / _agent_edit_all / MR单 / close)
    use this one function so they stay consistent."""
    src = topic.get("review_branch") or ""
    task = topic.get("jira_key") or "task"
    uid = topic.get("message_id") or ""
    if uid:
        import hashlib
        short = hashlib.sha1(uid.encode("utf-8")).hexdigest()[:8]
    else:
        short = ""
    suffix = f"-{task}-{short}" if short else f"-{task}"
    return f"{src}-fix{suffix}" if src else f"fix{suffix}"


def _fix_branch(topic, prefer_staged=True):
    """统一"本次修复分支"入口 —— 单一来源，杜绝各环节分支不一致。

    优先级:
      1. topic.pending_patch.branch (改码 staged 时持久化的真实分支)
      2. _new_branch_name(topic)   (推导兜底)
    所有需要"修复分支"的地方(push/建MR/关闭删除/MR单)都走这里，保证 push、建 MR、
    关闭删除用完全相同的分支，不会被命名规则变化或重新推导搞错。
    """
    if prefer_staged:
        staged = (topic.get("pending_patch") or {}).get("branch") or ""
        if staged:
            return staged
    return _new_branch_name(topic)


def _assert_branch_consistent(pushed_branch, mr_branch, key):
    """加固4: 防静默的心智保障——push 的分支必须与建 MR 的分支完全一致。
    若不一致，打印醒目错误日志(理论上被加固1/2/3排除，但这是最后一道网)。
    不 raise(避免中断确认），但确保运维看得到并排查。"""
    if pushed_branch and mr_branch and pushed_branch != mr_branch:
        print(f"[CRITICAL] branch mismatch: pushed={pushed_branch!r} vs mr={mr_branch!r} "
              f"topic={key} — MR 可能建错分支!", file=sys.stderr)
        return True
    return False


def _project_path(topic):
    mr_url = topic.get("mr_url") or ""
    if "merge_requests" in mr_url:
        import jira_parser as _jp
        pp, _ = _jp.parse_gitlab_mr_url(mr_url)
        return pp
    return ""


def _repo_project(topic, repo):
    """Resolve the GitLab project path for a repo: engine from the recorded MR url
    (authoritative), game from the game_repo URL (a different project). Used so the
    fix MR for the GAME repo is created on the GAME project, not the engine one."""
    if repo == "engine":
        return _project_path(topic)
    url = (topic.get("game_repo")
           or (topic.get("repos") or {}).get("game", {}).get("repo_url")
           or "") or ""
    if not url:
        # fall back to the engine project only if game is truly unresolved
        return _project_path(topic)
    import re as _re
    if url.startswith("git@"):
        m = _re.match(r'git@([^:]+):(.+)', url)
        if m:
            url = f"https://{m.group(1)}/{m.group(2)}"
    path = url.rstrip("/")
    # strip scheme+host -> group/subgroup/project (no .git)
    path = path.replace(".git", "")
    if "://" in path:
        path = path.split("://", 1)[1]
    # drop the host (first segment, e.g. "gitlab.booming-inc.com"), keep the project
    # path group/subgroup/project (drop any trailing segment like /-).
    parts = [p for p in path.split("/") if p]
    parts = parts[1:] if len(parts) > 1 else parts
    return "/".join(parts) if parts else ""


def _create_or_get_mr(topic, all_findings, create_if_missing=False, branch=None, repo="engine"):
    """检测/创建 fix 分支的修复 MR（根治后的幂等实现）。

    `branch`: 可显式指定本次要检测/创建 MR 的修复分支。优先顺序：
        branch(显式，confirm 传入实际 push 的分支) > topic.pending_patch.branch(改码 staged 的分支)
        > _new_branch_name(topic)(推导兜底)。
    这样确认链路(push)与建 MR 永远用同一分支，避免新旧命名规则不一致导致
    compare 空 / MR 建不出。

    修复的根因问题：
      - detect 用 state=all&per_page=50 会被项目大量 closed MR 淹没/截断，漏掉真正的
        open MR -> 走创建 -> 撞 409 （"已存在同源分支 MR"）。现改为 state=opened + 精确
        source_branch 过滤，几乎必中。
      - 创建遇 409 无兜底 -> 直接报失败。现改为 409 时重新查询该分支 open MR 并复用。
      - _get 网络失败被静默当"无 MR" -> 误创建。现区分"查询失败(raise)"与"确实无MR"。
      - target 用 base_branch or 'master'，脆弱。现优先 base_branch，否则从 review 分支推导。
    Returns (mr_iid, mr_web_url, source_branch, note)."""
    import urllib.request, urllib.error, urllib.parse, json as _json
    pp = _repo_project(topic, repo)   # engine from mr_url; game from game_repo URL
    tok = _env("GITLAB_TOKEN")
    if not pp or not tok:
        return None, None, "", "no project path or token"
    # 分支单源: 显式 > staged > 推导
    staged = (topic.get("pending_patch") or {}).get("branch") or ""
    new_branch = branch or staged or _new_branch_name(topic)
    proj = urllib.parse.quote(pp, safe="")

    def _get(url):
        # Raises on transport/protocol error (caller decides); never silently returns [].
        r = urllib.request.Request(url, headers={"PRIVATE-TOKEN": tok})
        with urllib.request.urlopen(r, timeout=20) as resp:
            return _json.loads(resp.read())

    def _find_open_mr_on_branch():
        """精确按 source_branch 查该项目 open MR；返回 (iid, web_url, branch) 或 None."""
        try:
            q = urllib.parse.quote(new_branch, safe='')
            mrs = _get(f"https://gitlab.booming-inc.com/api/v4/projects/{proj}"
                       f"/merge_requests?state=opened&per_page=100&source_branch={q}")
        except Exception:
            # Query failed (not "no MR"). Let callers retry/re-raise rather than treat as missing.
            return "QUERY_ERROR"
        for m in (mrs or []):
            if (m.get("source_branch") or "").lower() == new_branch.lower():
                return (m.get("iid"), m.get("web_url", ""), new_branch)
        return None

    # 1) detect existing OPEN MR on the fix branch (authoritative, pagination-safe).
    found = _find_open_mr_on_branch()
    if found is not None:
        if found == "QUERY_ERROR":
            print(f"[mr] query existing fix MR failed for {new_branch}; retry later", file=sys.stderr)
            return None, None, new_branch, "查询已存在 MR 失败(网络)，请稍后再试"
        iid, url, _ = found
        return iid, url, new_branch, "detected existing"

    if not create_if_missing:
        return None, None, new_branch, "no MR for fix branch yet"

    # 2) Only create if the fix branch actually has changes vs the merge target — never
    #    create an empty/meaningless MR. The fix branch is cut from the ORIGINAL review
    #    branch (review_branch), and the fix MR must merge back INTO review_branch
    #    (not the original MR's target/master) — the fix corrects the code on the review
    #    branch, and the dev later merges review_branch to master themselves.
    target = topic.get("review_branch") or topic.get("base_branch") or ""
    if not target:
        # fallback: the review branch's likely base (up to last '/'), else review_branch.
        rb = topic.get("review_branch") or ""
        target = (rb.rsplit("/", 1)[0] if "/" in rb else "") or "master"
    try:
        cmp = _get(f"https://gitlab.booming-inc.com/api/v4/projects/{proj}"
                   f"/repository/compare?from={urllib.parse.quote(target, safe='')}"
                   f"&to={urllib.parse.quote(new_branch, safe='')}")
        diffs = (cmp or {}).get("diffs") or []
        cmp_commits = (cmp or {}).get("commits") or []
    except Exception:
        diffs, cmp_commits = [], []
    if not diffs:
        return None, None, new_branch, "fix 分支相对 " + target + " 无改动，先推送修复再更新MR"
    # 方案A(基线一致性): fix MR 只能"只含 bot 自己的修复提交"。若 compare 出的提交里
    # 混入了非 bot 提交(例如共享 checkout 串线带来的 mempool 历史, 见 MR7091), 说明修复
    # 分支没有干净地落在 review_branch 之上 —— 此时宁可拒建也绝不创建一个"标签对、diff 错"
    # 的坏 MR。bot 提交特征: 提交信息以 [codereview-agent] 开头(改码/优先生成的)。
    bot_commits = [c for c in cmp_commits if _is_bot_fix_commit(c.get("title") or "")]
    if len(bot_commits) != len(cmp_commits):
        foreign = len(cmp_commits) - len(bot_commits)
        return None, None, new_branch, (
            f"修复分支基线异常: 相对 {target} 的提交中 {foreign} 个不是机器人修复提交"
            f"(疑似串线历史)。已取消创建, 请先 `重新审查` 重建修复分支。"
        )

    # 3) create
    title = f"Fix {topic.get('jira_key') or topic.get('message_id') or ''}: code review fixes"
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
        with urllib.request.urlopen(r, timeout=30) as resp:
            m = _json.loads(resp.read())
        return m.get("iid"), m.get("web_url", ""), new_branch, "created"
    except urllib.error.HTTPError as e:
        # 409 = "Another open merge request already exists for this source branch".
        # Root-cause fix: re-query and REUSE that existing open MR instead of failing.
        if e.code == 409:
            found2 = _find_open_mr_on_branch()
            if found2 and found2 != "QUERY_ERROR":
                iid, url, _ = found2
                return iid, url, new_branch, "reused existing (409)"
        return None, None, new_branch, f"create failed HTTP {e.code}: {e.read()[:150]}"



def _generate_mr_card(topic, all_findings, workspace, key, state_file=""):
    """指令 `mr/生成MR单/更新MR`: 基于 findings + 已应用修改生成 MR 描述。区分
    评审 MR（原始）与本次修复 MR（新分支）。检测/创建新 MR 后填真实新 URL。"""
    jira = topic.get("jira_key") or key
    branch = topic.get("review_branch") or ""
    # 加固3: MR单也读同一修复分支入口(staged 优先), 与 push/建 MR/关闭一致。
    new_branch = _fix_branch(topic)
    # MR单 is READ-ONLY: detect/reuse an existing fix-branch MR, but never CREATE one
    # here (only the 改码确认 path may create). This keeps a stray `MR单` from
    # creating an unintended MR.
    mriid, murl, nbranch, mrnote = _create_or_get_mr(topic, all_findings, create_if_missing=False)
    if mriid and state_file:
        pipeline_state.record_fix_mr(state_file, key, mriid)  # R2 ownership ledger
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
    lines.append("> 本次修复 MR 为机器人通过 GitLab API 创建/检测得到；机器人不合并代码，请到 GitLab 自行 review 后决定。")
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
    # 方案C: GitLab CI 结果不再显示在 review 卡上（用户要求）。CI 跑在 GitLab
    # 页面，review 卡只展示代码审查发现 + 交互指令。这里只记录状态去重，不回写卡。
    # 若真要通知，可在 CI=FAILED 时另发一条独立消息（可选，暂不启用）。
    # if summary.get("status") == "failed" and render and app_id and app_secret:
    #     _run_py("feishu_notifier.py", ["send-message", ...])  # 独立 failed 提醒
    # record last reported status to prevent repeat posts
    pipeline_state.set_topic_fields(state_file, key, ci_status=new_status)
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


def _is_bad_empty_result(res):
    """True if a review result is an LLM-empty stub that must NOT be cached/published.

    A "clean" result must carry an explicit conclusion (review summary or a
    review_text that states one). An empty findings set with no summary and only a
    mechanical stub ("🔍 Code Review — 0 项 (0 必改)…") is an LLM failure
    (CB2N-30597: MR had 17 changed files but posted a 0/0/0 card), not a real
    zero-issue review.
    """
    rv = (res or {}).get("review")
    if not rv or not isinstance(rv, dict):
        # No review block at all — nothing to judge as an LLM-empty stub.
        return False
    if rv.get("error"):
        # Error/partial results must never be served from cache — a re-review
        # should re-run rather than replay the error.
        return True
    if rv.get("findings"):
        return False
    if (rv.get("summary") or "").strip():
        # An explicit summary counts as a conclusion (incl. "no issues").
        return False
    rt = (rv.get("review_text") or "").strip()
    if not rt:
        return True
    # Only the mechanical stub with no real conclusion -> still bad-empty.
    try:
        import code_reviewer as _cr
        return not _cr._empty_output_allowed({}, rt)
    except Exception:
        return True


def _review_repos(key, project, issue_key, review_branch, base_branch, engine_base,
                  engine_repo, game_repo, mr_url, workspace, eng_out, gam_out,
                  use_agent=False, last_review_commit="", carried=None):
    # Cache dir for reuse by diff_hash (avoids re-POSTing unchanged diffs to the LLM).
    cache_dir = os.path.join(workspace, ".review_cache")
    # Review cache version: bump when the rendered review format/prompt changes, so
    # caches written under an older render (e.g. pre-skill-template) are not reused.
    REVIEW_CACHE_VERSION = 2

    def _one(repo, repo_url, rb, baseb, out_path):
        base_args = ["--repo", repo_url, "--branch", rb, "--base-branch", baseb,
                     "--project", project, "--issue-key", issue_key,
                     "--repo-type", repo, "--mr-url", mr_url, "--workspace", workspace]
        # round-N incremental: only diff since last review, skip carried issues.
        if last_review_commit:
            base_args += ["--last-review-commit", last_review_commit]
        if carried:
            base_args += ["--carried", ",".join(str(i) for i in carried)]
        review_args = base_args + (["--agent"] if use_agent else [])
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
        _real = diff_hash not in EMPTY_DIFF_HASHES  # a real, non-empty diff

        if _real:
            cached = os.path.join(cache_dir, f"v{REVIEW_CACHE_VERSION}_{key}_{repo}_{diff_hash}.json")
            if os.path.exists(cached):
                try:
                    with open(cached, encoding="utf-8") as f:
                        cached_res = json.load(f)
                except (OSError, ValueError):
                    cached_res = None
                if cached_res and not _is_bad_empty_result(cached_res):
                    # Reuse cached review result (same diff, already reviewed).
                    _log('REPO', 'CACHED', key, issue_key, project, repo,
                         f"diff {diff_hash[:8]} already reviewed; reusing result")
                    with open(out_path, "w", encoding="utf-8") as f:
                        json.dump(cached_res, f, ensure_ascii=False)
                    return cached_res, 0
                elif cached_res:
                    _log('REPO', 'SOLVED', key, issue_key, project, repo,
                         f"cached result for {diff_hash[:8]} is an LLM-empty stub; re-reviewing")
        # 2) Real review (LLM).
        rc2, _, err2 = _run_py("code_reviewer.py", review_args + ["--output", out_path])
        res = _read_json_file(out_path)
        # Save to cache for reuse only for a REAL (non-empty) diff AND a valid result
        # (not an LLM-empty stub) — see _is_bad_empty.
        if res and _real and not _is_bad_empty_result(res):
            os.makedirs(cache_dir, exist_ok=True)
            with open(os.path.join(cache_dir, f"v{REVIEW_CACHE_VERSION}_{key}_{repo}_{diff_hash}.json"), "w", encoding="utf-8") as f:
                json.dump(res, f, ensure_ascii=False)
        if res is None and rc2 != 0:
            res = {"error": err2 or f"{repo} review failed"}
        return res or {}, rc2

    eng_res, rc_e = _one("engine", engine_repo, review_branch, engine_base, eng_out)
    gam_res, rc_g = _one("game", game_repo, review_branch, base_branch, gam_out)
    return eng_res, gam_res

def _record_repo_state(state_file, key, repo, out_path, res, review_branch, base_branch, repo_url=""):
    repo_url = repo_url or ""
    if not res:
        pipeline_state.set_repo(state_file, key, repo, status="FAILED", error="no result",
                                repo_url=repo_url)
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
                                result_file=os.path.basename(out_path), repo_url=repo_url)
    elif branch_merged:
        pipeline_state.set_repo(state_file, key, repo, status="SKIPPED",
                                skip_reason="already merged",
                                result_file=os.path.basename(out_path), repo_url=repo_url)
    elif not err and changed == 0:
        pipeline_state.set_repo(state_file, key, repo, status="SKIPPED",
                                skip_reason=f"no changes vs {base_branch}",
                                result_file=os.path.basename(out_path), repo_url=repo_url)
    else:
        pipeline_state.set_repo(state_file, key, repo, status="SUCCESS", error=err,
                                result_file=os.path.basename(out_path),
                                severity_counts=sev, stats=stats, changed_files=changed,
                                repo_url=repo_url)


def _update_state_card(state_file, app_id, app_secret, key):
    """使用 review 结果(skill 模板)更新进度卡(render_msg_id), 让进度卡演进出最终
    review 结果 —— 而非另发一条, 从而不出现"两条 review 结果"。若无 review_summary
    则回退渲染状态卡。"""
    if not app_id or not app_secret:
        return
    try:
        topic = pipeline_state.get_topic(state_file, key)
        if not topic or not topic.get("render_msg_id"):
            return
        text = topic.get("review_summary") or feishu_notifier.render_state_card(topic)
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
        # F1: 独立 auto-close 扫描 —— 每个 Jenkins tick 关闭闲置话题。
        ac = run_autoclose(args.pipeline_state_file, args.workspace,
                           _env("FEISHU_APP_ID"), _env("FEISHU_APP_SECRET"), lock_dir=lock_dir,
                           chat_id=_env("FEISHU_CHAT_ID"))
        for k, (ok, note) in ac.items():
            print(f"[autoclose] {k}: {'OK' if ok else 'FAIL'} — {note}", flush=True)
        # F: 排队复入健壮性 —— 独立重跑 queued topic (不依赖群消息可见)。
        rr = run_queued_topics(args.pipeline_state_file, args.workspace,
                               _env("FEISHU_APP_ID"), _env("FEISHU_APP_SECRET"))
        for k, st in rr.items():
            print(f"[queued] replays {k}: {st}", flush=True)
        sys.exit(0 if (results or ac or rr) else 1)
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