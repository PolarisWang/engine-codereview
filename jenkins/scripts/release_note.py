#!/usr/bin/env python3
"""release_note.py — 从 git 历史生成本周期(release)改动分组 + 计算下一个版本号。

纯逻辑/无 LLM：分组 + 版本计算都在此（可单测）；官方文案润色由调用方(orchestrate
_cmd_release)用 _call_llm_simple 完成，避免本模块重依赖。

版本规则（与 docs/release-management.md 一致）：
  - 默认 patch +1（1.0.0 -> 1.0.1）
  - 本周期 feat >= minor_if_feat_ge(默认3) 或含 breaking -> minor +1（1.0.0 -> 1.1.0）
  - major 仅当调用方显式要求（机器人绝不自动 bump major）
"""
import argparse
import re
import subprocess
import sys

# 本周期边界：上版本 tag。首次发布(无 tag)用双点到根。
TYPE_ORDER = ["feat", "fix", "chore", "docs", "refactor", "perf", "test", "build", "ci", "style", "other"]
# 每个严重度/类型小节最多展示的行数（防刷屏；超出显式标注剩余条数）
MAX_LINES = 12
TYPE_CN = {"feat": "✨ 新功能", "fix": "🐛 问题修复", "chore": "🧰 维护",
           "docs": "📝 文档", "refactor": "♻️ 重构", "perf": "⚡ 性能",
           "test": "🧪 测试", "build": "🔨 构建", "ci": "🚦 CI",
           "style": "🎨 样式", "other": "📦 其他"}


def run_git(args, cwd):
    r = subprocess.run(["git"] + list(args), capture_output=True, text=True, cwd=cwd, timeout=60)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def list_tags(cwd):
    rc, out, _ = run_git(["tag", "-l", "v*"], cwd)
    tags = [t for t in out.splitlines() if re.match(r"^v\d+\.\d+\.\d+$", t)]
    return sorted(tags, key=lambda s: [int(x) for x in s[1:].split(".")])


def parse_version(tag):
    m = re.match(r"^v(\d+)\.(\d+)\.(\d+)$", tag or "")
    if not m:
        return None
    return tuple(int(x) for x in m.groups())


def _scope_of(title):
    # conventional: type(scope)!: rest
    m = re.match(r"^([\w-]+)(?:\(([^)]*)\))?(!)?:\s*(.*)$", title)
    if not m:
        return "other", "", False, title
    t, scope, bang, rest = m.groups()
    return t.lower(), (scope or ""), bool(bang), rest


def group_commits(commits):
    """commits: [(short_sha, title)]. Return list of (type, [{'sha','title','scope','rest'}])."""
    groups = {}
    for sha, title in commits:
        t, scope, _bang, rest = _scope_of(title)
        groups.setdefault(t, []).append({"sha": sha, "title": title,
                                          "scope": scope, "rest": (rest or title)})
    ordered = [t for t in TYPE_ORDER if t in groups]
    ordered += [t for t in groups if t not in TYPE_ORDER]
    return [(t, groups[t]) for t in ordered]


def detect_breaking(commits):
    """True if any commit is breaking (feat!: or contains 'breaking')."""
    for _sha, title in commits:
        t, _scope, bang, _rest = _scope_of(title)
        if bang or "breaking" in title.lower():
            return True
    return False


def feat_count(commits):
    return sum(1 for _s, t in commits if _scope_of(t)[0] == "feat")


def next_version(prev_tag, commits, minor_if_feat_ge=3, force_major=False):
    """计算下一个版本号。prev_tag 可为空(首次 -> v1.0.0)。"""
    if force_major:
        if prev_tag:
            maj, _minor, _patch = parse_version(prev_tag)
            return (maj + 1, 0, 0)
        return (1, 0, 0)
    if not prev_tag:
        return (1, 0, 0)
    maj, minor, patch = parse_version(prev_tag)
    if feat_count(commits) >= minor_if_feat_ge or detect_breaking(commits):
        return (maj, minor + 1, 0)
    return (maj, minor, patch + 1)


def build_note(version, commits, include_summary=True):
    """生成纯分组的官方中文 release note（无 LLM 润色版）。返回 str。"""
    major, minor, patch = version
    lines = [f"🆕 版本 v{major}.{minor}.{patch} 发布", ""]
    if include_summary:
        n_feat, n_fix = feat_count(commits), sum(1 for _s, t in commits if _scope_of(t)[0] == "fix")
        parts = []
        if n_feat:
            parts.append(f"{n_feat} 项新功能")
        if n_fix:
            parts.append(f"{n_fix} 项修复")
        lines.append(f"📦 本版本含{'、'.join(parts) if parts else '若干改动'}。")
        lines.append("")
    for typ, items in group_commits(commits):
        lines.append(f"{TYPE_CN.get(typ, '📦 其他')}")
        for it in items[:MAX_LINES]:
            rest = it['rest'].strip().strip('.')
            scope = (it.get('scope') or '').strip()
            # 言简意赅：若有 scope(文件/模块)则前置突出，再加一句简短描述。
            lines.append(f" · {scope}：{rest}" if scope else f" · {rest}")
        if len(items) > MAX_LINES:
            lines.append(f" （该节共 {len(items)} 项，其余见 history）")
        lines.append("")
    return "\n".join(lines).rstrip()


def _git_range_commits(prev_tag, cwd):
    """返回 [(short_sha, title)] — 本周期(prev_tag..HEAD)提交，去 merge。"""
    if prev_tag:
        rc, out, _ = run_git(["log", "--oneline", "--no-decorate", f"{prev_tag}..HEAD"], cwd)
    else:
        rc, out, _ = run_git(["log", "--oneline", "--no-decorate"], cwd)
    commits = []
    for line in out.splitlines():
        parts = line.split(None, 1)
        if not parts:
            continue
        short, title = parts[0], (parts[1] if len(parts) > 1 else "")
        if title.lower().startswith("merge ") or "Merge " in title:
            continue
        commits.append((short, title))
    return commits


def main():
    ap = argparse.ArgumentParser(description="生成 release 改动分组 + 下一个版本号")
    ap.add_argument("--repo-dir", default=".", help="git 仓库目录")
    ap.add_argument("--prev-tag", default="", help="上版本 tag；留空自动找最新 vX.Y.Z")
    ap.add_argument("--minor-if-feat-ge", type=int, default=3)
    ap.add_argument("--force-major", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    prev = a.prev_tag
    if not prev:
        tags = list_tags(a.repo_dir)
        prev = tags[-1] if tags else ""
    commits = _git_range_commits(prev, a.repo_dir)
    ver = next_version(prev, commits, a.minor_if_feat_ge, a.force_major)
    note = build_note(ver, commits)
    if a.json:
        import json
        print(json.dumps({"prev_tag": prev or None, "version": ".".join(map(str, ver)),
                          "commits": len(commits), "note": note}, ensure_ascii=False, indent=2))
    else:
        print(f"prev={prev or '(none)'} next=v{'.'.join(map(str, ver))} commits={len(commits)}")
        print()
        print(note)


if __name__ == "__main__":
    sys.exit(main())
