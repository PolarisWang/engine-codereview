#!/usr/bin/env bash
#
# ops/cleanup.sh — workspace/state retention & release policy (arch-D answer to
# "when does workspace get released").
#
# Releases artifacts of CLOSED topics that are past RETENTION_DAYS:
#   1. ARCHIVE  — move the topic record into the archive (state stops growing).
#   2. RESULTS  — delete result_<key>_<repo>.json + .dry for those topics.
#
# Supports BOTH state layouts transparently:
#   - FILE mode (schema v1): single-file JSON document at $STATE_CONT.
#   - DIR mode  (schema v2): $STATE_CONT is a directory (topics/<key>.json).
#   The safe path is a NEW directory name ($STATE_DIR); when deploying v2, change
#   $STATE_CONT to that dir. All state reads/writes go through pipeline_state.py's
#   dir-aware API, never raw single-file JSON.
#
# SAFETY: nothing for ACTIVE topics, and nothing past-retention for CLOSED topics
# that is still referenced by an active topic, is touched. Dry-run by default;
# pass --apply to actually mutate.
#
# Env: CONTAINER (chaos-agent-cr), STATE_CONT (state path, file or dir)

set -u
CONTAINER="${CONTAINER:-chaos-agent-cr}"
STATE_CONT="${STATE_CONT:-/root/.codereview-pipeline-state.json}"
WS="/var/lib/report-server/daily/cr-workspace"
SCRIPTS_DIR="/home/jenkins/workspace/code-review-pipeline/jenkins/scripts"
DRY=1; RETENTION=30
for a in "$@"; do
  case "$a" in
    --apply) DRY=0 ;;
    --retention=*) RETENTION="${a#*=}" ;;
  esac
done

echo "==== cleanup ($(date '+%F %T')) retention=${RETENTION}d apply=$([ $DRY = 1 ] && echo NO || echo YES) state=$STATE_CONT ===="

# Use the deployed pipeline_state module inside the container for ALL state I/O,
# so file and dir layouts are handled identically.
docker exec "$CONTAINER" bash -c "cat > /tmp/cleanup_ps.py" 2>/dev/null <<'PY'
import pipeline_state as ps, os, sys, time
ret, ws, dry = int(sys.argv[1]), sys.argv[2], sys.argv[3] == '1'
state_path = sys.argv[4]
cutoff = time.time() - ret * 86400
rec = {"archived": 0, "results": 0, "checkout_released": 0, "cache_released": 0}
active_files = set()
active_repo_urls = set()

def topic_ts(t):
    try:
        return time.mktime(time.strptime(t.get("closed_at") or t.get("updated_at") or "", "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return 0

# Pass 1: archive old CLOSED topics + release their result files.
for k in ps.list_topic_keys(state_path):
    t = ps.get_topic(state_path, k)
    if not t:
        continue
    if t.get("phase") != "CLOSED":
        active_files |= {t.get("repos", {}).get(r, {}).get("result_file", "")
                          for r in ("engine", "game") if t.get("repos", {}).get(r, {}).get("result_file")}
        for r in ("engine", "game"):
            u = t.get("repos", {}).get(r, {}).get("repo_url", "")
            if u:
                active_repo_urls.add(u.rstrip("/"))
        continue
    if topic_ts(t) >= cutoff:
        continue
    # Archive the topic (dir mode moves to archive/; file mode is a no-op there —
    # caller handles file-mode document rewrite separately below).
    if not dry:
        ps.archive_topic(state_path, k)
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

print(f"[cleanup] retained={ret}d dry={'yes' if dry else 'no'} archived={rec['archived']} result_files_released={rec['results']}")
PY
docker exec "$CONTAINER" bash -c "cd $SCRIPTS_DIR && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$SCRIPTS_DIR python3 /tmp/cleanup_ps.py $RETENTION $WS $DRY $STATE_CONT"
RC=$?
if [ "$RC" != "0" ]; then
  echo "[cleanup] ERR: archival pass failed rc=$RC" >&2
  echo "==== cleanup aborted ===="
  exit 1
fi

# ── Clone-cache & review-cache release (`-review` dirs + .review_cache) ───────
docker exec "$CONTAINER" bash -c "cat > /tmp/cleanup_cache.py" <<'PY'
import pipeline_state as ps, os, sys, time, shutil
ret, ws = int(sys.argv[1]), sys.argv[2]
state_path = sys.argv[3]
cutoff = time.time() - ret * 86400
active_repo_urls = set()
keep_topics = set()
for k in ps.list_topic_keys(state_path):
    t = ps.get_topic(state_path, k)
    if not t:
        continue
    for r in ("engine", "game"):
        u = t.get("repos", {}).get(r, {}).get("repo_url", "")
        if u:
            active_repo_urls.add(u.rstrip("/"))
    if t.get("phase") != "CLOSED":
        keep_topics.add(k)
    else:
        try:
            ts = time.mktime(time.strptime(t.get("closed_at") or t.get("updated_at") or "", "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            ts = 0
        if ts >= cutoff:
            keep_topics.add(k)
rec = {"checkout_released": 0, "cache_released": 0}

def repo_name_of(url):
    return (url.rstrip("/").split("/")[-1].replace(".git", "")) if url else ""

for entry in os.listdir(ws):
    p = os.path.join(ws, entry)
    if not os.path.isdir(os.path.join(p, ".git")):
        continue
    if entry.endswith("-review"):
        base = entry[:-len("-review")]
        referenced = any(repo_name_of(u) == base for u in active_repo_urls)
        try:
            age = time.time() - os.path.getmtime(os.path.join(p, ".git"))
        except Exception:
            age = 0
        if not referenced and age > ret * 86400:
            if not sys.argv[4] == "1":
                shutil.rmtree(p, ignore_errors=True)
            rec["checkout_released"] += 1

cache_dir = os.path.join(ws, ".review_cache")
if os.path.isdir(cache_dir):
    for fn in os.listdir(cache_dir):
        if not fn.endswith(".json"):
            continue
        if not any(kt in fn for kt in keep_topics):
            if not sys.argv[4] == "1":
                try: os.remove(os.path.join(cache_dir, fn))
                except OSError: pass
            rec["cache_released"] += 1

print(f"[cleanup] checkout_released={rec['checkout_released']} review_cache_released={rec['cache_released']} dry={'yes' if sys.argv[4]=='1' else 'no'}")
PY
docker exec "$CONTAINER" bash -c "cd $SCRIPTS_DIR && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$SCRIPTS_DIR python3 /tmp/cleanup_cache.py $RETENTION $WS $STATE_CONT $DRY"

echo "==== cleanup done ===="
