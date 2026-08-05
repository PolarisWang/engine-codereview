#!/usr/bin/env bash
#
# apply.sh — single deployment entry point for the Feishu code-review bot.
#
#   ./deploy/apply.sh            # deploy origin/main to the running container + restart
#
# Works because the agent container's /home/jenkins is a bind mount of the host's
# /home/jenkins/cr, so the running checkout is directly addressable on this host
# at /home/jenkins/cr/workspace/code-review-pipeline. We update that shared
# checkout to origin/main (same GitHub remote the authoritative repo tracks), then
# restart the watchdog chain so the event server picks up the new code.
#
# Safe to re-run (idempotent); only restarts services when the code actually
# changed OR --force is given.

set -euo pipefail

REPO_DIR_DEV="$(cd "$(dirname "$0")/.." && pwd)"          # this repo (authoritative)
DEST="${1:-/home/jenkins/cr/workspace/code-review-pipeline}"  # shared/container checkout
CONTAINER="${CONTAINER:-chaos-agent-cr}"
STARTUP="$DEST/deploy/startup.sh"
REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-main}"
FORCE="0"
for a in "$@"; do
    case "$a" in
        --force|force) FORCE="1" ;;
    esac
done

echo "==== apply.sh ($(date '+%F %T')) ===="
echo "authoritative repo : $REPO_DIR_DEV"
echo "shared/container   : $DEST  ($(docker exec "$CONTAINER" readlink -f /home/jenkins 2>/dev/null))"

# 1. Authoritative repo must be at/beyond origin/main for what we intend to deploy.
#    (Push is done separately; this step just documents intent.)
pushd "$REPO_DIR_DEV" >/dev/null
    LOCAL_HEAD=$(git rev-parse --short HEAD 2>/dev/null || echo "?")
popd >/dev/null

# 2. Bring the shared checkout to origin/main.
if [ -d "$DEST/.git" ]; then
    echo ">> syncing shared checkout to $REMOTE/$BRANCH"
    git -C "$DEST" fetch "$REMOTE" "$BRANCH" 2>/dev/null || \
        { echo "[apply] WARN: fetch failed (no net?) — will reset to existing origin/main"; }
    BEFORE=$(git -C "$DEST" rev-parse --short HEAD 2>/dev/null || echo "?")
    # Use FETCH_HEAD (the just-fetched commit) so we never reset to a stale
    # origin/main when the local remote-tracking ref lags behind the fetch.
    if git -C "$DEST" rev-parse FETCH_HEAD >/dev/null 2>&1; then
        TARGET="FETCH_HEAD"
    else
        TARGET="$REMOTE/$BRANCH"
    fi
    git -C "$DEST" reset --hard "$TARGET"
    AFTER=$(git -C "$DEST" rev-parse --short HEAD 2>/dev/null || echo "?")
else
    echo "[apply] ERROR: $DEST is not a git checkout; nothing to do."
    exit 1
fi

echo ">> shared HEAD: $BEFORE -> $AFTER (authoritative: $LOCAL_HEAD)"

# 3. Re-sync the deploy scripts into the shared checkout too (in case apply.sh /
#    startup.sh changed). We always copy the current deploy/ over the shared copy
#    so watchdog/startup/ops are the same version as this repo.
echo ">> syncing deploy/ toolkit to shared checkout"
rsync -a "$REPO_DIR_DEV/deploy/" "$DEST/deploy/" 2>/dev/null || cp -a "$REPO_DIR_DEV/deploy/." "$DEST/deploy/"

# 4. Restart the service chain: only if code changed, or --force.
if [ "$BEFORE" != "$AFTER" ] || [ "$FORCE" = "1" ]; then
    echo ">> restarting event-server chain via startup.sh"
    docker exec "$CONTAINER" bash "$STARTUP"
else
    echo ">> no change (HEAD = $AFTER); skipping restart (use --force to force)."
fi

echo "==== apply.sh done ($(date '+%F %T')) ===="
