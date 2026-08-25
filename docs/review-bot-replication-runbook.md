# Review-Bot 复刻 —— P7 灰度上线 & 存量迁移 runbook

> 方案 D 已实现 P0-P6（rage 质量引擎复刻 + 容器实测 Path A agent）。本文是
> 上线操作手册：先灰度、再存量迁移、最后全量。**实际部署由 `deploy/apply.sh`
> 触发，这里给出执行的次序、开关、回滚与验证。** 涉及真实 GitLab MR / Feishu
> 交互的端到端需在部署环境完成；本仓库把这些都写成了开关（config/env）以便灰度。

## 0. 上线前必须人工确认 / 准备（否则停在这一步）

- [ ] **R2 飞书 doc scope**：在飞书开放平台确认 app 已授权 `docx:document` /
      `drive:drive` 权限；未授权则 `review.doc_enabled=false`（走 PlanB 长贴）。
- [ ] **各项目 approver open_id**：填 `config.yaml projects.<id>.approver_open_ids`
      （留空回退 `policy.yaml` 的 admins）。
- [ ] **app_id/app_secret/gitlab token** 已注入运行容器 env（现有部署即有）。

## 1. 灰度切换（先对部分项目 / 新话题）

节点与开关：

| 功能 | 开关 | 灰度建议 |
|---|---|---|
| Path A agent 审查（claude-opus-5） | `review.agent_enabled` 或 env `REVIEW_AGENT=1` | 先对 1 个项目开 |
| round-N 增量 | 自动（closure `re_review` + topic `last_review_commit`） | agent 开后即生效 |
| 人审集成（DiffNote→写回 resolved） | `review.mark_resolved` / env `MARK_RESOLVED=1` | 先关，人工熟悉后再开 |
| 复杂审查 doc | `review.doc_enabled`（PlanA doc / PlanB 长贴） | 依 R2 结果 |
| 闭环 dev-triage | 自动（topic 有 `review_state` 即接管回复） | agent + REVIEW 生效后 |

**灰度方式**：临时只对某个项目在其 `projects.<id>.approver_open_ids` 配置 +
群内只对该项目话题发单；其它项目维持旧流程。验证通过后放开到全量群。

**deploy**：
```bash
cd <宿主 authoritative repo>
# 改好 config.yaml / config 后:
./deploy/apply.sh            # 只同步 + 重启（代码没变则不重启，用 --force 强制）
./deploy/apply.sh --force    # 代码变了强制重启 bot
```

## 2. 存量 topic 迁移

现有 topic 的 `phase`（单程流水线) 与 rage 闭环状态不同。迁移策略：

| 旧 phase | 新处置 | 理由 |
|---|---|---|
| `CLOSED` / `FAILED`（终态） | 原样保留 | 已是终态，不进入闭环 |
| `DONE`（旧单程成功） | → `CLOSED`（`closed_reason="迁移:旧版本单程审查完成"`） | 避免旧 findings 混进闭环状态机；历史卡保留在 topic |
| 进行中（`REVIEWING/PARSING/SCANNED`） | `reset_for_retry` 走新流程 | 重新按 rage 闭环跑 |

迁移脚本（幂等）轮廓：
```python
# scripts/migrate_legacy_to_closure.py (示意，实施时落成 CLI)
for key, t in topics.items():
    if t.get("phase") in ("DONE",):
        pipeline_state.set_topic_fields(sf, key, phase="CLOSED",
            closed_reason="迁移:旧版本单程审查完成")
    elif t.get("phase") in ("REVIEWING","PARSING","SCANNED","NOTIFYING"):
        pipeline_state.reset_for_retry(sf, key)   # 新流程重跑
```
迁移前后各 `pipeline_state.py query` 一遍比对；迁移只改动存量 topic，不动新话题。

## 3. 端到端验证清单（部署环境跑，本仓库单测覆盖纯逻辑）

1. **Path A agent**：对一真 MR 触发 review → 卡含 `#N [严重|中|轻|建议] [Repo]
   file:line 问题`；随机抽查一种 `[Repo] file:line` 能 `git show` 到真实行。
   - 若结果不理想（模型代理把 opus 别名落到 deepseek）→ 调 `REVIEW_AGENT_MODEL`
     或走真 Anthropic API。
2. **闭环**：开发在群话题回 `1 3 5` → bot 记录 dev_triage → 开发 push 后回 `ok`
   → 触发 round-2 增量（只 diff 新增）→ 修完 `done` → 审查人 `ok`/`close`。
3. **人审**：MR 上人工评论进 topic；`@bot 同步` 手动刷新；确定修复后 GitLab 写回
   resolved=true。
4. **复杂 doc**（若 R2 已授权）：>5文件 出飞书 doc + thread 只发 index；无 scope
   则出长贴。
5. **回归**：本仓库 `pytest jenkins/tests` 全绿（243 用例）再部署。

## 4. 回滚

- 单项目灰度发现问题 → 把该项目 `approver_open_ids` 清空（回退 admins）或对该
  项目 `REVIEW_AGENT=0` 关掉 agent，其它项目不受影响。
- 全量回归 → `git revert` 相关 commit 或切回 `f644a4f` 前的部署，`apply.sh`
  重放即可（容器 checkout 回退 + 重启）。
- 存量迁移出错 → topic 已是 CLOSED，无破坏；可手动 reset。

## 5. 成本观测（全 Opus 是全量最大的不可测项）

- rage 自带 token 遥测；我们 http/子进程未接入账户级统计。上线后建议按 topic
  记 `last_review_commit` + 消耗（可在 `pipeline_state` 记一个 `token_approx`）。
- 只对新话题开 agent → 先观察 2-3 天单票成本，再决定是否需要按项目限流/降级
  （`REVIEW_AGENT_MODEL` 是 env 级开关，可对某项目单独设）。
