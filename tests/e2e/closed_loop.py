#!/usr/bin/env python3
"""tests/e2e/closed_loop.py — 端到端自测: 驱动 codereview 完整闭环。

在容器内运行时, 直接调用 orchestrate 命令(绕开飞书事件层)走:
  run(review) → interact 优化(改码+push+建MR) → 验证 → 关闭(清理 fix MR+分支)。
会真实在 GitLab 上创建/删除 fix MR 与分支(用完后清理)。适合 CI/回归。

用法(在容器 jenkins/scripts 下):
  python3 /path/tests/e2e/closed_loop.py --jira-url <URL> [--owner <id>]
"""
import argparse, json, os, subprocess, sys, time, random

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(os.path.dirname(SCRIPTS), "jenkins", "scripts") if "orchestrate" in SCRIPTS else os.path.join(os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "jenkins", "scripts")
WS = os.environ.get("CR_WORKSPACE", "/var/lib/report-server/daily/cr-workspace")
STATE = os.environ.get("PIPELINE_STATE_FILE", "/root/.codereview-pipeline-state.json")
LOCK = "/var/lib/report-server/daily/cr-locks"
OWNER = os.environ.get("CR_OWNER", "ou_55bca7b7dae982e96749bd84f57c21e8")

def _orchestrate(args, timeout=900):
    cmd = [sys.executable, os.path.join(SCRIPTS, "orchestrate.py")] + args
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or "")[:200], (r.stderr or "")[:150]

def _state(key):
    d = json.load(open(STATE)); return d.get("topics", {}).get(key, {})

def wait_phase(key, want, tries=30, wait=20):
    for _ in range(tries):
        t = _state(key)
        if t.get("phase") == want: return t
        if t.get("phase") in ("FAILED",): break
        time.sleep(wait)
    return _state(key)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jira-url", required=True)
    ap.add_argument("--jira-key", default="")
    ap.add_argument("--owner", default=OWNER)
    a = ap.parse_args()
    key = f"e2e_closed_{int(time.time()%100000)}_{random.randint(100,999)}"
    print(f"[e2e] key={key} jira={a.jira_url}")
    ok = True
    # 1. review
    rc, _, err = _orchestrate(["run","--key",key,"--mode","scan","--jira-url",a.jira_url,
        "--jira-key",a.jira_key,"--workspace",WS,"--pipeline-state-file",STATE,"--sender-id",a.owner])
    print(f"[e2e] review rc={rc}")
    if rc != 0: ok = False
    t = wait_phase(key, "DONE")
    print(f"[e2e] review phase={t.get('phase')}")
    # 2. 优化(全自动改码+建MR): interact 入队 + consume 执行
    rc, _, _ = _orchestrate(["interact","--key",key,"--reply","优化","--sender-id",a.owner,
        "--workspace",WS,"--pipeline-state-file",STATE], timeout=60)
    mriid = None
    for _ in range(8):   # consume 驱动改码→push→建MR
        _orchestrate(["consume","--workspace",WS,"--pipeline-state-file",STATE,"--lock-dir",LOCK], timeout=120)
        t = _state(key)
        if t.get("fix_mr_iids"):
            mriid = t["fix_mr_iids"][0]; print(f"[e2e] MR 建出 iid={mriid}"); break
        time.sleep(25)
    if not mriid:
        print("[e2e] FAIL: 未建出 MR"); ok = False
    # 3. 关闭清理
    sys.path.insert(0, SCRIPTS)
    import pipeline_state as ps, orchestrate as O
    O._env  # noqa
    t = _state(key)
    try: note = O._close_topic_resources(t)
    except Exception as e: note = str(e)
    ps.close_topic(STATE, key, closed_by="e2e", reason="e2e 闭环自测")
    ps.set_topic_fields(STATE, key, phase="CLOSED")
    print(f"[e2e] close 释放: {note}")
    print(f"[e2e] RESULT: {'PASS' if ok else 'FAIL'}")
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
