#!/usr/bin/env bash
#
# run-event-server-watchdog.sh
#
# Self-healing supervisor for the Feishu code-review event server (event_server.py
# --mode ws) inside the Jenkins-agent container. The container "owns" the agent via
# entrypoint (a separate concern); THIS script owns the bot process itself.
#
# Why it exists: the bot is a plain python process not managed by systemd or the
# container entrypoint, so if it crashes (exception/OOM/kill) nothing brings it back.
# This watchdog restarts it with the SAME environment and working directory it needs
# to operate (Feishu creds, ANTHROPIC creds, correct cwd so common.load_config()
# finds config.yaml).
#
# Usage:
#   nohup bash deploy/run-event-server-watchdog.sh > /tmp/ev-watchdog.log 2>&1 &
#
# Notes / limitations (documented, not silently hidden):
#   - This guards the PROCESS. It does NOT make the container restart the bot after
#     a container restart (that needs editing the shared agent entrypoint or a
#     Jenkins master-side watcher — out of scope on purpose). Re-run it after the
#     container comes back up.
#   - Serializes against concurrent launches with a pidfile; refuses to double-run.

set -u

SCRIPTS_DIR="/home/jenkins/workspace/code-review-pipeline/jenkins/scripts"
ENV_FILE="${ENV_FILE:-/var/lib/report-server/daily/cr-env/env.sh}"
BOT_PY="$SCRIPTS_DIR/event_server.py"
PID_FILE="${PID_FILE:-/var/run/ev-server-watchdog.pid}"
LOG_DIR="/tmp/ev-server-logs"

# Refuse to run twice under the same pidfile.
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "[watchdog] already running (pid $(cat "$PID_FILE")); exiting."
    exit 0
fi
echo $$ > "$PID_FILE"
trap 'rm -f "$PID_FILE"' EXIT

mkdir -p "$LOG_DIR"

# Load the exact environment the bot needs (captured from a working instance).
set -a
if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$ENV_FILE"
else
    echo "[watchdog] WARNING: $ENV_FILE missing; bot may lack credentials."
fi
set +a
export HOME="${HOME:-/root}"
export PATH="${PATH:-/usr/bin:/bin}"
# FEISHU_APP_ID / FEISHU_APP_SECRET must come from ENV_FILE (cr-env/env.sh),
# NOT from fallbacks in this file — a committed literal would leak the secret.
# Warn loudly if they are missing so a mis-configured env is obvious.
if [ -z "${FEISHU_APP_ID:-}" ] || [ -z "${FEISHU_APP_SECRET:-}" ]; then
    echo "[watchdog] WARNING: FEISHU_APP_ID/FEISHU_APP_SECRET empty (expected from $ENV_FILE); bot auth will fail." >&2
fi

cd "$SCRIPTS_DIR" || { echo "[watchdog] cannot cd $SCRIPTS_DIR"; exit 1; }

find_bot_pid() {
    # Match only the real bot process (exclude this watchdog's own subshells by
    # filtering for the literal python entry point).
    pgrep -f "python3 -B ${SCRIPTS_DIR}/event_server.py --mode ws" 2>/dev/null
}

launch_bot() {
    local ts log
    ts=$(date +%Y%m%d-%H%M%S)
    log="$LOG_DIR/ev-server-$ts.log"
    # Use an absolute path to the script and re-check the cwd, so a transient fs
    # hiccup during the loop cannot make us launch a non-existent relative file.
    if [ ! -f "$BOT_PY" ]; then
        echo "[watchdog] BOT_PY missing: $BOT_PY; will retry" >&2
        return 1
    fi
    echo "[watchdog] $(date '+%F %T') starting bot -> $log"
    setsid nohup python3 -B "$BOT_PY" --mode ws > "$log" 2>&1 < /dev/null &
    echo $! > /var/run/ev-server.pid
}

echo "[watchdog] ($(date '+%F %T')) supervisor up; monitoring '$BOT_PY' (bot pid: $(find_bot_pid | tr '\n' ' '))"

while true; do
    if [ -z "$(find_bot_pid)" ]; then
        echo "[watchdog] no bot running; relaunching..."
        launch_bot
    fi
    sleep 10
done
