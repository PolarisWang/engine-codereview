# CodeReview Bot — 权威说明（2026-08-10 更新）

> 本文是**唯一权威**的总览/交接文档。结合 `deploy-architecture.md`（部署）、`full-pipeline-status.md`（历史流程）、`jenkins-credentials.md`（凭证）阅读。本文件覆盖：整体架构、完整流程、配置化、监控、本轮全部修复、已知边界。

## 一、当前架构（arch-D：交互/执行解耦）

```
飞书群消息
  └ event_server(wsl长连接)  — 只有飞书/LLM 凭证, 无 GitLab/Jira 凭证
        识别新话题(Jira/MR URL) → 交 Jenkins scanner
        回复/@bot            → interact(命令路由/答疑)
  └ Jenkins scan(每30s cron)  — 有全部凭证
        consume_all_pending  — 消费排队意图(re_review/apply/push/优化)
        run_autoclose        — 【F1新增】独立驱动, 关闭闲置话题
        ci-poll              — 回填 MR CI 状态
  └ 共享 workspace /var/lib/report-server/daily  (bind, 跨容器可见)
```

## 二、完整流程

```
发 Jira/MR → scanner → review(全量中文文字消息, 不折叠)
  → 用户 @优化  → 自动改码(claude -p deepseek) → 自动 push → 自动建/更新 MR
  → 用户 @关闭/4 → 关闭话题, 释放 fix-MR/分支
  → 闲置 IDLE_CLOSE_DAYS 天 → 独立 auto-close 驱动自动关闭
```

- **命令词**：`优化`(自动改码+建MR) / `改码`(手动 staged) / `指引` / `MR单` / `关闭`/`4` / `状态`。
- 命令词、文案全在 `config.yaml` 的 `commands:` / `messages:`，改词不碰代码。

## 三、配置化（方案C）— 改这文件即可，不碰代码

改 `config.yaml` 后重启服务生效：
- **`commands:`** — 命令词表（优化/改码/指引/MR单/状态/关闭）
- **`messages:`** — 所有用户可见文案（60+ 条），模板用 `{name}` 占位 `.format`
- **`lifecycle:`** — `idle_close_days`(自动关闭天数) / `auto_close_mr`
- **`concurrency:`** — `max_reviews`(并发 review 上限)
- **`claude:`** — 模型 / max_tokens
- **`projects:` / `workspace:` / `event:`** — 项目 / 工作区 / 事件服

代码侧：`config.py` 加载 yaml 暴露 `MSG`/`CMD` + `M(key, **kw)`。用户可见文案一律经它读，不再 hardcode。

## 四、本轮全部修复（相对 git 历史的要点）

| 域 | 修复 |
|---|---|
| 改码/建MR | per-topic 唯一 fix 分支(#22避免R1漂移)、push 认证(_auth_git_env)、MR 根因(detect截断+409复用)、分支单源(_fix_branch)、孤儿分支释放(close删实际分支)、fix-MR target=review分支 |
| 交互 | 确认命令空格容错、建议补丁不覆盖改码、回执去"合并"措辞、agent loop 只读(方案C无写工具)、方案C双通道新卡 |
| review 展示 | 全量中文文字消息(不折叠)、分片(_split_text) |
| 生命周期 | F1 独立 auto-close 驱动、F2 last_user_activity 闲置判定、F3 防误删 MR |
| 并发 | 排队吞吐(nFresh 1→3, cap 4→6) |
| 安全 | 路径穿越(_safe_checkout_path)、MR归属(iid台账)、token不泄漏 |
| 运维 | apply.sh 改码后自动重启 bot、健康/僵尸告警 |

## 五、监控（route③ + 方案C）

- **VictoriaMetrics**(:8428) + **Grafana**(:9300, admin/abcd@1234) + **node-exporter**(:9100, host proc 看进程/僵尸)。
- collector(5s)→textfile→VM→Grafana 18面板：主机/服务/话题/资源（含"逐话题独享 vs 项目共享"分层, 回答"关哪个释放多少"）+ 趋势 + $topic/$phase变量。
- 告警：bot/watchdog/GitLab 不可达 → 飞书 webhook（**已验证通路**发测试成功）。
- 访问：`http://10.10.1.173:9300`（内网）。监控容器 `restart: always`，host 重启后 docker 自动恢复。

## 六、测试

`PYTHONPATH=.deps-pytest python3 -m pytest` — 66 用例，覆盖全部核心逻辑（幂等MR、分支单源、auto-close、权限、路径安全、并发）。

## 七、已知边界 / 不做

- **bot 不随容器 init 自动起**：container 重启后需 `startup.sh`/`apply.sh` 拉起（监控容器则自动恢复）。
- **并发满时排队 topic 靠 scanner 重新选中群消息复入**：若消息不可见可能滞留 — 待 F 加固。
- **内部日志(print/_log/stderr ~64处)不迁 config**：职责不同(调试/结构化监控输出)，迁无收益。可选加 config.yaml `logging.level` 控制详细度。
- **`cr-env/env.sh` 内含真实凭据**：入 .gitignore，丢失后按 .env.example 的导出方法重建。

## 八、快速操作

```bash
./deploy/apply.sh                # 部署 main + 重启 bot（代码变化自动重启）
docker exec chaos-agent-cr bash /home/jenkins/workspace/code-review-pipeline/deploy/startup.sh  # 容器重启后恢复
sudo /home/debian/agent/engine-codereview/deploy/ops/healthcheck.sh  # 健康探活
```

---

## 九、review 输出（方案C: skill 模板 + 相关机制）

bot 的 review 结果按 **code-review-skill PR 模板**输出(中文):
```
# 🔍 Code Review 报告（依据 code-review-skill）
## 📋 Summary   ## ✅ Strengths   ## 🔍 Architecture & Performance
## 🔴 [blocking] 必须修复   ## 🟡 [important] 应处理   ## 🟢 [nit] 可选
## 📊 总结
```
由 `code_reviewer._build_markdown_from_findings(meta)` 渲染; `_call_llm_batch` 返回
summary/strengths/category, review_with_claude **跨批次聚合**后用全量 findings + 聚合
meta 重渲染单份完整模板(避免大 MR 分 8 批时 Strengths 只来自末批)。
`run()` 直接用该 render 出的 review_text 发出(不经 feishu_notifier 重渲染)。

**缓存失效**: `.review_cache` 按 `v<REVIEW_CACHE_VERSION>_<key>_<repo>_<hash>.json` 命名;
渲染/格式变化时**把 REVIEW_CACHE_VERSION+1** 即让旧缓存忽略,不误复用旧格式。
当前版本=2。

## 十、已知坑 / 注意

| 坑 | 说明 |
|---|---|
| 缓存绕开新格式 | 渲染变化后若没 bump 版本, 旧缓存会复用旧格式。已由版本号解决; 但仍建议测试新模板时用新分支/无缓存话题。 |
| 分批 review | 大 diff 分 N 批("第 N/M 部分"), 每批独立 LLM; meta 已跨批聚合。 |
| 发卡分条 | review 超 45000 字符会分多条普通消息(非重复, 是分片)。超大 review 非单条。 |
| CI 不触发 | `.gitlab-ci rules` 只认 merge_request_event, ci-poll 只跟踪不触发。 |
| bot 不随容器 init 起 | 容器重启需 startup.sh/apply.sh(监控容器自动恢复)。 |
| 进度卡显示 | 显示 jira_key 或短 id(非一长串 message_id); `\n` 已转真换行。 |

## 十一、端到端自测

`python tests/e2e/closed_loop.py --jira-url <URL>` 在容器内驱动完整闭环
(review→优化→建MR→验证→关闭清理), 用于回归。pytest: `PYTHONPATH=.deps-pytest python3 -m pytest`(66 用例)。
