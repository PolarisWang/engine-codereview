---
name: review-bot
description: >-
  Automated code review bot for Lark topic group. Monitors a Lark group for review requests (Jira
  ticket IDs), runs /review, manages approval cycle. Usage: /review-bot start | status | stop |
  recover
---

Automated code review bot that monitors a Lark topic group for review
requests, triages each change, runs the review via sub-agent, and drives
an approve/revise/close cycle tracked in per-topic JSON files and a Lark Base.

**Docs to keep in sync** (behavioural change → all three, in one commit):

| File | Audience | Canonical? |
|------|----------|------------|
| `SKILL.md` (this file) | Operator spec — commands, templates, runtime contract | ✅ write changes here first |
| `SKILL_zh.md` | Chinese mirror of `SKILL.md`, also uploaded to the Lark doc below | Translate after SKILL.md settles |
| `DESIGN.md` | Architecture + footguns — the *why* behind the pipeline | Add a `#### 1.x.y` child under the owning feature (never a new top-level catalog) when the rationale would outlive the code |

**Before committing any doc change**, run `python scripts/lint_design_doc.py` (must exit 0) — it enforces the feature-anchored DESIGN.md hierarchy and that every `§` cross-reference resolves. Hooks run it automatically on doc edits and on `git commit`.

**Chinese reference doc**: [Review Bot Skill 技术文档（中文版）](https://www.feishu.cn/docx/Ly0ydTHMYoBKZ7xYpEQcWgWonqC).
After updating `SKILL_zh.md`, re-upload it with:

```bash
python "{skill_dir}/scripts/upload_zh_doc.py"      # --dry-run to preview
```

Do **not** hand-run the `lark-cli docs +update` call — the script exists because
two details bite every time:

- **Size**: the file is ~80 KB. Only `--markdown "@<relative-path>"` works (the
  path goes through argv, the content is streamed from disk). An inline
  `--markdown "<content>"` hits the Windows ~32 KB `CreateProcess` ceiling
  (`WinError 206`) and stdin (`-`) truncates silently. The `@` path must be
  **relative**, so the upload runs with `cwd` = skill dir.
- **Frontmatter**: `SKILL_zh.md` opens with YAML frontmatter, which is
  skill-loader metadata, not documentation. Uploaded as-is, the closing `---`
  turns the `name:` line into a setext heading and swallows `description:` into
  it, so the doc opened with a mangled `## name: review-botdescription: "…"`.
  The script strips the block, uploads the body, and deletes the temp copy.

The script fails loud on a missing/unterminated frontmatter block and on a
response without `"success": true` — a silent partial upload is the thing being
prevented. For a one-paragraph tweak you can still target a single section with
`--mode replace_range` / `insert_after` + `--selection-by-title`, but the
whole-file overwrite is the default.

## Parameters

- `$ARGUMENTS` — command: `start [--silent] | status | stop [--silent] | recover`

| Command | Purpose |
|---------|---------|
| `start` | Launch listener + daemon + monitor, init state, post greeting |
| `start --silent` | Same as `start` but skip the greeting message in the Lark group |
| `status` | Show active topics, listener, recent log |
| `stop`  | Kill listener + daemon, show final status, post farewell |
| `stop --silent` | Same as `stop` but skip the farewell message in the Lark group |
| `recover` | Force-reconcile all open topics against GitLab API |

## Required Permissions

The review bot runs autonomously. Every tool call that isn't pre-authorized
will trigger a Claude Code permission prompt, halting the bot. The `start`
command should request these permissions at session start so all subsequent
poll cycles and sub-agents run uninterrupted.

**Bash commands** (all require pre-authorization):
- `glab` — GitLab API: `mr list`, `mr approve`, `mr close`, `api` (MR state, pipeline, merge PUT)
- `git` — fetch, diff, log, ls-remote, show (branch resolution, diff generation)
- `node` / `lark-cli` — `im +messages-reply/send`, `drive permission.members create`, `drive permission.public update` (best-effort; surface dropped in current lark-cli), `base +record-upsert`, `docs +create/+update`
- `python` — all scripts: `parse_args.py`, `resolve_start.py`, `status_report.py`, `topic_store.py`, `reply_parser.py`, `state_machine.py`, `render.py`, `merge_tracker.py`, `dispatcher.py`, `start_listener.py`, `poll_dispatch.py`, `stop_bot.py`, `lark_doc_helper.py`, `build_review_doc.py`, `finalize_review.py`, `restart_bot.py`
- `powershell` — `stop_bot.py` invokes `Get-CimInstance Win32_Process` to scan for surviving daemon/listener processes by command line during `stop`
- `wscript` — daemon launch via VBS
- `taskkill.exe` — process termination (stop mode)
- Output redirection (`>`) — temp files to `$WRITE_TMP_DIR`

**Directory creation** (triggered a permission prompt in previous runs):
- `cfg/topics/`, `cfg/topics/closed/`, `cfg/events/` via `resolve_start.py` or `topic_store.py`

**File writes** (via Bash output redirection or Python scripts):
- `$WRITE_TMP_DIR/*.json`, `*.diff`, `*.stat` (temp files)
- `cfg/topics/*.json` (topic state, atomic write + rename)
- `cfg/open_topic_index.json`, `cfg/*.pid`, `cfg/*.lock`, `cfg/sessions.json`

**Agent/Monitor**:
- Up to 4 topic Agents per cycle (the merge queue runs in-process inside
  the daemon — no Claude spawn needed)
- 1 persistent Monitor on `$WRITE_TMP_DIR/poll_trigger.txt`

## Step 1: Parse arguments and validate environment

```bash
python "{skill_dir}/scripts/parse_args.py" {$ARGUMENTS}
```

If the JSON has an `"error"` key, report it to the user and stop.
The output includes `command`, `env` (all resolved env vars), `paths`
(all absolute paths), and `listener` status.

**Never hardcode open_ids in post payloads.** Always resolve the approver
through `env.approver_id` and the developer through the topic's stored
`creator_open_id`.

## Step 2: Execute command

Branch on `command` from the parse_args output.

---

### `start` Mode

#### 2a. Resolve prerequisites

```bash
python "{skill_dir}/scripts/resolve_start.py" --params-json '{parse_args_output}'
```

This creates directories, initializes `open_topic_index.json`, resolves
Lark Base config, and registers this session's parent `claude.exe` PID in
`cfg/sessions.json` so the daily 08:00 restart can end it — including when the
bot was started by hand rather than by the scheduled task (DESIGN §1.1.5). Read
the JSON output; `session.pid == null` means the ancestry walk missed and this
session will outlive the next restart.

#### 2b. Start listener

**Default (OS-detached, no console popup, survives `/clear`):**

```bash
python "{paths.scripts}/start_listener.py"
```

This uses `subprocess_util.detached_popen` with
`DETACHED_PROCESS | CREATE_NO_WINDOW` to spawn `node lark-cli event +subscribe`
at the OS level. The window is hidden by `CREATE_NO_WINDOW` independently
of Claude Code, so the spawn is silent on every Claude Code version (it
does NOT depend on the windowsHide fix from anthropics/claude-code#14828).
Lifetime is tied to the OS process, not the Claude Code session — the
listener survives `/clear`, session exit, and parent crashes. The
nightly Windows Task Scheduler stop (§2e) is the canonical shutdown path.
`_restart_listener()` in `dispatcher.py` uses the same `detached_popen`
machinery for mid-day health-check restarts, so they're also silent.

**Alternate (Claude-Code-managed):** if you specifically want the
listener to die when the Claude Code session ends (e.g. interactive
sandbox testing where you want a clean tear-down on `/clear`), use:

```bash
python "{paths.scripts}/start_listener.py" --foreground
```

with `run_in_background: true` on the Bash tool call. `--foreground`
runs the same setup, then `execvp`s into the listener so the
Claude-managed background task IS the listener. **Caveat**: on Claude
Code versions missing the windowsHide fix (anthropics/claude-code#14828),
this path pops a `node.exe` console for a moment at start and on every
respawn after `/clear`. Prefer the default unless you have a reason.

#### 2c. Create Lark Base (if `base.need_create`)

If `resolve_start` returns `base.need_create: true`, **confirm with the user**
before creating. Show: Base name `代码审查跟踪`, table name `代码审查记录`.
After approval, create using the schema in **Lark Base Integration** below.
Write `app_token`/`table_id` to both `{paths.base_config}` and
`.claude/settings.local.json`.

#### 2d. Launch event-driven daemon + Monitor

Ensure the trigger file exists, then launch the daemon. The daemon is a
**singleton** — `poll_dispatch.py --watch` checks `cfg/daemon.pid` on
startup and exits immediately if a live instance already holds it, so
re-launching is safe.

**Default (OS-detached via wscript, no console popup, survives `/clear`):**

```bash
touch "$WRITE_TMP_DIR/poll_trigger.txt"
wscript //nologo "{paths.scripts}/run_poll.vbs"
```

`run_poll.vbs` invokes `pythonw poll_dispatch.py --watch` via
`WshShell.Run` with `WindowStyle=0` (hidden) and `WaitOnReturn=False`,
producing a fully OS-detached daemon. The window suppression is done by
WSH, not Claude Code, so the spawn is silent on every Claude Code
version (independent of the windowsHide fix from
anthropics/claude-code#14828). Lifetime is tied to the OS process, not
the Claude Code session — the daemon survives `/clear`, session exit,
and parent crashes. The nightly Windows Task Scheduler stop (§2e) is the
canonical shutdown path.

**Alternate (Claude-Code-managed):** if you specifically want the
daemon to die when the Claude Code session ends, use the Bash tool
with `run_in_background: true` running:

```bash
touch "$WRITE_TMP_DIR/poll_trigger.txt"
pythonw "{paths.scripts}/poll_dispatch.py" --watch
```

**Caveat**: on Claude Code versions missing the windowsHide fix
(anthropics/claude-code#14828), this path pops a `pythonw.exe` console
for a moment at start and again whenever `/clear` + `/review-bot start`
re-spawns it. Prefer the default unless you have a reason.

Then set up a persistent Monitor using the dispatch wrapper script.
The wrapper skips backlog (existing lines), reads the dispatch plan on
each trigger, and only emits when there's actionable work — each
notification is a self-contained JSON with ticket IDs and file paths.

```python
Monitor(
    description="Review bot dispatch",
    persistent=True,
    timeout_ms=3600000,
    command='python "{paths.scripts}/monitor_dispatch.py"',
)
```

**CRITICAL — dispatch loop contract**: Each Monitor notification is a
JSON object containing `work`, `merge_queue`, and `plan_file`. On
**every** notification, you MUST:

1. Parse the JSON from the notification event text
2. Render specs with `render_spawn_prompt.py --all` (it reads `plan_file`)
3. Spawn one topic agent per returned spec (lock-fresh topics are pre-filtered)
4. This is the **main autonomous loop** — do NOT wait for user input

`merge_queue` and `3p_merge_queue` are telemetry only — the daemon's
in-process `process_merge_queue.py` handles all rebase + merge +
archive work; no agent spawn is required. They appear in the
notification payload so you can see what the daemon is merging.

The daemon only triggers when real work exists (30s cooldown).
Over-spawning is safe: the per-topic lock causes duplicate agents to
exit with `skipped_locked` immediately.

**Spawn topic agents**:

Render the spawn specs deterministically — do NOT hand-build prompts or pick
the model by eye:

```bash
python "{paths.scripts}/render_spawn_prompt.py" --all
```

This reads `cfg/dispatch_plan.json` and emits `{cycle_id, count, specs[],
skipped[]}`. In the default `ref` mode each `spec` is deliberately tiny —
`{thread_id, ticket_id, model, description, spec_file, prompt}` — because this
output lands in YOUR context every cycle. The resolved inputs are written to
`spec_file` (`cfg/spawn/<thread>__<cycle>.json`) and the `prompt` just points
the agent at it plus the contract; the agent reads both in its own context.
Do NOT read `spec_file` yourself — nothing in the dispatch loop needs it, and
reading it re-imports the cost the mode exists to avoid (DESIGN §1.4.6).

`--all` covers up to `parallel_limit` (=4) work items and drops any topic whose
`.lock` is held & fresh (reported under `skipped[]` with `skip_reason:
"lock_held_fresh"`) so you never double-spawn a running agent. For a single
topic use `--thread-id <om_...>` (returns one spec object).

Fire one Task call per `spec` **in a single message**:

```
plan = json.loads(<render_spawn_prompt.py --all output>)
for spec in plan["specs"]:          # capped at parallel_limit, lock-filtered
    Agent(
        model=spec["model"],         # helper picks it (opus for every current event type)
        description=spec["description"],
        prompt=spec["prompt"],
        run_in_background=True,
    )
```

The model mapping (every event type → `opus` today; mechanical
`approve`/`revision`/`close` never reach an agent — they drain in-process via
`mechanical_reply_handler.py`) lives in `_MODEL_BY_EVENT` in the helper, not
here. `--mode compact` inlines the inputs and `--mode full` inlines the whole
rendered contract; both cost several times more of your context per spawn and
exist for debugging, not for the loop. Each sub-agent processes one topic per
the **Per-topic agent contract** below.

**Do not log per-spawn token telemetry.** The dispatcher already runs
`collect_session_tokens.py` at the end of every cycle, which reconstructs the
same `spawn_tokens` records from the subagent transcripts (deduped by agent_id
via `cfg/token_ledger.json`). Calling `activity_logger.py --tokens` from here
duplicates it and costs a tool round per spawn for nothing — see DESIGN §1.4.7.
`--mode compact/full` still emit a ready-to-run `telemetry.argv` for ad-hoc
manual use.

**Agent results are one JSON line.** The contract (§6) forbids a prose summary;
a spawn's findings reach their audience through the Lark thread and doc, not
through your context. If an agent returns prose anyway, do not relay or
summarize it — act on the JSON and move on.

**Capturing a real-runtime baseline**: the telemetry line above lands
as `[ts] [INFO] spawn_tokens {JSON}` in
`{paths.cfg}/activity.log` alongside the existing dispatcher entries
(the log rotates at 1 MB keeping the last 500 lines, so a week of
steady traffic stays in-file). To review token spend over that window:

```bash
# Summary grouped by state / event_type
python {paths.scripts}/status_report.py \
    --params-json '{"paths":{"topics_dir":"…","cfg":"…","activity_log":"…/activity.log"}}' \
    --token-summary

# Only the last N spawns (e.g. after landing T1+T2)
python {paths.scripts}/status_report.py --params-json '…' \
    --token-summary --max-entries 200
```

The output is a JSON dict keyed by bucket
(`{"event_types":{"dev_reply":{"spawns":N,"input_mean":…,"input_p50":…,"input_p95":…},…}}`)
that can be pasted into a Lark doc, diffed between runs, or fed to a
spreadsheet. For a persistent before/after record, snapshot the raw
`spawn_tokens` lines to a fixture after each meaningful landing:

```bash
grep "^\[.* spawn_tokens " {paths.cfg}/activity.log \
    > {paths.cfg}/baselines/pre_t1.jsonl   # adapt filename per milestone
```

Reuse that fixture (restored into `activity.log` or read directly via a
throwaway `--params-json` pointing at the fixture path) to compute
before/after deltas without depending on log rotation. The deltas are
what validate whether a tactic (drain_no_new_commits, incr_cache,
future T3/T4) actually moved per-spawn spend.

**Offline back-fill from Claude Code transcripts** (`scripts/collect_session_tokens.py`):
the in-conversation `activity_logger.py --tokens` call only runs when
the parent session remembers to log right after each spawn. For a
lossless accounting use the Claude Code subagent transcripts directly:

```bash
python {paths.scripts}/collect_session_tokens.py \
    --projects-dir "$HOME/.claude/projects" \
    --skill-dir {paths.skill_dir}
```

Walks every `projects/<slug>/<parent-session>/subagents/agent-*.jsonl`
under the project slug, sums each turn's `message.usage` (input,
output, cache read/creation), extracts ticket/thread/cycle from the
first user turn's `TOPIC_FILE`/`POLL_CYCLE_ID` markers, and appends one
`spawn_tokens` line per new agent to `cfg/activity.log` in the same
format `status_report.py --token-summary` already consumes. A
`cfg/token_ledger.json` dedup keyed by agent_id keeps reruns idempotent,
so it's safe to wire into the end-of-cycle dispatcher step (or run by
hand after a milestone) without double-counting. Useful when the live
`--tokens` hook was skipped, when reconstructing history from an older
session, or to cross-check live numbers.

**Merge queue** is handled in-process by the daemon — no agent spawn.

`scripts/process_merge_queue.py` runs inside `poll_dispatch.py`'s
watch loop every dispatcher cycle. It drains `dispatch_plan.merge_queue`
and `3p_merge_queue` sequentially: rebase (skip_ci) → merge via GitLab
API PUT → post `merged` template → transition state to MERGED → archive
to `cfg/topics/closed/`. Zero LLM tokens, zero Claude spawn — the parent
session should ignore the merge_queue fields in the notification except
as telemetry.

On a **rebase conflict**, the topic is *parked* rather than retried: the
bot sets `review.rebase_conflict_blocked`, posts the `rebase_conflict`
template (rebase locally → push → reply `ok`), and `_check_approved_topics`
then skips the topic so it stops re-attempting the rebase every cycle. The
developer's `ok` is consumed mechanically by
`mechanical_reply_handler.drain_rebase_conflict_ack` (also in-process, no
agent): if `git ls-remote` shows the branch advanced it clears the flag +
posts `merge_resuming` (`流水线运行中，完成后将自动合并。`) and the topic
re-enters the normal merge flow; if not, it posts `rebase_no_push` and stays
parked. See DESIGN §1.6.7.

#### 2e. Daily 08:00 restart — owned by Windows Task Scheduler (no action here)

There is **no nightly shutdown**; the bot runs around the clock. The
`ClaudeReviewBot` Scheduled Task fires **daily at 08:00** (registered via
`scheduling/register_review_bot_task.ps1`) and runs
`launch_review_bot.vbs` → `launch_review_bot.ps1`, which does
`stop_review_bot.ps1 -Silent` **then** `/review-bot start` in one task
instance. The stop kills the detached daemon (**first**, so the
health-check can't respawn the listener), then the listener, monitor, and
the previous `claude.exe` session, by pid file.

The restart exists to recycle the parent session's context, not to take
the bot down — 08:00 is simply when a review is least likely to be in
flight. `-Silent` skips the `已停止` farewell since the greeting follows
seconds later. The old `ClaudeReviewBotStop` task is **disabled**;
re-enable with `schtasks /change /tn ClaudeReviewBotStop /enable` if a
real overnight shutdown is ever wanted.

**`start` registers no in-session stop cron.** The previous `CronCreate`
approach was removed. See DESIGN §1.1.1 for why daemon-first killing
makes the external stop safe.

#### 2f. Post greeting

**Skip this step if `--silent` was passed.** Otherwise:

```bash
"$APPDATA/npm/node_modules/@larksuite/cli/bin/lark-cli.exe" \
  im +messages-send --as bot \
  --chat-id "{env.chat_id}" \
  --msg-type text \
  --text "🤖 Review Bot 已启动。在话题中发送 Jira 单号（如 RAGE-12469）即可触发代码审查。"
```

---

### `status` Mode

```bash
python "{skill_dir}/scripts/status_report.py" --params-json '{parse_args_output}'
```

Display `formatted_table`, `listener_summary`, and `log_tail` from the output.

---

### `stop` Mode

1. **Kill the daemon + listener + monitor processes via `stop_bot.py`** — do
   NOT hand-roll `taskkill` on the pid files. The naive "read `listener.pid` +
   `daemon.pid`, kill those two" is unreliable and silently leaves the bot
   running: the daemon's health-check (`dispatcher._restart_listener`)
   respawns the listener under a NEW pid (so `listener.pid` is stale the
   moment you read it), and a second / orphaned `poll_dispatch.py --watch`
   keeps respawning it after you kill the recorded one. `stop_bot.py` trusts
   BOTH the pid files AND a command-line process scan, kills **daemons first**
   (so nothing respawns the listener mid-teardown), loops until a scan finds
   zero matches, then removes the pid files. See DESIGN §1.1.2.

   ```bash
   python "{paths.scripts}/stop_bot.py"
   ```

   Reads one JSON line: `{"status":"ok"|"survivors_remain"|"scan_failed",
   "killed":{...}, "survivors":[...], "passes":N, "pid_files_removed":[...]}`.
   `status:"ok"` means the bot is fully down and pid files are cleared. On
   `survivors_remain` (rare — a process held a handle past the kill), the
   `survivors[]` list carries each pid + command line; re-run the script,
   and if it still won't die, kill the named pids by hand — via
   `subprocess_util.kill_process`, NOT `taskkill`, which is itself broken in
   this state. `scan_failed` means the process scan broke (see `error`) and
   **nothing was killed or deleted** — do not treat it as a stop; fix the
   machine state and re-run. Pid files for surviving processes are
   deliberately left in place: the pid file is the only record of a process's
   identity, so deleting one for a live process orphans it permanently
   (DESIGN §1.1.4). Steps 2–3 (read pid files, kill, remove pid files) are
   folded into this one call — do them no other way.

   **No `TaskStop` is needed for the Monitor.** Killing the
   `monitor_dispatch.py` *process* (above) pulls the script out from under
   the Claude-Code Monitor *task* wrapper, which then fires a benign
   `Monitor script failed (exit 1)` notification and **deregisters the task
   itself**. A `TaskStop` afterward always returned `No task found` — it was
   a guaranteed no-op, so it was removed from this procedure. The wrapper
   self-cleans on every stop; the scheduled-stop path (which can't call
   `TaskStop` at all) relies on the same self-deregister. See DESIGN §1.1.2.
2. Wipe scratch files left behind by topic agents:
   `python "{paths.scripts}/clean_tmp.py"`.
   The script does two narrow passes: (a) files directly in `_tmp/`
   whose suffix is `.json` / `.diff` / `.stat`, (b) per-spawn
   artifact-writeback scratch scripts in `scripts/` matching
   `_write_artifact_*.py` (topic agents drop these next to the real
   scripts and never clean them up). No wildcards, no recursion, no
   surprise deletions. Do NOT use `rm -f _tmp/*` or `rm scripts/_write_artifact_*` —
   the shell wildcards trip permission prompts and are broader than needed.
3. Run `status_report.py` and show final status.
4. **Skip this step if `--silent` was passed.** Otherwise post in group: `🤖 Review Bot 已停止。`

There is **no stop cron to delete** — `start` no longer registers one. If a
legacy `cfg/stop_cron.id` is present (left by an old start), delete the file
and `CronDelete(id)` it as a one-time cleanup; otherwise nothing to do.

> **There is no nightly auto-stop.** The daily `ClaudeReviewBot` task
> (08:00) runs `scheduling/stop_review_bot.ps1 -Silent` as the first half
> of a restart, which kills the **daemon first** — so the dispatcher's
> listener health-check is gone before it can respawn the listener — then
> the listener, monitor, and the previous `claude.exe` session. The old
> respawn-race concern (a Windows kill being undone by a still-live
> daemon) is eliminated by the daemon-first order; the in-session
> `CronCreate` stop was removed and the separate `ClaudeReviewBotStop`
> task is disabled. See DESIGN §1.1.1.

---

### `restart` Mode

Kill and relaunch only the long-lived processes that need it — no session
recycle, no group greeting. Use after editing any script the daemon or monitor
imports (they load Python once at startup and never hot-reload).

```bash
python "{paths.scripts}/restart_bot.py" --components "{components or 'stale'}"
```

| argument | restarts |
|---|---|
| *(omitted)* / `stale` | only components whose loaded code is older than the newest edit — the default, and the one to use after a code change |
| `daemon` | the dispatch daemon (`poll_dispatch.py --watch`) |
| `listener` | the Lark event listener — **only when genuinely wedged**; respawns spend the app-wide Feishu long-connection quota. The daemon keeps running and is told to pause its listener health-check via `cfg/listener_restart.guard` for the duration, so it cannot respawn a second one mid-swap (DESIGN §1.1.6) |
| `monitor` | the dispatch Monitor (see the session step below) |
| `daemon,monitor` | any comma-separated combination |
| `all` | daemon + listener + monitor |

Report the JSON verbatim: `restarted` (new pids confirmed from the pid file),
`failed`, and `untouched` with a `reason` per component. Exit code is non-zero
if anything failed.

**If `needs_session_action` contains `monitor`**, the script killed it but
cannot recreate it — the Monitor is a Claude Code tool task owned by this
session, not an OS process. Re-issue it yourself:

```python
Monitor(
    description="Review bot dispatch",
    persistent=True,
    timeout_ms=3600000,
    command='python "{paths.scripts}/monitor_dispatch.py"',
)
```

The parent **session** is not a component: it cannot restart itself. A change
to `resolve_start.py`, or reclaiming parent context, needs `stop` + `start` or
the daily 08:00 task. See DESIGN §1.1.6.

### `recover` Mode

Catch up after downtime, then force-reconcile all open topics against the
GitLab API. Use when the daemon missed events, pipeline_status is stale, or
topics are stuck.

#### 2a. Run the recover script

`scripts/recover.py` encodes the full reconciliation contract — no
LLM walking required.

```bash
python "{paths.scripts}/recover.py"
```

Behavior:

- Refuses with `status:"refused"` and the holder list if any topic lock
  is currently held by a live agent. Wait for the agent (stale >10 min,
  force-cleared >30 min by the janitor) or terminate it manually before
  retrying. Running recover while an agent is mid-flight would clobber
  in-progress writes (state transitions, audit entries, MR approve/merge
  results).
- **Message catch-up first** (`catch_up` in the output): runs one guarded
  dispatcher cycle to ingest topics opened — and mechanical replies
  (`ok`/indices/`close`) sent — while the bot was down. These are
  Lark messages the dead listener never captured; only the dispatcher's
  history-backfill reconcile recovers them (GitLab reconciliation below
  can't — those topics may not exist yet). Skipped (`ran:false,
  reason:"daemon_alive"`) when a live daemon already holds `daemon.pid`,
  since it reconciles every cycle and a concurrent one-shot would
  double-process. The reconcile window floors at the persisted
  `last_reconcile_ts` (auto-widening to cover the whole downtime, 7-day cap)
  rather than a fixed 1h — see DESIGN §1.2.6.
- For each open topic in `cfg/topics/*.json` (via `iter_topic_files` — inbox
  and tmp siblings are skipped; see DESIGN §1.1.3), fetches each MR via
  `glab api` and applies:

  | GitLab `state` | Topic `review.state` | Action |
  |----------------|---------------------|--------|
  | `merged` (all MRs) | Any non-terminal | Post `merged` notice, set `MERGED`, archive to `closed/` |
  | `closed` (any MR) | Any non-terminal | Post `mr_closed` notice, set `CLOSED`, archive to `closed/` |
  | `opened` | `APPROVED` | Refresh `mrs[repo].pipeline_status` from `head_pipeline.status` |
  | `opened` | Other | No change (waiting for review cycle) |

  `head_pipeline.status` maps `success`/missing → `passed`,
  `running`/`pending` → `running`, `failed`/`canceled` → `failed`.
  Terminal transitions post an honest thread notice via
  `terminal_notice.post_terminal_notice` (idempotent — see DESIGN §1.6.11),
  then archive via `topic_store.archive_topic` (moves the file, drops the
  index entry, removes the sibling inbox) — never a bare file move
  (DESIGN §1.1.3).

- Output is a JSON `{"status":"ok","catch_up":{...},"rows":[...]}` — the
  `catch_up` block reports the guarded ingestion cycle (§2a), and each `rows`
  entry records before/after state, per-MR fetch result, and any archive
  move.

#### 2b. Restart daemon + monitor if dead

Check if `pythonw.exe` is running. If not:
```bash
wscript //nologo "{paths.scripts}/run_poll.vbs"
```
Re-create the Monitor on `poll_trigger.txt`.

#### 2c. Report

Display the JSON output. Flag rows whose `mrs[*]` carry an `error` field
(stuck MRs needing manual attention).

---

## State Machine

```
APPROVER APPROVAL VERB: `ok` only (DESIGN §1.23.2). `pass`/`通过`/`lgtm`/`approved`
are NOT recognized (removed). (In *_REVISION states `ok` is the *developer's*
re-review trigger, not approval — role + state gated; the approver has no
approve verb during revision, only `close`.)

3RD-PARTY PHASE (when 3rd-party MR detected, review_phase="3rd_party"):
  TRIAGING → INLINE_REVIEW (3rd-party only) → round-1 flow below (§1.23.5)
                                                  │    │
                                            (ok)   (fix list)
                                              │       │
                    merge 3rd-party MR ←──────┘  SIMPLE_REVISION
                    review_phase="main"              (dev replies)
                    state=TRIAGING ←─────────────────┘
                    (re-enter normal flow)

EVERY ROUND, EITHER PHASE (self-service dev loop, DESIGN §1.23):
  TRIAGING → INLINE_REVIEW (simple) ─┬→ DEV_TRIAGE        [review posted @dev, ≥1 issue]
  TRIAGING → FULL_REVIEW (complex) ──┘      │
      DEV_TRIAGE:                           │
        (dev: indices / `all`) ───────────→ SIMPLE_REVISION | FULL_REVISION  [by review.triage]
        (dev: some rejected / `none`) ────→ SIMPLE_REVISION | FULL_REVISION  [dissent recorded]
        (dev: nothing left to fix) ───────→ DEV_TRIAGE     [nudge: reply `done`]
        (dev: `done`) ────────────────────→ AWAITING_APPROVAL  [handoff_summary @approver]
        (approver: ok = override) ────────→ APPROVED
        (close) ──────────────────────────→ CLOSED
      SIMPLE_REVISION | FULL_REVISION:
        (dev: push + `ok`) ───────────────→ round N+1 review → DEV_TRIAGE
        (dev: `done`) ────────────────────→ AWAITING_APPROVAL
        (approver: ok / close) ───────────→ APPROVED | CLOSED
      AWAITING_APPROVAL  (the approver's only stop):
        (approver: ok) ───────────────────→ APPROVED
        (approver: indices = reinstate) ──→ SIMPLE_REVISION | FULL_REVISION
        (approver: full, triage=simple) ──→ FULL_REVIEW → DEV_TRIAGE  [dev re-triages full list]
        (close) ──────────────────────────→ CLOSED
  Zero-issue round 1: nothing to triage → legacy review_round1 →
  TRIAGE_DECISION (simple) / AWAITING_APPROVAL (complex).
  ARBITRATION is no longer entered (retained for in-flight topics, §1.23.4).

LEGACY ROUND-N SHAPE (pre-§1.23.6, still drains for in-flight topics):
ROUND N (approver decides; approver `ok` = approve):
  SIMPLE_REVISION ──(dev ok)──→ TRIAGE_DECISION
                  ↑               │    │    │       │
                  │         (indices) (full) (ok)  (close)
                  │               │    │      │       │
                  └───────────────┘    │      │       └→ CLOSED
                                      ↓      ↓
                                 FULL_REVIEW  APPROVED
  FULL_REVISION ──(dev ok)──→ AWAITING_APPROVAL
                  ↑               │    │    │
                  │         (indices) (ok)  (close)
                  │               │    │    │
                  └───────────────┘    │    └→ CLOSED
                                      ↓
                                  APPROVED
```

`APPROVED` is NOT terminal. The review agent does NOT manage APPROVED's
subsequent state — the daemon's in-process `process_merge_queue.py`
and `merge_tracker` handle APPROVED → MERGED / CLOSED transitions.
`TERMINAL_STATES = {MERGED, CLOSED}`.

**Post-merge cherry-pick window** (DESIGN §1.24). On merge the bot discovers
the live release branches (`rc_*` / `rc_next_*` / `rc_dev_*`, highest number
per family wins) and, if any exist, posts the token→branch mapping and holds the topic
**out of `closed/`** for 24 h so the approver can answer:

```
MERGED ──(approver: "p1" / "p1 p2")──→ cherry-pick, then archive
       ──(approver: "no")────────────→ archive
       ──(24 h, no reply)────────────→ archive (dispatcher janitor)
```

`MERGED` stays terminal — the topic is finished, merely still addressable.
The state admits **only** `cherrypick` / `cherrypick_skip`; an `ok` or
`close` arriving there is dropped (they would act on already-merged MRs).
Tokens are numbers resolved against the *active* set, so `p1` means `rc_p1`
when `rc_p1` is the live `rc_*` branch — not `rc_next_p1`, which is dead.
Direct cherry-pick is tried first; a protected branch or a conflict falls
back to an auto-created MR. No release branches discovered → no offer, topic
archives immediately as before. `3rd_party/*` merges are excluded from the
offer entirely (§1.24.2) — those libs have their own release versions.

The offer @-mentions `REVIEW_BOT_CHERRYPICK_DECIDER_OPEN_ID` (the release
owner), falling back to the primary approver when unset. Mention only —
any open_id in `REVIEW_BOT_APPROVER_OPEN_IDS` may still answer.

## Per-Topic Agent Contract

Each sub-agent receives a single `TOPIC_FILE` and processes it end-to-end.
The prompt template is at `{paths.scripts}/spawn_topic_agent.md`.

**Inputs**: `TOPIC_FILE`, `LOCK_FILE`, `THREAD_ID`, `TICKET_ID`,
`POLL_CYCLE_ID`, `APPROVER_OPEN_ID`, `CHAT_ID`, `CHAOS_REPO_ROOT`,
`RAGE_REPO_ROOT`, `SKILL_DIR`.

**Lock acquisition**: `topic_store.acquire_lock(thread_id, cycle_id)` via
`O_CREAT|O_EXCL`. If locked and fresh (<10 min), exit with
`{"status":"skipped_locked"}`. Release before returning.

**Note**: Some operations (file creation via `O_CREAT|O_EXCL`, directory creation, subprocess calls) may trigger Claude Code's tool authorization prompts even with Bypass Permissions enabled. The **Required Permissions** section above lists all tool categories that must be pre-authorized at session start for autonomous operation.

**Drain `events.pending[]`** in order — classify, execute side-effects,
transition state, append `audit[]`, write atomically per event.

### Action: `new_topic`

**Assigned developer**: the operator can file on behalf of someone else by writing `RAGE-XXXXX @<dev-name>` in the root message. `ack_new_topic` runs `event_utils.extract_assigned_dev` against the root: structured Lark `@`-mention via picker → use the mentioned user's `open_id`; literal text `@<name>` → look up in the org-contacts cache; neither → fall back to the sender. The resolved developer becomes `identity.creator_open_id` (used as the @-target in templates) and `identity.developer` (display name); the original sender is preserved as `identity.filed_by_open_id` for audit. All bot-to-developer replies (`revision_request`, `no_new_commits`, `merged`, and the ack-time close notices) inherit this routing automatically. See DESIGN.md §1.20.

**Approver fast-track**: an approver can skip the review entirely by posting `RAGE-XXXXX ok` (optionally with `@<dev>` mixed in) as the root message. Authorization gates on the **original** `event["sender_id"]` against `env.approver_open_ids` — not `identity.creator_open_id`, since `@<dev>` rewrites the latter. The match is strict and literal: lowercase ASCII `ok` only. Anything else (extra prose, `pass`, `通过`, `OK`) falls through to the normal review flow. Pre-checks (no MR, MRs merged/closed, `version_3rd` mismatch) still close the topic with the standard ⚠️ message — fast-track only skips the review, not the safety nets. The approver branch runs in `ack_new_topic._fast_track_approve`: glab-approves every MR, drains the root event from `events.pending` (otherwise the dispatcher re-lists the APPROVED topic as work — APPROVED is not terminal), posts the standard `approval` template, transitions to `APPROVED`, and lets `process_merge_queue` handle the merge. 3rd-party phase is supported (skips the main-phase pipeline recheck, posts the fixed `等待合并队列处理。` copy, same as the mechanical 3p-approve path). Audit: `fast_track_approved`. See DESIGN.md §1.21 for the feature and §1.21.1 for the event-drain footgun.

1. Set `review.state = TRIAGING`.
2. **Resolve MRs** via `glab mr list --search "RAGE-XXXXX"` on both repos.
   Write output to temp file (piping corrupts JSON on Windows):
   ```bash
   glab mr list --search "RAGE-XXXXX" -R booming/dev/projects/rage/rage  -F json > "$WRITE_TMP_DIR/gm.json"
   glab mr list --search "RAGE-XXXXX" -R booming/dev/projects/rage/chaos -F json > "$WRITE_TMP_DIR/cm.json"
   ```
   Read with `encoding='utf-8'`. **Bind by title, not the raw `--search`
   result** — `--search` matches the ticket against the MR title AND
   description, so an MR that only mentions the ticket in its body (or a
   sibling ticket's MR cross-referencing it) also comes back. Keep only MRs
   whose **title** carries the ticket token (`RAGE-XXXXX`, allowing a leading
   verb like `Fix RAGE-XXXXX:`), then extract `source_branch` + `iid` from the
   first open match in each repo. Populate the `mrs` dict with an entry per
   found MR (one or both of `"rage"` and `"chaos"`). **Never reconstruct branch
   name from ticket ID** — trust `source_branch` from JSON. If no title-matched
   MR in either repo → post `⚠️ 未找到 <ticket> 对应的 MR。…` (@-mentioning the
   requester) → `CLOSED`. See DESIGN §1.3.5.

3. **Update state**: for each found MR, set `mrs[repo].mr_iid`,
   `mrs[repo].branch`, `mrs[repo].branch_sha`, `mrs[repo].web_url`.
   Also set `identity.developer`, `identity.creator_open_id`.

4. **Create Lark Base record** (see Lark Base Integration).

5. **Fetch diff**: resolve branch SHA via `ls-remote` (encoding workaround):
   ```bash
   SHA=$(git ls-remote origin | grep "$ticket_id" | awk '{print $1}' | head -1)
   git fetch origin "$SHA"
   git diff origin/$base...$SHA --stat > "$WRITE_TMP_DIR/$ticket_id.stat"
   git diff origin/$base...$SHA        > "$WRITE_TMP_DIR/$ticket_id-r1.diff"
   ```
   `$base` is the MR's `target_branch`; when absent it falls back to the repo's
   master branch from `.claude/cfg/branches.json`. See DESIGN §1.4.1.

6. **Triage** (deterministic — do NOT override with judgment):
   `complex` if: (files > 5 **OR** lines > 100 **within a single repo**) OR schema/codegen
   (.rsd/.nsd/.gsd/.csd) OR architectural. Otherwise `simple`.
   Cross-repo alone does NOT trigger complex.

7. **3rd-party MR discovery and phase routing** (after triage, before routing):
   After searching rage and chaos, also search `3rd_party_cpplibs` group for open MRs:
   ```bash
   glab api "groups/3rd_party_cpplibs/merge_requests?search=RAGE-XXXXX&state=opened" \
     > "$WRITE_TMP_DIR/3rd_party_mrs.json"
   ```
   For each found MR, add to `mrs` with key `"3rd_party/<project_name>"` and include a
   `repo_slug` field (e.g., `"3rd_party_cpplibs/renderdoc"`).

   A **separate `state=merged` probe** on the same group sets a `has_merged_3p` flag.
   A lib MR that landed *ahead* of its consumer chaos/rage MR is a legitimate workflow
   (the lib ships first, the consumer bumps to it). Such a merged MR still satisfies
   the `version_3rd.cmake` requirement, but is **not** added to `mrs` — it is already
   merged, so there is nothing to review or merge, and adding it would wrongly route
   the topic into the 3rd-party phase. It is a separate query so an error on one state
   can't mask the other. See DESIGN §1.3.4.

   **Bidirectional check** (`_compute_version_3rd_check`): Only close if
   `version_3rd.cmake` changed in the chaos MR but NO 3rd-party MR exists **at all —
   neither open nor merged** (`has_merged_3p` false; post `⚠️ 检测到 version_3rd.cmake 版本升级，但未找到对应的第三方库 MR。` → `CLOSED`),
   or the reverse: an **open** 3rd-party MR exists but `version_3rd.cmake` has no changes
   (post `⚠️ 第三方库 MR 已存在，但 chaos 仓库的 version_3rd.cmake 未包含版本升级。` → `CLOSED`).
   `has_merged_3p` relaxes only the first (cmake→3p) direction; the second (3p→cmake)
   still keys on an **open** MR. Every ack-time ⚠️ close @-mentions the requester.
   If a 3rd-party probe errors, the `cmake_without_3p` close is deferred (retry,
   not close) — the verdict is unreliable until the probe succeeds (DESIGN §1.3.6).

   The 3rd-party group probe reads the project path via
   `_project_slug_from_entry` (`project_path_with_namespace` → `references.full`
   → `web_url`). The **group** MR endpoint omits the first field, and reading it
   alone silently dropped every open lib MR → false `cmake_without_3p` closes
   and a 3rd-party phase that never triggered (RAGE-23816).

   **Revivable closes**: `mr_not_found`, `3rd_party_mr_not_found` and
   `missing_version_3rd_bump` ask the developer to fix something and come back,
   so they set `lifecycle.revivable` and append `补齐后在本话题回复即可继续审查。`.
   A later reply in that thread reopens the topic via `topic_revive.try_revive`
   (router Gate 4a) and ack re-runs, capped at 3 revives. `mr_already_merged` /
   `mr_already_closed` are terminal as before. See DESIGN §1.3.7.

   **Phase routing**: If any `mrs` key starts with `"3rd_party/"`, set
   `review.review_phase = "3rd_party"` and review only 3rd-party MRs first.
   Otherwise `review_phase = null`, normal flow.

8. **Route** (the topic agent has no `Task` tool — every review is done inline by the agent itself, never delegated to a sub-sub-agent). Both phases use the inverted triage (DESIGN §1.23, §1.23.5): a round-1 review with ≥1 issue posts `review_round1_dev_triage` (@developer) and lands in `DEV_TRIAGE`; zero-issue reviews keep the legacy `review_round1` → approver-decision states:
   - **Simple** → `INLINE_REVIEW` → review the diff inline → post
     `review_round1_dev_triage` → `DEV_TRIAGE` (zero issues:
     `review_round1` → `TRIAGE_DECISION`).
   - **Complex** → `FULL_REVIEW` → review the full diff inline → create **Chinese**
     Lark doc (title: `代码审查 RAGE-XXXXX`, body in Simplified Chinese) →
     grant view to approver AND developer → post doc link via
     `review_round1_dev_triage` → `DEV_TRIAGE` (zero issues:
     `review_round1` → `AWAITING_APPROVAL`).

   **Critical**: Always grant view permission to BOTH the approver AND the developer. Previous runs have failed to grant developer permission — the developer gets a clickable link but cannot read the doc without this grant.

### Action: `approver_reply`

**Approver namelist gate**: approver intents (`ok` approval, `full`/`完整版`, number indices, `close`/`关闭`) are only honored when `sender_id ∈ env.approver_open_ids`. Replies from any other sender carrying these tokens fall through to the dev-intent rules (`ok` re-review / `@bot ...`) or are dropped with audit `unauthorized_intent`. The namelist comes from `REVIEW_BOT_APPROVER_OPEN_IDS` in settings.json (comma-separated open_ids); the legacy single-approver `REVIEW_BOT_APPROVER_OPEN_ID` is kept as a fallback and used as the primary approver for `@`-mentions in templates. `mechanical_reply_handler._classify` checks `sender_id ∈ env.approver_open_ids` before parsing.

**Approval verb (DESIGN §1.23.2)**: `ok` is the **only** approver approval verb, honored in every decision state — `TRIAGE_DECISION` / `AWAITING_APPROVAL` (final approve), `DEV_TRIAGE` (override), and `ARBITRATION` (accept the dev's triage). `pass`/`通过`/`lgtm`/`approved` are **no longer recognized** (removed — `_APPROVER_OK_STATES` + the `ok` token is the whole gate). An approver `ok` is NOT treated as approval in `SIMPLE_REVISION`/`FULL_REVISION` — there `ok` is the developer's re-review trigger; the approver has no approve verb during revision (only `close`), and approves at `AWAITING_APPROVAL` after the developer hands off with `done` (DESIGN §1.23.7).

**Indices format**: digit runs separated by whitespace, comma, or 中文逗号 — `1 3 5`, `1,3,5`, `1，3，5`, `1, 3, 5` all parse to `[1, 3, 5]`. A single all-digit token (`1` or `42`) is one index. Also accepts two shorthand forms:

- `all` (case-insensitive) — flag every issue. Equivalent to typing every index.
- `-N` (negative prefix) — exclude N from the flagged set. `-1 -3` means "flag every issue except #1 and #3"; `all -1 -3` is the explicit equivalent.

Mixed positive+negative (`1 -2`) and `all`+positive (`all 1`) are ambiguous and reject as `unknown`. `0` / `-0` reject (issue indices start at 1). An exclude list that empties the flagged set (e.g. `-1 -2 -3` on a 3-issue review) also rejects — use `ok` to approve. Regex (extended): `^\s*(?:all|-?\d+)(?:[\s,，]+(?:all|-?\d+))*\s*$` followed by `parse_indices_with_mode` validation. The classifier returns `{indices, exclude}`; `exclude=True` with `indices=[]` is the bare-`all` form. See `reply_parser.parse_indices_with_mode`.

Parse based on current state:

| State | Input | Action |
|-------|-------|--------|
| `TRIAGE_DECISION` | `ok` | → `APPROVED` + approve MR |
| `TRIAGE_DECISION` | `full/完整版/完整审查` | → `FULL_REVIEW` (escalate) |
| `TRIAGE_DECISION` | `close/关闭` | → `CLOSED` + close MR |
| `TRIAGE_DECISION` | number indices `1,3,5` | → `SIMPLE_REVISION` + notify dev |
| `AWAITING_APPROVAL` | `ok` | → `APPROVED` + approve MR |
| `AWAITING_APPROVAL` | `close/关闭` | → `CLOSED` + close MR |
| `AWAITING_APPROVAL` | number indices | **reinstate** the dev-rejected issues when `dev_triage.rejected_indices` is non-empty (DESIGN §1.23.9), else a plain re-flag → `SIMPLE_REVISION`/`FULL_REVISION` + notify dev |
| `AWAITING_APPROVAL` | `full/完整版/完整审查` (only `review.triage == "simple"`) | → `FULL_REVIEW` (agent re-reviews, sets `triage="complex"`) → `DEV_TRIAGE` |
| `DEV_TRIAGE` | `ok` | → `APPROVED` (override — skip the dev loop entirely) |
| `DEV_TRIAGE` | number indices | dropped (`ignored`) — the dev triages first; the approver decides at hand-off |
| `ARBITRATION` | `ok` | accept dev triage: fix set non-empty → `SIMPLE_REVISION`/`FULL_REVISION` (by `review.triage`) + notify dev; fix set empty → `APPROVED` (`ok` doubles as approval, DESIGN §1.23.2) |
| `ARBITRATION` | number indices `2,4` | reinstate dev-rejected issues → `SIMPLE_REVISION`/`FULL_REVISION` + notify dev with the final list |
| `ARBITRATION` | `full/完整版/完整审查` (only `review.triage == "simple"`) | → `FULL_REVIEW` (agent re-reviews, sets `triage="complex"`) → `DEV_TRIAGE` |
| `ARBITRATION` | `close/关闭` | → `CLOSED` + close MR |

**3rd-party phase handling** (when `review.review_phase == "3rd_party"`):

| State | Phase | Input | Action |
|-------|-------|-------|--------|
| `TRIAGE_DECISION` | `review_phase == "3rd_party"` | `ok` | Approve + immediately merge 3rd-party MR(s) via `merge_tracker`. Reset `review_phase="main"`, `state=TRIAGING`, `round=0`. Post `✅ 第三方库 MR 已合并。正在继续审查主仓库代码...` |
| `TRIAGE_DECISION` | `review_phase == "3rd_party"` | number indices | → `SIMPLE_REVISION` + notify dev |
| `TRIAGE_DECISION` | `review_phase == "3rd_party"` | `close/关闭` | → `CLOSED` + close ALL MRs |

See the **Reply instructions** in the Review Post Template section below for the exact wording shown to the approver.

**MR Approval** (approve + pipeline check):
Approve ALL MRs in `mrs` — iterate `mrs.items()` and call `glab mr approve` for each:
```bash
glab mr approve "$iid" -R "$repo"
```
Then check each MR's `head_pipeline` status via glab api. Set `mrs[repo].pipeline_status` field:
- "success" → pipeline_status = "passed"
- "running"/"pending" → pipeline_status = "running"
- "failed"/"canceled" → pipeline_status = "failed", post warning to dev

Game repo has no CI pipeline — treat missing `head_pipeline` field as `pipeline_status = "passed"`.

Post approval using the `approval` template. Do NOT merge or rebase — the daemon's in-process `process_merge_queue.py` handles that.

**MR Close**: Close ALL MRs in `mrs` — iterate `mrs.items()` and call `glab mr close $iid -R $repo` for each. Post `MR ` → `CLOSED`.

### Action: `dev_triage`

Sender = developer (matches `identity.creator_open_id`), state = `DEV_TRIAGE` — after **every** review post, not just round 1, either phase (DESIGN §1.23.6). Content is the dev's triage of the bot's `#N` issue list — **indices = issues the dev will fix**; unlisted = pushed back. From round 2 on, only issues still **open** are up for decision: anything already verified fixed, or already rejected in an earlier round, is not re-asked (and naming it is an invalid index). Indices may be followed by a free-text reason — `-2 -3 这两个是误报` — which is stored per round and shown to the approver at hand-off (DESIGN §1.23.8):

| Dev reply | Meaning |
|-----------|---------|
| `1 3 5` | fix these, reject the rest |
| `all` | fix everything |
| `-2` / `-1 -3` | reject the listed issues, fix the rest |
| `none` / `0` / `不修` | reject all (nothing to fix) |

Handled **mechanically** (no agent spawn): `mechanical_reply_handler._handle_dev_triage` records `review.dev_triage = {accepted_indices, rejected_indices, reinstated_indices, reasons}` as running sets across rounds, then posts `revision_request` to the dev and transitions straight to `SIMPLE_REVISION`/`FULL_REVISION` (by `review.triage`); audit `dev_triage_recorded`. **Rejections no longer escalate** — the approver never sees them until the dev replies `done` (DESIGN §1.23.6). Naming an index in a later round accepts it, clearing an earlier rejection. Two events are dropped with a plain-text correction rather than left pending (anti-poison-loop; the dev just re-replies): invalid/closed indices (audit `dev_triage_invalid_indices`) and any attempt to re-reject an issue the approver reinstated, which is locked (audit `dev_triage_reinstated_locked`, DESIGN §1.23.9). If a round leaves nothing to fix, the bot posts a nudge and holds in `DEV_TRIAGE` (audit `dev_triage_all_rejected`). A dev `ok` in `DEV_TRIAGE` is dropped — triage needs indices. See DESIGN §1.23.1.

### Action: `dev_handoff`

Sender = developer, content is `done` / `submit` / `提审` / `完成`, state ∈ `DEV_TRIAGE` / `SIMPLE_REVISION` / `FULL_REVISION`. This is the **only** thing that ends the self-service loop and puts the topic in the approver's court. Handled mechanically: `_handle_dev_handoff` posts the `handoff_summary` template @approver — the fix ledger plus every disputed issue with the dev's stated reason — and transitions to `AWAITING_APPROVAL`; audit `dev_handoff`. Gated to the topic's developer, so a bystander cannot submit someone else's branch. See DESIGN §1.23.7.

### Action: `dev_question`

Sender = developer, content **starts with `@bot`** (literal text or a Lark `@`-mention to the bot user) — fires in any reply state, including `DEV_TRIAGE` / `ARBITRATION` (the dev-triage round-1 footer explicitly invites it). Any other prefix from the developer is NOT a question and is dropped (see `dev_reply` rules below).

The dispatcher posts a mechanical ack (`🤖 已收到 ... 的提问，正在查证，稍后回复…`, `ack_dev_question` template) within seconds of the question arriving; the event stays pending and the topic agent still owns the actual answer below. See DESIGN §1.3.3.

1. Re-read code at current SHA to understand the context.
2. Explain the rationale behind the review finding in Chinese. Do NOT transition state or remove issues from `flagged_issues` — the approver decides what to accept.

### Action: `dev_reply` / dev `close`

Sender = developer (matches `identity.creator_open_id`), content (after Lark @-mention stripping) is **exactly the literal token `ok`** (case-insensitive, ignoring leading/trailing whitespace and trailing newlines) while in `SIMPLE_REVISION` or `FULL_REVISION`. The `ok` token signals "fixes pushed, please re-review".

The developer can also reply `close` / `关闭` / `no` to terminate their own topic — same effect as the approver `close` action (closes all MRs, posts the close-notice, transitions to `CLOSED`). The router authorizes this by matching `sender_id == identity.creator_open_id`; random group members typing `close` are dropped at the router (no MR-killing footgun). The audit entry is `developer_close` (vs `approver_close` for the approver path).

Developer messages that are anything *other* than `ok`, `@bot ...`, or `close` are dropped at the router with audit `reply_intent_ignored` — they do NOT trigger a re-review and do NOT spawn an agent. This is the explicit narrowing of the legacy "anything from dev counts as dev_reply" behavior; pushing commits without posting `ok` leaves the topic in `*_REVISION` (consistent with §1.18.1 — the bot has no GitLab webhook, the thread message is the trigger).

**Rebase-conflict resume (`ok` in `APPROVED`)**: a dev `ok` is *also* meaningful on a topic parked by a merge-queue rebase conflict (`review.rebase_conflict_blocked`). That `ok` is consumed **mechanically** by `mechanical_reply_handler.drain_rebase_conflict_ack` — never by an agent, and the parent must NOT treat it as a §1.18.2 undefined-row drop. The drain SHA-verifies the push and either resumes the merge (`merge_resuming`) or asks the dev to push first (`rebase_no_push`). See DESIGN §1.6.7.

1. Refresh `mrs[repo].branch_sha` (via `glab api projects/<slug>/merge_requests/<iid>` — see §1.4.2). If every repo's head equals its `last_review_commit[repo]` (per-repo dict — see §1.4.5), post `no_new_commits` template and stay (developer typed `ok` but pushed nothing).
2. Run the incremental review inline (the topic agent has no `Task` tool — no sub-agent). Resolve each repo's diff base from `last_review_commit[repo]` and ancestry-check it: if the stored base is no longer an ancestor of the head (rebase / force-push), diff three-dot against the repo's target branch instead of two-dot against the orphaned base. See §1.4.5.
3. **Big-picture verification**: run it only on flagged issues an earlier round
   has NOT already settled (`addressed` / `obsolete`) — the spawn Context lists
   the `to_verify` and `carried` indices; carried ones keep their verdict and are
   never re-grepped (DESIGN §1.4.8). For each issue still in scope: before
   marking it `unfixed`, re-grep the **current full file** at `$SHA`. If pattern
   is gone → `fixed (obsolete)`. If still present → `unfixed` with current
   location.
4. **Manual-issue verification** (additive — runs alongside step 3): refresh `review.manual_issues[]` via `gitlab_threads.reconcile_manual_issues`, then for every entry with `verified_at_sha != head_sha` adjudicate it inline using `manual_issue_verifier.py context` output (no sub-agent — the topic agent has no `Task` tool). Persist the `{verification, verification_rationale, verified_at_sha}` triplet per issue. When the verdict is `addressed` or `obsolete`, also call `gitlab_threads.mark_resolved` and stamp `marked_resolved_at` so the GitLab UI tracks the bot's verdict. See DESIGN §1.14 / §1.14.1 and `spawn_topic_agent.md` §3a.
5. From `SIMPLE_REVISION` → `TRIAGE_DECISION`. From `FULL_REVISION` →
   append to Lark doc → `AWAITING_APPROVAL`.

### Action: `manual_refresh`

Sender = anyone (developer, approver, or any group member), content matches `@bot 同步` / `@bot refresh` / `@bot sync` — fires in any reply state (`TRIAGE_DECISION`, `AWAITING_APPROVAL`, `DEV_TRIAGE`, `ARBITRATION`, `SIMPLE_REVISION`, `FULL_REVISION`).

Use case: a human added inline review comments to the MR (or new comments since last sync) and wants the bot to acknowledge / verify them, separately from the dev's `ok` push trigger.

1. Re-fetch GitLab MR threads via `gitlab_threads.fetch_for_topic`, reconcile against `review.manual_issues[]`.
2. For each manual issue with `verified_at_sha != current_head_sha`, adjudicate it inline with `manual_issue_verifier.py context` output (no sub-agent). Idempotent — already-verified entries at HEAD are skipped.
3. Post a `review_roundN` artifact summarising the manual section only — bot's own findings are NOT re-reviewed (use `ok` for that).
4. State unchanged. Audit entry: `manual_refresh_completed` with the reconcile summary + verification counts.

Short-circuit: if reconcile reports no `added` entries AND every existing entry has `verified_at_sha == head_sha`, post a brief `freeform_reply` `"人工审查无变化（{N} 条均已验证至当前提交）"` and exit without running per-issue verification.

## Review Post Template

All user-facing content in **Simplified Chinese**.

**Template files** are in `{paths.scripts}/templates/`. Agents MUST use these
templates via `python "$SKILL_DIR/scripts/templates/render.py" <name> --vars-file vars.json`
rather than constructing post JSON manually. Available templates:
review_round1, review_round1_dev_triage, review_roundN, revision_request,
dev_triage_summary, approval, no_new_commits, merged, mr_closed, freeform_reply,
cherrypick_prompt (daemon-posted after a merge when live release branches
exist, DESIGN §1.24 — agents never post it),
topic_reopened (daemon-posted by the withdrawn-supersede reopen path,
DESIGN §1.2.9 — agents never post it).

The `merged` / `mr_closed` notices are normally posted by the daemon, not an
agent: the active merge path (`process_merge_queue`) and the passive reconcile
paths (`dispatcher._check_approved_topics`, `recover.py`) all post through
`terminal_notice.post_terminal_notice` so every terminal transition is
reflected in the thread (idempotent — DESIGN §1.6.11).

Structure (each bullet = a paragraph in `post_json.zh_cn.content`):

1. Bold repo header: `<Game|Chaos> 仓库 (<branch>)`. For cross-repo, list each repo header + MR link on its **own paragraph** (separate line) so links are individually clickable.
2. MR link (round 1 only): `{"tag":"a","text":"<repo>!<iid>","href":"https://gitlab.booming-inc.com/booming/dev/projects/rage/<repo>/-/merge_requests/<iid>"}`. For cross-repo, list each repo header + MR link on its **own paragraph** (separate line) so links are individually clickable.
3. Plain: `N 个文件变更，+X / −Y`
4. (blank line)
5. Per changed file: bold filename + `+x/-y` line counts + short Chinese description
6. (blank line)
7. Bold `问题汇总`
8. Per issue: bold `#N  [严重|中|轻] ` prefix + `[Repo] file[:line_range] [function: ]text` (round-1 review list AND the 修改 revision list use the identical line). `repo`/`file`/`text` required; `line_range` required for line-scoped findings and omitted gracefully only for whole-file ones (`[Repo] file: text`); `function` optional; `text` is prose only — `render.build_issue_paragraphs` composes the location prefix. **Full reviews must still populate `line_range`** even though `text` is terse, so the inline list shows where each issue lives. The round-N 问题复查 (`VERIFIED_ISSUES`) keeps its verdict-marker shape, unchanged. See DESIGN §1.9.4.
8a. Optional `人工审查（N 条）` section — populated from `review.manual_issues[]` via the `MANUAL_ISSUES` template var. Each entry rendered as `[M{i}]` (clickable link to the GitLab discussion) + verification marker (`📌 待验证` / `✅ 已修复` / `⚠️ 未修复` / `🟡 部分修复` / `📝 代码已删除/重构` / `❓ 无法判断`) + `[Repo] file.cpp:line — body 短句（author）`. Verification rationale (when present) goes on its own indented paragraph. Manual issues use a SEPARATE numbering space `[M1]`, `[M2]` — they are NOT mixed into the bot's `#N` numbering, because the approver's `1,3,5` index reply only addresses bot findings (manual issues are tracked on GitLab, not via the revision flow). See DESIGN §1.14.1.

Issues MUST be sorted by severity (严重 > 中 > 轻 > 建议) before assigning `#N` numbers. Both the Lark thread post and the Lark doc (for full reviews) must use the same ordering — the full-review doc's `问题详情` section is rendered deterministically from the same `review.issues[]` array via `render.build_doc_issue_markdown` (CLI: `scripts/templates/render_doc_issues.py`), so its `#N` and each `[Repo] file[:line_range] （function）` heading stay in lockstep with the thread reply. Do NOT hand-write the doc issue list. See DESIGN §1.9.5.

**Severity rubric** (this codebase treats conventions as load-bearing — see `/cpp-conventions` and `.claude/rules/`):

| Tier | Meaning | Examples |
|------|---------|----------|
| `严重` | Correctness bugs — must fix before merge | Races, memory corruption, logic errors, security issues |
| `中` | Significant design/rule-pattern violations | Raw `new`/`delete`, `dynamic_cast`, `std::shared_ptr`, architectural concerns, perf regressions |
| `轻` | Localized project-rule violations — fix before merge if practical | Single-letter vars, magic numbers, missing const, misleading names, raw primitives instead of `Chaos::Int`, missing handle wrappers |
| `建议` | Pure opinion — no project rule invoked | Alternative implementation, ordering/style preferences where no rule dictates |

**Key**: naming/convention issues default to `轻`, not `建议`, because `.claude/skills/cpp-conventions/reference/08-naming.md` treats them as blocking. Reserve `建议` for genuinely opinion-based suggestions where no `.claude/rules/` or `.claude/skills/cpp-conventions/reference/` file is being cited.
9. (blank line)
10. The @-mention + reply instructions depend on the template (one choice per paragraph):

**`review_round1_dev_triage`** (round 1 with ≥1 issue, either phase, DESIGN §1.23) — `{"tag":"at","user_id":"$DEVELOPER_ID","user_name":"$DEVELOPER_NAME"}`, then the dev-triage instructions (baked into the template):
  - `· 回复问题序号（如 1 3 5）确认修复这些问题，其余视为有异议`
  - `· 回复 "-2" 表示仅对 #2 有异议，其余修复；回复 "all" 全部修复`
  - `· 回复 "none" 或 "不修" 表示全部有异议（异议将转交审查人裁决）`
  - `· 如有疑问，请以 ` + bot mention + ` 开头提问` — the mention is the builtin `BOT_MENTION_SEGMENTS` render var (a real `at` tag pointing at the bot when `REVIEW_BOT_OPEN_ID` resolves, else literal bold `@bot`; DESIGN §1.9.6)

**`review_round1`** (3rd-party phase, zero-issue reviews, round-N escalate) and **`review_roundN`** — `{"tag":"at","user_id":"$APPROVER_ID","user_name":"审查人"}`, then:
- Simple:
  - `· 回复问题序号（如 1 3 5）标记需修改；回复 "all" 标记全部；回复 "-2 -4" 标记除 #2 #4 外的全部`
  - `· 回复 "ok" 批准`
  - `· 回复 "full" 或 "完整版" 进行完整审查`
  - `· 回复 "close" 或 "关闭" 终止MR`
- Full (no "full" option):
  - `· 回复问题序号（如 1 3 5）标记需修改；回复 "all" 标记全部；回复 "-2 -4" 标记除 #2 #4 外的全部`
  - `· 回复 "ok" 批准`
  - `· 回复 "close" 或 "关闭" 终止MR`

**`dev_triage_summary`** (posted mechanically on dev triage → `ARBITRATION`) — `@审查人`, then accept (`ok`) / reinstate-indices / escalate (simple only) / close instructions; see the template file.

When the topic enters `SIMPLE_REVISION` / `FULL_REVISION` (`revision_request` template), the post addressed to the **developer** also includes:

- `· 修改完成后回复 "ok" 触发下一轮审查`
- `· 提问请以 @bot 开头（如 "@bot 这个问题为什么是中级？"）`

Available in any reply state — open to **anyone** in the thread (developer, approver, group member):

- `· 回复 "@bot 同步" / "@bot refresh" / "@bot sync" — 重新拉取 GitLab MR 上的人工审查评论并验证修复状态`

Use case: the bot's automated review found nothing actionable, but a human added inline comments on the MR after the fact. `@bot 同步` re-fetches those threads and (if the developer has pushed since the last verification) runs per-issue verification. Does NOT re-review the bot's own findings.

Other text from the developer is silently ignored — the bot does not re-review on every comment. Approver verbs (`ok`-as-approval / indices / `close`) typed by a non-approver are rejected by the sender-role gate (a non-approver `ok` in a revision state is just the dev's re-review trigger); audit `unauthorized_intent` where applicable.

Title: `代码审查 RAGE-XXXXX（第 N 轮）`

## Thread Reply Helper

### Plain text

```bash
"$APPDATA/npm/node_modules/@larksuite/cli/bin/lark-cli.exe" \
  im +messages-reply --message-id "$msg_id" --reply-in-thread --as bot \
  --msg-type text --text "content"
```

### Post format (preferred)

```json
{"zh_cn":{"title":"标题","content":[[{"tag":"text","text":"段落"}]]}}
```

| Element | JSON |
|---------|------|
| Bold | `{"tag":"text","text":"label","style":["bold"]}` |
| Link | `{"tag":"a","text":"doc","href":"https://..."}` |
| Blank line | `[{"tag":"text","text":"\n"}]` (own paragraph) |
| Mention | `{"tag":"at","user_id":"ou_xxx","user_name":"name"}` |

```bash
"$APPDATA/npm/node_modules/@larksuite/cli/bin/lark-cli.exe" \
  im +messages-reply --message-id "$msg_id" --reply-in-thread --as bot \
  --msg-type post --content "$(cat $WRITE_TMP_DIR/post.json)"
```

### Recalling a bot message

```bash
"$APPDATA/npm/node_modules/@larksuite/cli/bin/lark-cli.exe" \
  im messages delete --as bot --yes --params '{"message_id":"om_xxx"}'
```

## Lark Base Integration

**Table schema** (create fields one at a time with ~2s delays):

| Field | Type | Notes |
|-------|------|-------|
| Ticket ID | text (url) | `[RAGE-XXXXX](https://jira.boomingtechs.cn/browse/RAGE-XXXXX)` |
| Developer | text | — |
| Game Branch | text | — |
| Chaos Branch | text | — |
| Game MR | text (url) | `[rage!N](https://gitlab.booming-inc.com/.../rage/-/merge_requests/N)` |
| Chaos MR | text (url) | `[chaos!N](https://gitlab.booming-inc.com/.../chaos/-/merge_requests/N)` |
| 3rd-party MR | text (url) | `[<project>!N](https://gitlab.booming-inc.com/3rd_party_cpplibs/<project>/-/merge_requests/N)` |
| Triage Result | select | `simple`, `complex` |
| Review Rounds | number | — |
| Issues Found | number | — |
| Status | select | `REVIEWING`, `APPROVED`, `CLOSED` |
| Review Doc | text (url) | Lark doc URL |
| Created At | datetime | — |
| Resolved At | datetime | — |

Field names with spaces can cause `+record-upsert` failures — verify with
`+field-list` after each `+field-create`.

```bash
lark-cli base +record-upsert \
  --base-token "$app_token" --table-id "$table_id" \
  --json '{"Ticket ID":"[RAGE-12469](...)", "Status":"REVIEWING", ...}'
```

## Activity Logging

**Per-topic** events → `topic.audit[]` in JSON. **Dispatcher-cycle** events →
`cfg/activity.log`. Log rotation at 1MB (keep last 500 lines).

## Rebooting Across Sessions

| Layer | Survives `/clear`? |
|-------|-------------------|
| Listener (detached `node.exe`) | Yes — singleton via `listener.pid` |
| Daemon (`pythonw poll_dispatch.py --watch`) | Yes — singleton via `daemon.pid` |
| Monitor (`monitor_dispatch.py`) | Yes — singleton via `monitor.pid` (new start kills old) |
| `cfg/topics/*.json`, index, events, logs | Yes |
| Claude conversation context | No |

**Important**: all three processes load their Python scripts once at startup.
If you modified any daemon/listener/monitor scripts since the last launch,
kill and relaunch them — they won't pick up file changes automatically.

All three long-lived processes use a unified singleton-via-PID-file pattern
from `subprocess_util.py` (`read_pid_file`, `write_pid_file`, `kill_process`,
`release_pid_file`). On `/clear` → `/review-bot start`, the listener and
daemon are already running; the Monitor self-replaces (kills old PID, writes
its own). First poll cycle reconciles everything from the gap.

## Per-Topic File Schema (v3)

```json
{
  "schema_version": 3,
  "thread_id": "om_...",
  "root_message_id": "om_...",
  "identity": {"ticket_id": "RAGE-XXXXX", "creator_open_id": "ou_...",
                "developer": "...", "chat_id": "oc_..."},
  "mrs": {
    "rage": {"mr_iid": 2511, "branch": "...", "branch_sha": "...",
             "web_url": "...", "pipeline_status": "passed"},
    "chaos": {"mr_iid": 2058, "branch": "...", "branch_sha": "...",
              "web_url": "...", "pipeline_status": "passed"}
  },
  "review": {"state": "...", "triage": "simple", "review_round": 1,
             "review_phase": null, "version_3rd_check": "ok",
             "ack_stats": {
               "rage":  {"file_count": 2, "insertions": 14, "deletions": 3,
                          "files": [{"path":"src/a.cpp","insertions":10,"deletions":2},
                                    {"path":"src/a.h","insertions":4,"deletions":1}]},
               "chaos": {"file_count": 0, "insertions": 0, "deletions": 0, "files": []}
             },
             "last_review_commit": {"rage": "...", "chaos": "..."}, "issues_found": 4,
             "flagged_issues": [4], "lark_doc_token": null,
             "manual_issues": [
               {"index": 1, "discussion_id": "abc...", "note_id": 12345,
                "author": "muhan.liu", "repo": "chaos",
                "file": "...cpp", "line_old": null, "line_new": 50,
                "base_sha": "...", "body": "...", "web_url": "...",
                "verification": "addressed",
                "verification_rationale": "...",
                "verified_at_sha": "...",
                "marked_resolved_at": 1778060230198}
             ],
             "review_history": [{"round":1,"timestamp":0,"commit":"...",
                                 "issues":4,"review_post_message_id":"om_..."}]},
  "events": {"pending": [], "last_processed_event_id": null,
             "last_processed_ts": 0},
  "lifecycle": {"created_at": 0, "updated_at": 0, "resolved_at": null,
                "merge_sha": null, "merge_detected_at": null,
                "closed_reason": null},
  "audit": []
}
```

- `mrs` is a dict keyed by repo slug (`"rage"`, `"chaos"`, or `"3rd_party/<project>"`). Each entry holds `mr_iid`, `branch`, `branch_sha`, `web_url`, and `pipeline_status`. 3rd-party entries additionally have a `repo_slug` field storing the full GitLab path (e.g., `"3rd_party_cpplibs/renderdoc"`) used by `merge_tracker.py`. A topic may have one or more repos present. Example with a 3rd-party MR:
  ```json
  "mrs": {
    "rage": {"mr_iid": 2511, "branch": "...", "branch_sha": "...",
             "web_url": "...", "pipeline_status": "passed"},
    "chaos": {"mr_iid": 2058, "branch": "...", "branch_sha": "...",
              "web_url": "...", "pipeline_status": "passed"},
    "3rd_party/renderdoc": {"mr_iid": 3, "repo_slug": "3rd_party_cpplibs/renderdoc",
                             "branch": "...", "branch_sha": "...",
                             "web_url": "...", "pipeline_status": null}
  }
  ```
- `review.review_phase` controls the two-phase 3rd-party review flow: `null` (no 3rd-party MR, normal flow), `"3rd_party"` (reviewing 3rd-party MRs first), or `"main"` (reviewing rage/chaos after 3rd-party merged). Pre-computed by `ack_new_topic.py` at ack time.
- `review.triage` — `"simple"` or `"complex"`. Pre-computed by `ack_new_topic._compute_triage` from `review.ack_stats` (file count > 5 OR lines > 100 in a single repo, OR a `.rsd/.nsd/.gsd/.csd` schema file is touched). The topic agent MAY promote `simple` → `complex` on architectural grounds that the mechanical rule can't see.
- `review.version_3rd_check` — `"ok"`, `"cmake_without_3p"`, or `"3p_without_cmake"`. Bidirectional consistency check between chaos `version_3rd.cmake` bumps and the presence of `3rd_party/*` MRs. A merged 3rd-party lib MR (probed separately, flagged `has_merged_3p`) counts toward the cmake→3p direction, so `cmake_without_3p` fires only when **neither** an open **nor** a merged lib MR exists (DESIGN §1.3.4). Mismatch states cause `ack_new_topic` to close the topic with the appropriate ⚠️ warning (which @-mentions the requester) — so by the time the topic agent sees the topic, it's always `"ok"`.
- `review.last_review_commit` — **per-repo dict** `{repo: sha}` recording the SHA each repo was at when last reviewed (the base for the next round's incremental diff). Legacy topics may carry a bare scalar string; `incr_base.expected_sha_for` normalizes both (scalar → same SHA for the single repo). Consumers (`incr_cache`, `ack_dev_reply`, the topic agent) ancestry-check the base via `incr_base.resolve_incr_range` and fall back to a three-dot diff vs the target branch when the branch was rebased / force-pushed. A bare scalar on a cross-repo topic is the bug fixed by this dict — see DESIGN §1.4.5.
- `review.ack_stats[repo]` — per-repo diff stats populated by `ack_new_topic._fetch_stats` (via `git diff --numstat`): `{file_count, insertions, deletions, files: [{path, insertions, deletions}]}`. The agent reads per-file counts from `files[]` for FILE_PARAGRAPHS instead of re-running `git diff --stat`. 3rd-party repos are skipped (not checked out locally).
- `review.manual_issues[]` — human GitLab MR review threads (DiffNotes), pulled at ack time by `gitlab_threads.fetch_for_topic` and refreshed on `dev_reply` / `manual_refresh`. Each entry: `{index, discussion_id, note_id, author, repo, file, line_old, line_new, base_sha, body, web_url, verification, verification_rationale, verified_at_sha, marked_resolved_at}`. The bot independently verifies each issue against current HEAD via Sonnet sub-agents (`manual_issue_verifier.py`); GitLab's `resolved` flag is intentionally NOT read as input, but IS set to `true` via `gitlab_threads.mark_resolved` when the bot's verdict is `addressed` or `obsolete` so the GitLab UI reflects the bot's verdict. See DESIGN §1.14 / §1.14.1.
- `review.rebase_conflict_blocked` (bool) + `review.rebase_conflict_shas` (`{repo: sha}`) — set by `process_merge_queue` when a merge-queue rebase hits a content conflict. While set, `_check_approved_topics` skips the topic (no per-cycle rebase retry). A SHA-verified developer `ok` clears both via `drain_rebase_conflict_ack`. Parallel to `review.merge_manual_required` (the merge-failure circuit breaker). See DESIGN §1.6.7.
- `review.dev_triage` — the developer's round-1 triage record (additive, no schema bump): `{"accepted_indices": [1,3], "rejected_indices": [2,4], "reinstated_indices": [4], "decided_at": <ms>, "triggered_by_event_id": "..."}`. Written by `mechanical_reply_handler._handle_dev_triage` in `DEV_TRIAGE`; `reinstated_indices` appears only after an arbitration reinstate. `flagged_issues` keeps its existing meaning (the final fix list round N verifies). See DESIGN §1.23.
- `events.pending[]` is the router's output queue; topic agent drains it.
- No `lock` field — locks are sibling `.lock` files (filesystem-atomic).

## Design Notes

Operator-facing index into `DESIGN.md`, which holds the full rationale
(single source of truth — do not re-duplicate prose here).

- **Autonomous operation** — no user supervision; don't narrate user actions ("you replied no") or say "waiting for trigger". DESIGN §1.15.
- **Reconcile on cold start / listener restart only** (not every cycle); floors at the persisted `last_reconcile_ts`. DESIGN §1.2.6 / §1.2.7.
- **Event-driven daemon, not cron** — OS-detached `wscript`/`pythonw`, silent spawn, zero idle tokens. DESIGN §1.16.
- **Daemon + listener don't hot-reload** — kill and relaunch after editing any of their scripts; a stale daemon logs cycles but never picks up new code. DESIGN §1.16.
- **Merge queue: skip_ci rebase + direct API merge** — never `glab mr merge --auto-merge` / `glab mr rebase`. DESIGN §1.6.6.
- **Rebase conflict → park, wait for dev `ok`, then resume** — no per-cycle retry. DESIGN §1.6.7.
- **Mechanical terminal transitions post an honest thread notice** — the passive reconcile paths (`dispatcher._check_approved_topics`, `recover.py`) post `merged`/`mr_closed` via `terminal_notice` (idempotent) so a manual merge / GitLab-side close / downtime catch-up never leaves the thread frozen. DESIGN §1.6.11.
- **Infra-failure auto-retry** via per-job `POST /jobs/{id}/retry` (not pipeline-level retry). DESIGN §1.6.5.
- **All Lark posts use plain post format**, never card-style; templates enforce this. DESIGN §1.9.1.
- **Two-phase 3rd-party review** — review + merge the 3rd-party MR first, then reset to the main phase. The reset (a) enqueues its own synthetic pending event, since no inbound Lark message drives it, or the main-phase review never dispatches, and (b) **re-resolves the rage/chaos heads + `ack_stats` first** — the ack-time SHAs are hours old by then and the developer has usually pushed during the 3rd-party phase. DESIGN §2 / §1.6.12.
- **Confirmed-fixed issues are not re-verified** — once a round marks issue #m `addressed` (or `obsolete`), it is carried into every later round with that verdict instead of being re-checked. `review_rounds.carry_verification` preserves the verdict across the `flagged_issues` rebuild that an approver revision reply triggers (which used to erase it), and the spawn Context hands the agent the exact `to_verify` / `carried` index lists. Re-flagging an index re-arms it — that is the way back in if a fix gets reverted. DESIGN §1.4.8.
- **Inverted round-1 triage** — round 1 goes to the developer first (`DEV_TRIAGE`), in both the main and 3rd-party phases (§1.23.5); if the dev accepts every issue it skips straight to revision, otherwise the approver arbitrates the dev's rejections (`ARBITRATION`). Both transitions are mechanical. Round N and zero-issue round 1 keep the legacy approver-first decision states. DESIGN §1.23.
- **Single approval verb `ok`** — the approver approves with `ok` in every decision state; `pass`/`通过`/`lgtm`/`approved` are no longer recognized (removed). In `SIMPLE_REVISION`/`FULL_REVISION` `ok` is the developer's re-review trigger, not approval (no approve verb there — only `close`). DESIGN §1.23.2.
- **Withdrawn superseding root reopens its predecessor** — when a duplicate root that superseded a live topic is withdrawn, the dispatcher reopens the superseded topic, backfills its missed replies, and posts `topic_reopened`; Gate 4a closed-thread drops are WARN-logged. DESIGN §1.2.9.
- **Post-merge cherry-pick** — on merge the bot offers the live release branches (`rc_*` / `rc_next_*` / `rc_dev_*`, highest number per family) as `p<N>` tokens and holds the topic out of `closed/` for 24 h; the approver answers `p1` / `p1 p2` / `no`. Direct cherry-pick, MR on conflict or protected branch. Mechanical — no agent spawn. **Cross-repo**: every game repo in `lifecycle.merge_shas` is picked, each onto its own branch name (`rc_p1` in rage, `rage/rc_p1` in chaos); if a repo's branch discovery fails it is named in an ⚠️ line on the offer and audited as `cherrypick_partial` rather than silently dropped. **`3rd_party/*` repos are never offered** — they ship on their own release cadence, and a 3rd-party-only merge gets no offer at all (audited as `skipped_3rd_party`). DESIGN §1.24 / §1.24.1 / §1.24.2.

## Debugging & Replay

For pipeline-level bugs (event dedup, router/drain races, lock handling,
`process_merge_queue` state transitions) you don't need to bother devs
with a real MR — use `scripts/replay.py` to drive the pipeline in a
sandbox directory with external I/O stubbed out.

```
python scripts/replay.py init /tmp/sb --from-closed <thread_id>
python scripts/replay.py inject /tmp/sb --thread <id> --content "ok" --sender approver
python scripts/replay.py pipeline /tmp/sb        # route + drain
python scripts/replay.py merge-queue /tmp/sb     # process_merge_queue with glab/Lark stubs
python scripts/replay.py show /tmp/sb --thread <id>
python scripts/replay.py lock /tmp/sb --thread <id> [--release]
```

`init --from-closed` clones a closed topic back to open, strips per-MR
runtime state (`state`, `pipeline_status`) and `review_phase` so replay
starts fresh. Does NOT cover the LLM topic agent — for agent logic you
still need a real topic.

## Appendix: Script-Enforced Invariants

These are handled inside `scripts/` — do NOT re-solve:

- **UTF-8 everywhere**: every `open()` uses `encoding='utf-8'`, every
  `json.dump` uses `ensure_ascii=False`.
- **Self-message filter** (`router.py` `_is_self_message`): skips the bot's own
  messages by BOTH sender shapes (`cli_*` for reconcile/chat-history, the bot's
  `ou_…` open_id for the listener). DESIGN §1.2.3.
- **Withdrawn-message filter** (`router.py` + `reconcile.py`): skips
  `deleted: true` messages.
- **System-message filter** (`reconcile.py`): skips `msg_type == "system"`
  (group invites/joins) so an unresolvable-root system message can't pin the
  reconcile floor. DESIGN §1.2.4.
- **HTML tag stripping** (`event_utils.py`): removes `<p>...</p>` etc.
- **Crash-safe event routing** (`router.py`): raw file deleted only after
  `topic_store.write_atomic` succeeds.
- **Per-topic lock** (`topic_store.py`): `O_CREAT|O_EXCL`, stale >10 min
  stolen. Topic agents wrap their work in
  `with topic_store.LockKeepalive(LOCK_FILE):` — refreshes the lock
  mtime every 60 s so legitimate long reviews don't trip the stale rule;
  caps at 30 min so the safety valve still fires for hung agents
  (DESIGN §1.8.3).
- **Reply-artifact write + topic writeback** (`finalize_review.py`,
  invoked from `spawn_topic_agent.md` §3 rule 2): the agent builds one
  result JSON; `finalize_review.py` schema-validates the artifact,
  render-validates its vars (`render.py --check-only` — catches missing
  `SUMMARY` + unfilled placeholders before the artifact lands, no
  poison-loop retries), atomic-writes it into `cfg/replies/`, drains the
  event, persists `review.*` + audit, and releases the lock. Agents no
  longer hand-author this glue. The full-review Lark doc is built by
  `build_review_doc.py` (body assembly + `lark_doc_helper` create in one
  call).
- **Reply-artifact retry quarantine** (`reply_dispatcher.py`): per-
  artifact retry count in `cfg/replies/.retry_counts.json`; after
  `RETRY_MAX = 3` failures the artifact moves to
  `cfg/replies/quarantine/` and the topic gets an audit
  `reply_artifact_quarantined`. Counter clears on any terminal
  outcome (DESIGN §1.19.1).
- **Superseded-work donation** (`topic_store.donate_review_to_topic`,
  `reply_dispatcher._attempt_artifact_donation`): when an artifact's
  thread_id resolves to a topic that was archived via
  "superseded by new topic", the dispatcher rewrites the artifact for
  the canonical successor (SHA-gated). Same mechanism applies to a
  drift-detected topic agent that already finished expensive work
  (DESIGN §1.8.3).
- **Event normalization** (`event_utils.normalize_listener_event`): flattens
  websocket payload to `{chat_id, sender_id, thread_id, root_id, content}`.
- **`om_` vs `omt_` normalization**: prefer `root_id` (`om_`) over
  `thread_id` (`omt_`). Mixing creates duplicate topics.
- **Reconcile root resolution** (`reconcile.py`): keys each message by its
  ROOT `om_` id via the bulk `thread_replies` map, never `_resolve_root_id`.
  DESIGN §1.2.5.
- **Withdrawn root auto-close** (`dispatcher._close_withdrawn_topics`):
  closes topics whose root message was withdrawn. If the closed topic had
  superseded another topic for the same ticket,
  `topic_reopen.reopen_superseded_predecessor` undoes the supersede: purges
  any deferred-supersede ledger entry (unconditionally, before
  `_retry_pending_supersedes` can fire), reopens the predecessor at its
  pre-close state, backfills the thread's missed replies as synthesized
  events, and posts the `topic_reopened` notice. Guards fail open on
  transient errors; definitive blocks only (root withdrawn/dead, MR
  merged/closed, rival open topic). Manual recovery CLI:
  `python topic_reopen.py --withdrawn-thread om_... [--dry-run]`.
  DESIGN §1.2.9.
- **Same-ticket supersede** (`router.py`): new root for existing ticket
  closes old topic before creating new one. Undone automatically if the
  new root is later withdrawn (see the reopen bullet above, DESIGN §1.2.9).
- **Closed-thread reply drops are logged** (`router.py` Gate 4a): an event
  whose thread sits in `topics/closed/` is dropped with a
  `closed_topic_reply_dropped` WARN in activity.log and counted as
  `closed_thread_drops` in the router summary — never silently. DESIGN §1.2.9.
- **Rebase-conflict park + ack** (`process_merge_queue.py` +
  `mechanical_reply_handler.drain_rebase_conflict_ack`): a merge-queue
  rebase conflict sets `review.rebase_conflict_blocked` so
  `_check_approved_topics` stops re-queueing the topic; a SHA-verified
  developer `ok` clears it and resumes the merge. No per-cycle retry, no
  agent spawn (DESIGN §1.6.7).
- **No PowerShell or cmd.exe for detached processes**: `Start-Process`
  hangs in MSYS2; `cmd.exe /c start /min` creates a visible console.
  Use Python `subprocess.Popen` with
  `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`.
- **lark-cli stderr output**: all output goes to stderr. Listener redirects
  with `2>&1`. Health check checks both `.log` and `.err` mtime.
