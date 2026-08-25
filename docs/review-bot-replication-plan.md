# Review-Bot 复刻 —— P0/P0.5 决策门记录

> 方法论 **方案 D**：保留 linux/Jenkins/feishu-http 外壳，照搬 rage 的质量引擎
> （agent 契约 + 闭环状态机 + round-N 复审 + 人审 + 复杂 doc），完全对齐 rage 标准，
> 尽可能照搬它的脚本与 skill。

## 已拍板的 4 个决策

| 决策项 | 结论 |
|---|---|
| 方案 | **D 混合**（保留外壳，照搬引擎） |
| 模型 | **全切 Opus 级**（`claude-opus-5`，容器内 agent spawn 用） |
| 范围 | M1+M2+M3+M4 全进本轮（内核+闭环+人审+复杂飞书doc）；**M5 合并队列/cherry-pick 暂缓** |
| 部署 | 灰度切换（新流程对新话题启用，存量 DONE/CLOSED 映射到新终态），`重新审查` 走新流程 |

## P0.5 平台探测结论

### R1 ✅ 容器 claude Opus 可用（探测通过）
- 容器 `chaos-agent-cr` 有 `/usr/local/bin/claude`
- 端点 `https://llm-api.booming-inc.com`（Anthropic 兼容代理）
- 配置模型映射：
  - `ANTHROPIC_MODEL=deepseek-v4-flash`
  - `ANTHROPIC_DEFAULT_SONNET_MODEL=deepseek-v4-pro`（容器把这当 sonnet 别名）
  - `ANTHROPIC_DEFAULT_HAIKU_MODEL=deepseek-v4-flash`
- `claude --model claude-opus-5` **可运行**，且能执行完整工具循环（bash 已实测）+ 返回 `{"model": "claude-sonnet-5"}` 同构。
- **结论**：容器内 spawn Opus 级审查 agent 可行，无需改端点。
- **注意**：`claude-opus-5` 是否映射到真 Anthropic Opus 由 `llm-api.booming-inc.com` 代理决定。灰度期需抽检几单确认真实质量达到 opus 水准，若代理把 opus 别名也落到 deepseek 则需 P1 同步走真 Anthropic API。

### R2 ⚠️ 飞书 doc 权限（无法自动探测，转人工验证，不阻塞 P1）
- 现有 `feishu_notifier.py` 只封装 IM（消息/回复/卡），**无 docs/drive 封装**。
- 尝试用 bot app 取 tenant token 做范围探测失败：`code:10003 invalid param` —— 无法安全拿到真实 app_secret 来主动探测，也不应扫容器 secret。
- 代理端点 `llm-api` 在容器内，`lark-cli` 不存在（Windows 专属工具，我们不能用）。
- **结论**：M4 的飞书 doc 落地**依赖人工验证 app 是否已授权 `docx:document` / `drive:drive` scope**。
  - **Plan A（若已授权）**：写 `lark_doc_helper.py` http 版（tenant token → create docx → grant drive permission.members → tenant_readable public link）。
  - **Plan B（若未授权）**：复杂审查降级为**分页长贴**（thread 多段卡），doc 延后到权限就绪。**PlanA/PlanB 由同一开关切换**，不阻塞其余流程。

### G1 ✅ target 占位
- approver open_id 列表：按项目写入 `config.yaml`（`review.approver_open_ids`），P2 前需用户提供。

## 实施顺序（照搬 rage 脚本，逐文件落）
| 阶段 | 内容 | 照搬来源 | 状态 |
|---|---|---|---|
| P0 | Vendor rage review-bot 到 `vendor/rage-review-bot` | 上游 c035b16 | ✅ 完成 |
| P0.5 | 平台探测 R1/R2 | — | ✅ R1过 / R2转人工 |
| P1 | **契约化审查 agent（Path A）** | `spawn_topic_agent.md`/`state_machine.py` 等 | ✅ 容器实测通过 |
| P2 | 闭环状态机 + 机械回复 | `mechanical_reply_handler.py`/`reply_parser.py` | ✅ 完成 (222125a) |
| P3 | round-N 增量复审 | `incr_base.py`/`review_rounds.py` | ✅ 完成 (c492f08) |
| P4 | 人审集成 | `gitlab_threads_http.py`/`manual_issue_verifier` | ✅ 完成 (885f0ba) |
| P5 | 复杂审查飞书 doc | `build_review_doc_http.py`(PlanA doc/PlanB 长贴) | ✅ 完成 (16d5cb1) |
| P7 | 灰度上线 + 存量迁移 | `config review:` 开关 + runbook | ✅ 代码/runbook 完成；部署待执行 |

## P1 实测结论（容器，commit 5911e5f）
- **Path A agent 真实可用**：容器 `code_reviewer --agent` spawn claude-opus-5（7 turns / ~107s），返回 rage 标准结果：
  ```
  🔍 Code Review — 2 项
  #1 [严重] [engine] asset.cpp:3-3 update: 内存泄漏 → 修法
  #2 [严重] [engine] asset.cpp:2-2 Asset: 裸指针违反 rule of three …
  📊 严重2 / 中0 / 轻0 / 建议0
  ```
- **质量对齐 rage**：findings 绑定真实行（file:line）、agent 做了整文件 scope 验证（读了 hunk header 确认类完整）、严重/中/轻/建议 + 排序 + `[Repo] file:line` 前缀。
- **容器坑已修**：`claude -p --output-format json` 信封解包 + fenced-JSON；`-p` 会话无 review_findings 工具（改纯 JSON 契约）；容器无 pygments（`_lex_identifiers` 加正则回退）。全在 commit 5911e5f。
- **残余**：R2 飞书 doc 权限待人工验证（PlanA/PlanB 开关）；灰度期抽检 opus 别名是否落地真 Anthropic Opus。

