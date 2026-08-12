# 版本管理与自动发布方案（整合）

> 目标：代码有更新后，由**用户主动要求**、bot 自动生成**官方中文 release note** 并**纯文字**发到目标群（`FEISHU_CHAT_ID`）。版本号按**小版本递增 + 大功能跳 minor + 大版本仅用户授权**规则自动演进。

## 一、版本号规则（语义化，自增）

从 `v1.0.0` 起。每次发布按下列规则算下一个版本号：

1. **小版本自增（patch +1）**：默认。`1.0.0 → 1.0.1`。
2. **大功能更新（minor +1）**：当本周期内有「足够多的 `feat` 提交」判定为重大功能更新时，`1.0.0 → 1.1.0`。
   - 判定（可配阈值 `release.minor_if_feat_ge`，默认 `>=3` 个 `feat` 提交，或含任意 `breaking`/`feat!:`)：达阈值 → minor+1。
3. **大版本（major +1）**：**仅当用户明确授权**（`@机器人 发布大版本` 或显式 `major`）。bot 绝不自动 bump major；未授权时即使改动巨大也不动 major。
   - 大版本语义：`1.x.y → 2.0.0`（重置 minor/patch）。

优先级：**major(用户显式) > minor(阈值) > patch(默认)**。

## 二、发布流程（受控，仅用户对话触发）

```
用户：@机器人 发布            （或 发布 v1.1.0 / 发布大版本）
  │ @-gate 放行 → interact() → _cmd_release（guarded, owner 专属）
  ▼
_cmd_release:
  1) 定位"上个发布点" = 最近的发布 tag（vX.Y.Z）；无则首次从 v1.0.0 起、区间 = 全部历史
  2) 计算本周期改动 = git log <prev_tag>..HEAD（去掉 merge/噪音）
  3) 按规则算出新版本号（major/minor/patch），并**回执预览**：
     "将发布 v1.1.0：<note 预览>。确认？"（二次确认）
  ▼ 用户 @机器人 确认
  4) 生成官方中文 release note（N5：脚本分组 + LLM 润色 + 模板兜底）
  5) 打新 tag vX.Y.Z
  6) 纯文字调 feishu_notifier send-text-message 发到 FEISHU_CHAT_ID
  7) 回执成功/失败；更新 release-marker
```

## 三、Release Note 写法（N5 混合）

**固定章节模板（兜底 N3）**：
```
🆕 版本 vX.Y.Z 发布
📦 本版本概述          （LLM 润色 1-3 句）
✨ 新功能             （feat 提交，LLM 整理成官方中文）
🐛 问题修复           （fix 提交）
🧰 维护              （chore/docs/refactor/perf…）
⚠️ 注意事项          （breaking 或需人工操作项；无则省略）
```
**生成**（N2）：
- 脚本按 `type` 分组 `git log prev_tag..HEAD`；
- 每组喂 `_call_llm_simple` 润色成 1-3 句官方中文；
- 单节超过阈值（默认 6 条）折叠为「…共 N 项，详见 history/提交」；
- 纯中文。零 LLM 时回退 N1（直接分组标题）。

## 四、版本边界 / 标记

- **发布 tag**（`vX.Y.Z`）是本周期边界（首次从 v1.0.0 起）。
- 同时写 `docs/release-history.md`（可选留档）+ git tag。

## 五、配置（config.yaml 新增）

```yaml
release:
  enabled: true
  chat_id: ""            # 空则用 FEISHU_CHAT_ID / feishu.chat_id
  minor_if_feat_ge: 3    # 周期内 feat ≥3 → minor；默认 patch
  note_language: 中文
  note_llm: true         # 用 _call_llm_simple 润色官方文案；false=纯分组
  confirm: true          # 发布前二次确认（推荐）
notify:
  release_hook: false    # 否：不在 deploy/cron 自动发，只由用户对话触发
```

## 六、受控与安全边界

- `_cmd_release` guarded（owner 专属，`policy.yaml` 加 `release` 动作）。
- `major` 仅当用户显式要求（bot 不 auto）。
- 二次确认防手滑。
- 不在 deploy/apply.sh / cron 自动发布 —— **只有用户对话要求才发布**。

## 七、落地文件

| 文件 | 作用 |
|---|---|
| `jenkins/scripts/release_note.py` | 分组 + LLM 润色生成官方 note；返回建议版本（major/minor/patch 判定）|
| `jenkins/scripts/orchestrate.py` | `_cmd_release`（受控发布：预览→确认→打 tag→发群→回执）|
| `config.yaml` | `release:` 节 + `notify.release_hook` |
| `policy.yaml` | `release` guarded（topic_owner）|
| `feishu_notifier.py` | 复用 `send-text-message`（无新文件）|

## 八、待你最后确认的决策点

1. **大功能阈值**：`feat ≥3` 才算 minor（可调）；你要更严/更松？
2. **LLM 润色**：默认开（官方中文），可关（纯分组）。
3. **二次确认**：默认开。
4. **首次发布**：从 `v1.0.0` 起，区间=全部历史 —— 接受？
