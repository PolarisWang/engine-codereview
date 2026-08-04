# Jenkins 凭据配置指南

代码审查流水线（`jenkins/Jenkinsfile`）依赖多个外部服务的密钥/凭据。为避免密钥硬编码在
流水线源码里，建议统一通过 **Jenkins 全局凭据（Credentials）+ 环境变量注入** 提供。

> 说明：`engine-codereview` 仓库本身是配置与脚本源码。真正的密钥应放在 Jenkins 凭据管理，
> 通过 `环境变量` 或 `Secret text` 凭据注入到流水线的 `environment { }` / `env.*` 中。

---

## 凭据清单

| 环境变量 | 用途 | 凭据类型 | 来源 |
|---------|------|----------|------|
| `FEISHU_APP_ID` | 飞书应用 App ID（Bot API 查询 token） | Secret text | 飞书开放平台 → 应用凭证 |
| `FEISHU_APP_SECRET` | 飞书应用 App Secret | Secret text | 飞书开放平台 → 应用凭证 |
| `FEISHU_CHAT_ID` | 要扫描/发送消息的群聊 ID（`oc_xxxx`） | Secret text | 飞书群 → 机器人管理 或 API 查询 |
| `FEISHU_WEBHOOK_URL_OVERRIDE` | Bot API 不可用时的兜底入群 webhook | Secret text / URL | 飞书群 → 群机器人 → 自定义机器人 |
| `GITLAB_TOKEN` | GitLab API（MR 分支探测） + GitLab HTTPS 拉仓库鉴权 | Secret text (PAT) | GitLab 用户 → Access Tokens |
| `GITLAB_USER` | GitLab HTTPS 鉴权用户名（默认 `gitlab-ci-token`） | 环境变量 | 固定值，无需凭据 |
| `JIRA_HOST` | Jira 服务地址（如 `https://jira.example.com`） | Secret text | 运维提供 |
| `JIRA_TOKEN` | Jira API token（PAT 或 user:apitoken 的 base64） | Secret text | Jira 用户 → API tokens |
| `ANTHROPIC_AUTH_TOKEN` | LLM API key（== `x-api-key`） | Secret text | LLM 网关 / 供应商控制台 |
| `ANTHROPIC_BASE_URL` | LLM API 网关地址（如 `https://llm-api.example.com`） | 环境变量 | 网关地址 |
| `ANTHROPIC_MODEL` | 模型名（如 `deepseek-v4-flash`） | 环境变量 | 网关支持列表 |

> LLM 相关也可从 `config.yaml` 的 `claude:` 块读取；Jenkins 侧环境变量优先级更高。

---

## Jenkins 配置步骤

### 1. 添加全局凭据（Manage Jenkins → Credentials → Global → Add）

以 `FEISHU_APP_SECRET` 为例：

- **Kind**: `Secret text`
- **ID**（全局唯一，建议与变量名一致）: `FEISHU_APP_SECRET`
- **Secret**: 填入真实密钥

对 `API token` 类（GitLab/Jira）可用 `Username with password`，在 Jenkinsfile 中通过
`withCredentials([usernamePassword(credentialsId: 'GITLAB', usernameVariable: 'GITLAB_USER', passwordVariable: 'GITLAB_TOKEN')])`
解包。

### 2. 在流水线中使用

方式 A —— 项目级环境变量（简单，凭据走 Secret text 注入）：

```groovy
environment {
    FEISHU_APP_ID    = credentials('FEISHU_APP_ID')
    FEISHU_APP_SECRET = credentials('FEISHU_APP_SECRET')
    FEISHU_CHAT_ID   = credentials('FEISHU_CHAT_ID')
    GITLAB_TOKEN     = credentials('GITLAB_TOKEN')
    JIRA_HOST        = credentials('JIRA_HOST')
    JIRA_TOKEN       = credentials('JIRA_TOKEN')
    ANTHROPIC_AUTH_TOKEN = credentials('ANTHROPIC_AUTH_TOKEN')
    FEISHU_WEBHOOK_URL_OVERRIDE = credentials('FEISHU_WEBHOOK_URL_OVERRIDE')
}
```

方式 B —— 全局构建环境（Manage Jenkins → Configure System → Global properties），
为 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`GITLAB_TOKEN`、`JIRA_TOKEN` 等定义环境变量名并绑定对应凭据。

### 3. 注入后移除硬编码 fallback

当所有凭据都通过 Jenkins 注入后，可移除 `Jenkinsfile` 中如下硬编码默认值：

- `FEISHU_CHAT_ID` 的 `'oc_254e95f0687245b9df82ab8bf823ca54'` fallback
- `FEISHU_WEBHOOK_URL` 的 `'f2ec1781-06d2-42c8-91b4-37616d973970'` fallback

保留 fallback 的目的是在凭据未配置时流水线不至于直接崩（缺少凭据时报空），
待全部迁移完成后再删除。

---

## 安全注意事项

- 不要把任何密钥提交进 `config.yaml`、`.env` 或 `Jenkinsfile` 源码。
- `feishu-bot/.env` 已被 `.gitignore` 忽略；若历史提交曾泄露过密钥，请联系运维轮换。
- `code_reviewer.py` 现通过 `git -c http.extraheader` 注入 GitLab 鉴权，token 不会写入
  git remote URL 或 `git remote -v`。残留工作区如有旧 `https://user:token@host` 的
  remote，脚本会自动脱敏。
