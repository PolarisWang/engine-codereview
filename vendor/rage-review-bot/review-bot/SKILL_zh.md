---
name: review-bot
description: "飞书话题群自动代码审查机器人。监控飞书群中的审查请求（Jira 单号），运行 /review，管理审批流程。用法：/review-bot start | status | stop | recover"
---

飞书话题群自动代码审查机器人：监控飞书群中的审查请求、对每个变更进行 triage、
通过子 agent 运行审查，并驱动 审批/修订/关闭 循环，状态通过每话题一份 JSON
文件和飞书多维表格跟踪。

**需保持同步的三份文档**（任何行为改动都要在同一 commit 内覆盖完）：

| 文件 | 面向 | 是否规范源 |
|------|------|------------|
| `SKILL.md`（英文） | Operator 规范 —— 命令、模板、运行时契约 | ✅ 先改这里 |
| `SKILL_zh.md`（本文） | `SKILL.md` 的中文镜像，同时是下面飞书文档的源 | SKILL.md 稳定后整篇重译 |
| `DESIGN.md` | 架构 + 坑位 —— 解释管道背后的 *为什么* | 改动背后的理由需要留档时，更新 §1.* 或 §3.* |

**中文参考文档**：[Review Bot Skill 技术文档（中文版）](https://www.feishu.cn/docx/Ly0ydTHMYoBKZ7xYpEQcWgWonqC)。
更新 `SKILL_zh.md` 后，用这一条命令上传：

```bash
python "{skill_dir}/scripts/upload_zh_doc.py"      # 加 --dry-run 可先预览
```

**不要**手敲 `lark-cli docs +update` —— 这个脚本存在的原因是每次手动都会踩两个坑：

- **体积**：文件已约 80 KB。只有 `--markdown "@<相对路径>"` 可行（argv 里只走路径，正文从磁盘流式读取）。内联 `--markdown "<content>"` 会撞上 Windows `CreateProcess` 的 ~32 KB argv 上限（`WinError 206`），走 stdin（`-`）则会静默截断。`@` 路径必须是**相对**路径，所以上传时 `cwd` 设为 skill 目录。
- **frontmatter**：`SKILL_zh.md` 开头的 YAML frontmatter 是 skill 加载器的元数据，不是文档内容。原样上传时，结尾的 `---` 会把 `name:` 那行变成 setext 标题并把 `description:` 吞进去，于是文档开头出现一行错乱的 `## name: review-botdescription: "…"`。脚本会剥掉这段、上传正文、再删掉临时副本。

frontmatter 缺失/未闭合，或响应里没有 `"success": true` 时，脚本会显式报错退出 —— 要防的就是"看起来成功了的半截上传"。只改一小段时仍可用 `--mode replace_range` / `insert_after` + `--selection-by-title` 定向更新，但默认走全文覆盖。

## 参数

- `$ARGUMENTS` —— 命令：`start [--silent] | status | stop [--silent] | recover`

| 命令 | 用途 |
|---------|---------|
| `start` | 启动 listener + daemon + monitor，初始化状态，发送问候消息 |
| `start --silent` | 同 `start`，但跳过飞书群中的问候消息 |
| `status` | 显示活跃话题、listener、最近日志 |
| `stop`  | 关闭 listener + daemon，显示最终状态，发送告别消息 |
| `stop --silent` | 同 `stop`，但跳过飞书群中的告别消息 |
| `recover` | 对所有开放话题强制以 GitLab API 做一次对账 |

## 所需权限

Review Bot 是全自动运行的。任何未预授权的工具调用都会触发 Claude Code
的权限提示，导致 bot 停摆。`start` 命令应在会话开始时一次性把后续所有
轮询和子 agent 都会用到的权限请好。

**Bash 命令**（全部需要预授权）：
- `glab` —— GitLab API：`mr list`、`mr approve`、`mr close`、`api`（MR 状态 / 流水线 / merge PUT）
- `git` —— fetch、diff、log、ls-remote、show（分支解析、diff 生成）
- `node` / `lark-cli` —— `im +messages-reply/send`、`drive permission.members create`、`drive permission.public update`（best-effort；当前 lark-cli 已下掉该子命令）、`base +record-upsert`、`docs +create/+update`
- `python` —— 所有脚本：`parse_args.py`、`resolve_start.py`、`status_report.py`、`topic_store.py`、`reply_parser.py`、`state_machine.py`、`render.py`、`merge_tracker.py`、`dispatcher.py`、`start_listener.py`、`poll_dispatch.py`、`stop_bot.py`、`lark_doc_helper.py`、`build_review_doc.py`、`finalize_review.py`、`restart_bot.py`
- `powershell` —— `stop_bot.py` 在 stop 时用 `Get-CimInstance Win32_Process` 按命令行扫描残留的 daemon/listener 进程
- `wscript` —— 通过 VBS 启动 daemon
- `taskkill.exe` —— 停止进程（stop 模式；`stop_bot.py` 内部即调用它）
- 输出重定向（`>`）—— 临时文件写到 `$WRITE_TMP_DIR`

**目录创建**（历史会触发权限提示的情况）：
- 通过 `resolve_start.py` 或 `topic_store.py` 创建 `cfg/topics/`、`cfg/topics/closed/`、`cfg/events/`

**文件写入**（通过 Bash 重定向或 Python 脚本）：
- `$WRITE_TMP_DIR/*.json`、`*.diff`、`*.stat`（临时文件）
- `cfg/topics/*.json`（话题状态，原子写 + rename）
- `cfg/open_topic_index.json`、`cfg/*.pid`、`cfg/*.lock`、`cfg/sessions.json`

**Agent / Monitor**：
- 每周期最多并发 4 个话题 Agent（合并队列跑在 daemon 进程内，不走 Claude）
- 1 个持久 Monitor，监听 `$WRITE_TMP_DIR/poll_trigger.txt`

## 第 1 步：解析参数并校验环境

```bash
python "{skill_dir}/scripts/parse_args.py" {$ARGUMENTS}
```

JSON 输出若包含 `"error"` 字段，报告给用户然后停止。
输出里有 `command`、`env`（所有解析后的环境变量）、`paths`
（所有绝对路径）和 `listener` 状态。

**绝不要把 open_id 硬编码进 post payload。** 审查人永远从 `env.approver_id`
取，开发者永远从话题文件里存的 `creator_open_id` 取。

## 第 2 步：执行命令

按 `parse_args` 输出的 `command` 分支。

---

### `start` 模式

#### 2a. 解析前置条件

```bash
python "{skill_dir}/scripts/resolve_start.py" --params-json '{parse_args_output}'
```

这一步创建目录、初始化 `open_topic_index.json`、解析飞书 Base 配置，并把本会话
父进程 `claude.exe` 的 PID 登记到 `cfg/sessions.json`，好让每天 08:00 的重启能
把它关掉 —— 手动启动的会话同样适用（DESIGN §1.1.5）。读取 JSON 输出；若
`session.pid` 为 `null`，说明父进程没找到，这个会话会活过下一次重启。

#### 2b. 启动 listener

**默认（OS 级 detached，无弹窗，跨 `/clear` 存活）：**

```bash
python "{paths.scripts}/start_listener.py"
```

默认模式用 `subprocess_util.detached_popen` 加
`DETACHED_PROCESS | CREATE_NO_WINDOW`，在 OS 层面拉起
`node lark-cli event +subscribe`。窗口隐藏由
`CREATE_NO_WINDOW` 在 OS 层面完成，**不依赖 Claude Code 的
spawn 行为**，因此在所有 Claude Code 版本上都不会弹窗（不依赖
anthropics/claude-code#14828 的 windowsHide 修复）。生命周期由
OS 进程决定，不绑定 Claude Code 会话 —— listener 跨 `/clear`、
跨会话退出、跨 parent 崩溃都存活。每日 Windows 计划任务 stop（§2e）
是规范的关停路径。`dispatcher.py` 的 `_restart_listener()` 用同一套
`detached_popen` 在 daemon health-check 时重启 listener，因此
中间的所有重启也都不会弹窗。

**备选（Claude-Code 托管）：** 仅在你**明确**希望 listener 在
Claude 会话结束时自动死掉（比如交互式 sandbox 测试需要 `/clear`
干净 tear-down）时才用：

```bash
python "{paths.scripts}/start_listener.py" --foreground
```

配合 Bash 工具的 `run_in_background: true`。`--foreground` 走完
同样的前置工作后 `execvp` 到 listener，让 Claude 的 background
task 本身成为 listener。**注意**：在没有 windowsHide 修复
（anthropics/claude-code#14828）的 Claude Code 版本上，这条路径
**会**在 start 时和每次 `/clear` 后弹一下 `node.exe` 控制台。
没有特殊原因，请用默认路径。

#### 2c. 创建飞书 Base（当 `base.need_create` 为真时）

若 `resolve_start` 返回 `base.need_create: true`，**先跟用户确认**
再创建。需展示：Base 名 `代码审查跟踪`、表名 `代码审查记录`。
用户同意后，按下方 **Lark Base 集成** 中的 schema 创建。
把 `app_token`/`table_id` 同时写入 `{paths.base_config}` 和
`.claude/settings.local.json`。

#### 2d. 启动事件驱动 daemon + Monitor

先确保触发文件存在，然后启动 daemon。daemon 是**单例** ——
`poll_dispatch.py --watch` 启动时检查 `cfg/daemon.pid`，若已有活进程
持有则立刻退出，所以重拉是安全的。

**默认（wscript 拉起的 OS 级 detached daemon，无弹窗，跨 `/clear` 存活）：**

```bash
touch "$WRITE_TMP_DIR/poll_trigger.txt"
wscript //nologo "{paths.scripts}/run_poll.vbs"
```

`run_poll.vbs` 通过 `WshShell.Run` 以 `WindowStyle=0`（隐藏）、
`WaitOnReturn=False` 调起 `pythonw poll_dispatch.py --watch`，得到
一个完全 OS 级 detached 的 daemon。窗口隐藏由 WSH 在 OS 层面完成，
**不依赖 Claude Code 的 spawn 行为**，因此在所有 Claude Code
版本上都不会弹窗（不依赖 anthropics/claude-code#14828 的
windowsHide 修复）。生命周期由 OS 进程决定，不绑定 Claude Code
会话 —— daemon 跨 `/clear`、跨会话退出、跨 parent 崩溃都存活。
每日 Windows 计划任务 stop（§2e）是规范的关停路径。

**备选（Claude-Code 托管）：** 仅在你**明确**希望 daemon 在
Claude 会话结束时死掉时才用 Bash 工具的 `run_in_background: true`：

```bash
touch "$WRITE_TMP_DIR/poll_trigger.txt"
pythonw "{paths.scripts}/poll_dispatch.py" --watch
```

**注意**：在没有 windowsHide 修复的 Claude Code 版本上，这条路径
会在 start 时和每次 `/clear` 后弹一下 `pythonw.exe` 控制台。
没有特殊原因，请用默认路径。

然后用 dispatch wrapper 脚本起一个持久 Monitor。wrapper 会跳过
既有的历史行（backlog），只在每次触发时读 dispatch plan，仅在
真有可执行工作时才发出通知 —— 每条通知都是一个自包含的 JSON，
带 ticket ID 和文件路径。

```python
Monitor(
    description="Review bot dispatch",
    persistent=True,
    timeout_ms=3600000,
    command='python "{paths.scripts}/monitor_dispatch.py"',
)
```

**关键 —— dispatch 循环契约**：每条 Monitor 通知都是一个 JSON 对象，
包含 `work`、`merge_queue`、`plan_file`。**每一条**通知你都必须：

1. 从通知事件文本里解出 JSON
2. 用 `render_spawn_prompt.py --all` 渲染 spec（它会读 `plan_file`）
3. 按返回的每个 spec spawn 一个话题 agent（锁新鲜的话题已被预过滤掉）
4. 这就是**主自动循环** —— 不要等待用户输入

`merge_queue` 和 `3p_merge_queue` 仅为遥测字段 —— daemon 内的
`process_merge_queue.py` 进程内处理所有 rebase + merge + 归档，
**不需要** Claude spawn。它们出现在通知 payload 里只是让你看到
daemon 正在合并什么。

daemon 只在真有工作时才触发（30 秒冷却）。过量 spawn 也是安全的：
每话题锁会让重复的 agent 立即以 `skipped_locked` 退出。

**spawn 话题 agent**：

确定性地渲染 spawn spec —— 不要手搓提示，也不要凭眼睛选模型：

```bash
python "{paths.scripts}/render_spawn_prompt.py" --all
```

它读 `cfg/dispatch_plan.json`，输出 `{cycle_id, count, specs[], skipped[]}`。默认
`ref` 模式下每个 `spec` 刻意做得很小 —— `{thread_id, ticket_id, model,
description, spec_file, prompt}` —— 因为这份输出每个周期都会进入**你自己**的上下文。
解析好的输入写在 `spec_file`（`cfg/spawn/<thread>__<cycle>.json`）里，`prompt` 只是
指向它和契约文件；agent 在它自己的上下文里读这两份。**不要自己去读 `spec_file`** ——
派发循环里没有任何一步需要它，读了就等于把这个模式想省掉的开销又加回来
（DESIGN §1.4.6）。

`--all` 最多覆盖 `parallel_limit`（= 4）个工作项，并丢弃 `.lock` 仍被新鲜持有的话题
（在 `skipped[]` 里以 `skip_reason: "lock_held_fresh"` 报告），这样就不会对正在运行的
agent 重复 spawn。单个话题用 `--thread-id <om_...>`（返回单个 spec 对象）。

**在同一条消息里**按每个 `spec` 起一个 Task 调用：

```
plan = json.loads(<render_spawn_prompt.py --all 输出>)
for spec in plan["specs"]:          # 已按 parallel_limit 截断、按锁过滤
    Agent(
        model=spec["model"],         # 由 helper 选定（当前所有 event 类型都是 opus）
        description=spec["description"],
        prompt=spec["prompt"],
        run_in_background=True,
    )
```

模型映射（当前所有 event 类型 → `opus`；主阶段审查人的
`approve`/`revision`/`close` 永不到 agent —— 由 `mechanical_reply_handler.py` 进程内处理）
写在 helper 的 `_MODEL_BY_EVENT` 里，不在这里。`--mode compact` 会把输入内联，
`--mode full` 会把整份渲染后的契约内联；两者每次 spawn 都要多花你好几倍的上下文，
只用于调试，不用于派发循环。每个子 agent 都按下文 **每话题 agent 契约** 处理一个话题。

**不要再记录单次 spawn 的 token 遥测。** dispatcher 已经在每个周期结束时运行
`collect_session_tokens.py`，它会从 subagent transcript 里还原出同样的
`spawn_tokens` 记录（按 agent_id 经 `cfg/token_ledger.json` 去重）。在这里再调
`activity_logger.py --tokens` 属于重复劳动，且每次 spawn 白白多花一个工具往返 ——
见 DESIGN §1.4.7。`--mode compact/full` 仍会输出可直接运行的 `telemetry.argv`
供临时手工使用。

**Agent 的返回只有一行 JSON。** 契约（§6）禁止散文式总结；一次 spawn 的结论是通过
飞书话题和文档抵达受众的，不是通过你的上下文。若某个 agent 仍然返回了散文，不要
转述或再总结 —— 按那行 JSON 处理，然后继续。

**采集实际运行时基线**：上面的遥测行会落到
`{paths.cfg}/activity.log`，格式是 `[ts] [INFO] spawn_tokens {JSON}`，
和现有 dispatcher 条目混排（日志 1 MB 滚动，保留最近 500 行，
稳定流量一周内不丢）。要看这段窗口内的 token 花费：

```bash
# 按 state / event_type 分桶汇总
python {paths.scripts}/status_report.py \
    --params-json '{"paths":{"topics_dir":"…","cfg":"…","activity_log":"…/activity.log"}}' \
    --token-summary

# 仅看最近 N 次 spawn（例如 T1+T2 落地后）
python {paths.scripts}/status_report.py --params-json '…' \
    --token-summary --max-entries 200
```

输出是按分桶 key 的 JSON dict
（`{"event_types":{"dev_reply":{"spawns":N,"input_mean":…,"input_p50":…,"input_p95":…},…}}`），
可以贴进飞书文档、做跨运行对比、或导进表格。要做持久的前后对比，
每次有意义的落地后把原始 `spawn_tokens` 行快照到一份 fixture：

```bash
grep "^\[.* spawn_tokens " {paths.cfg}/activity.log \
    > {paths.cfg}/baselines/pre_t1.jsonl   # 文件名按 milestone 命名
```

之后（把 fixture 恢复到 `activity.log`，或用一次性 `--params-json`
指向 fixture 路径）就能算出前后 delta，且不受日志滚动影响。
delta 才是判断一个策略（drain_no_new_commits、incr_cache、
未来的 T3/T4）是否真的把单次 spawn 花费降下来的证据。

**从 Claude Code transcript 做离线回填**（`scripts/collect_session_tokens.py`）：
会话中的 `activity_logger.py --tokens` 只有在父 session 记得在每次
spawn 后去调用时才跑得上。想要无损入账，直接用 Claude Code 的
subagent transcript：

```bash
python {paths.scripts}/collect_session_tokens.py \
    --projects-dir "$HOME/.claude/projects" \
    --skill-dir {paths.skill_dir}
```

脚本会遍历项目 slug 下的每个
`projects/<slug>/<parent-session>/subagents/agent-*.jsonl`，把每轮
`message.usage`（input、output、cache read/creation）汇总，从首个
用户轮里抓 `TOPIC_FILE` / `POLL_CYCLE_ID` 标记做归因，然后以
`status_report.py --token-summary` 能消费的同一格式把一条
`spawn_tokens` 追加到 `cfg/activity.log`。按 agent_id 记账的
`cfg/token_ledger.json` 让重复运行幂等，所以既可以挂到 dispatcher
cycle 末端，也可以在 milestone 之后手动跑，不会重复计数。适合
在线 `--tokens` 钩子被漏记、需要回溯旧 session 历史，或者想交叉
核对在线数据时使用。

**合并队列** 由 daemon 进程内处理 —— 不 spawn agent。

`scripts/process_merge_queue.py` 在 `poll_dispatch.py` 的 watch 循环
里每个 dispatcher cycle 跑一次。它按序 drain `dispatch_plan.merge_queue`
和 `3p_merge_queue`：rebase（skip_ci）→ 通过 GitLab API PUT 合并
→ 发 `merged` 模板 → 状态切 MERGED → 归档到 `cfg/topics/closed/`。
零 LLM token，零 Claude spawn —— 父 session 除把 `merge_queue` 字段当
遥测看之外，**忽略即可**。

遇到 **rebase 冲突**时，话题会被*挂起*而非反复重试：bot 设
`review.rebase_conflict_blocked`、发 `rebase_conflict` 模板（本地 rebase →
推送 → 回复 `ok`），随后 `_check_approved_topics` 跳过该话题，不再每周期
重试 rebase。开发者的 `ok` 由 `mechanical_reply_handler.drain_rebase_conflict_ack`
进程内机械处理（不 spawn agent）：若 `git ls-remote` 显示分支已推进，则清除
标志并发 `merge_resuming`（`流水线运行中，完成后将自动合并。`），话题重新进入
正常合并流程；否则发 `rebase_no_push` 并保持挂起。见 DESIGN §1.6.7。

#### 2e. 每日 08:00 重启 —— 由 Windows 计划任务负责（此处无需操作）

**不再有夜间关停**，机器人 7×24 常驻。`ClaudeReviewBot` 计划任务（通过
`scheduling/register_review_bot_task.ps1` 注册）在**每天 08:00** 运行
`launch_review_bot.vbs` → `launch_review_bot.ps1`，在同一个任务实例里
先执行 `stop_review_bot.ps1 -Silent`、**再**执行 `/review-bot start`。
stop 按 pid 文件先杀 daemon（**优先**，这样 health-check 来不及把
listener 拉起来），再杀 listener、monitor，以及上一个 `claude.exe` 会话。

重启的目的是回收 parent 会话的上下文，而不是让机器人下线 —— 选 08:00
只是因为那个时间点几乎不会有审查正在进行。`-Silent` 会跳过 `已停止`
告别消息，因为几秒后紧跟着就是问候消息。旧的 `ClaudeReviewBotStop`
任务已**禁用**；若确实需要真正的夜间关停，用
`schtasks /change /tn ClaudeReviewBotStop /enable` 重新启用。

**`start` 不再注册任何会话内 stop cron。** 旧的 `CronCreate` 方案已移除。
为什么先杀 daemon 就安全，详见 DESIGN §1.1.1。

#### 2f. 发送问候消息

**若传了 `--silent` 则跳过此步。** 否则：

```bash
"$APPDATA/npm/node_modules/@larksuite/cli/bin/lark-cli.exe" \
  im +messages-send --as bot \
  --chat-id "{env.chat_id}" \
  --msg-type text \
  --text "🤖 Review Bot 已启动。在话题中发送 Jira 单号（如 RAGE-12469）即可触发代码审查。"
```

---

### `status` 模式

```bash
python "{skill_dir}/scripts/status_report.py" --params-json '{parse_args_output}'
```

把输出里的 `formatted_table`、`listener_summary`、`log_tail` 展示给用户。

---

### `stop` 模式

1. **用 `stop_bot.py` 杀掉 daemon + listener + monitor 进程** —— 不要手写
   `taskkill` 去杀 PID 文件里的两个 PID。朴素做法（读 `listener.pid` +
   `daemon.pid`，杀这两个，删文件）不可靠，会悄悄留着 bot 在跑：daemon 的
   health-check（`dispatcher._restart_listener`）会用**新 PID**重新拉起
   listener（所以你读到的 `listener.pid` 当场就过期了），而第二个 / 残留的
   `poll_dispatch.py --watch` 会在你杀掉记录在案的那个之后继续重拉
   listener。`stop_bot.py` 同时信任 PID 文件**和**命令行进程扫描，**先杀
   daemon**（这样关停过程中没人能重拉 listener），循环到扫描结果为零，再删
   PID 文件。详见 DESIGN §1.1.2。

   ```bash
   python "{paths.scripts}/stop_bot.py"
   ```

   读一行 JSON：`{"status":"ok"|"survivors_remain"|"scan_failed",
   "killed":{...}, "survivors":[...], "passes":N, "pid_files_removed":[...]}`。
   `status:"ok"` 表示 bot 已完全关停且 PID 文件已清。若返回 `survivors_remain`
   （罕见 —— 某进程在被杀后还短暂持有句柄），`survivors[]` 会列出每个 pid +
   命令行；重跑一次脚本，若仍杀不掉就按列出的 pid 手动杀 —— 用
   `subprocess_util.kill_process`，**不要**用 `taskkill`（这种状态下它本身
   就是坏的）。`scan_failed` 表示进程扫描失败（见 `error`），**没有杀任何进程、
   也没有删任何文件** —— 不要当作已停止，先修复机器状态再重跑。存活进程对应的
   PID 文件会被刻意保留：PID 文件是该进程身份的唯一记录，给存活进程删掉 PID
   文件会让它永久失联（DESIGN §1.1.4）。旧的第 2–3 步（读 PID 文件、杀进程、
   删 PID 文件）已并入这一次调用 —— 不要再用别的方式做。

   **Monitor 无需 `TaskStop`。** 上面杀掉 `monitor_dispatch.py` *进程* 后，
   Claude-Code 的 Monitor *task* 包装层的脚本被抽走，会发一条无害的
   `Monitor script failed (exit 1)` 通知并**自行注销**。事后再调 `TaskStop`
   永远返回 `No task found`（纯属空操作），故已从本流程移除；包装层每次
   stop 都会自清，计划任务的 stop 路径（根本无法调 `TaskStop`）也一直依赖
   这一自注销。见 DESIGN §1.1.2。
2. 清理话题 agent 留下的临时文件：
   `python "{paths.scripts}/clean_tmp.py"`。
   脚本做两个窄范围的清理：(a) `_tmp/` 下后缀为 `.json` / `.diff` /
   `.stat` 的文件；(b) `scripts/` 下匹配 `_write_artifact_*.py` 的
   每次 spawn 留下的写工件脚本（话题 agent 会把它们写到真正的
   scripts 目录里，并且自己不清）。无通配符、无递归、不会误删。
   **不要**用 `rm -f _tmp/*` 或 `rm scripts/_write_artifact_*` ——
   shell 通配会触发权限弹窗，且范围比需要的更大。
3. 跑一次 `status_report.py`，展示最终状态。
4. **若传了 `--silent` 则跳过此步。** 否则在群里发：`🤖 Review Bot 已停止。`
**没有 stop cron 需要删除** —— `start` 不再注册。若发现遗留的
`cfg/stop_cron.id`（旧版 start 留下的），一次性清理：删文件并
`CronDelete(id)`；否则无需处理。

> **没有夜间自动 stop。** 每日 08:00 的 `ClaudeReviewBot` 任务把
> `scheduling/stop_review_bot.ps1 -Silent` 作为重启的前半段执行，它
> **先杀 daemon** —— 这样 dispatcher 的 listener health-check 在它能重启
> listener 之前就没了 —— 再杀 listener、monitor 以及上一个 `claude.exe`
> 会话。旧的 respawn 竞态（Windows kill 被仍活着的 daemon 撤销）已被
> 「先杀 daemon」的顺序消除；会话内的 `CronCreate` stop 已移除，独立的
> `ClaudeReviewBotStop` 任务已禁用。详见 DESIGN §1.1.1。

---

### `restart` 模式

只重启需要重启的常驻进程 —— 不重建会话、不发群问候。
改过 daemon / monitor 会 import 的任何脚本后都要跑一次（它们只在启动时加载一次 Python，不热重载）。

```bash
python "{paths.scripts}/restart_bot.py" --components "{components or 'stale'}"
```

| 参数 | 重启范围 |
|---|---|
| *（缺省）* / `stale` | 只重启所加载代码早于最新修改的部分 —— 默认值，改完代码用它 |
| `daemon` | 调度 daemon（`poll_dispatch.py --watch`） |
| `listener` | 飞书事件 listener —— **只在真卡死时用**；重启会消耗应用级的长连接配额。期间 daemon 照常运行，但会通过 `cfg/listener_restart.guard` 暂停它的 listener 健康检查，不会在替换中途再拉起一个（DESIGN §1.1.6） |
| `monitor` | 调度 Monitor（见下方会话步骤） |
| `daemon,monitor` | 任意逗号组合 |
| `all` | daemon + listener + monitor |

原样报告 JSON：`restarted`（新 pid 已从 pid 文件确认）、`failed`、
以及带 `reason` 的 `untouched`。有失败时退出码非零。

**若 `needs_session_action` 包含 `monitor`**：脚本已把它 kill 掉，但无法重建
—— Monitor 是归本会话所有的 Claude Code 工具任务，不是独立进程。请自行重发：

```python
Monitor(
    description="Review bot dispatch",
    persistent=True,
    timeout_ms=3600000,
    command='python "{paths.scripts}/monitor_dispatch.py"',
)
```

父**会话**不在可重启范围内（它无法重启自己）。改了 `resolve_start.py`、
或者想回收父会话上下文，请用 `stop` + `start` 或等每天 08:00 的任务。见 DESIGN §1.1.6。

### `recover` 模式

先补齐掉线期间漏掉的消息，再对所有开放话题强制以 GitLab API 做一次对账。
在 daemon 漏事件、pipeline_status 过期或话题卡住时使用。

**消息补齐（catch-up）**：在 GitLab 对账之前，先跑一个受保护的 dispatcher
cycle，把 bot 掉线期间新开的话题、以及发出的机械回复（`ok`/序号/
`close`）补进来——这些是死掉的监听器没抓到的飞书消息，只有 dispatcher 的
历史回溯 reconcile 能找回（下面的 GitLab 对账不行，因为那些话题可能还不
存在）。当有活跃 daemon 持有 `daemon.pid` 时跳过（它每个 cycle 都在对账，
并发的一次性 cycle 会重复处理）。回溯窗口以持久化的 `last_reconcile_ts`
为下界（自动覆盖整个掉线时长，7 天封顶），而非固定 1 小时——详见
DESIGN §1.2.6。

#### 前置条件 —— 无活跃 agent 锁

开始之前先检查是否有话题锁被活跃 agent 持有：

```bash
python -c "import sys; sys.path.insert(0, r'.claude/skills/review-bot/scripts'); \
  import json, topic_store; \
  print(json.dumps(topic_store.active_lock_holders(r'.claude/skills/review-bot/cfg/topics')))"
```

若输出非空列表，**拒绝继续**，把持有者信息打出来（topic_file、holder、
cycle、age_seconds）。在话题 agent 在途时跑 recover 会覆盖它的
进行中写入（状态迁移、audit 条目、MR approve/merge 结果）——
操作员要么等 agent 跑完（锁 10 分钟后过期，30 分钟后被 janitor
强清），要么手动杀掉再重试 recover。

只有 `active_lock_holders` 返回 `[]` 时才继续下面的每话题对账。

对每个开放话题（通过 `iter_topic_files` 遍历——会跳过 `.inbox.json`、
`.tmp-` 等同名兄弟文件；用裸 `glob("om_*.json")` 会把列表型 inbox 文件
当成话题读，`.get()` 直接崩溃，详见 DESIGN §1.1.3）：

#### 2a. 通过 GitLab API 查 MR 状态

对 `mrs` 中的每个 MR 查状态：

```bash
glab api "projects/$(echo $repo_slug | sed 's|/|%2F|g')/merge_requests/$iid" \
  > "$WRITE_TMP_DIR/mr_recover_${repo}.json"
```

读每个响应，和话题当前状态比对：

| GitLab `state` | 话题 `review.state` | 动作 |
|----------------|---------------------|--------|
| `merged` | 任意非终态 | 发 `merged` 通知，设为 `MERGED`，归档到 `closed/` |
| `closed` | 任意非终态 | 发 `mr_closed` 通知，设为 `CLOSED`，归档到 `closed/` |
| `opened` | `APPROVED` | 用 `head_pipeline.status` 更新 `mrs[repo].pipeline_status` |
| `opened` | 其他 | 不改（还在审查循环中） |

终态迁移先通过 `terminal_notice.post_terminal_notice` 往话题里发一条诚实的状态
通知（幂等，详见 DESIGN §1.6.11），再通过 `topic_store.archive_topic` 归档
（移动文件 + 删除索引条目 + 删除兄弟 inbox），而非裸文件移动——裸移动会把索引
条目和 inbox 残留，导致"幽灵话题复活"，详见 DESIGN §1.1.3。只有更改了内容才回写话题文件
（纯无变化对账不再刷新 `updated_at`）。

`head_pipeline.status` 的映射：
- `"success"` 或缺失（game 仓库无流水线）→ `pipeline_status = "passed"`
- `"running"` / `"pending"` → `pipeline_status = "running"`
- `"failed"` / `"canceled"` → `pipeline_status = "failed"`

若 `mrs` 中**任一** MR 为 `closed`，话题立刻变 `CLOSED`。
只有 `mrs` 中**所有** MR 都 `merged` 时，话题才变 `MERGED`。
混合状态（一部分 merged、一部分开着）保持 `APPROVED`。

#### 2b. daemon + monitor 死了就拉起

看 `pythonw.exe` 是否在跑。没在跑就：
```bash
wscript //nologo "{paths.scripts}/run_poll.vbs"
```
重新起 `poll_trigger.txt` 上的 Monitor。

#### 2c. 汇报

输出为 `{"status":"ok","catch_up":{...},"rows":[...]}`：`catch_up` 块汇报
§2a 的受保护补齐 cycle；`rows` 每条列出话题的前后状态、流水线更新、所施加
的迁移。把需要人工关注的话题标出来（例如锁卡住、MR 缺失、`mrs[*]` 带
`error` 字段）。

---

## 状态机

```
审查人批准动词：仅 `ok`（DESIGN §1.23.2）。`pass`/`通过`/`lgtm`/`approved`
已移除，不再识别。（在 *_REVISION 状态下 `ok` 是**开发者**的重审触发词，
不是批准 —— 按角色 + 状态门控；修改阶段审查人没有批准动词，只能 `close`。）

3RD-PARTY 阶段（检测到 3rd-party MR 时 review_phase="3rd_party"）：
  TRIAGING → INLINE_REVIEW（仅 3rd-party） → 走下方第 1 轮流程（§1.23.5）
                                                  │    │
                                             (ok)   (修复清单)
                                              │       │
                    合并 3rd-party MR ←───────┘  SIMPLE_REVISION
                    review_phase="main"           （开发者回复）
                    state=TRIAGING ←─────────────────┘
                    （重新进入正常流程）

每一轮，两个阶段通用（开发者自助循环，DESIGN §1.23）：
  TRIAGING → INLINE_REVIEW（simple）─┬→ DEV_TRIAGE      [审查发布 @开发者，≥1 条问题]
  TRIAGING → FULL_REVIEW（complex）──┘      │
      DEV_TRIAGE：                          │
        （开发者：序号 / all）─────────────→ SIMPLE_REVISION | FULL_REVISION  [按 review.triage]
        （开发者：部分有异议 / none）───────→ SIMPLE_REVISION | FULL_REVISION  [异议仅记录，不上报]
        （开发者：本轮无需修复项）─────────→ DEV_TRIAGE   [提示回复 done]
        （开发者：done）───────────────────→ AWAITING_APPROVAL  [handoff_summary @审查人]
        （审查人：ok = 越过确认） ─────────→ APPROVED
        （close） ─────────────────────────→ CLOSED
      SIMPLE_REVISION | FULL_REVISION：
        （开发者：推送后回复 ok）──────────→ 第 N+1 轮审查 → DEV_TRIAGE
        （开发者：done）───────────────────→ AWAITING_APPROVAL
        （审查人：ok / close）─────────────→ APPROVED | CLOSED
      AWAITING_APPROVAL（审查人唯一的介入点）：
        （审查人：ok） ────────────────────→ APPROVED
        （审查人：序号 = 恢复异议问题） ───→ SIMPLE_REVISION | FULL_REVISION
        （审查人：full，仅 triage=simple）─→ FULL_REVIEW → DEV_TRIAGE  [开发者重新确认完整问题列表]
        （close） ─────────────────────────→ CLOSED
  第 1 轮零问题：无可确认项 → 走旧版 review_round1 →
  TRIAGE_DECISION（simple）/ AWAITING_APPROVAL（complex）。
  ARBITRATION 不再进入（仅为存量话题保留，§1.23.4）。

旧版第 N 轮形态（§1.23.6 之前，存量话题仍可正常流转）：
第 N 轮（审查人决策；审查人 `ok` = 批准）：
  SIMPLE_REVISION ──（开发者 ok）──→ TRIAGE_DECISION
                  ↑               │    │    │       │
                  │         (indices) (full) (ok)  (close)
                  │               │    │      │       │
                  └───────────────┘    │      │       └→ CLOSED
                                      ↓      ↓
                                 FULL_REVIEW  APPROVED
  FULL_REVISION ──（开发者 ok）──→ AWAITING_APPROVAL
                  ↑               │    │    │
                  │         (indices) (ok)  (close)
                  │               │    │    │
                  └───────────────┘    │    └→ CLOSED
                                      ↓
                                  APPROVED
```

`APPROVED` **不是**终态。审查 agent **不负责**管理 APPROVED 之后的
状态 —— daemon 内的 `process_merge_queue.py` 和 `merge_tracker`
负责 APPROVED → MERGED / CLOSED 迁移。`TERMINAL_STATES = {MERGED, CLOSED}`。

**合并后的 cherry-pick 窗口**（DESIGN §1.24）。合并时机器人会探测当前活跃的
发布分支（`rc_*` / `rc_next_*` / `rc_dev_*`，同一 family 取编号最大的那个），若存在则发出
代号→分支的映射，并把话题**暂不归档到 `closed/`**，保留 24 小时等审查人回复：

```
MERGED ──(审查人回复 "p1" / "p1 p2")──→ cherry-pick，然后归档
       ──(审查人回复 "no")───────────→ 归档
       ──(24 小时无回复)─────────────→ 归档（dispatcher janitor）
```

`MERGED` 仍是终态 —— 话题已经结束，只是还可被回复。该状态**只**接受
`cherrypick` / `cherrypick_skip`；此时到达的 `ok` 或 `close` 一律丢弃
（它们会作用在已经合并的 MR 上）。代号是数字，按**当前活跃**集合解析：
当 `rc_p1` 是活跃的 `rc_*` 分支时，`p1` 指的就是 `rc_p1`，而不是已经作废的
`rc_next_p1`。先尝试直接 cherry-pick；遇到保护分支或冲突则回退为自动建 MR。
没探测到任何发布分支则不发出询问，话题照旧立即归档。`3rd_party/*` 仓库完全
不参与询问（§1.24.2）—— 这些库有自己的发布版本节奏。

**跨仓库**：`lifecycle.merge_shas` 里的每个游戏仓库都会被 cherry-pick，且各自用
各自的分支名（rage 是 `rc_p1`，chaos 是 `rage/rc_p1`）—— 绝不能跨仓库复用
同一个名字。若某个仓库的发布分支探测失败，会在询问消息里追加一条 ⚠️ 点名该
仓库，并写入审计 `cherrypick_partial`，而不是把它悄悄丢掉（DESIGN §1.24.1：
仓库根目录一旦回退到 `os.getcwd()`，rage 能用而 chaos 不存在，半个
cherry-pick 会看起来像完整的）。

询问消息 @ 的是 `REVIEW_BOT_CHERRYPICK_DECIDER_OPEN_ID`（发布负责人），
未设置时回退到主审查人。这只影响 @ 谁 —— 鉴权不变，
`REVIEW_BOT_APPROVER_OPEN_IDS` 里的任何 open_id 都可以回复。

## 每话题 Agent 契约

每个子 agent 只拿到一个 `TOPIC_FILE`，端到端处理完。提示模板在
`{paths.scripts}/spawn_topic_agent.md`。

**输入**：`TOPIC_FILE`、`LOCK_FILE`、`THREAD_ID`、`TICKET_ID`、
`POLL_CYCLE_ID`、`APPROVER_OPEN_ID`、`CHAT_ID`、`CHAOS_REPO_ROOT`、
`RAGE_REPO_ROOT`、`SKILL_DIR`。

**加锁**：通过 `O_CREAT|O_EXCL` 调用 `topic_store.acquire_lock(thread_id, cycle_id)`。
若已被锁且新鲜（< 10 分钟），以 `{"status":"skipped_locked"}` 退出。
返回前一定要释放。

**注意**：某些操作（通过 `O_CREAT|O_EXCL` 创建文件、建目录、子进程调用）
即便在 Bypass Permissions 下也可能触发 Claude Code 的工具授权提示。
上面的 **所需权限** 章节列出了要在会话起手就预授权的所有工具类别，
以保证全程自动化。

**drain `events.pending[]`**：按顺序分类、执行副作用、切状态、
追加 `audit[]`，每个事件都原子写回。

### 动作：`new_topic`

**指定开发者（assigned developer）**：操作者可以代不在群里的开发者发起审查 —— 在话题首贴写 `RAGE-XXXXX @<开发者>` 即可。`ack_new_topic` 调用 `event_utils.extract_assigned_dev` 解析首贴：飞书 picker 选中的结构化 `@`-mention → 取 `mentions[]` 里的 `open_id`；字面文本 `@<姓名>` → 查 org-contacts 缓存；都没有 → 退回到发送者。被解析出的开发者写入 `identity.creator_open_id`（模板里的 `@`-target）和 `identity.developer`（显示名）；原发送者保留为 `identity.filed_by_open_id` 供 audit。所有面向开发者的 bot 回复（`revision_request`、`no_new_commits`、`merged`，以及接单期的关闭通知）都自动按这个 routing 走。详见 DESIGN.md §1.20。

**审查人快速通过（fast-track）**：审查人在首贴直接写 `RAGE-XXXXX ok`（可与 `@<开发者>` 混用）即可跳过审查直接进入 APPROVED。鉴权用首贴的 **原始** `event["sender_id"]` 对 `env.approver_open_ids` 做匹配，而不是 `identity.creator_open_id`（后者可能被 `@<dev>` 重写覆盖）。匹配是严格字面的：只有小写 ASCII `ok` 才触发，其他变体（多余正文、`pass`、`通过`、`OK`）都落回常规审查流程。前置检查（无 MR、MR 已合并/关闭、`version_3rd` 不一致）仍然按原样关话题 —— fast-track 只跳过审查，不跳过安全闸。审查人分支由 `ack_new_topic._fast_track_approve` 执行：glab approve 所有 MR、从 `events.pending` 排掉首贴事件（否则 dispatcher 会把 APPROVED 话题反复列为 work —— APPROVED 不是终态）、发标准 `approval` 模板、转 `APPROVED`，剩下交给 `process_merge_queue`。3rd-party 阶段同样支持（跳过主阶段的 pipeline 重检，用固定文案 `等待合并队列处理。`，与 mechanical 3p-approve 路径一致）。audit 条目：`fast_track_approved`。功能说明见 DESIGN.md §1.21，事件排干的坑见 §1.21.1。

1. 设 `review.state = TRIAGING`。
2. **解析 MR**：在两个仓库分别跑 `glab mr list --search "RAGE-XXXXX"`。
   输出写到临时文件（Windows 下管道会损坏 JSON）：
   ```bash
   glab mr list --search "RAGE-XXXXX" -R booming/dev/projects/rage/rage  -F json > "$WRITE_TMP_DIR/gm.json"
   glab mr list --search "RAGE-XXXXX" -R booming/dev/projects/rage/chaos -F json > "$WRITE_TMP_DIR/cm.json"
   ```
   用 `encoding='utf-8'` 读。**按标题绑定，不能直接用 `--search` 结果**
   —— `--search` 会同时匹配 MR 的标题和描述，因此只在描述里提到该单号的
   MR（或另一个单号的 MR 交叉引用了本单号）也会被返回。只保留**标题**
   里带有单号 token 的 MR（`RAGE-XXXXX`，允许前置动词如 `Fix RAGE-XXXXX:`），
   再从每个仓库第一个匹配的 open MR 抽 `source_branch` + `iid`。根据找到的
   MR 填 `mrs` 字典（`"rage"` 和/或 `"chaos"`）。**绝不从 ticket ID 反推分支名**
   —— 分支名以 JSON 里的 `source_branch` 为准。两个仓库都没有标题匹配的 MR →
   发 "未找到相关MR" → `CLOSED`。详见 DESIGN §1.3.5。

3. **更新状态**：对每个找到的 MR，设 `mrs[repo].mr_iid`、
   `mrs[repo].branch`、`mrs[repo].branch_sha`、`mrs[repo].web_url`。
   同时设 `identity.developer`、`identity.creator_open_id`。

4. **创建 Lark Base 记录**（见下方 Lark Base 集成）。

5. **取 diff**：通过 `ls-remote` 解析分支 SHA（编码绕过）：
   ```bash
   SHA=$(git ls-remote origin | grep "$ticket_id" | awk '{print $1}' | head -1)
   git fetch origin "$SHA"
   git diff origin/$base...$SHA --stat > "$WRITE_TMP_DIR/$ticket_id.stat"
   git diff origin/$base...$SHA        > "$WRITE_TMP_DIR/$ticket_id-r1.diff"
   ```
   `$base` 为 MR 的 `target_branch`；缺失时回退到 `.claude/cfg/branches.json` 中该仓库的
   master 分支。详见 DESIGN §1.4.1。

6. **triage**（确定性 —— 不要用主观判断覆盖）：
   `complex` 条件：（**同一仓库内** 文件数 > 5 **或** 行数 > 100）
   或 schema/codegen（.rsd/.nsd/.gsd/.csd）或架构级改动。否则 `simple`。
   跨仓库本身不构成 complex。

7. **3rd-party MR 发现 + 阶段路由**（triage 之后、路由之前）：
   在搜过 rage/chaos 之后，再搜 `3rd_party_cpplibs` 组里的 open MR：
   ```bash
   glab api "groups/3rd_party_cpplibs/merge_requests?search=RAGE-XXXXX&state=opened" \
     > "$WRITE_TMP_DIR/3rd_party_mrs.json"
   ```
   每个找到的 MR 以 key `"3rd_party/<project_name>"` 加进 `mrs`，
   并带上 `repo_slug` 字段（例 `"3rd_party_cpplibs/renderdoc"`）。

   再对同一组做一次**独立的 `state=merged` 探测**，置 `has_merged_3p` 标志。
   第三方库 MR **先于**其消费方 chaos/rage MR 合并是合法流程（库先落地，
   消费方 MR 再引用升级后的版本）。这类已合并的库 MR 仍满足 `version_3rd.cmake`
   要求，但**不加入** `mrs` —— 它已合并，无需再审查或合并，若加入会错误地把
   话题路由进 3rd-party 阶段并尝试重复合并。它是独立查询，避免一个 state 的
   错误掩盖另一个。详见 DESIGN §1.3.4。

   **双向检查**（`_compute_version_3rd_check`）：只在 chaos MR 改了
   `version_3rd.cmake` 但**既无 open 也无 merged** 的第三方库 MR 时才关话题
   （`has_merged_3p` 为假；发 `⚠️ 检测到 version_3rd.cmake 版本升级，但未找到对应的第三方库 MR。` → `CLOSED`）；
   反向亦然：存在 **open** 的第三方库 MR 但 chaos 的 `version_3rd.cmake` 没动
   （发 `⚠️ 第三方库 MR 已存在，但 chaos 仓库的 version_3rd.cmake 未包含版本升级。` → `CLOSED`）。
   `has_merged_3p` 只放宽第一个方向（cmake→3p）；第二个方向（3p→cmake）
   仍以 **open** MR 为准。每条接单期 ⚠️ 关闭通知都会 @ 提及发起人。
   若第三方库探测报错，`cmake_without_3p` 的关闭会改为 retry（判定不可靠，
   探测成功前不关闭）—— 详见 DESIGN §1.3.6。

   第三方群组探测通过 `_project_slug_from_entry` 解析仓库路径
   （`project_path_with_namespace` → `references.full` → `web_url`）。**群组**级
   MR 接口不返回第一个字段，只读它会静默丢弃所有 open 的库 MR —— 导致
   `cmake_without_3p` 误关单、第三方阶段永不触发（RAGE-23816）。

   **可恢复的关闭**：`mr_not_found`、`3rd_party_mr_not_found`、
   `missing_version_3rd_bump` 这三类是要求开发者补齐后再回来，故会置
   `lifecycle.revivable` 并在通知末尾追加 `补齐后在本话题回复即可继续审查。`。
   之后该话题内的任意回复会经 router Gate 4a 调 `topic_revive.try_revive`
   重开话题并重跑接单，最多 3 次。`mr_already_merged` / `mr_already_closed`
   仍为终态。详见 DESIGN §1.3.7。

   **阶段路由**：若任一 `mrs` key 以 `"3rd_party/"` 开头，设
   `review.review_phase = "3rd_party"`，本轮先只审查 3rd-party MR。
   否则 `review_phase = null`，正常流程。

8. **路由**（话题 agent 没有 `Task` 工具 —— 所有审查都由它自己内联完成，绝不拉起子 agent）。两个阶段都采用反转 triage（DESIGN §1.23、§1.23.5）：第 1 轮审查有 ≥1 条问题时，发 `review_round1_dev_triage`（@开发者）并进入 `DEV_TRIAGE`；零问题审查保留旧版 `review_round1` → 审查人决策状态：
   - **Simple** → `INLINE_REVIEW` → 自己内联做 diff 审查 → 发
     `review_round1_dev_triage` → `DEV_TRIAGE`（零问题：
     `review_round1` → `TRIAGE_DECISION`）。
   - **Complex** → `FULL_REVIEW` → 自己内联做完整审查 → 建 **中文**
     飞书文档（标题 `代码审查 RAGE-XXXXX`，正文用简体中文）→
     给审查人和开发者都授予查看权限 → 在 thread 里用
     `review_round1_dev_triage` 发文档链接 → `DEV_TRIAGE`（零问题：
     `review_round1` → `AWAITING_APPROVAL`）。

   **关键**：始终同时给**审查人**和**开发者**授予查看权限。之前
   曾发生漏授开发者权限的情况 —— 链接点得开但内容读不到。

### 动作：`approver_reply`

**审查人名单门控**：审查人意图（`ok` 批准、`full`/`完整版`、数字序号、`close`/`关闭`）只在 `sender_id ∈ env.approver_open_ids` 时才被识别。其他人发同样的 token 会落到开发者意图规则（`ok` 重审 / `@bot ...`），或被路由层丢弃并写 `unauthorized_intent` audit。名单来自 `settings.json` 的 `REVIEW_BOT_APPROVER_OPEN_IDS`（逗号分隔的 open_id 列表）；旧字段 `REVIEW_BOT_APPROVER_ID` 作为兼容回退，并继续作为模板 `@`-mention 的主审查人。`mechanical_reply_handler._classify` 在解析 token 之前会先做 `sender_id ∈ env.approver_open_ids` 检查。

**批准动词（DESIGN §1.23.2）**：`ok` 是审查人**唯一**的批准动词，在所有决策状态下生效 —— `TRIAGE_DECISION` / `AWAITING_APPROVAL`（最终批准）、`DEV_TRIAGE`（越过确认）、`ARBITRATION`（同意开发者的处理意见）。状态集合是 `reply_parser._APPROVER_OK_STATES`；`pass`/`通过`/`lgtm`/`approved` 已移除，不再识别。审查人 `ok` 在 `SIMPLE_REVISION`/`FULL_REVISION` 不算批准 —— 那里 `ok` 是开发者的重审触发词；修改阶段审查人没有批准动词（只能 `close`），改为在开发者回复 `done` 提交终审后、于 `AWAITING_APPROVAL` 批准（DESIGN §1.23.7）。

**序号格式**：连续数字之间可以用空格、英文逗号或中文逗号分隔 —— `1 3 5`、`1,3,5`、`1，3，5`、`1, 3, 5` 都解析为 `[1, 3, 5]`。一个纯数字 token（`1` 或 `42`）算一个序号。另外接受两种简写形式：

- `all`（大小写不敏感）—— 标记所有问题，等同于把每个序号都列出来。
- `-N`（负号前缀）—— 把 N 从标记集合中排除。`-1 -3` 表示"除 #1、#3 外全部需修改"；`all -1 -3` 是显式等价形式。

混合正负（`1 -2`）和 `all` + 正数（`all 1`）都是歧义形式，按 `unknown` 丢弃。`0` / `-0` 拒绝（问题序号从 1 起算）。排除列表把所有问题都排掉的情况（例如 3 条问题的审查回复 `-1 -2 -3`）也拒绝 —— 想批准请用 `ok`。扩展正则：`^\s*(?:all|-?\d+)(?:[\s,，]+(?:all|-?\d+))*\s*$`，之后再用 `parse_indices_with_mode` 做语义校验。分类器返回 `{indices, exclude}`；`exclude=True` + `indices=[]` 就是裸 `all` 形式。代码见 `reply_parser.parse_indices_with_mode`。

按当前状态解析：

| 状态 | 输入 | 动作 |
|-------|-------|--------|
| `TRIAGE_DECISION` | `ok` | → `APPROVED` + approve MR |
| `TRIAGE_DECISION` | `full/完整版/完整审查` | → `FULL_REVIEW`（升级完整审查） |
| `TRIAGE_DECISION` | `close/关闭` | → `CLOSED` + close MR |
| `TRIAGE_DECISION` | 数字序号 `1 3 5` | → `SIMPLE_REVISION` + 通知开发者 |
| `AWAITING_APPROVAL` | `ok` | → `APPROVED` + approve MR |
| `AWAITING_APPROVAL` | `close/关闭` | → `CLOSED` + close MR |
| `AWAITING_APPROVAL` | 数字序号 | 当 `dev_triage.rejected_indices` 非空时表示**恢复**开发者有异议的问题（DESIGN §1.23.9），否则为普通重新标记 → `SIMPLE_REVISION`/`FULL_REVISION` + 通知开发者 |
| `AWAITING_APPROVAL` | `full/完整版/完整审查`（仅 `review.triage == "simple"`） | → `FULL_REVIEW`（agent 重做完整审查，置 `triage="complex"`）→ `DEV_TRIAGE` |
| `DEV_TRIAGE` | `ok` | → `APPROVED`（越过确认 —— 完全跳过开发者自助循环） |
| `DEV_TRIAGE` | 数字序号 | 丢弃（`ignored`）—— 开发者先确认，审查人在开发者提交终审时再决策 |
| `ARBITRATION` | `ok` | 同意开发者的处理意见：修复集非空 → `SIMPLE_REVISION`/`FULL_REVISION`（按 `review.triage`）+ 通知开发者；修复集为空 → `APPROVED`（`ok` 即批准，DESIGN §1.23.2） |
| `ARBITRATION` | 数字序号 `2,4` | 恢复开发者有异议的问题 → `SIMPLE_REVISION`/`FULL_REVISION` + 把最终修复列表通知开发者 |
| `ARBITRATION` | `full/完整版/完整审查`（仅 `review.triage == "simple"`） | → `FULL_REVIEW`（agent 重做完整审查，置 `triage="complex"`）→ `DEV_TRIAGE` |
| `ARBITRATION` | `close/关闭` | → `CLOSED` + close MR |

**3rd-party 阶段处理**（当 `review.review_phase == "3rd_party"` 时）：

| 状态 | 阶段 | 输入 | 动作 |
|-------|-------|-------|--------|
| `TRIAGE_DECISION` | `review_phase == "3rd_party"` | `ok` | 通过 `merge_tracker` approve 并立即合并 3rd-party MR。重置 `review_phase="main"`、`state=TRIAGING`、`round=0`。发 `✅ 第三方库 MR 已合并。正在继续审查主仓库代码...` |
| `TRIAGE_DECISION` | `review_phase == "3rd_party"` | 数字序号 | → `SIMPLE_REVISION` + 通知开发者 |
| `TRIAGE_DECISION` | `review_phase == "3rd_party"` | `close/关闭` | → `CLOSED` + 关掉所有 MR |

审查人看到的具体措辞见下方 **审查 Post 模板** 中的 **回复说明**。

**MR 通过**（approve + 流水线检查）：
对 `mrs` 中每个 MR 调用 `glab mr approve`：
```bash
glab mr approve "$iid" -R "$repo"
```
然后通过 glab api 检查每个 MR 的 `head_pipeline` 状态，写回
`mrs[repo].pipeline_status`：
- "success" → pipeline_status = "passed"
- "running"/"pending" → pipeline_status = "running"
- "failed"/"canceled" → pipeline_status = "failed"，给开发者发警告

Game 仓库没有 CI —— 缺失 `head_pipeline` 字段视为 `pipeline_status = "passed"`。

使用 `approval` 模板发通过通知。**不要**在此处 merge 或 rebase ——
这些交给 daemon 内的 `process_merge_queue.py`。

**MR 关闭**：对 `mrs` 中每个 MR 调用 `glab mr close $iid -R $repo`。
发 `MR ` → `CLOSED`。

### 动作：`dev_triage`

开发者（sender 与 `identity.creator_open_id` 匹配）在 `DEV_TRIAGE` 状态下的回复 —— **每一轮**审查发布之后都适用，不限于第 1 轮，两个阶段通用（DESIGN §1.23.6）。内容是开发者对 bot `#N` 问题列表的确认 —— **序号 = 开发者将修复的问题**；未列出的视为有异议。第 2 轮起只对仍**未结**的问题做确认：已确认修复的、以及此前已表示异议的都不再询问（再次点名会判为无效序号）。序号后可附自由文本理由 —— `-2 -3 这两个是误报` —— 按轮次记录，并在提交终审时呈现给审查人（DESIGN §1.23.8）：

| 开发者回复 | 含义 |
|-----------|------|
| `1 3 5` | 修复这些，其余有异议 |
| `all` | 全部修复 |
| `-2` / `-1 -3` | 对列出的问题有异议，其余修复 |
| `none` / `0` / `不修` | 全部有异议（无需修复） |

**机械处理**（不拉 agent）：`mechanical_reply_handler._handle_dev_triage` 把 `review.dev_triage = {accepted_indices, rejected_indices, reinstated_indices, reasons}` 作为跨轮次累积的集合记录下来，然后向开发者发 `revision_request`，直接转到 `SIMPLE_REVISION`/`FULL_REVISION`（按 `review.triage`）；audit `dev_triage_recorded`。**异议不再上报**：在开发者回复 `done` 之前，审查人完全不会被打扰（DESIGN §1.23.6）。后续轮次中再次点名某个序号即表示接受它，会清除此前的异议。有两类情况会发纯文本纠正并**丢弃事件**（防止毒化循环，开发者重新回复即可）：无效或已结的序号（audit `dev_triage_invalid_indices`），以及试图对审查人已恢复的问题再次表示异议 —— 该问题已被锁定（audit `dev_triage_reinstated_locked`，DESIGN §1.23.9）。若某一轮没有任何需要修复的问题，bot 发提示并停留在 `DEV_TRIAGE`（audit `dev_triage_all_rejected`）。在 `DEV_TRIAGE` 回 `ok` 会被丢弃 —— triage 需要序号。详见 DESIGN §1.23.1。

### 动作：`dev_handoff`

开发者回复 `done` / `submit` / `提审` / `完成`，状态 ∈ `DEV_TRIAGE` / `SIMPLE_REVISION` / `FULL_REVISION`。这是**唯一**能结束开发者自助循环、把话题交给审查人的动作。机械处理：`_handle_dev_handoff` 发 `handoff_summary` 模板 @审查人 —— 问题处理情况汇总，加上每条有异议的问题及开发者给出的理由 —— 并转到 `AWAITING_APPROVAL`；audit `dev_handoff`。仅限该话题的开发者，避免旁人替别人提交终审。详见 DESIGN §1.23.7。

### 动作：`dev_question`

开发者发的 thread 回复，**内容必须以 `@bot` 开头**（字面文本，或者飞书的 `@`-mention 指向 bot 用户均可）。在任意回复状态下触发，包括 `DEV_TRIAGE` / `ARBITRATION`（dev-triage 第 1 轮的页脚就明确邀请提问）。任何其他前缀的开发者消息都不会被当作问题（参见下方 `dev_reply` 规则）。

dispatcher 会在收到提问后几秒内机械发一条确认（`🤖 已收到 ... 的提问，正在查证，稍后回复…`，`ack_dev_question` 模板）；事件保留在 pending，真正的回答仍由 topic agent 按下面的步骤完成。详见 DESIGN §1.3.3。

1. 在当前 SHA 下重读代码以理解上下文。
2. 用中文解释该审查发现背后的理由。**不要**切状态、**不要**从
   `flagged_issues` 里删条目 —— 是否接受由审查人决定。

### 动作：`dev_reply` / 开发者 `close`

开发者（sender 与 `identity.creator_open_id` 匹配）发的 thread 回复，去掉飞书 `@`-mention 之后**正好等于字面 token `ok`**（大小写不敏感、忽略首尾空白和换行），处于 `SIMPLE_REVISION` 或 `FULL_REVISION` 状态。`ok` 表示「修复已 push，请再审一轮」。

开发者也可以回复 `close` / `关闭` / `no` 来主动关闭自己的 topic —— 与审查人 `close` 等价（关闭所有 MR、发 close 通知、状态切到 `CLOSED`）。授权由 router 层做：`sender_id == identity.creator_open_id` 才识别；群里其他人随手输 `close` 直接被丢弃，避免误关 MR。Audit 写 `developer_close`（与 `approver_close` 区分）。

开发者发的、既不是 `ok`、`@bot ...` 也不是 `close` 的消息，都会在路由层被丢弃并写 `reply_intent_ignored` audit —— **不**触发再审、**不**拉子 agent。这是对旧版"开发者发任何消息都算 dev_reply"行为的显式收紧；只 push 代码却没在 thread 回 `ok`，topic 会留在 `*_REVISION`（与 §1.18.1 一致：bot 没有 GitLab webhook，话题里的消息才是触发器）。

**Rebase 冲突恢复（`APPROVED` 状态下的 `ok`）**：当话题因合并队列 rebase 冲突被挂起（`review.rebase_conflict_blocked`）时，开发者的 `ok` *也*有意义。该 `ok` 由 `mechanical_reply_handler.drain_rebase_conflict_ack` **机械处理** —— 绝不走 agent，父 session 也不得按 §1.18.2 未定义行将其丢弃。该 drain 用 SHA 校验推送，随后恢复合并（`merge_resuming`）或要求开发者先推送（`rebase_no_push`）。见 DESIGN §1.6.7。

1. 通过 `glab api projects/<slug>/merge_requests/<iid>` 刷新 `mrs[repo].branch_sha`（参见 §1.4.2）。如果与 `last_review_commit` 相等，发 `no_new_commits` 模板并留在当前状态（开发者只回了 `ok`，但其实没新 push）。
2. 自己内联做增量审查（话题 agent 没有 `Task` 工具，不拉子 agent）。
3. **大局面核对**：只对前面轮次尚未定案（非 `addressed` / `obsolete`）的
   flagged issue 执行 —— spawn Context 会给出 `to_verify` / `carried` 两组序号，
   已定案的直接沿用原结论，不重新 grep（DESIGN §1.4.8）。对仍需核对的：
   标某条 issue 为 `unfixed` 之前，对 `$SHA` 下的
   **当前整文件**再 grep 一次。模式消失 → `fixed (obsolete)`；
   仍在 → `unfixed` 并记录当前位置。
4. **人工评论验证**（与第 3 步一并执行）：调用 `gitlab_threads.reconcile` 刷新 `review.manual_issues[]`，对所有 `verified_at_sha != head_sha` 的条目逐条内联判定（没有子 agent —— 话题 agent 没有 `Task` 工具），通过 `manual_issue_verifier.py context` 生成验证 prompt。把每条结果的 `{verification, verification_rationale, verified_at_sha}` 写回。当验证结果为 `addressed` 或 `obsolete` 时，再调用 `gitlab_threads.mark_resolved` 把 GitLab thread 标为已解决并写 `marked_resolved_at`，使 GitLab UI 与 bot 判定一致。详见 DESIGN §1.14 / §1.14.1 与 `spawn_topic_agent.md` §3a。
5. 从 `SIMPLE_REVISION` → `TRIAGE_DECISION`；从 `FULL_REVISION` →
   追加到飞书文档 → `AWAITING_APPROVAL`。

### 动作：`manual_refresh`

任意发送者（开发者、审查人、群成员均可），消息匹配 `@bot 同步` / `@bot refresh` / `@bot sync` —— 在任意回复状态下生效（`TRIAGE_DECISION`、`AWAITING_APPROVAL`、`DEV_TRIAGE`、`ARBITRATION`、`SIMPLE_REVISION`、`FULL_REVISION`）。

使用场景：人工在 MR 上加了行内评论（或自上次同步以来又加了新评论），希望 bot 在不依赖 dev `ok` 推送触发的前提下，单独承认/验证这些人工评论。

1. 通过 `gitlab_threads.fetch_for_topic` 重新拉取 GitLab MR 的 thread，与 `review.manual_issues[]` 做对账合并。
2. 对每条 `verified_at_sha != current_head_sha` 的人工评论逐条内联判定（没有子 agent），使用 `manual_issue_verifier.py context` 输出。已在当前 HEAD 验证过的条目跳过 —— 幂等。
3. 发一条 `review_roundN` artifact，**只**汇总人工评论部分；bot 自身的发现**不**重审（要重审请用 `ok`）。
4. 状态保持不变。Audit 记 `manual_refresh_completed`，含对账 summary 与验证计数。

短路：如果对账显示**无**新增条目、且每条已有条目的 `verified_at_sha == head_sha`，则用 `freeform_reply` 模板发一条简短回复 `"人工审查无变化（{N} 条均已验证至当前提交）"` 后退出，不拉验证 agent。

## 审查 Post 模板

所有面向用户的内容**一律简体中文**。

**模板文件**在 `{paths.scripts}/templates/`。Agent **必须**通过
`python "$SKILL_DIR/scripts/templates/render.py" <name> --vars-file vars.json`
来用这些模板，不要手搓 post JSON。可用模板：
review_round1、review_round1_dev_triage、review_roundN、revision_request、
dev_triage_summary、approval、no_new_commits、merged、mr_closed、freeform_reply、
cherrypick_prompt（合并后存在活跃发布分支时由 daemon 发出，
DESIGN §1.24 —— agent 绝不发它）、
topic_reopened（由 daemon 的 withdrawn-supersede 恢复路径发出，
DESIGN §1.2.9 —— agent 绝不发它）。

`merged` / `mr_closed` 通知通常由 daemon 发出，而非 agent：主动合并路径
（`process_merge_queue`）和被动对账路径（`dispatcher._check_approved_topics`、
`recover.py`）都经 `terminal_notice.post_terminal_notice` 发送，保证每次终态
迁移都反映到话题里（幂等——DESIGN §1.6.11）。

结构（每个条目 = `post_json.zh_cn.content` 中一个段落）：

1. 加粗仓库头：`<Game|Chaos> 仓库 (<branch>)`。跨仓库时，每个
   仓库头 + MR 链接各占一段（独立行），这样链接才能各自点开。
2. MR 链接（只在第 1 轮）：`{"tag":"a","text":"<repo>!<iid>","href":"https://gitlab.booming-inc.com/booming/dev/projects/rage/<repo>/-/merge_requests/<iid>"}`。跨仓库同样每个仓库单独一段。
3. 纯文本：`N 个文件变更，+X / −Y`
4. （空行）
5. 每个变更文件：加粗文件名 + `+x/-y` 行数 + 简短中文说明
6. （空行）
7. 加粗 `问题汇总`
8. 每个 issue：加粗 `#N  [严重|中|轻] ` 前缀 + `[Repo] file[:line_range] [function: ]text`。`repo`/`file`/`text` 必填；行级问题**必须**填 `line_range`，仅当确为整文件/结构性问题时才省略（渲染为 `[Repo] file: text`）；`function` 可选；`text` 仅为散文 —— 位置前缀由 `render.build_issue_paragraphs` 拼接。**完整审查即便 `text` 精简也必须填 `line_range`**，使内联列表仍能指明问题所在。详见 DESIGN §1.9.4。
8a. 可选 `人工审查（N 条）` 段落 —— 由 `review.manual_issues[]` 经 `MANUAL_ISSUES` 模板变量生成。每条渲染为 `[M{i}]`（链向 GitLab discussion 的可点链接）+ 验证标记（`📌 待验证` / `✅ 已修复` / `⚠️ 未修复` / `🟡 部分修复` / `📝 代码已删除/重构` / `❓ 无法判断`）+ `[Repo] file.cpp:line — body 短句（author）`。验证理由（如有）独占缩进一行。人工评论使用**独立编号空间** `[M1]` `[M2]` —— **不**与 bot 的 `#N` 混编，因为审查人的 `1,3,5` 序号回复只针对 bot 的发现（人工评论由 GitLab 跟踪，不进入 revision 流程）。详见 DESIGN §1.14.1。

issue **必须**按严重度（严重 > 中 > 轻 > 建议）排序后再赋 `#N`
编号。飞书 thread post 和飞书文档（完整审查时）**必须**使用相同的排序 ——
完整审查文档的 `问题详情` 段落由同一份 `review.issues[]` 经
`render.build_doc_issue_markdown`（CLI：`scripts/templates/render_doc_issues.py`）
确定性渲染，其 `#N` 与每条 `[Repo] file[:line_range] （function）` 标题
与 thread reply 保持一致。**不要**手写文档 issue 列表。详见 DESIGN §1.9.5。

**严重度分级**（本仓库把约定当作硬约束 —— 详见 `/cpp-conventions` 和 `.claude/rules/`）：

| 等级 | 含义 | 举例 |
|------|---------|----------|
| `严重` | 正确性 bug —— 合并前必须修 | race、内存损坏、逻辑错误、安全问题 |
| `中` | 显著的设计/规则违反 | 裸 `new`/`delete`、`dynamic_cast`、`std::shared_ptr`、架构问题、性能回退 |
| `轻` | 局部的项目规则违反 —— 条件允许应在合并前修 | 单字母变量、魔数、缺 const、误导命名、用原始基本类型而非 `Chaos::Int`、缺 handle 包装 |
| `建议` | 纯主观意见 —— 无项目规则可引用 | 在无规则约束下的替代实现、排序/风格偏好 |

**关键**：命名/约定类问题默认为 `轻` 而非 `建议`，因为
`.claude/skills/cpp-conventions/reference/08-naming.md` 把它们当作阻塞项。只有在完全没有
`.claude/rules/` 或 `.claude/skills/cpp-conventions/reference/` 可引用的主观建议时才用 `建议`。
9. （空行）
10. `@`-mention 与回复说明取决于模板（每个选项一段）：

**`review_round1_dev_triage`**（第 1 轮且 ≥1 条问题，两个阶段通用，DESIGN §1.23）—— `{"tag":"at","user_id":"$DEVELOPER_ID","user_name":"$DEVELOPER_NAME"}`，随后是 dev-triage 回复说明（已内置在模板里）：
  - `· 回复问题序号（如 1 3 5）确认修复这些问题，其余视为有异议`
  - `· 回复 "-2" 表示仅对 #2 有异议，其余修复；回复 "all" 全部修复`
  - `· 回复 "none" 或 "不修" 表示全部有异议（异议将转交审查人裁决）`
  - `· 如有疑问，请以 ` + bot 提及 + ` 开头提问` —— 这里的提及是 render 内置变量 `BOT_MENTION_SEGMENTS`（`REVIEW_BOT_OPEN_ID` 可解析时渲染成指向 bot 的真实 `at` 标签，否则退回字面加粗 `@bot`；DESIGN §1.9.6）

**`review_round1`**（3rd-party 阶段、零问题审查、第 N 轮升级重审）与 **`review_roundN`** —— `{"tag":"at","user_id":"$APPROVER_ID","user_name":"审查人"}`，随后：
- Simple：
  - `· 回复问题序号（如 1 3 5）标记需修改；回复 "all" 标记全部；回复 "-2 -4" 标记除 #2 #4 外的全部`
  - `· 回复 "ok" 批准`
  - `· 回复 "full" 或 "完整版" 进行完整审查`
  - `· 回复 "close" 或 "关闭" 终止MR`
- Full（没有 "full" 选项）：
  - `· 回复问题序号（如 1 3 5）标记需修改；回复 "all" 标记全部；回复 "-2 -4" 标记除 #2 #4 外的全部`
  - `· 回复 "ok" 批准`
  - `· 回复 "close" 或 "关闭" 终止MR`

**`dev_triage_summary`**（开发者 triage 后机械发布 → `ARBITRATION`）—— `@审查人`，随后是同意（`ok`）/ 序号恢复 / 升级完整审查（仅 simple）/ close 的说明；见模板文件。

话题进入 `SIMPLE_REVISION` / `FULL_REVISION` 时（`revision_request` 模板），发给**开发者**的 post 还要包含：

- `· 修改完成后回复 "ok" 触发下一轮审查`
- `· 提问请以 @bot 开头（如 "@bot 这个问题为什么是中级？"）`

任意回复状态下，**所有人**（开发者、审查人、群成员）均可使用：

- `· 回复 "@bot 同步" / "@bot refresh" / "@bot sync" —— 重新拉取 GitLab MR 上的人工审查评论并验证修复状态`

使用场景：bot 自动审查没找到可处理项，但人工事后又在 MR 上加了行内评论。`@bot 同步` 会重新拉取这些人工评论；如果开发者在上次验证之后又有新 push，会顺带跑一轮单条验证。**不**重审 bot 自身的发现。

标题：`代码审查 RAGE-XXXXX（第 N 轮）`

## Thread 回复助手

### 纯文本

```bash
"$APPDATA/npm/node_modules/@larksuite/cli/bin/lark-cli.exe" \
  im +messages-reply --message-id "$msg_id" --reply-in-thread --as bot \
  --msg-type text --text "content"
```

### Post 格式（优先使用）

```json
{"zh_cn":{"title":"标题","content":[[{"tag":"text","text":"段落"}]]}}
```

| 元素 | JSON |
|---------|------|
| 加粗 | `{"tag":"text","text":"label","style":["bold"]}` |
| 链接 | `{"tag":"a","text":"doc","href":"https://..."}` |
| 空行 | `[{"tag":"text","text":"\n"}]`（独占一段） |
| @ 提及 | `{"tag":"at","user_id":"ou_xxx","user_name":"name"}` |

```bash
"$APPDATA/npm/node_modules/@larksuite/cli/bin/lark-cli.exe" \
  im +messages-reply --message-id "$msg_id" --reply-in-thread --as bot \
  --msg-type post --content "$(cat $WRITE_TMP_DIR/post.json)"
```

### 撤回 bot 消息

```bash
"$APPDATA/npm/node_modules/@larksuite/cli/bin/lark-cli.exe" \
  im messages delete --as bot --yes --params '{"message_id":"om_xxx"}'
```

## Lark Base 集成

**表 schema**（字段一个一个建，每个之间 ~2 秒延迟）：

| 字段 | 类型 | 备注 |
|-------|------|-------|
| Ticket ID | text (url) | `[RAGE-XXXXX](https://jira.boomingtechs.cn/browse/RAGE-XXXXX)` |
| Developer | text | — |
| Game Branch | text | — |
| Chaos Branch | text | — |
| Game MR | text (url) | `[rage!N](https://gitlab.booming-inc.com/.../rage/-/merge_requests/N)` |
| Chaos MR | text (url) | `[chaos!N](https://gitlab.booming-inc.com/.../chaos/-/merge_requests/N)` |
| 3rd-party MR | text (url) | `[<project>!N](https://gitlab.booming-inc.com/3rd_party_cpplibs/<project>/-/merge_requests/N)` |
| Triage Result | select | `simple`、`complex` |
| Review Rounds | number | — |
| Issues Found | number | — |
| Status | select | `REVIEWING`、`APPROVED`、`CLOSED` |
| Review Doc | text (url) | 飞书文档 URL |
| Created At | datetime | — |
| Resolved At | datetime | — |

带空格的字段名可能让 `+record-upsert` 失败 —— 每次
`+field-create` 之后用 `+field-list` 核对。

```bash
lark-cli base +record-upsert \
  --base-token "$app_token" --table-id "$table_id" \
  --json '{"Ticket ID":"[RAGE-12469](...)", "Status":"REVIEWING", ...}'
```

## 活动日志

**每话题**事件 → JSON 里的 `topic.audit[]`。**dispatcher cycle**
事件 → `cfg/activity.log`。日志 1MB 滚动（保留最近 500 行）。

## 跨会话重启

| 层 | 能扛 `/clear` 吗？ |
|-------|-------------------|
| Listener（detached `node.exe`） | 能 —— 以 `listener.pid` 为单例 |
| Daemon（`pythonw poll_dispatch.py --watch`） | 能 —— 以 `daemon.pid` 为单例 |
| Monitor（`monitor_dispatch.py`） | 能 —— 以 `monitor.pid` 为单例（新起会杀旧的） |
| `cfg/topics/*.json`、index、events、logs | 能 |
| Claude 会话上下文 | 不能 |

**重要**：这三个进程都在启动时一次性加载 Python 脚本。若你
改过任何 daemon/listener/monitor 的脚本，要 kill 再拉 —— 它们
不会自动热重载。

三个常驻进程都用 `subprocess_util.py` 里统一的 PID 文件单例模式
（`read_pid_file`、`write_pid_file`、`kill_process`、`release_pid_file`）。
在 `/clear` → `/review-bot start` 的切换下，listener 和 daemon
本来就还在跑；Monitor 自我替换（杀旧 PID、写自己的）。第一次
poll cycle 会把中间的空档补上。

## 每话题文件 Schema（v3）

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
             "last_review_commit": "...", "issues_found": 4,
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

- `mrs` 是以仓库 slug（`"rage"`、`"chaos"` 或 `"3rd_party/<project>"`）
  为 key 的 dict。每个条目含 `mr_iid`、`branch`、`branch_sha`、
  `web_url`、`pipeline_status`。3rd-party 条目另有 `repo_slug` 字段
  保存完整 GitLab 路径（例 `"3rd_party_cpplibs/renderdoc"`），供
  `merge_tracker.py` 使用。一个话题可以同时出现多个仓库。带 3rd-party
  MR 的例子：
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
- `review.review_phase` 控制两阶段 3rd-party 审查流程：`null`（没有
  3rd-party MR，正常流程）、`"3rd_party"`（先审 3rd-party）、或
  `"main"`（3rd-party 合完后审主仓库）。由 `ack_new_topic.py` 在
  ack 时预计算好。
- `review.triage` —— `"simple"` 或 `"complex"`。由
  `ack_new_topic._compute_triage` 从 `review.ack_stats` 预计算
  （同一仓库内文件数 > 5 **或** 行数 > 100，或触及 `.rsd/.nsd/.gsd/.csd`
  schema 文件）。话题 agent **可以**基于架构层面的判断把 `simple`
  升级到 `complex`（机械规则看不到的那类）。
- `review.version_3rd_check` —— `"ok"`、`"cmake_without_3p"` 或
  `"3p_without_cmake"`。对 chaos 的 `version_3rd.cmake` 改动与
  `3rd_party/*` MR 存在性的双向一致性检查。已合并的第三方库 MR（独立探测，
  记为 `has_merged_3p`）计入 cmake→3p 方向，故 `cmake_without_3p` 仅在
  **既无 open 也无 merged** 的库 MR 时才成立（DESIGN §1.3.4）。mismatch
  状态会让 `ack_new_topic` 带上相应的 ⚠️ 警告（并 @ 提及发起人）直接关掉
  话题 —— 因此等话题 agent 拿到话题时它永远是 `"ok"`。
- `review.ack_stats[repo]` —— 由 `ack_new_topic._fetch_stats`
  （通过 `git diff --numstat`）填的每仓库 diff 统计：
  `{file_count, insertions, deletions, files: [{path, insertions, deletions}]}`。
  agent 直接从 `files[]` 读每文件行数填 FILE_PARAGRAPHS，不要再跑
  `git diff --stat`。3rd-party 仓库本地没有 checkout，会被跳过。
- `review.manual_issues[]` —— GitLab MR 上的人工审查 thread（DiffNote），
  ack 时由 `gitlab_threads.fetch_for_topic` 拉取，`dev_reply` /
  `manual_refresh` 时刷新。每条结构：
  `{index, discussion_id, note_id, author, repo, file, line_old, line_new, base_sha, body, web_url, verification, verification_rationale, verified_at_sha, marked_resolved_at}`。
  bot 由话题 agent 逐条内联（用 `manual_issue_verifier.py` 生成上下文）独立验证每条
  人工评论是否在当前 HEAD 已修复；GitLab 自身的 `resolved` 标记**故意不作为输入读取**，
  但当 bot 验证为 `addressed` 或 `obsolete` 时**会**通过 `gitlab_threads.mark_resolved`
  写回 `resolved=true`，使 GitLab UI 与 bot 的判定一致。详见 DESIGN §1.14 / §1.14.1。
- `review.dev_triage` —— 开发者第 1 轮 triage 记录（增量字段，无需 schema 升版）：
  `{"accepted_indices": [1,3], "rejected_indices": [2,4], "reinstated_indices": [4], "decided_at": <ms>, "triggered_by_event_id": "..."}`。
  由 `mechanical_reply_handler._handle_dev_triage` 在 `DEV_TRIAGE` 状态写入；
  `reinstated_indices` 只在审查人裁决恢复异议问题后出现。`flagged_issues`
  含义不变（第 N 轮复查的最终修复列表）。详见 DESIGN §1.23。
- `events.pending[]` 是 router 的输出队列，由话题 agent drain。
- 无 `lock` 字段 —— 锁是并排的 `.lock` 文件（文件系统原子）。

## 设计备注

面向操作者的 `DESIGN.md` 索引；完整理由都在 DESIGN（单一事实来源 ——
不要在这里重复正文）。

- **全自动运行** —— 无人监督；不要复述用户动作（"你回了 no"）或说 "等待触发"。DESIGN §1.15。
- **reconcile 只在冷启动 / 监听器重启时跑**（非每个 cycle）；以持久化的 `last_reconcile_ts` 为下界。DESIGN §1.2.6 / §1.2.7。
- **事件驱动 daemon，不是 cron** —— OS 级 detached（`wscript`/`pythonw`），静默 spawn，空载 0 token。DESIGN §1.16。
- **daemon 与 listener 不会热重载** —— 改过它们的脚本后必须 kill 再拉起；过期 daemon 会照跑 cycle 但永不加载新代码。DESIGN §1.16。
- **合并队列：skip_ci rebase + 直接 API 合并** —— 绝不用 `glab mr merge --auto-merge` / `glab mr rebase`。DESIGN §1.6.6。
- **Rebase 冲突 → 挂起，等开发者 `ok` 再恢复** —— 不每周期重试。DESIGN §1.6.7。
- **机械终态迁移要往话题发诚实通知** —— 被动对账路径（`dispatcher._check_approved_topics`、`recover.py`）经 `terminal_notice` 发 `merged`/`mr_closed`（幂等），确保手动合并 / GitLab 端关闭 / 宕机补账都不会让话题停在最后一条回复。DESIGN §1.6.11。
- **Infra 故障自动重试**：走 job 级 `POST /jobs/{id}/retry`（不是 pipeline 级重试）。DESIGN §1.6.5。
- **所有飞书 post 用纯 post 格式**，绝不用卡片式；模板强制此格式。DESIGN §1.9.1。
- **两阶段 3rd-party 审查** —— 先审查并合并 3rd-party MR，再重置回主阶段。
  该重置由机器人自己的合并触发、背后没有飞书消息，因此必须（a）自行入队
  一条合成 pending 事件，否则主仓库审查永远不会被派发；（b）**先重新解析
  rage/chaos 的 head 与 `ack_stats`** —— 此时 ack 时记录的 SHA 已是几小时
  之前的，开发者通常在第三方阶段期间还在推送。DESIGN §2 / §1.6.12。
- **已确认修复的问题不再复查** —— 某一轮把问题 #m 判为 `addressed`（或 `obsolete`）后，后续每轮都直接沿用该结论，不再重新核查。`review_rounds.carry_verification` 让结论能熊过审查人回复序号时的 `flagged_issues` 重建（以前会被清空），spawn Context 直接把 `to_verify` / `carried` 两组序号告诉 agent。审查人重新标记该序号就会重新启用核查 —— 修复被回退时走这条路。DESIGN §1.4.8。
- **反转第 1 轮 triage** —— 第 1 轮先发给开发者确认（`DEV_TRIAGE`），主阶段与 3rd-party 阶段一致（§1.23.5）；开发者全部接受则直接进入修改、跳过裁决，否则审查人裁决开发者的异议（`ARBITRATION`）。两个转换都是机械处理。第 N 轮与零问题第 1 轮保留旧版审查人先决策的状态。DESIGN §1.23。
- **唯一批准动词 `ok`** —— 审查人在所有决策状态下都用 `ok` 批准；`pass`/`通过`/`lgtm`/`approved` 已移除，不再识别。在 `SIMPLE_REVISION`/`FULL_REVISION` 下 `ok` 是开发者的重审触发词，不是批准（那里没有批准动词，只能 `close`）。DESIGN §1.23.2。
- **superseding 根消息被撤回时恢复前驱话题** —— supersede 掉活跃话题的重复根消息若事后被撤回，dispatcher 会恢复被 supersede 的话题、回填漏掉的回复并发 `topic_reopened` 通知；Gate 4a 关闭线程丢弃有 WARN 日志。DESIGN §1.2.9。
- **合并后 cherry-pick** —— 合并时机器人把活跃发布分支（`rc_*` / `rc_next_*` / `rc_dev_*`，同 family 取最大编号）以 `p<N>` 代号发出询问，并把话题在 `closed/` 之外多留 24 小时；审查人回 `p1` / `p1 p2` / `no`。优先直接 cherry-pick，遇冲突或保护分支则建 MR。机械路径 —— 不 spawn agent。**跨仓库**：`lifecycle.merge_shas` 里的每个游戏仓库都会处理，各用各的分支名（rage `rc_p1`、chaos `rage/rc_p1`）；某仓库探测失败会在询问里 ⚠️ 点名并记审计 `cherrypick_partial`，不再静默漏掉。**`3rd_party/*` 一律不询问** —— 它们有自己的发布节奏；整单只有第三方合并时完全不发询问（审计记 `skipped_3rd_party`）。DESIGN §1.24 / §1.24.1 / §1.24.2。

## 调试 & 回放

管道级 bug（事件去重、router/drain race、锁处理、
`process_merge_queue` 状态迁移）不需要麻烦真实开发者提 MR ——
用 `scripts/replay.py` 在沙箱目录里驱动管道，外部 I/O 全部打桩。

```
python scripts/replay.py init /tmp/sb --from-closed <thread_id>
python scripts/replay.py inject /tmp/sb --thread <id> --content "ok" --sender approver
python scripts/replay.py pipeline /tmp/sb        # route + drain
python scripts/replay.py merge-queue /tmp/sb     # process_merge_queue，glab / Lark 全打桩
python scripts/replay.py show /tmp/sb --thread <id>
python scripts/replay.py lock /tmp/sb --thread <id> [--release]
```

`init --from-closed` 把已关闭话题克隆回 open，清掉每 MR 的运行时
状态（`state`、`pipeline_status`）和 `review_phase`，让回放从干净
状态开始。**不**覆盖 LLM 话题 agent —— agent 逻辑还是要真实话题。

## 附录：脚本内已实现的约束

这些在 `scripts/` 内部处理 —— **不要**在上层重复做：

- **UTF-8 无处不在**：每个 `open()` 用 `encoding='utf-8'`，每个
  `json.dump` 用 `ensure_ascii=False`。
- **自消息过滤**（`router.py` `_is_self_message`）：按机器人**两种 sender 形态**跳过它自己的消息（reconcile/历史用 `cli_*`，listener 用机器人的 `ou_…` open_id）。DESIGN §1.2.3。
- **撤回消息过滤**（`router.py` + `reconcile.py`）：跳过
  `deleted: true` 的消息。
- **系统消息过滤**（`reconcile.py`）：跳过 `msg_type == "system"`（入群/拉人通知），避免无可解析 root 的系统消息钉死 reconcile floor。DESIGN §1.2.4。
- **HTML 标签剥离**（`event_utils.py`）：去掉 `<p>...</p>` 等。
- **崩溃安全的事件路由**（`router.py`）：只有
  `topic_store.write_atomic` 成功之后才删原始事件文件。
- **每话题锁**（`topic_store.py`）：`O_CREAT|O_EXCL`，超过 10 分钟
  可被偷。话题 agent 用
  `with topic_store.LockKeepalive(LOCK_FILE):` 包住自己的工作 ——
  daemon 线程每 60 秒刷一次锁文件 mtime，让正常的长 review 不会被
  10 分钟陈旧规则误偷；30 分钟封顶后停止刷新，把"真挂掉"的安全阀
  留给确实卡死的 agent（DESIGN §1.8.3）。
- **回复工件落盘 + 话题回写**（`finalize_review.py`，由
  `spawn_topic_agent.md` §3 规则 2 调用）：agent 只构造一个 result JSON；
  `finalize_review.py` 负责对工件做 schema 校验、对 vars 做
  `render.py --check-only` 渲染校验（在落盘前抓出缺失的 `SUMMARY` 或
  未填占位符，避免 `reply_dispatcher` 反复重试毒性工件）、原子写入
  `cfg/replies/`、排空事件、持久化 `review.*` 与 audit、并释放锁。
  agent 不再手写这段胶水代码。完整审查的飞书文档由 `build_review_doc.py`
  一次性完成（拼正文 + 调 `lark_doc_helper` 创建）。
- **回复工件重试隔离**（`reply_dispatcher.py`）：每个工件的失败次数
  记录在 `cfg/replies/.retry_counts.json`；`RETRY_MAX = 3` 次失败后
  把工件移到 `cfg/replies/quarantine/` 并在话题里写
  `reply_artifact_quarantined` 审计。任何终态结果（成功 / drift /
  withdrawn / thread_missing）都会清空计数（DESIGN §1.19.1）。
- **superseded 工作转移**（`topic_store.donate_review_to_topic`、
  `reply_dispatcher._attempt_artifact_donation`）：当工件的 thread_id
  指向一个被 "superseded by new topic" 关掉的话题时，dispatcher 会
  根据 SHA 校验把工件改写到规范的后继话题上。话题 agent 在 preflight
  检测到 drift 且已经做了重活时，也走同一个 donation 路径
  （DESIGN §1.8.3）。
- **事件归一化**（`event_utils.normalize_listener_event`）：把
  websocket payload 拍平为 `{chat_id, sender_id, thread_id, root_id, content}`。
- **`om_` vs `omt_` 归一化**：优先 `root_id`（`om_`）而非
  `thread_id`（`omt_`）。混用会制造重复话题。
- **reconcile 根消息解析**（`reconcile.py`）：用批量 `thread_replies` 映射把每条消息归到其 ROOT `om_` id，绝不调用 `_resolve_root_id`。DESIGN §1.2.5。
- **根消息撤回自动关话题**（`dispatcher._close_withdrawn_topics`）：
  根消息被撤时关掉对应话题。若被关话题曾 supersede 过同单号的另一话题，
  `topic_reopen.reopen_superseded_predecessor` 会撤销该 supersede：无条件清理
  延迟 supersede 台账（抢在 `_retry_pending_supersedes` 之前）、把前驱话题
  恢复到关闭前状态、回填线程中漏掉的回复（合成事件走正常管道）、并发
  `topic_reopened` 通知。守卫遇瞬时错误一律放行（fail open）；只有确定性
  结论才阻止（前驱根消息已撤/已删、MR 已合并/关闭、同单号已有其他开放话题）。
  手动恢复 CLI：`python topic_reopen.py --withdrawn-thread om_... [--dry-run]`。
  DESIGN §1.2.9。
- **同单号 supersede**（`router.py`）：同一 ticket 来了新根消息时，
  先关掉旧话题再建新的。新根消息事后被撤回时自动撤销（见上面的
  reopen 条目，DESIGN §1.2.9）。
- **关闭线程回复丢弃有日志**（`router.py` Gate 4a）：目标线程已在
  `topics/closed/` 的事件被丢弃时，会在 activity.log 记
  `closed_topic_reply_dropped` WARN，并计入 router 摘要的
  `closed_thread_drops` —— 绝不静默。DESIGN §1.2.9。
- **Rebase 冲突挂起 + 确认**（`process_merge_queue.py` +
  `mechanical_reply_handler.drain_rebase_conflict_ack`）：合并队列 rebase
  冲突时设 `review.rebase_conflict_blocked`，`_check_approved_topics` 不再
  重新入队该话题；经 SHA 校验的开发者 `ok` 清除标志并恢复合并。不每周期
  重试，不 spawn agent（DESIGN §1.6.7）。
- **PowerShell / cmd.exe 不能起 detached 进程**：`Start-Process`
  在 MSYS2 下会挂；`cmd.exe /c start /min` 会弹出可见控制台。
  请用 Python `subprocess.Popen` +
  `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`。
- **lark-cli stderr 输出**：所有输出都到 stderr。listener 用
  `2>&1` 合并；健康检查同时看 `.log` 和 `.err` 的 mtime 避免
  误判 "过期"。

## 附录：机器人消息示例

机器人在飞书话题中发送的各类消息示例（按生命周期分组，含每条的触发场景与开发者/审查人回复选项），见交互式图示：

[Review Bot · 话题回复示例（交互式）](https://claude.ai/code/artifact/656c232d-a2cc-4a6e-8ac3-90fa4daaf950)

> 消息文案的规范来源始终是 `scripts/templates/*.json`（以及 `ack_new_topic.py` 内联的接单期关闭文案）；上面的图示是便于浏览的渲染版，改动模板后需同步重建。
