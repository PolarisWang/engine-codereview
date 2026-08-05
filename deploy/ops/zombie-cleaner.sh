#!/usr/bin/env bash
#
# ops/zombie-cleaner.sh — detect + mitigate the zombie-process buildup in the
# agent container.
#
# Background: the container's PID 1 is the Jenkins agent (java), which does NOT
# reap orphaned children. Jenkins scan/build steps spawn `sh`→`git` workers; when
# they orphan (and are not reaped), they accumulate as zombies in the
# container PID namespace. At scale this exhausts the container's pids limit and
# every new fork gets EAGAIN (review pipeline stalls). Mitigations:
#
#   a) "probe"   — report total/zombie counts (host-side, avoids fork in container)
#   b) "warn"    — if zombies exceed THRESHOLD, post a Feishu alert
#   c) "clean"   — `docker restart <container>` to clear zombies (disruptive: drops
#                  Jenkins agent + event server briefly; apply.sh/startup.sh brings
#                  them back). Only authorized if AUTO_RESTART=1.
#
# Usage:
#   probes/alerting:  ops/zombie-cleaner.sh warn            (host cron every N min)
#   operational:      ops/zombie-cleaner.sh clean           (one-off, requires ok)
#   probe only:       ops/zombie-cleaner.sh probe
#
# Env: THRESHOLD (default 5000), CONTAINER (chaos-agent-cr), WEBHOOK_URL,
#      AUTO_RESTART (default 0; set 1 to allow auto clean below threshold advisory).

set -u
CONTAINER="${CONTAINER:-chaos-agent-cr}"
THRESHOLD="${THRESHOLD:-5000}"
WEBHOOK_URL="${FEISHU_WEBHOOK_URL:-}"
CMD="${1:-probe}"

# Get container process totals from the host side WITHOUT forking inside the
# (possibly pids-exhausted) container: use nsenter into its PID ns via its main pid.
main_pid() { sudo docker inspect "$CONTAINER" --format '{{.State.Pid}}' 2>/dev/null; }

counts() {
    local pid
    pid=$(main_pid)
    [ -n "$pid" ] || { echo "0 0"; return; }
    local tot zom
    tot=$(sudo nsenter -t "$pid" -m -p -- bash -c 'ps -e -o pid= 2>/dev/null | wc -l' 2>/dev/null)
    zom=$(sudo nsenter -t "$pid" -m -p -- bash -c 'ps -eo stat= 2>/dev/null | grep -c "^Z"' 2>/dev/null)
    echo "${tot:-0} ${zom:-0}"
}

alert() {
    local title body
    title="$1"; body="$2"
    if [ -n "${WEBHOOK_URL}" ]; then
        msg="{\"msg_type\":\"text\",\"content\":{\"text\":\"$title\n$body\"}}"
        curl -fsS -m 20 -X POST -H 'Content-Type: application/json' \
            -d "$msg" "$WEBHOOK_URL" >/dev/null 2>&1 && echo "  alert sent" || echo "  alert FAILED"
    else
        echo "  (no WEBHOOK_URL; alert skipped)"
    fi
}

read -r TOTAL ZOMBIES <<<"$(counts)"
echo "[zombie] container=$CONTAINER total=$TOTAL zombies=$ZOMBIES threshold=$THRESHOLD"

case "$CMD" in
  probe)
    exit 0 ;;
  warn)
    if [ "$ZOMBIES" -ge "$THRESHOLD" ]; then
        echo "  WARN: zombies above threshold"
        alert "⚠️ CodeReview 容器僵尸过多" \
"容器 $CONTAINER 僵尸=$ZOMBIES / 总=$TOTAL，超过阈值 $THRESHOLD。
建议：低峰执行 \`ops/zombie-cleaner.sh clean\` 重启清僵尸，或用 \`deploy/apply.sh --force\` 恢复服务。"
    fi
    exit 0 ;;
  clean)
    if [ "${AUTO_RESTART:-0}" != "1" ]; then
        echo "  AUTO_RESTART != 1; refusing to auto-restart. Set AUTO_RESTART=1 to allow."
        echo "  To do it deliberately: docker restart $CONTAINER (clears zombies), then deploy/startup.sh."
        exit 0
    fi
    if [ "$ZOMBIES" -ge "$THRESHOLD" ]; then
        echo "  cleaning: docker restart $CONTAINER"
        sudo docker restart "$CONTAINER"
        sleep 20
        # bring the event server back
        sudo docker exec "$CONTAINER" bash /home/jenkins/workspace/code-review-pipeline/deploy/startup.sh || true
    else
        echo "  below threshold; no action"
    fi
    exit 0 ;;
  *)
    echo "usage: $0 probe|warn|clean" >&2
    exit 2 ;;
esac
