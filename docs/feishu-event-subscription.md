# 飞书事件订阅 + 话题交互配置指南

本服务（`jenkins/scripts/event_server.py`）让机器人在话题群里**实时**响应：
用户发新话题（带 Jira URL）→ 立即审查；用户 @机器人 / 话题内回复 → 实时作答，
实现"review 后给修复选项 + 对话"的交互能力。

### ⭐ 推荐：长连接模式（无需公网 IP / 回调 URL）

本机**没有公网 IP**，飞书回调 URL 无法直达。因此用飞书的**长连接（WebSocket）**接收事件：

- 事件服务**主动连接飞书的 WebSocket**，飞书把消息事件推送到这条长连接上。
- **不需要公网 IP、域名、回调 URL、HTTPS 反代。**
- 实时性一样（秒级），单触发链路设计不变。
- 启动：`event_server.py --mode ws`（默认），依赖 `lark_oapi`（已装）。

飞书后台只需：**开启 `im.message.receive_v1` 事件订阅，订阅方式选「长连接」，无需填回调地址。**

> 备选：`--mode webhook`（Flask 回调端点）仍保留，但本环境无公网，推荐用长连接。

---

## 一次性依赖

| 项 | 状态 |
|----|------|
| app_id / app_secret | ✅ `FEISHU_APP_ID` / `FEISHU_APP_SECRET` |
| `lark_oapi` | ✅ 已安装（长连接用） |
| 公网 IP / 回调 URL | ❌ 不需要（长连接） |

---

## 步骤 1：飞书开放平台开启「长连接」事件订阅（无公网）

1. 打开飞书开放平台后台：`https://open.feishu.cn/app/<app_id>/event`（或 开发者后台 → 你的应用 → 事件与回调）。
2. **「事件订阅」→「订阅方式」**：选「**长连接**」（WebSocket / Long Connection）。
   - ⚠️ **不要**选「请求地址 / Webhook URL 回调」——那需要公网可达。
3. 左侧「**事件**」→ 搜索并订阅「**接收消息** `im.message.receive_v1`」→ 启用。
4. 确认应用已开通相关权限（`im:message` 等，读消息/发消息）且已被拉进目标话题群。

> 长连接模式通常也支持自动重连；飞书后台一般还能看到「长连接状态」。

---

## 步骤 2：配置 app 凭据

长连接需要 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`：

- 写入 `.env`（`/home/debian/agent/engine-codereview/.env`），或
- config.yaml 的 `event:` 块加 `app_id` / `app_secret`，或直接设环境变量。

```yaml
event:
  # app_id / app_secret: 事件服务长连接用
  # app_id: cli_xxx
  # app_secret: xxx
  state_file: /root/.codereview-pipeline-state.json
```

（长连接模式无需 verification token / encrypt key —— 那是 webhook 回调模式用的。）

---

## 步骤 3：启动事件服务（长连接）

```bash
cd /home/debian/agent/engine-codereview
# 前台调试
FEISHU_APP_ID=cli_xxx FEISHU_APP_SECRET=<secret> \
  python3 jenkins/scripts/event_server.py --mode ws
# 看到 "long-connection mode" 且无报错 = 已连上飞书长连接
```

常驻（systemd）：
```bash
sudo cp deploy/feishu-event.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now feishu-event
journalctl -u feishu-event -f
```
（该 service 已用 `--mode ws`。）

---
## 交互指令约定（用户在本话题内回复）

默认审查完成后，卡片末尾会追加交互提示。用户在**该话题 thread 内 @机器人** 回复：

| 回复 | 动作 |
|------|------|
| `1` / `修复` | 生成修复补丁预览（基于 critical findings 的建议） |
| `2` / `重审` | 重新审查（diff 未变则复用 diff-hash 缓存） |
| `3 <关键词>` / `解释 <x>` | 用 LLM 解释某个 finding |
| `/状态` / `状态` | 当前该话题的审查状态 |
| 直接提问 | 基于该话题 findings 自动答疑 |

> 所有回复都更新**同一条话题卡片**（`render_msg_id`），不新起话题；符合"只回复"约定。

---

## 故障与兜底

- **事件服务挂了**：长连接断开 → 现有 Jenkins cron 扫描仍按周期处理新话题（兜底），靠 `pipeline_state` 的 processed/terminal 去重，不会重复。长连接断开会由 `lark_oapi` 自动重连。
- **收不到事件**：确认后台**订阅方式选的是「长连接」**（而非 Webhook/回调 URL），且已开启 `im.message.receive_v1`、应用已被拉入目标群。查看日志应有 `[event] NEW TOPIC ...` / `[event] REPLY ...` 路由行。
- **应用未应答**：确认启动时 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 正确、`lark_oapi` 已装；后台「长连接」状态应显示已连接。
