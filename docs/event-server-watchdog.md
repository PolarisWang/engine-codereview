# Event server (Feishu bot) 自愈 watchdog

> ⚠️ **权威文档见 `deploy-architecture.md`**。本文是 watchdog 单项的补充细节。

`deploy/run-event-server-watchdog.sh` 是对 code-review 事件服务（`event_server.py --mode ws`）
的进程级自愈监督进程。它确保 **bot 进程崩溃 / 被 OOM-kill / 被误 kill 时被自动重新拉起**，
并以正确的运行环境（Feishu / ANTHROPIC / cwd）重启，避免"静默失联"。

## 背景

容器的 entrypoint 只负责拉起 Jenkins agent（`java -jar agent.jar`，容器 PID 1）。
code-review bot 是容器里独立 `python3 -B event_server.py --mode ws` 的裸进程，
**没有任何 systemd / supervisor 守护**。一旦 bot 进程退出，没人拉起它。

Docker 容器的 `restart=unless-stopped` 只保证"容器本身崩溃/重启时容器会回来"，
**不会**自动重启 bot（entrypoint 不认识 bot）。所以需要这个进程级 watchdog。

## 依赖

watchdog 启动 bot 时需要一份**完整且正确的环境变量**，来自：

- `/tmp/ev-env.sh`：从"可正常工作的 bot 进程"导出的 `NAME=value` 环境（Feishu creds、
  ANTHROPIC creds、JIRA/GitLab creds 等）。**含密钥，禁止提交到 git。**
- 固定 `cwd` = `/home/jenkins/workspace/code-review-pipeline/jenkins/scripts`
  （`common.load_config()` 按 cwd 找 `config.yaml`）。

如果这份容器里失效/被清，可从任一正在运行的 bot 重新生成（在**容器外/宿主**执行）：

```bash
sudo docker exec -i chaos-agent-cr bash -c 'cat > /tmp/ev-env.sh' \
    < <(sudo tr '\0' '\n' < /proc/<bot_host_pid>/environ)
```

其中 `<bot_host_pid>` 是宿主侧看到的 `event_server.py --mode ws` 进程 PID
（用 `pgrep -f "python3 -B event_server.py --mode ws"` 查）。

## 启动（在容器内）

```bash
nohup bash /home/jenkins/workspace/code-review-pipeline/deploy/run-event-server-watchdog.sh \
    > /tmp/ev-watchdog.log 2>&1 & disown
```

- 已在跑会因 pidfile 拒绝重复启动（`/var/run/ev-server-watchdog.pid`）。
- 每次它拉起 bot，日志写在 `/tmp/ev-server-logs/ev-server-<时间戳>.log`。

## 验证自愈

```bash
# 容器内批量看 bot + watchdog
ps -eo pid,ppid,args | grep -E 'event_server.py --mode ws|run-event-server-watchdog' | grep -v grep

# 手动 kill 测试（谨慎）：bot 应在 <=10s 内被 watchdog 重新拉起，且日志出现
# "[Lark] connected to wss://msg-frontier.feishu.cn/ws/v2 ..."
```

## ⚠️ 已知局限（不静默掩盖）

- watchdog 只守护**进程**。若**整个容器重启/重建**（docker restart / 机器重启），
  watchdog 和 bot 一起消失，容器回来时 entrypoint 仍只拉起 agent、不起 bot。
  因此**容器重启后需手动重新运行上面的启动命令**。
- 彻底解决"容器重启自动全拉起"需要改 agent 镜像 entrypoint 或由 Jenkins master 侧
  实现 watcher（超出本仓库范围，有意不做）。

## 会话中的落地记录（2026-08-05）

- 部署到容器 `/home/jenkins/workspace/code-review-pipeline/deploy/run-event-server-watchdog.sh`
- 已在容器内启动 watchdog（进程 `bash .../run-event-server-watchdog.sh`, pidfile
  `/var/run/ev-server-watchdog.pid`），其下 bot 为 `event_server.py --mode ws`。
- 已实测：kill bot → ~10s 内 watchdog 拉起新 bot，新 bot 环境完整、成功连接 Feishu。
- 本仓库 `deploy/run-event-server-watchdog.sh` 为源（git 管理）；容器里那份是运行副本。
