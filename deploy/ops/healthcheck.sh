#!/usr/bin/env bash
#
# ops/healthcheck.sh — monitor the Feishu code-review event server and alert on
# problems (dead watchdog/bot, no Feishu WS connection, stale state).
#
# Run from host cron every few minutes; alerts go to the Feishu webhook.
#
# Usage:
#   ops/healthcheck.sh            # check + alert on failure
#   ops/healthcheck.sh --quiet    # check, exit 0/1, no alert (for scripting)
#
# Env: CONTAINER (chaos-agent-cr), WEBHOOK_URL (FEISHU_WEBHOOK_URL),
#      STATE_AGE_MAX (seconds the pipeline state may be stale before alerting,
#      default 900), WORKSPACE state path inside container.

set -u
CONTAINER="${CONTAINER:-chaos-agent-cr}"
WEBHOOK_URL="${FEISHU_WEBHOOK_URL:-https://open.feishu.cn/open-apis/bot/v2/hook/9ba5e264-6486-4ba6-abd3-094bb4d923ff}"
STATE_AGE_MAX="${STATE_AGE_MAX:-900}"           # 15 min
STATE_FILE_CONTAINER="${STATE_FILE:-/root/.codereview-pipeline-state.json}"
QUIET=0; [ "${1:-}" = "--quiet" ] && QUIET=1

alert() {
    local title body
    title="$1"; body="$2"
    if [ "$QUIET" = "1" ]; then return; fi
    if [ -n "$WEBHOOK_URL" ]; then
        curl -fsS -m 20 -X POST -H 'Content-Type: application/json' \
            -d "{\"msg_type\":\"text\",\"content\":{\"text\":\"⚠️ $title\n$body\"}}" \
            "$WEBHOOK_URL" >/dev/null 2>&1 && echo "  [alert sent]" || echo "  [alert FAILED]"
    fi
}

ok=1

# 1. watchdog + bot processes alive inside container
WATCHDOG=$(sudo docker exec "$CONTAINER" bash -c "pgrep -fc run-event-server-watchdog.sh 2>/dev/null" 2>/dev/null || echo 0)
BOT=$(sudo docker exec "$CONTAINER" bash -c "pgrep -f 'python3 -B /home/jenkins/workspace/code-review-pipeline/jenkins/scripts/event_server.py --mode ws' 2>/dev/null | wc -l" 2>/dev/null || echo 0)
echo "[health] watchdog_pid=$WATCHDOG bot_count=$BOT"
if [ "$BOT" -lt 1 ]; then
    echo "  FAIL: no event-server bot process"
    alert "CodeReview bot 进程不在" "容器 $CONTAINER 里没有 event_server.py 进程。\n跑 \`deploy/startup.sh\` 拉起。"
    ok=0
fi

# 2. Feishu long-connection present in the newest event-server log (within window)
LATEST=$(sudo docker exec "$CONTAINER" bash -c "ls -t /tmp/ev-server-logs/ 2>/dev/null | head -1" 2>/dev/null)
CONN=0; LOGAGE=999999
if [ -n "$LATEST" ]; then
    CONN=$(sudo docker exec "$CONTAINER" bash -c "grep -c 'connected to wss' /tmp/ev-server-logs/$LATEST 2>/dev/null" 2>/dev/null || echo 0)
    LOGAGE=$(sudo docker exec "$CONTAINER" bash -c "echo \$(( \$(date +%s) - \$(stat -c %Y /tmp/ev-server-logs/$LATEST 2>/dev/null || date +%s) ))" 2>/dev/null || echo 999999)
fi
echo "[health] bot_log_conn=$CONN log_age_s=$LOGAGE"
if [ "$CONN" -lt 1 ] || [ "$LOGAGE" -gt 7200 ]; then
    echo "  FAIL: no Feishu WS connection / stale log"
    alert "CodeReview bot 未连 Feishu" "最新日志($LATEST) 无连接($CONN)，日志年龄 ${LOGAGE}s。\n检查 watchdog 与 event_server 日志。"
    ok=0
fi

# 3. pipeline-state freshness (reviews should keep touching it; a long-frozen state
#    without new activity may be OK, but combined with no bot it's a real signal).
STATE_AGE=$(sudo docker exec "$CONTAINER" bash -c "echo \$(( \$(date +%s) - \$(stat -c %Y $STATE_FILE_CONTAINER 2>/dev/null || date +%s) ))" 2>/dev/null || echo 0)
echo "[health] state_age_s=$STATE_AGE (max $STATE_AGE_MAX)"
if [ "$STATE_AGE" -gt "$STATE_AGE_MAX" ]; then
    echo "  INFO: state idle ${STATE_AGE}s (no recent review) — not an error by itself."
fi

if [ "$QUIET" = "1" ]; then
    [ "$ok" = "1" ] && exit 0 || exit 1
fi
echo "[health] overall=$([ "$ok" = 1 ] && echo OK || echo DEGRADED)"
[ "$ok" = "1" ] && exit 0 || exit 1
