# Code Review Bot — 完整流程与待办事项

## 当前状态（2026-08-07）

### ✅ 已完成且验证通过

| 模块 | 状态 | 说明 |
|------|------|------|
| Review 绑定真实 MR | ✅ | jira_parser 用 GitLab 反查绑定 OPEN MR（source_branch 存在），不再用 closed MR / 裸分支 |
| Review diff 源 | ✅ | code_reviewer 用真实 git 分支 diff（非 MR diffs API），findings 反映真实代码 |
| engine base 修正 | ✅ | MR 绑定后 engine_base = MR target(master)，不再用 config main（导致空 diff） |
| 空 diff 缓存防护 | ✅ | da39a3ee（空 diff hash）不被缓存/复用 |
| 持久 env 凭证加载 | ✅ | orchestrate.py 顶部从 cr-env/env.sh 加载 GITLAB_TOKEN 等 |
| Review 重点突出卡片 | ✅ | build_summary_text：一句话结论 + Top findings(critical优先) + 交互指引 |
| 可靠交互路由 | ✅ | 1(补丁)/2(重审)/3(解释)/4(关闭)/MR单/指引/预览/状态 — 直接路由不靠 LLM 猜 |
| @提及剥离 | ✅ | @_user_1 MR → 首词 MR（路由命中） |
| agent 循环耗尽 wrap-up | ✅ | 不再发 [tool_use] 占位 |
| `指引` 精确修改指引 | ✅ | 基于真实 checkout 内容，LLM 给精确改动位置/方式 |
| MR 检测/创建 | ✅ | _create_or_get_mr：检测 fix 分支 OPEN MR，无则创建（有改动才建，不建空 MR） |
| MR 单区分评审/修复 | ✅ | 评审 MR(原始) vs 本次修复 MR(新)，填真实新 URL |
| 话题关闭清理 OPEN MR | ✅ | _cmd_close 调 _close_topic_created_mrs 关掉 fix 分支的 OPEN MR |
| CI 状态跟踪 | ✅ | ci-poll 轮询 MR pipeline 状态回写卡片（跟踪不触发） |
| clone 缓存复用 | ✅ | code_reviewer 复用已 clone checkout + 增量 fetch |
| workspace 释放/清理 | ✅ | cleanup.sh：超龄 CLOSED 话题归档 + result 文件释放 + checkout 释放 + .review_cache 清理 |
| watchdog + startup + apply | ✅ | 进程级自愈 + 容器重启恢复 + 一键部署 |
| 健康监控 + 僵尸清理 | ✅ | healthcheck + zombie-cleaner host cron |
| **AI 自动改码(claude -p)** | ✅ | _agent_edit_one 用 `claude -p --model glm-5.2[1m]` 驱动，窗口定位 + 迭代，产出干净 git diff |

### 🔧 待完成（闭环最后几步）

| # | 待做 | 说明 |
|---|------|------|
| 1 | **自动改码 → commit → push 新分支** | _agent_edit_one 产出了 diff，但还没 commit + push 到 `{src}-fix-{task}` 新分支 |
| 2 | **push 后自动建 MR** | _create_or_get_mr 已就绪，push 后调它即可 |
| 3 | **`改码` 指令路由** | 在 interact 里加 `改码`/`自动修复` 指令 → 调 _agent_edit_preview → 展示 diff → 确认后 push+建MR |
| 4 | **多文件改码** | 当前 _agent_edit_one 只改 1 个 finding，需扩展到遍历所有 critical |
| 5 | **编译验证（可选）** | push 前可选跑编译检查（容器有 gcc/make），确保改动不破坏编译 |
| 6 | **端到端联调** | 群里发 Jira → review → 回复 `改码` → AI 改+push+建MR → 你确认合入 |

### ⚠️ 已知限制

- **`deepseek-v4-flash[1m]` 网关 503**：raw HTTP 调 `/v1/messages` 返回 model_not_found。只有 `claude -p` CLI 能通（它走 Claude Code 内部通道）。
- **`glm-5.2[1m]` 同理**：只能通过 `claude -p` 调用，不能裸 HTTP。
- **root 下 claude -p 限制**：`--dangerously-skip-permissions` 在 root 下被拒。需 settings.json `skipDangerousModePermissionPrompt=false` + `defaultMode=default`（已改）。
- **容器内无 claude CLI**：`claude` 只装在 host `/usr/local/bin/claude`（→ `/root/.hermes/node/bin/claude`）。自动改码只能在 host 侧跑，或把 claude 装进容器。
- **CI 不触发**：项目 `.gitlab-ci.yml` rules 只认 `merge_request_event`，手动 api 触发为空。ci-poll 只跟踪不触发。
- **review 大 MR 慢**：103 文件/95万字符的 diff，LLM 审查需 5-10 分钟（clone 缓存后 fetch 快，但 LLM 审查本身慢）。

## 完整流程（目标）

```
群里发 Jira/MR 链接
  → 事件服务器识别 → 交 Jenkins scanner
  → review(真实 git diff, 绑定 OPEN MR source_branch)
  → 重点突出结果卡(结论 + Top findings + 交互指引)
  → 用户回复 `指引` → 精确修改指引
  → 用户回复 `改码` → AI(claude -p glm-5.2[1m])自动改 checkout 里的代码
  → 展示 git diff 给用户确认
  → 用户确认 → commit + push 到新分支 {src}-fix-{task}
  → 自动建 MR(source=fix分支, target=master) → 返回真实新 MR URL
  → 用户在 GitLab 审核合入(需用户同意)
  → ci-poll 跟踪 CI 状态回写卡片
  → 用户回复 `4` 关闭话题 → 自动关闭 fix 分支 OPEN MR
```

## 关键文件

| 文件 | 作用 |
|------|------|
| `orchestrate.py` | 主逻辑：run(review) / interact(交互) / consume(执行) / ci(CI跟踪) / _agent_edit_one(自动改码) |
| `code_reviewer.py` | 审查引擎：prepare_repo(clone/fetch) + diff + LLM 审查 |
| `jira_parser.py` | Jira/MR 解析：GitLab 反查 OPEN MR + gitlab_branch_exists + gitlab_search_issue_mrs |
| `feishu_notifier.py` | 卡片渲染：build_summary_text(重点突出) + with_action_row(按钮,已退场) |
| `gitlab_ci.py` | CI 状态查询：pipeline_summary + render_ci_card_block |
| `pipeline_state.py` | 状态管理：topic/pending/close_topic/lock |
| `event_server.py` | Feishu ws 长连接：消息路由 + card.action.trigger(已退场) |
| `deploy/apply.sh` | 一键部署：同步共享 checkout + 重启服务 |
| `deploy/startup.sh` | 容器重启恢复：起 watchdog + bot |
| `deploy/ops/cleanup.sh` | workspace 清理：归档/释放/缓存清理 |
| `deploy/ops/healthcheck.sh` | 健康监控：bot 进程 + Feishu 连接 + state 新鲜度 |
| `deploy/ops/zombie-cleaner.sh` | 僵尸清理：探测 + 阈值告警 + 可选重启 |

## 配置要点

- **持久 env**：`/var/lib/report-server/daily/cr-env/env.sh`（GITLAB_TOKEN/FEISHU/ANTHROPIC 凭证，不入 git）
- **共享 workspace**：`/var/lib/report-server/daily/cr-workspace`（checkout + result + cache）
- **共享 checkout**：`chaos-cb-2`（code_reviewer 复用 clone 缓存）
- **claude CLI**：`/usr/local/bin/claude`（host only），settings.json `skipDangerousModePermissionPrompt=false`
- **EDIT_MODEL**：`glm-5.2[1m]`（通过 `claude -p` 调用）
- **host cron**：healthcheck(5min) + zombie(5min) + cleanup(每天3:15)
