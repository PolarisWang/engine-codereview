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

# ── Clone-cache & review-cache release ───────────────────────────────────────
# release_rules:
#   - checkout dirs named "<repo>-review" (auto-cloned for auto-fix/MR) are released
#     if no ACTIVE topic references the repo AND they are older than RETENTION days.
#   - "<repo>" reusable cache (code_reviewer) is KEPT (high reuse value); report size.
#   - .review_cache/<topic>_<repo>_<hash>.json entries are released when their topic is
#     in topics_archive or CLOSED-beyond-retention and no active topic references it.
# Re-reads the (possibly updated) container state to know topic phases.
docker exec "$CONTAINER" bash -c "cat $STATE_CONT 2>/dev/null" > "$TMPSTATE" 2>/dev/null
python3 - "$RETENTION" "$WS" "$DRY" "$TMPSTATE" <<'PY'
import json, os, sys, time, shutil
ret, ws, dry, src = int(sys.argv[1]), sys.argv[2], sys.argv[3] == '1', sys.argv[4]
d = json.load(open(src))
topics = d.get("topics", {})
arch   = d.get("topics_archive", {})
# active repo urls referenced by non-CLOSED topics
active_repo_urls = set()
for t in topics.values():
    if t.get("phase") != "CLOSED":
        for r in ("engine", "game"):
            u = t.get("repos", {}).get(r, {}).get("repo_url", "")
            if u: active_repo_urls.add(u.rstrip("/"))
cutoff = time.time() - ret * 86400
rec = {"checkout_released": 0, "cache_released": 0, "reusable_kept": []}

# derive repo name from a url (engine_repo basename minus .git)
def repo_name_of(url):
    return (url.rstrip("/").split("/")[-1].replace(".git", "")) if url else ""

# scan workspace subdirs that are git checkouts
for entry in os.listdir(ws):
    p = os.path.join(ws, entry)
    if not os.path.isdir(os.path.join(p, ".git")):
        continue
    # -review dirs are auto-clone candidates for release; plain repo dirs are the reusable cache.
    if entry.endswith("-review"):
        # keep if any active topic's repo matches this dir base
        base = entry[:-len("-review")]
        referenced = any(repo_name_of(u) == base for u in active_repo_urls)
        try:
            age = time.time() - os.path.getmtime(os.path.join(p, ".git"))
        except Exception:
            age = 0
        if not referenced and age > ret * 86400:
            if not dry:
                shutil.rmtree(p, ignore_errors=True)
            rec["checkout_released"] += 1
    else:
        # reusable cache: never delete here; record size
        pass

# .review_cache release: drop entries whose topic is archived/old-closed
cache_dir = os.path.join(ws, ".review_cache")
if os.path.isdir(cache_dir):
    # set of topic keys that are "current" (active or CLOSED within retention)
    keep_topics = set()
    for k, t in topics.items():
        if t.get("phase") != "CLOSED":
            keep_topics.add(k)
        else:
            try:
                ts = time.mktime(time.strptime(t.get("closed_at") or t.get("updated_at") or "", "%Y-%m-%dT%H:%M:%S"))
            except Exception:
                ts = 0
            if ts >= cutoff:
                keep_topics.add(k)
    for fn in os.listdir(cache_dir):
        if not fn.endswith(".json"):
            continue
        # filename format: <topic>_<repo>_<hash>.json  (topic may itself contain '_')
        parts = fn.split("_")
        if len(parts) < 2:
            continue
        # the topic key is not _-delimited reliably; approximate: if no keep_topic is a prefix, drop
        if not any(kt in fn for kt in keep_topics):
            if not dry:
                try: os.remove(os.path.join(cache_dir, fn))
                except OSError: pass
            rec["cache_released"] += 1

print(f"[cleanup] checkout_released={rec['checkout_released']} review_cache_released={rec['cache_released']} dry={'yes' if dry else 'no'}")
PY

echo "==== cleanup done ===="
