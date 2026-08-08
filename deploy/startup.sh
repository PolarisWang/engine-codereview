#!/usr/bin/env bash
#
# startup.sh — bring the Feishu code-review event server back up after a
# container restart (or any time the watchdog/bot are not running).
#
# A container restart clears the event server + watchdog (they are not managed
# by the agent entrypoint). Run this script once the agent container is back to
# put everything in order with a single command:
#
#   docker exec chaos-agent-cr bash /home/jenkins/workspace/code-review-pipeline/deploy/startup.sh
#
# It is idempotent: it will not start a second watchdog or bot if one is already
# up. It also reports zombie/pid pressure so leftover-terminal-process buildup is
# visible (identify + clean via a container restart when the count is high).

set -u

SCRIPTS_DIR="/home/jenkins/workspace/code-review-pipeline/jenkins/scripts"
DEPLOY_DIR="/home/jenkins/workspace/code-review-pipeline/deploy"
WATCHDOG="$DEPLOY_DIR/run-event-server-watchdog.sh"
WATCHDOG_LOG="/tmp/ev-watchdog.log"
BOT_PAT="event_server.py --mode ws"
WATCHDOG_PIDFILE="/var/run/ev-server-watchdog.pid"

echo "==== code-review startup ($(date '+%F %T')) ===="

# 1. Sanity: env-backed bot environment present? (secrets are needed to run ws)
# Prefer the persistent copy (survives container restarts); /tmp is a fallback.
ENV_PATH="${ENV_PATH:-/var/lib/report-server/daily/cr-env/env.sh}"
if [ ! -f "$ENV_PATH" ] && [ ! -f /tmp/ev-env.sh ]; then
    echo "[startup] WARN: neither $ENV_PATH nor /tmp/ev-env.sh exists — the bot needs "
    echo "         these secrets. Regenerate from a working bot process before bringing it up."
fi

# 2. Bring up the watchdog if it is not already running (pidfile guard prevents dupes).
if [ -f "$WATCHDOG_PIDFILE" ] && kill -0 "$(cat "$WATCHDOG_PIDFILE")" 2>/dev/null; then
    echo "[startup] watchdog already running (pid $(cat "$WATCHDOG_PIDFILE"))."
else
    echo "[startup] starting watchdog: $WATCHDOG"
    setsid nohup bash "$WATCHDOG" > "$WATCHDOG_LOG" 2>&1 < /dev/null &
    sleep 3
    if [ -f "$WATCHDOG_PIDFILE" ] && kill -0 "$(cat "$WATCHDOG_PIDFILE")" 2>/dev/null; then
        echo "[startup] watchdog up (pid $(cat "$WATCHDOG_PIDFILE"))."
    else
        echo "[startup] ERROR: watchdog failed to start; see $WATCHDOG_LOG"
        tail -5 "$WATCHDOG_LOG" 2>/dev/null
    fi
fi

# 2b. If code changed, FORCE the bot to reload: kill any running event-server so
#     the watchdog relaunches it from the now-current checkout. Without this, the
#     watchdog keeps a live bot on the OLD code — apply.sh would "deploy" but the
#     running bot silently keeps the previous behavior. Guard on RESTART_BOT=1 so
#     a plain startup.sh (container-restart recovery) remains idempotent/no-op for
#     an already-running bot.
if [ "${RESTART_BOT:-0}" = "1" ]; then
    echo "[startup] code changed -> forcing bot restart"
    for bpid in $(pgrep -f "python3 -B ${SCRIPTS_DIR}/event_server.py --mode ws" 2>/dev/null); do
        echo "[startup] killing stale bot pid=$bpid"
        kill "$bpid" 2>/dev/null || true
    done
    sleep 2
fi

# 3. Give the watchdog a moment to (re)launch the bot, then verify.
echo "[startup] waiting for the watchdog to ensure the bot is running..."
for i in 1 2 3 4 5 6; do
    BOT_PID=$(pgrep -f "python3 -B ${SCRIPTS_DIR}/event_server.py --mode ws" 2>/dev/null | head -1)
    if [ -n "$BOT_PID" ]; then
        break
    fi
    sleep 4
done

BOT_PID=$(pgrep -f "python3 -B ${SCRIPTS_DIR}/event_server.py --mode ws" 2>/dev/null | head -1)
if [ -n "$BOT_PID" ]; then
    echo "[startup] bot running: pid=$BOT_PID (cmd: $(tr '\0' ' ' < /proc/$BOT_PID/cmdline 2>/dev/null))"
else
    echo "[startup] WARN: bot not detected yet. Check watchdog: $WATCHDOG_LOG"
fi

# 4. Health / pid-pressure report (zombies indicate terminal-process buildup to clean later).
ZOMBIES=$(ps -eo stat= 2>/dev/null | grep -c '^Z' || true)
[ -z "$ZOMBIES" ] && ZOMBIES=0
TOTAL=$(ps -e -o pid= 2>/dev/null | wc -l)
echo "[startup] pid-pressure: total=$TOTAL zombies=$ZOMBIES"
if [ "$ZOMBIES" -gt 5000 ]; then
    echo "[startup] NOTE: $ZOMBIES zombies present (Java PID1 not reaping orphans). "
    echo "         Plan a container restart soon; they are the source of fork EAGAIN."
fi

echo "==== startup done ($(date '+%F %T')) ===="
