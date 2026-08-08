#!/usr/bin/env bash
#
# monitor/run-collector.sh — periodically run monitor_collect.py so node-exporter's
# textfile collector always has fresh custom metrics.
#
# Usage:  bash monitor/run-collector.sh          (foreground, loop)
#         (optional)  nohup bash ... > /tmp/cr-monitor-collector.log 2>&1 &
#
# Because `du` over multi-GB checkouts is slow, monitor_collect.py caches sizes
# for SIZE_TTL (300s) and only recomputes them then; the instant metrics
# (process/lock/topic) refresh every loop tick (5s). So a 5s loop is cheap.
set -u

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
COLLECTOR="$REPO_DIR/monitor/monitor_collect.py"
TEXTFILE_DIR="$REPO_DIR/monitor/node-exporter/textfile"
LOCK_DIR="/var/lib/report-server/daily/cr-locks"
CR_WS="/var/lib/report-server/daily/cr-workspace"
STATE="/root/.codereview-pipeline-state.json"

mkdir -p "$TEXTFILE_DIR"

echo "[cr-monitor] collector loop started ($(date '+%F %T')) -> $TEXTFILE_DIR"

while true; do
  TEXTFILE_DIR="$TEXTFILE_DIR" \
  LOCK_DIR="$LOCK_DIR" \
  CR_WORKSPACE="$CR_WS" \
  STATE_FILE_HOST="$STATE" \
  python3 "$COLLECTOR" 2>>"$TEXTFILE_DIR/collector.err.log"
  sleep 5
done
