# Review-Bot 复刻 —— 综合风险评估报告

> 针对「完全还原 rage codereview」（方案 D，P0-P7）的全面 review。
> 评估基于真实 MR 审查实测、容器 Path A agent 实测、代码审查、全量测试。
> 日期：2026-08-25。

## 1. 已确认达标（低风险）

- **Path A agent 真 MR 实测**：chaos-cb-2!7201 产出 10 findings，全绑定真实
  变更文件、0 编造（kept:10/flagged:0/dropped:0）、severity 校准合理、deep 全局
  视角（物理/渲染 mask 不对称、字符夹带）。**复刻质量地基达标。**
- **R2 飞书 doc scope**：真实创建探针 doc 成功，docx 权限已授权。
- **纯逻辑**：246 测试全绿，覆盖状态机/closure/round-N/人审/doc builder/迁移。
- **容器坑**：pygments 缺失回退、claude -p 信封/fence 解包、无 lark-cli/glab 的
  HTTP 适配。

## 2. 已修复的高/中风险（self-review 自查发现 + 已修）

| 风险 | 原问题 | 修复 commit |
|---|---|---|
| R1 🔴 **round-N 触发未串通** | closure `dev_reply`（dev push 后回 ok）只发消息+LOG，**不触发真实重跑**；P3 数据层就绪但触发器没接 | 6decfcc：`set_pending('re_review')` 走 executor |
| R5 🟡 **use_agent 只读 env** | `run()` 只读 env `REVIEW_AGENT`，config.yaml `review.agent_enabled` 形同虚设；executor 若只靠 config 会回退 HTTP，丢弃 agent 产物 | 57e0ecc：`use_agent` 也读 config |
| R3 🟡 **severity 双轨** | `severity_counts` 3 级但卡片 4 级 | 57e0ecc：新增 `severity_counts_zh` 4 级（保留 3 级兼容） |
| R10 🟠 **存量迁移无代码** | 只有 runbook 没有脚本 | 57e0ecc：`migrate_legacy_to_closure.py` + 单测 |
| R7 🟠 **复杂 doc 内容为空** | `create_lark_doc_http` 只建空 docx + grant，没写 review 内容 | 8b0cfe4：markdown→docx blocks 注入（真实 app 实测 4 blocks 落盘）；code_block14 400 → 改用 multiline inline_code text |

## 3. 待处理风险（按优先级）

### P0 —— 上线前必做
1. **executor 消费 `re_review` 是否透传 agent**（R5 残余）：已让 `run()` 读 config，
   但需真实验证 executor 确实触发 `orchestrate run` 且 `use_agent=True`（单测覆盖
   了逻辑，真实 Jenkins 需跑一单确认）。

### P1 —— 上线后尽快
2. **agent 长时无 keepalive/超时兜底**（R11）：rage 用 `keepalive-spawn` 子进程刷锁防
   >10min 大 MR 被误归档；我们 `_spawn_review_agent` 只有 `timeout`，无 keepalive。
   - 大 MR + opus 可能 >10min，若外部有 stale-lock 判定会误杀。目前我们没有这些
     判定（pipeline_state 无此逻辑），风险偏低，但大仓库要注意。
3. **并发 spawn 无统一上限**（R12）：`max_reviews` 限了 orchestrate 子进程，但 agent
   是再套一层 claude 子进程，资源未统一。灰度观察。
4. **全 Opus 无成本观测**（R9）：未接 token 统计。建议 topic 记 `token_approx` + 观察
   单票成本，必要时按项目降级 `REVIEW_AGENT_MODEL`。

### P2 —— 观察/优化
5. **人审/复杂 doc E2E 未实体验证**（R8）：`gitlab_threads_http` / `build_review_doc_http`
   的 API 调用只在真环境建了探针 doc，DiffNote 拉取/写回 resolved、doc 内容注入的
   E2E 待真 MR 跑通。
6. **`_set_review_closure_fields` 用 `topic.sender_id` 当开发者**：若话题是他人代开或
   sender 不是实际开发者，triage 授权会错。建议明确 developer 来源。

## 4. 现有配置/开关清单

| 开关 | 位置 | 说明 |
|---|---|---|
| agent 审查 | `review.agent_enabled` / env `REVIEW_AGENT` | 灰度切 agent vs HTTP |
| agent 模型 | env `REVIEW_AGENT_MODEL`（默认 claude-opus-5） | 分层/降级 |
| claude 路径 | env `CLAUDE_EXE` | 容器内 claude bin |
| 复杂 doc | `review.doc_enabled`（PlanA doc / PlanB 长贴） | 依 R2 权限 |
| 人审写回 resolved | `review.mark_resolved` | P4 双向同步 |
| round-N | 自动（topic `last_review_commit` + `carried`） | 增量 diff + 跳过已修 |

## 5. 上线顺序建议

1. ✅ （P0）复杂 doc 内容注入已实现（R7，8b0cfe4，真实 app 实测内容落盘）。
2. （P0）真环境跑一单完整闭环：发单 → agent 出卡 → 回 `1 3 5` → push → `ok` →
   round-2 增量 → `done` → approver `ok`；确认 executor 走 agent 且 round-N 生效。
3. （P1）灰度：先对 1 项目开 agent，2-3 天观察成本/质量；再放开全量。
4. （P1）跑 `migrate_legacy_to_closure.py --dry-run` → 自查 → 正式迁移存量 topic。
5. 持续：补 token 成本观测；人审/复杂 doc 在真 MR 上补 E2E。

## 6. 结论

P0-P7 实现完整、测试全绿、真 MR 审查质量达标。上线前唯一剩余 P0 是**确认 executor
走 agent 路径 + round-N 生效**（真环境跑一单闭环）。R1/R5/R3/R10/R7 已修复，消除了
复刻链路里的功能性断点与空 doc 缺陷。R9（全 Opus 成本）与 R8（人审/复杂 doc E2E）
建议在灰度期补齐观察。

