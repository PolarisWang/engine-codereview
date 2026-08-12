#!/usr/bin/env python3
"""release.py — 本地版本发布 CLI（agent 侧执行；仅当用户明确要求发布时运行）。

用法（agent 告知用户有新改动 -> 用户让发布 -> 由 agent 运行）：
    python3 deploy/release.py                 # 自动版本(patch+1 或 判定 minor)；发布并通知群
    python3 deploy/release.py --major         # 用户显式要求大版本(major+1)，否则绝不 bump major
    python3 deploy/release.py --on-behalf-of 别名   # 记录发布者
    python3 deploy/release.py --dry-run       # 只预览 note+版本，不打 tag/不发群

版本规则（docs/release-management.md）：
  - 默认 patch+1：1.0.0 -> 1.0.1
  - 本周期 feat>=minor_if_feat_ge(默认3) 或 含 breaking -> minor+1：1.0.0 -> 1.1.0
  - major 仅当显式 --major（机器人绝不自动 major）

前置检查（防发错）：
  - 工作区干净；当前分支=main；main 已领先于 origin（无未推送工作）
  - 目标群 id 可取；否则只打 tag 不通知群（并提示）
幂等：同名 tag 已存在则拒绝重复发布（防重复发群）
"""
import argparse
import base64
import os
import subprocess
import sys

# 让脚本能 import 同仓的 jenkins/scripts（兄弟模块）
SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "jenkins", "scripts")
sys.path.insert(0, os.path.abspath(SCRIPTS))
import release_note as rn  # noqa: E402


def git(args, cwd):
    r = subprocess.run(["git"] + list(args), capture_output=True, text=True, cwd=cwd, timeout=120)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def load_cfg():
    try:
        import yaml
        return yaml.safe_load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.yaml"), encoding="utf-8")) or {}
    except Exception:
        return {}


def _polish_release_note(version, commits):
    """用 LLM 把分组版 release note 润色成官方中文文案。失败/无凭证返回原分组版。"""
    import json as _json
    import urllib.request as _ur
    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")
    base_url = (os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/")
    model = os.environ.get("ANTHROPIC_MODEL") or "deepseek-v4-flash"
    if not api_key:
        return None
    grouped_lines = []
    for t, items in rn.group_commits(commits):
        for it in items[:8]:
            scope = (it.get('scope') or '').strip()
            rest = it['rest'].strip().strip('.')
            grouped_lines.append(f"{t} {scope + ':' if scope else ''} {rest}".rstrip())
    raw = "\n".join(grouped_lines)
    prompt = (
        f"你是一名发布经理。请把下面代码仓库的改动整理成一份**言简意赅**的官方中文 release note（版本 v{version}）。\n"
        "规则：\n"
        "- 只按实际提交类型分节：✨ 新功能 / 🐛 问题修复 / 🧰 维护；某类没有改动就不要列该节（禁止编造）\n"
        "- **每条一行**，`· 一句话`（10-25 字）。**不要重复**同一文件名/模块前缀多次，也不要同一改动拆成多行\n"
        "- 英文 scope(如 release-note/config) 若只为标注类别则**不出现**在行首，改成描述里自然的词\n"
        "- 开头 `🆕 版本 v{version} 发布`，只输出 markdown 正文\n\n"
        f"改动：\n{raw}"
    )
    try:
        payload = _json.dumps({
            "model": model, "max_tokens": 700,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        req = _ur.Request(f"{base_url}/v1/messages", data=payload,
                          headers={"Content-Type": "application/json", "x-api-key": api_key,
                                   "anthropic-version": "2023-06-01"}, method="POST")
        with _ur.urlopen(req, timeout=120) as resp:
            result = _json.loads(resp.read())
        text = "".join(b.get("text", "") for b in result.get("content", []) if b.get("type") == "text")
        text = text.strip()
        return text if text else None
    except Exception as e:
        print(f"[release] LLM 润色失败，退回分组版：{e}", file=sys.stderr)
        return None


def main():
    ap = argparse.ArgumentParser(description="本地版本发布（仅用户要求时运行）")
    ap.add_argument("--repo-dir", default=".", help="要被发布(打 tag)的 git 仓库（默认当前仓）")
    ap.add_argument("--major", action="store_true", help="用户显式要求大版本(major+1)")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不打 tag/不发群")
    ap.add_argument("--on-behalf-of", default="", help="发布者标识（记录日志）")
    a = ap.parse_args()

    repo = os.path.abspath(a.repo_dir)
    cfg = load_cfg()
    rel = cfg.get("release") or {}
    minor_ge = int(rel.get("minor_if_feat_ge", 3))

    # ── 前置检查 ──
    rc, status, _ = git(["status", "--porcelain"], repo)
    if rc == 0 and status.strip():
        print(f"❌ 工作区不干净，存在未提交改动：\n{status}\n请先提交/清理。", file=sys.stderr)
        return 1
    branch = "main"
    rc, cur, _ = git(["rev-parse", "--abbrev-ref", "HEAD"], repo)
    branch = cur or branch
    if branch != "main":
        print(f"⚠️ 当前分支是 {branch}，不是 main。", file=sys.stderr)
    rc, _unpushed, _ = git(["log", "origin/main..HEAD", "--oneline"], repo)
    if _unpushed:
        print(f"❌ main 有 {len(_unpushed.splitlines())} 个未推送到 origin 的提交，请先 push。", file=sys.stderr)
        return 1

    # ── 计算版本 + note ──
    tags = rn.list_tags(repo)
    prev = tags[-1] if tags else ""
    commits = rn._git_range_commits(prev, repo)
    ver = rn.next_version(prev, commits, minor_ge, force_major=a.major)
    version = ".".join(map(str, ver))
    tag = f"v{version}"
    note = rn.build_note(ver, commits)

    # LLM 润色成官方中文（可选；失败/无凭证自动退回分组版）。
    # 小发布(≤2 提交)直接用分组 ters 版即可，避免 LLM 把一个提交拆出多个重复小节的噪音。
    if rel.get("note_llm") and len(commits) > 2:
        polished = _polish_release_note(version, commits)
        if polished:
            note = polished

    print(f"prev={prev or '(none)'} next={tag} commits={len(commits)}")

    if a.dry_run:
        print(note)
        print("\n[dry-run] 未打 tag / 未发群。")
        return 0

    # ── 幂等：同名 tag 已存在则拒绝 ──
    rc, has, _ = git(["rev-parse", "--verify", "refs/tags/"+tag], repo)
    if rc == 0:
        print(f"❌ tag {tag} 已存在（可能已发布过）。如需重新发布请删除或改版本。", file=sys.stderr)
        return 1

    # ── 打 tag + push ──
    rc, _, err = git(["tag", "-a", tag, "-m", f"Release {tag}"], repo)
    if rc != 0:
        print(f"❌ 打 tag 失败：{err}", file=sys.stderr); return 1
    rc, _, err = git(["push", "origin", tag], repo)
    if rc != 0:
        print(f"⚠️ tag 推送失败（本地已打，未推远端）：{err}", file=sys.stderr)

    # ── 发群（纯文字；失败不阻断发布） ──
    chat_id = (os.environ.get("FEISHU_CHAT_ID") or (cfg.get("feishu") or {}).get("chat_id") or rel.get("chat_id") or "")
    if chat_id and os.environ.get("FEISHU_APP_ID") and os.environ.get("FEISHU_APP_SECRET"):
        r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "feishu_notifier.py"),
                            "send-message",
                            "--app-id", os.environ["FEISHU_APP_ID"],
                            "--app-secret", os.environ["FEISHU_APP_SECRET"],
                            "--chat-id", chat_id,
                            "--message-base64", base64.b64encode(note.encode()).decode()],
                           capture_output=True, text=True)
        print(f"📨 已通知群 {chat_id[:10]}…" if r.returncode == 0 else f"⚠️ 发群失败：{r.stderr[:200]}")
    else:
        print("⚠️ 未配置群/凭证，已打 tag 但未发群。")

    whos = f"（发布者：{a.on_behalf_of}）" if a.on_behalf_of else ""
    print(f"✅ 已发布 {tag}{whos}，共 {len(commits)} 个提交。\n\n{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
