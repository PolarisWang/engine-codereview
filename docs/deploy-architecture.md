# CodeReview Bot — 部署架构与运维手册（权威）

> 这是唯一权威的部署/运维文档。其他提到「systemd 常驻」的旧文档已过时，仅作历史参考。

## 〇、架构-D：交互与执行解耦（当前架构）

**交互（事件服务器）与执行（Jenkins）分离**，通过共享持久化的 pipeline-state + 共享 workspace 协作：

```
事件服务器（实时，只写"意图"，无 GitLab/Jira 凭证）:
  收消息 → 识别指令 → 权限/审计 → 写 topic.pending 到 state → 回文案
         （新话题/重审/apply/push/rollback 都只写意图，不真正执行）
        │  共享 persistence：/var/lib/report-server/daily（bind 进容器）
        ▼
Jenkins code-review-scan（每分钟，有凭证，consume 队列）:
  读 state 里所有 topic.pending → 逐个执行:
        re_review → 拉真 diff 重新审查
        apply     → 在共享 checkout git apply
        push      → 共享 checkout commit + push（GitLab 凭证在此）
        rollback  → 共享 checkout reset
  完成后写回结果 + 清 pending
```

**这解决的核心问题**：
- 重审/提交真正能拉到代码（凭证在 Jenkins），不再因事件服务器缺凭证而"缓存重放"。
- 交互实时（文案秒回）、写操作异步（等下一个每分钟 scan 执行）。你已确认接受此取舍。
- **分布式适配**：事件服务器（消息入口）与执行（Jenkins）天然解耦，可多 worker、排队、背压。

**关键文件**：
- `pipeline_state.py`：`pending` 字段 + `set/get/clear_pending` + `list_pending_topics` + `topic_lock_context`（per-topic 跨进程 `flock`，锁目录默认 `/var/lib/report-server/daily/cr-locks`）。
- `orchestrate.py`：`consume_pending` / `consume_all_pending`（执行器）；`_confirm_apply/_confirm_push/_rollback/re_review` 均改为「写 pending 意图 + 回已记录」，不再本地执行。
- 共享 workspace：`/var/lib/report-server/daily/cr-workspace`（事件服务器与 Jenkins 都用它，保证同一份 checkout/结果文件）。
- `Jenkinsfile`：`code-review-scan` 里新增 `orchestrate.py consume` 一步，消费所有 pending。

**workspace 释放策略**（"什么时候释放"）：
- `ops/cleanup.sh`：把 `CLOSED` 且超过保留期（默认 30 天）的话题归档到 `topics_archive`，并删除其 `result_*.json`（若仍被活跃话题引用则保留）。默认 dry-run，`--apply` 才真正执行。
- repo checkout 随 topic 归档/无引用后由清理脚本一并释放（同一共享 workspace）。
- 保留期可调 `--retention N`。建议并入 host cron 每天跑一次 `--apply`。

---

## 一、整体拓扑

（原拓扑以"交互↔执行分离"为准，见上节；下述为运行/部署层面的物理关系）

```
┌─ 开发/配置层（host）──────────────────────────────────┐
│  /home/debian/agent/engine-codereview（git 仓库，权威）    │  ← 唯一要改、commit、push 的地方
│    deploy/apply.sh  startup.sh watchdog.sh ops/            │
│    config.yaml policy.yaml deploy/env/.env.example         │
└──────────────┬───────────────────────────────────────────┘
               │ 部署：`deploy/apply.sh`（见下）
               ▼
│ 共享 bind：host /home/jenkins/cr  ⇔ 容器 /home/jenkins      │
│   运行代码 checkout = host /home/jenkins/cr/workspace/      │
│   code-review-pipeline（容器内 /home/jenkins/workspace/...）  │
└──────────────┬───────────────────────────────────────────┘
               │
│ 持久卷：host /var/lib/report-server/daily  ⇔ 容器同名        │
│   cr-env/env.sh（真实密钥，重启不丢）                       │
└──────────────┬───────────────────────────────────────────┘
               │
│ 容器内运行链：watchdog.sh → event_server.py --mode ws → Feishu│
└──────────────┬───────────────────────────────────────────┘
               │
│ host cron：ops/healthcheck.sh（探活） + ops/zombie-cleaner.sh│
               ▼
│ Feishu webhook 告警                                        │
```

**关键机制**
- 容器的 `/home/jenkins` 是 host `/home/jenkins/cr` 的 **bind mount（双向即时可见）**。所以"部署代码"= 从 host 更新共享 checkout → 容器立刻可见 → 重启 bot 生效，**无需进容器 git pull**。
- 容器的 `/var/lib/report-server/daily` 也是 bind（持久），用于放密钥/持久状态，**容器重启不清空**。

## 二、三个核心脚本（deploy/）

| 脚本 | 作用 | 何时跑 |
|------|------|--------|
| **`apply.sh`** | **唯一部署入口**：把 host 权威仓库同步到共享 checkout（git fetch+reset 到 origin/main），再 `docker exec` 跑 startup.sh 重启服务。代码变了才重启。 | 每次发布/改配置后 |
| **`startup.sh`** | 容器重启后恢复服务：起 watchdog（防重复）→ 等 bot 起来 → 报告 pid 压力。幂等。 | 容器重启后；apply.sh 会调它 |
| **`run-event-server-watchdog.sh`** | 进程级 supervisor：bot 崩了自动用绝对路径+持久 env 重启。 | 常驻（由 startup.sh 拉起） |

用法：
```bash
cd /home/debian/agent/engine-codereview
./deploy/apply.sh            # 部署 origin/main + 重启服务
./deploy/apply.sh --force    # 强制重启（即使代码没变）
```

## 三、密钥与配置（deploy/env/）

- **真实密钥**：`/var/lib/report-server/daily/cr-env/env.sh`（持久卷，容器可见）。watchdog/startup 默认从这里读。
- **模板**：`deploy/env/.env.example`（占位符，**入 git**）。真实值**永不入库**（`.gitignore` 已挡 `deploy/env/env.sh`）。
- 丢失后用 `.env.example` 里的方法从一个运行中的 bot 进程重新导出，或对照模板补全后跑 `apply.sh`。
- 应用配置：`config.yaml`（项目/Jira/模型）、`policy.yaml`（管理员/受控动作）。改完跑 `apply.sh` 生效。

## 四、监控与告警（deploy/ops/）

| 脚本 | 作用 | 建议调度 |
|------|------|---------|
| **`healthcheck.sh`** | 探活：容器内 watchdog/bot 进程、Feishu 长连接、日志新鲜度；异常发飞书 webhook 告警。 | host cron 每 5 分钟 |
| **`zombie-cleaner.sh`** | 探测容器僵尸进程；超阈值告警；`clean`（AUTO_RESTART=1 时）自动重启容器清僵尸并恢复服务。 | `warn` 每 5 分钟；`clean` 按需 |

host cron（根 crontab，追加）：
```cron
*/5 * * * * /home/debian/agent/engine-codereview/deploy/ops/healthcheck.sh   # 探活告警
*/5 * * * * /home/debian/agent/engine-codereview/deploy/ops/zombie-cleaner.sh warn  # 僵尸阈值告警
```
> `ops/*.sh` 用 `sudo`/host 侧读容器，不用在本来就紧张的容器里 fork。

## 五、僵尸进程问题（根因 + 处置）

- **根因**：容器 PID1 = Jenkins agent(java)，**不 reap 孤儿**。Jenkins 扫描 job（Jenkinsfile `cron: 每 30 秒`）频繁 `sh`→`git`，孤儿累积成僵尸，顶爆容器 pids 上限 → 所有 fork EAGAIN（review 卡死）。
- **临时缓解**：`zombie-cleaner.sh clean` 或手动 `docker restart chaos-agent-cr`（清僵尸，PID1 变新），重启后 `startup.sh`/`apply.sh` 恢复服务。
- **长期**：需要（a）降低 Jenkins 扫描频率/避免孤儿（Jenkins master job 配置，超出本仓库），或（b）让容器 PID1 换成能 reap 的 init/supervisor（改共享 agent 镜像，需 CI 协调）。
- **报警阈值**：`THRESHOLD`（默认 5000）。

## 六、容器重启后的恢复清单

容器一旦重启（无论为何），僵尸/进程全清，需要把它拉回来。两种情况：
1. **手动**：`docker exec chaos-agent-cr bash /home/jenkins/workspace/code-review-pipeline/deploy/startup.sh`
2. **自动化**：`./deploy/apply.sh --force`（同步到最新 + 重启服务）

> 注意：容器 init（entrypoint）只拉起 Jenkins agent，**不会**自动起 watchdog/bot。当前未做"随容器 init 自动起"（需改 agent 镜像）。用 startup.sh/apply.sh 即可。

## 七、当前已知边界 / 不做

- **不随容器 init 自动起 watchdog**：因不进他人 agent 镜像，靠 startup.sh/apply.sh 手动/计划任务拉起。
- **Jenkins 扫描频率**（每 30 秒）不在本仓库控制；僵尸根因的根治需要 master/CI 侧配合。
- `deploy/feishu-event.service` + 旧文档的 systemd 方案：**不用于当前容器环境**，仅作历史/备选参考。

## 八、快速操作速查

```bash
# 部署最新代码
cd /home/debian/agent/engine-codereview && ./deploy/apply.sh
# 容器重启后恢复服务
docker exec chaos-agent-cr bash /home/jenkins/workspace/code-review-pipeline/deploy/startup.sh
# 手动看健康
sudo /home/debian/agent/engine-codereview/deploy/ops/healthcheck.sh
# 看僵尸
sudo /home/debian/agent/engine-codereview/deploy/ops/zombie-cleaner.sh probe
```
