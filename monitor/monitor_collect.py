#!/usr/bin/env python3
"""
monitor_collect.py — collect CodeReview server resource-panorama metrics into
Prometheus node-textfile format, so node-exporter exposes them and VictoriaMetrics
scrapes them into Grafana (route③).

Covers "engine-review 锁派生的全部服务器资源":
  - processes      : watchdog / bot / jenkins-agent / live review subprocesses
  - locks          : cr-locks files (checkout_*, om_*.lock) + review_slot_* concurrency
  - topic/state    : open topics by phase, pending queue, exhausted, concurrency
  - workspace      : checkout dir sizes (per repo + -review), result file sizes
  - integration    : MTR GitLab reachability probe (na → 0/1)

Behavior:
  * Runs every few seconds (e.g. host cron or a wakeup loop). Each run overwrites
    the *.prom files atomically in the node-exporter textfile dir.
  * Because `du` over multi-GB checkouts is slow, sizes are computed on a coarse
    cadence (SIZE_TTL, default 300s) and cached; the per-run instant metrics (process/
    lock/topic) are always fresh.
  * Read-only: never writes pipeline state.
"""
import json
import os
import subprocess
import sys
import time

# --- config ---------------------------------------------------------------

STATE_FILE = os.environ.get("PIPELINE_STATE_FILE", "/root/.codereview-pipeline-state.json")
WORKSPACE = os.environ.get("CR_WORKSPACE", "/var/lib/report-server/daily/cr-workspace")
LOCK_DIR = os.environ.get("CR_LOCK_DIR", "/var/lib/report-server/daily/cr-locks")
OUT_DIR = os.environ.get("TEXTFILE_DIR", "/home/debian/agent/engine-codereview/monitor/node-exporter/textfile")
# The pipeline state file is what collect_topic_state / collect_topic_resources
# read. Read from env so a test can inject a synthetic state; defaults to the
# bot's production state file (host bind of the container's daily volume).
STATE_FILE_HOST = os.environ.get("CR_STATE_FILE", "/root/.codereview-pipeline-state.json")

# The state file is inside the bot's container in production; the collector may
# run on the host where it is exposed via the bind mount of /var/lib/report-server/
# daily. If the given path isn't present, fall back to scanning the host bind dir.
SIZE_TTL = 300.0   # seconds between recomputing directory sizes

# --- tiny helpers ---------------------------------------------------------

def _q(cmd, timeout=15):
    """Run a shell command, return (rc, stdout). Tolerate absence."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip()
    except Exception:
        return 1, ""


def _tonum(v, default=0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _now():
    return str(int(time.time()))


# --- metric buffers -------------------------------------------------------
M = []   # list of lines


def g(name, value, labels=None, ts=""):
    """Emit one # TYPE + gauge + value. """
    if labels:
        l = ",".join(f'{k}="{v}"' for k, v in labels.items())
        M.append(f'{name}{{{l}}} {value} {ts}'.strip())
    else:
        M.append(f"{name} {value} {ts}".strip())


def _t(name, help_):
    M.append(f"# HELP {name} {help_}")
    M.append(f"# TYPE {name} gauge")


# --- collectors -----------------------------------------------------------

def collect_topic_state():
    """Read pipeline state: open topics, phase distribution, pending, exhausted."""
    _t("cr_open_topics_total", "Topics not CLOSED (active/recent)")
    _t("cr_topic_by_phase", "Count of topics per phase")
    _t("cr_pending_actions", "Topics with an unconsumed pending queue action")
    _t("cr_failed_exhausted", "Topics that exhausted auto-retries (need human)")
    try:
        d = json.load(open(STATE_FILE_HOST, encoding="utf-8"))
        topics = d.get("topics", {})
        # v1 single-file layout: topics is a dict {key: record}
        phases = {}
        open_n = 0
        pending_n = 0
        exhausted_n = 0
        for k, t in topics.items():
            ph = t.get("phase", "")
            phases[ph] = phases.get(ph, 0) + 1
            if ph != "CLOSED":
                open_n += 1
            if t.get("pending"):
                pending_n += 1
            if t.get("failed_exhausted"):
                exhausted_n += 1
        g("cr_open_topics_total", open_n)
        for ph, c in phases.items():
            g("cr_topic_by_phase", c, {"phase": ph})
        g("cr_pending_actions", pending_n)
        g("cr_failed_exhausted", exhausted_n)
    except Exception as e:
        g("cr_topic_by_phase", -1, {"phase": "error"})   # sentinel
        print(f"[monitor] collect_topic_state err: {e}", file=sys.stderr)


def collect_processes():
    """Watchdog / bot / jenkins-agent presence + live review subprocess count."""
    _t("cr_proc_watchdog", "Watchdog supervisor process present (1/0)")
    _t("cr_proc_bot", "event-server bot process present (1/0)")
    _t("cr_proc_jenkins_agent", "Jenkins agent (remoting) present (1/0)")
    _t("cr_proc_reviewers", "Live orchestrate/code_reviewer subprocesses (running reviews)")
    # These pgrep run in a context where the collector can see the processes;
    # on the host we use the same container via docker exec; here we keep it simple
    # and rely on node-exporter process metrics for host procs; we add bot/watchdog
    # via docker exec (the deploy host has docker).
    hits = {}
    for name, pat in {
        "watchdog": "run-event-server-watchdog.sh",
        "bot": "event_server.py --mode ws",
        "jenkins_agent": "hudson.remoting.Launcher",
        "reviewers": "code_reviewer.py",
    }.items():
        rc, out = _q(f"pgrep -fc '{pat}'", timeout=10)
        hits[name] = int(out or 0)
    g("cr_proc_watchdog", 1 if hits["watchdog"] > 0 else 0)
    g("cr_proc_bot", 1 if hits["bot"] > 0 else 0)
    g("cr_proc_jenkins_agent", 1 if hits["jenkins_agent"] > 0 else 0)
    g("cr_proc_reviewers", hits["reviewers"])


def collect_locks():
    """cr-locks inventory: count files + REAL review_slot_* flock occupancy."""
    _t("cr_locks_total", "Lock files present in cr-locks dir")
    _t("cr_review_slots_held", "Review concurrency slots currently flock-HELD (0..MAX), not file count")
    import fcntl  # local to avoid top-level import clutter
    try:
        if os.path.isdir(LOCK_DIR):
            files = os.listdir(LOCK_DIR)
            g("cr_locks_total", len(files))
            slots = [f for f in files if f.startswith("review_slot_")]
            # Real occupancy: try to take a non-blocking shared flock on each slot
            # file. If it fails (BlockingIOError) another process holds it => busy.
            # This is the true concurrency in use, not a count of files on disk.
            held = 0
            for name in slots:
                p = os.path.join(LOCK_DIR, name)
                try:
                    h = open(p, "a+")
                    try:
                        fcntl.flock(h.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(h.fileno(), fcntl.LOCK_UN)  # we just probed; release
                    except BlockingIOError:
                        held += 1
                    finally:
                        h.close()
                except Exception:
                    continue
            g("cr_review_slots_held", held)
        else:
            g("cr_locks_total", -1)
            g("cr_review_slots_held", -1)
    except Exception as e:
        print(f"[monitor] collect_locks err: {e}", file=sys.stderr)


def collect_workspace_sizes(cache):
    """Per-repo checkout + -review dir sizes (cached, coarse cadence)."""
    _t("cr_checkout_size_bytes", "Size of a checkout dir under cr-workspace", )
    now = time.time()
    if now - cache["t"] < SIZE_TTL:
        for key, val in cache["sizes"].items():
            g("cr_checkout_size_bytes", val, {"path": key})
        return
    cache["t"] = now
    cache["sizes"] = {}
    try:
        if os.path.isdir(WORKSPACE):
            for name in sorted(os.listdir(WORKSPACE)):
                p = os.path.join(WORKSPACE, name)
                if os.path.isdir(p):
                    rc, out = _q(f"du -s -B1 '{p}' 2>/dev/null", timeout=30)
                    # du prints "<bytes>\t<path>"; take the first whitespace field.
                    sz = _tonum(out.split()[0]) if out else 0
                    cache["sizes"][name] = sz
                    g("cr_checkout_size_bytes", sz, {"path": name})
    except Exception as e:
        print(f"[monitor] collect_workspace_sizes err: {e}", file=sys.stderr)


def _repo_name_from_topic(topic):
    """Derive the repo basename (== workspace checkout dir name) from a topic's
    mr_url project path last segment, matching the app's _ensure_checkout naming."""
    mr = topic.get("mr_url") or ""
    if "/-/merge_requests/" in mr:
        # .../<group>/<sub>/<repo>/-/merge_requests/<iid>
        proj = mr.split("/-/merge_requests/")[0] or ""
        base = proj.rstrip("/").split("/")[-1]
        if base:
            return base
    # fallback: try to infer from review_branch prefix is unreliable; leave empty.
    return ""


def collect_topic_resources(cache):
    """Per-OPEN-topic resource detail: for each non-CLOSED topic, emit its repo's
    shared checkout size + the -review (改码) dir size + its own result-file size.
    Reuses the cached du sizes so this is cheap; result-file size is computed live
    (tiny files). This powers the "逐话题资源明细表" Grafana table."""
    _t("cr_topic_checkout_size_bytes",
       "Shared checkout dir size (bytes) attributed to an OPEN topic's repo", )
    _t("cr_topic_review_size_bytes",
       "改码 -review dir size (bytes) attributed to an OPEN topic's repo", )
    _t("cr_topic_result_size_bytes",
       "This topic's review result file size (bytes)", )
    try:
        d = json.load(open(STATE_FILE_HOST, encoding="utf-8"))
        topics = d.get("topics", {})
        sizes = cache.get("sizes", {})
        for k, t in topics.items():
            if t.get("phase") == "CLOSED":
                continue
            topic_id = (k or "")[:44]
            repo = _repo_name_from_topic(t)
            if not repo:
                continue
            # shared checkout dir (repo name) + 改码 -review dir
            checkout_n = repo
            review_n = repo + "-review"
            g("cr_topic_checkout_size_bytes", sizes.get(checkout_n, 0),
              {"topic": topic_id, "repo": repo, "path": checkout_n})
            g("cr_topic_review_size_bytes", sizes.get(review_n, 0),
              {"topic": topic_id, "repo": repo, "path": review_n})
            # this topic's result file(s): result_<key>_<engine|game>.json live under
            # WORKSPACE root (not per-repo). Size them live (small), cached by key.
            for repofile in ("engine", "game"):
                p = os.path.join(WORKSPACE, f"result_{k}_{repofile}.json")
                try:
                    sz = os.path.getsize(p) if os.path.isfile(p) else 0
                except OSError:
                    sz = 0
                if sz:  # only emit if the file exists (size>0)
                    g("cr_topic_result_size_bytes", sz,
                      {"topic": topic_id, "repo": repo, "kind": repofile})
    except Exception as e:
        print(f"[monitor] collect_topic_resources err: {e}", file=sys.stderr)


def _iso2epoch(s):
    """Best-effort parse of our ISO local timestamp (no tz) -> epoch float; 0 on fail."""
    try:
        import time as _t
        return _t.mktime(_t.strptime(s, "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return 0.0


def collect_topic_detail():
    """C: per-topic business detail — severity counts, changed files, review
    duration (started->finished), age since created. Emits labelled series so
    Grafana can filter by topic / phase."""
    _t("cr_topic_severity_total", "Total findings per topic by severity (critical/warning/suggestion)")
    _t("cr_topic_changed_files", "Changed files per topic (from review result)")
    _t("cr_topic_age_seconds", "Seconds since topic creation (open-duration)")
    _t("cr_review_duration_seconds", "Review engine duration between started_at and finished_at")
    try:
        d = json.load(open(STATE_FILE_HOST, encoding="utf-8"))
        now = time.time()
        for k, t in d.get("topics", {}).items():
            topic_id = (k or "")[:44]
            eng = (t.get("repos") or {}).get("engine", {})
            ph = t.get("phase", "")
            sev = eng.get("severity_counts") or {}
            g("cr_topic_severity_total", sev.get("critical", 0), {"topic": topic_id, "sev": "critical", "phase": ph})
            g("cr_topic_severity_total", sev.get("warning", 0), {"topic": topic_id, "sev": "warning", "phase": ph})
            g("cr_topic_severity_total", sev.get("suggestion", 0), {"topic": topic_id, "sev": "suggestion", "phase": ph})
            g("cr_topic_changed_files", eng.get("changed_files", 0), {"topic": topic_id, "phase": ph})
            created = _iso2epoch(t.get("created_at") or "")
            if created:
                g("cr_topic_age_seconds", now - created, {"topic": topic_id, "phase": ph})
            s0 = eng.get("started_at") or ""
            f0 = eng.get("finished_at") or ""
            if s0 and f0:
                g("cr_review_duration_seconds", _iso2epoch(f0) - _iso2epoch(s0),
                  {"topic": topic_id, "phase": ph})
    except Exception as e:
        print(f"[monitor] collect_topic_detail err: {e}", file=sys.stderr)


def collect_action_counters():
    """C: server-activity counters from approval_log — how many 改码/确认/关闭/重审
    happened (cumulative; Grafana can rate()/increase() over time)."""
    _t("cr_actions_total", "Cumulative guarded actions per topic (from approval_log)")
    try:
        d = json.load(open(STATE_FILE_HOST, encoding="utf-8"))
        for k, t in d.get("topics", {}).items():
            topic_id = (k or "")[:44]
            for e in (t.get("approval_log") or []):
                action = e.get("action") or "?"
                result = e.get("result") or "?"
                # emit 1 per action so sum() gives cumulative count; labels allow filtering
                g("cr_actions_total", 1, {"topic": topic_id, "action": action, "result": result})
    except Exception as e:
        print(f"[monitor] collect_action_counters err: {e}", file=sys.stderr)


def collect_heartbeat_freshness():
    """C: bot heartbeat 'freshness' — age of the newest event-server log INSIDE the
    container. The container's /tmp (ev-server-logs) is NOT a host bind, so a host
    filesystem scan can't see it and would report a long-stale value even when the
    bot is healthy (false "bot dead" alert). We read it via `docker exec`, which the
    deploy host has. Falls back to a host-side scan only if exec fails."""
    _t("cr_bot_last_log_age_seconds", "Age (s) of the newest event-server log file (container)")
    now = time.time()
    log_age = None
    try:
        # Ask the container for the newest ev-server log and its mtime.
        rc, out = _q(
            "docker exec chaos-agent-cr sh -c "
            "'f=$(ls -t /tmp/ev-server-logs/ev-server-*.log 2>/dev/null | head -1); "
            "[ -n \"$f\" ] && echo \"$(stat -c %Y \"$f\")\"'",
            timeout=10,
        )
        if rc == 0 and out:
            mtime = _tonum(out)
            if mtime > 0:
                log_age = now - mtime
    except Exception as e:
        print(f"[monitor] heartbeat exec err: {e}", file=sys.stderr)
    if log_age is None:
        # Fallback: host-side scan (works if the container shares /var/lib/report-server
        # via bind, but NOT /tmp/ev-server-logs — so this is a best-effort).
        best = None
        for base in ("/var/lib/report-server/daily/cr-workspace",):
            try:
                if not os.path.isdir(base):
                    continue
                for name in os.listdir(base):
                    if "ev-server-" in name or name.endswith(".log"):
                        p = os.path.join(base, name)
                        try:
                            m = os.path.getmtime(p)
                        except OSError:
                            continue
                        if best is None or m > best[1]:
                            best = (p, m)
            except Exception:
                continue
        if best:
            log_age = now - best[1]
    # If we truly cannot determine freshness, emit -1 only if the bot process is also
    # absent; otherwise emit a sentinel that won't trigger a false "dead" alert.
    if log_age is not None:
        g("cr_bot_last_log_age_seconds", log_age)
    else:
        g("cr_bot_last_log_age_seconds", -1)


def collect_disk_usage():
    """C: dedicated disk usage for the persistent daily volume (where checkouts live)."""
    _t("cr_disk_usage_bytes", "Disk usage (bytes) of a monitored path")
    _t("cr_disk_total_bytes", "Disk total (bytes) of a monitored path (fs quota)")
    for path, mnt in (("/var/lib/report-server/daily", "daily"),):
        if not os.path.isdir(path):
            continue
        rc, out = _q(f"du -s -B1 '{path}' 2>/dev/null", timeout=30)
        used = _tonum(out.split()[0]) if out else 0
        g("cr_disk_usage_bytes", used, {"path": path, "mount": mnt})
        # fs total via node-exporter is elsewhere; here we just mark usage. We
        # set total to 0 (unknown) and let the dashboard derive from node fs.


def collect_review_failed():
    """C: per-topic review failure counter — emits 1 for each failed review attempt so
    Grafana can rate() to show error trend."""
    _t("cr_review_failed_total", "Cumulative failed review attempts per topic")
    try:
        d = json.load(open(STATE_FILE_HOST, encoding="utf-8"))
        for k, t in d.get("topics", {}).items():
            topic_id = (k or "")[:44]
            rc = int(t.get("retry_count") or 0)
            if t.get("phase") == "FAILED" and rc > 0:
                g("cr_review_failed_total", rc, {"topic": topic_id})
    except Exception as e:
        print(f"[monitor] collect_review_failed err: {e}", file=sys.stderr)

def collect_gitlab_probe():
    """Best-effort GitLab reachability probe (0/1); sets integration health."""
    _t("cr_gitlab_reachable", "GitLab API reachable (1) or not (0)")
    rc, out = _q("timeout 5 curl -s -o /dev/null -w '%{http_code}' "
                 "https://gitlab.booming-inc.com/api/v4/version 2>/dev/null || echo 000", timeout=10)
    code = out.strip()
    # Even a 401/404 proves reachability; only a curl-level failure (echo'd 000) means down.
    g("cr_gitlab_reachable", 1 if code not in ("", "000") else 0)


# --- main -----------------------------------------------------------------

def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    cache = {"t": 0.0, "sizes": {}}
    collect_topic_state()
    collect_processes()
    collect_locks()
    collect_workspace_sizes(cache)
    collect_topic_resources(cache)   # ② 逐话题资源明细
    collect_topic_detail()           # C: severity/changed/age/duration 业务层
    collect_action_counters()        # C: 改码/确认/关闭 审计动作计数
    collect_heartbeat_freshness()    # C: bot 心跳新鲜度
    collect_disk_usage()             # C: daily 卷磁盘占用
    collect_review_failed()          # C: review 失败计数
    collect_gitlab_probe()
    body = "\n".join(M) + "\n"
    # atomic write
    tmp = os.path.join(OUT_DIR, "cr_monitor.prom.tmp")
    dst = os.path.join(OUT_DIR, "cr_monitor.prom")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
    os.replace(tmp, dst)


if __name__ == "__main__":
    main()
