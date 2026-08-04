# 飞书事件订阅 + 话题交互配置指南

本服务（`jenkins/scripts/event_server.py`）让机器人在话题群里**实时**响应：
用户发新话题（带 Jira URL）→ 立即审查；用户 @机器人 / 话题内回复 → 实时作答，
实现"review 后给修复选项 + 对话"的交互能力。

> 需要先在飞书开放平台后台配置事件订阅，让消息事件回调到本服务。

---

## 一次性依赖（已具备 / 需确认）

| 项 | 状态 |
|----|------|
| app_id / app_secret | ✅ 已有（`FEISHU_APP_ID` / `FEISHU_APP_SECRET`） |
| 本机端口可监听 | ✅ event_server 监听 `0.0.0.0:8085` |
| 回调可达性 | ⚠️ 见下「回调 URL」；需要飞书能访问到该端点 |
| verification token / encrypt key | 需在飞书后台生成并填到配置 |

---

## 步骤 1：飞书开放平台配置事件订阅

1. 打开飞书开放平台后台：`https://open.feishu.cn/app/<app_id>/event`（或 开发者后台 → 应用 → 事件与回调）。
2. 在「事件订阅」→「订阅方式」，选 **将事件发送至开发者服务器**（自定义机器人 / 自建应用）。
3. **请求地址（回调 URL）**：填事件服务的地址，**末尾要有 `/webhook/event`**：
   - 内网可达时：`http://10.10.1.173:8085/webhook/event`
   - 若飞书要求公网 HTTPS：需要一层 HTTPS 反代把 `https://<域名>/webhook/event` 转发到本机 `8085/webhook/event`（见「HTTPS 反代」）。飞书对回调要求通常需要公网+HTTPS。
4. 点「保存」，飞书会向该 URL 发一个 `url_verify`（type=url_verify）请求做验证：event_server 会原样返回 `challenge`，验证即通过。
5. 记下后台显示的 **Verification Token** 和 **Encrypt Key**（若启用加密）。
6. 在**「事件」**里订阅 `接收消息 im.message.receive_v1`（在「消息与群组」分类下），启用。

---

## 步骤 2：把 token / key 填进配置

把后台的 Verification Token / Encrypt Key 填到 `config.yaml` 的 `event:` 块：

```yaml
event:
  port: 8085
  verification_token: "<后台VerificationToken>"
  encrypt_key: "<后台EncryptKey>"   # 若启用了加密；未启用留空
  state_file: /root/.codereview-pipeline-state.json
```

或通过环境变量注入（`.env` / systemd）：`FEISHU_VERIFICATION_TOKEN`、`FEISHU_ENCRYPT_KEY`。
event_server 会优先读 env，其次读 config.yaml。

---

## 步骤 3：启动事件服务

复用 systemd unit `deploy/feishu-event.service`：

```bash
sudo cp deploy/feishu-event.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now feishu-event
# 观察日志
journalctl -u feishu-event -f
```

手动启动（调试）：
```bash
cd jenkins/scripts && python3 event_server.py --host 0.0.0.0 --port 8085
```

---

## HTTPS 反代（若飞书要求公网 HTTPS）

若飞书后台要求 `https://` 回调，在本机（或网关）加一层反代转发：

- **nginx**（若本机可装）：
```nginx
server {
  listen 443 ssl;
  server_name cr-callback.example.com;
  ssl_certificate     /path/cert.pem;
  ssl_certificate_key /path/key.pem;
  location /webhook/event {
    proxy_pass http://127.0.0.1:8085;
    proxy_set_header Host $host;
  }
}
```
- 或任何已有的 ingress / 云负载均衡转发 `https://<域名>/webhook/event → 10.10.1.173:8085/webhook/event`。

回调 URL 就填 `https://<域名>/webhook/event`。

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

- **事件服务挂了**：飞书不再实时回调 → 现有 Jenkins cron 扫描仍按周期处理新话题（兜底），靠 `pipeline_state` 的 processed/terminal 去重，不会重复。
- **验证失败**：若后台提示 URL 验证失败，检查：URL 末尾 `/webhook/event`、端口可达、verification token 匹配。
- **消息没触发**：确认后台已订阅 `im.message.receive_v1` 且应用已被拉入目标群，事件通知开启。
