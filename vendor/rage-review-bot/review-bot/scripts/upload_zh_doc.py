"""Re-upload SKILL_zh.md to the Chinese reference doc on Lark.

One command for the whole documented flow, because two details bite every time
it is done by hand:

1. **The file is ~80 KB.** An inline `--markdown "<content>"` blows the Windows
   ~32 KB `CreateProcess` argv ceiling (`WinError 206`), and stdin (`-`)
   silently truncates. Only `--markdown "@<relative-path>"` works, and the path
   must be RELATIVE to the working directory.
2. **The YAML frontmatter must be stripped first.** It is skill-loader
   metadata, not documentation. Left in, the closing `---` turns the `name:`
   line into a setext heading and swallows `description:` into it, so the doc
   opened with a mangled `## name: review-botdescription: "..."` line.

Usage:
    python scripts/upload_zh_doc.py [--dry-run]
"""
import io
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import subprocess_util

# The target doc is per-workspace state, not source. CLAUDE.md requires
# machine-specific values to live in settings env vars rather than being
# baked into a script; the historical URL stays as the default so existing
# setups keep working without configuration.
DOC_URL = (os.environ.get("REVIEW_BOT_SKILL_ZH_DOC_URL")
           or "https://www.feishu.cn/docx/Ly0ydTHMYoBKZ7xYpEQcWgWonqC")

SKILL_DIR = os.path.dirname(SCRIPT_DIR)
SRC_NAME = "SKILL_zh.md"
# Sibling of the source so the upload's relative @path resolves from SKILL_DIR.
TMP_NAME = "SKILL_zh.body.md"


def strip_frontmatter(text):
    """Return `text` without its leading YAML frontmatter block.

    Raises ValueError rather than guessing when the block is missing or
    unterminated — silently uploading the wrong bytes is the failure mode
    this whole script exists to prevent.
    """
    if not text.startswith("---"):
        raise ValueError("no leading frontmatter found")
    lines = text.split("\n")
    for i, line in enumerate(lines[1:], start=1):
        if line.rstrip("\r") == "---":
            return "\n".join(lines[i + 1:]).lstrip("\r\n")
    raise ValueError("unterminated frontmatter block")


def main():
    dry_run = "--dry-run" in sys.argv
    src = os.path.join(SKILL_DIR, SRC_NAME)
    tmp = os.path.join(SKILL_DIR, TMP_NAME)

    with io.open(src, encoding="utf-8", newline="") as handle:
        text = handle.read()
    try:
        body = strip_frontmatter(text)
    except ValueError as exc:
        print(f"[FAIL] {SRC_NAME}: {exc}")
        return 1

    with io.open(tmp, "w", encoding="utf-8", newline="") as handle:
        handle.write(body)

    # `lark_cli_argv_prefix()` exists for exactly this and is what every
    # other script in this directory uses — it also picks the windowless
    # invocation path and degrades to `node run.js` off Windows.
    cmd = subprocess_util.lark_cli_argv_prefix() + [
           "docs", "+update", "--doc", DOC_URL,
           "--mode", "overwrite", "--markdown", "@" + TMP_NAME,
           "--format", "json"]
    try:
        if dry_run:
            print("[dry-run] would run:", " ".join(cmd[1:]))
            print(f"[dry-run] {len(text)} chars -> {len(body)} chars "
                  f"after frontmatter strip")
            return 0
        # cwd=SKILL_DIR: the @path must be relative to the working directory.
        out = subprocess.run(cmd, cwd=SKILL_DIR, capture_output=True,
                             text=True, encoding="utf-8")
        print((out.stdout or "").strip()[-1500:])
        if out.returncode != 0:
            print("[FAIL] rc", out.returncode)
            print((out.stderr or "")[-1500:])
            return 1
        if '"success": true' not in (out.stdout or ""):
            print("[FAIL] no success flag in response — treat as not uploaded")
            return 1
        print(f"[OK] uploaded {len(body.encode('utf-8'))} bytes to {DOC_URL}")
        return 0
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


if __name__ == "__main__":
    sys.exit(main())
