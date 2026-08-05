#!/usr/bin/env bash
#
# ops/cleanup.sh — workspace/state retention & release policy (arch-D answer to
# "when does workspace get released").
#
# Releases artifacts of CLOSED topics that are past RETENTION_DAYS:
#   1. ARCHIVE  — move the topic record into topics_archive (state stops growing).
#   2. RESULTS  — delete result_<key>_<repo>.json + .dry for those topics.
#
# SAFETY: nothing for ACTIVE topics, and nothing past-retention for CLOSED topics
# that is still referenced by an active topic, is touched. This is a dry-run by
# default; pass --apply to actually mutate.
#
# Usage (from host):
#   ops/cleanup.sh [--apply] [--retention N]
#
# Env: CONTAINER (chaos-agent-cr), STATE_CONT (/root/.codereview-pipeline-state.json)

set -u
CONTAINER="${CONTAINER:-chaos-agent-cr}"
STATE_CONT="${STATE_CONT:-/root/.codereview-pipeline-state.json}"
WS="/var/lib/report-server/daily/cr-workspace"
DRY=1; RETENTION=30
for a in "$@"; do
  case "$a" in
    --apply) DRY=0 ;;
    --retention=*) RETENTION="${a#*=}" ;;
  esac
done

echo "==== cleanup ($(date '+%F %T')) retention=${RETENTION}d apply=$([ $DRY = 1 ] && echo NO || echo YES) ===="

TMPSTATE="/tmp/cleanup-in.$$.json"
docker exec "$CONTAINER" bash -c "cat $STATE_CONT 2>/dev/null" > "$TMPSTATE" 2>/dev/null \
  || { echo "[cleanup] cannot read container state"; exit 1; }

python3 - "$RETENTION" "$WS" "$DRY" "$TMPSTATE" <<'PY'
import json, os, sys, time
ret, ws, dry, src = int(sys.argv[1]), sys.argv[2], sys.argv[3] == '1', sys.argv[4]
d = json.load(open(src))
topics = d.setdefault("topics", {})
arch  = d.setdefault("topics_archive", {})
cutoff = time.time() - ret * 86400
active_files = {t.get("repos", {}).get(r, {}).get("result_file", "")
                for k, t in topics.items()
                if t.get("phase") != "CLOSED"
                for r in ("engine", "game")}
rec = {"archived": 0, "results": 0}
for k in list(topics.keys()):
    t = topics[k]
    if t.get("phase") != "CLOSED":
        continue
    try:
        ts = time.mktime(time.strptime(t.get("closed_at") or t.get("updated_at") or "", "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        ts = 0
    if ts >= cutoff:
        continue
    arch[k] = t
    rec["archived"] += 1
    for r in ("engine", "game"):
        rf = t.get("repos", {}).get(r, {}).get("result_file", "")
        if not rf or rf in active_files:
            continue
        for cand in (os.path.join(ws, rf), os.path.join(ws, rf + ".dry")):
            if os.path.exists(cand):
                if not dry:
                    try: os.remove(cand)
                    except OSError: pass
                rec["results"] += 1
    del topics[k]
print(f"[cleanup] dry={'yes' if dry else 'no'} retention={ret}d "
      f"archived={rec['archived']} result_files_released={rec['results']}")
json.dump(d, open(src, "w"))
PY
RC=$?
if [ "$RC" = "0" ] && [ "$DRY" = "0" ]; then
  docker exec -i "$CONTAINER" bash -c "cat > $STATE_CONT" < "$TMPSTATE"
  echo "[cleanup] state written back to container"
fi
rm -f "$TMPSTATE"

echo "==== cleanup done ===="
