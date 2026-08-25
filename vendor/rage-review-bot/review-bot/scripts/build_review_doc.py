# -*- coding: utf-8 -*-
"""Assemble (and optionally create) the full-review Lark doc body.

Complex reviews post a Lark doc holding the full file list + per-issue detail.
Every topic agent used to hand-author a ~5 KB `build_doc.py` that stitched a
head (title / 概述 / 变更概览) onto the issue section and shelled out to
`lark_doc_helper.py create`. The issue section is already deterministic
(`render_doc_issues` / `render.build_doc_issue_markdown`); this CLI owns the
WHOLE body so the agent only supplies the variable data (summary text, file
descriptions, the issues array) and never re-authors doc scaffolding.

The body structure is fixed:

    # 代码审查 RAGE-XXXXX

    ## 概述
    <summary — the agent's judgment>

    ## 变更概览
    - **[Chaos] path/to/file.cpp** +X/-Y — <description>
    ...

    ## 问题详情
    #### #N [严重] [Chaos] file.cpp:120-145 （function）
    <full description>
    ...

Issue `#N` numbering + ordering come straight from `build_doc_issue_markdown`,
so the doc stays in lockstep with the thread reply's inline list (both derive
from the same issues array — DESIGN §1.9.5). Issue input is VALIDATED here
(raises on bad severity / missing repo/file/description), giving the agent the
same pre-write guard `render_doc_issues --check-only` provided.

Input: a single JSON (``--input-file PATH`` or ``--input-stdin``):

    {
      "ticket_id": "RAGE-XXXXX",          // required
      "summary": "概述文本",               // required (agent judgment)
      "files": [                          // optional change-overview rows
        {"repo": "chaos", "path": "a.cpp", "insertions": 5,
         "deletions": 3, "description": "改了点东西"}
      ],
      "issues": [ <review.issues-shaped> ],// optional; [] -> no-issue note
      "create": false,                    // create the doc via lark_doc_helper
      "grant_view": ["ou_AAA", "ou_BBB"], // view grants (when create)
      "public_link": "tenant_readable"    // optional org link (when create)
    }

CLI:
    # assemble only -> markdown on stdout (or --out-file)
    python build_review_doc.py --input-file doc.json
    python build_review_doc.py --input-file doc.json --out-file body.md
    # assemble + create the Lark doc -> JSON on stdout
    python build_review_doc.py --input-file doc.json --create

Output: the markdown body (assemble mode) or the lark_doc_helper create JSON
(create mode). The create-mode temp .md is cleaned up; only assemble mode with
``--out-file`` reports a persistent ``markdown_file`` path.
"""
import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR / "templates"))

from render import build_doc_issue_markdown  # validates + renders issues

LARK_DOC_HELPER = SCRIPT_DIR / "lark_doc_helper.py"

# Repo slug -> Chinese-doc label. Mirrors spawn_topic_agent.md §3 rule 3.
_REPO_LABELS = {"rage": "Game", "chaos": "Chaos"}


def _repo_label(repo):
    if repo in _REPO_LABELS:
        return _REPO_LABELS[repo]
    if str(repo).startswith("3rd_party/"):
        return "3rd-party"
    return repo


def build_body(data):
    """Assemble the full Chinese doc markdown. Raises ValueError on bad issues."""
    ticket = data.get("ticket_id")
    if not ticket:
        raise ValueError("input must include 'ticket_id'")
    summary = (data.get("summary") or "").strip()
    if not summary:
        raise ValueError("input must include a non-empty 'summary'")

    lines = [f"# 代码审查 {ticket}", "", "## 概述", "", summary, ""]

    files = data.get("files") or []
    if files:
        lines += ["## 变更概览", ""]
        for f in files:
            label = _repo_label(f.get("repo", ""))
            path = f.get("path", "")
            ins = f.get("insertions", 0)
            dele = f.get("deletions", 0)
            desc = (f.get("description") or "").strip()
            tail = f" — {desc}" if desc else ""
            lines.append(f"- **[{label}] {path}** +{ins}/-{dele}{tail}")
        lines.append("")

    issues = data.get("issues") or []
    lines += ["## 问题详情", ""]
    if issues:
        lines.append(build_doc_issue_markdown(issues))  # raises on bad input
    else:
        lines.append("（本轮自动审查未发现问题。）")
    lines.append("")

    return "\n".join(lines)


def _create_doc(ticket, markdown, grant_view, public_link):
    """Write the body to a temp file and shell out to lark_doc_helper create.

    The temp .md carries review content; it is removed in `finally` (the
    create caller only needs the docx + url, not a local markdown path —
    leaving one per run widens the exposure surface). Mirrors
    finalize_review._check_only's clean-up-by-default policy.

    `lark_doc_helper`'s `--grant-view` is a comma-separated string, so a
    comma/whitespace inside an open_id would be mis-split into wrong members.
    open_ids are bare `ou_`+alnum tokens, so reject any that aren't — fail
    loud BEFORE creating the doc rather than silently granting the wrong user.
    """
    bad = [g for g in grant_view if "," in g or any(c.isspace() for c in g)]
    if bad:
        return ({"status": "error", "stage": "grant",
                 "error": f"grant_view entries must be bare open_ids "
                          f"(no comma/whitespace): {bad}"}, 1)

    with tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8") as mf:
        mf.write(markdown)
        md_path = mf.name
    argv = [sys.executable, str(LARK_DOC_HELPER), "create",
            "--title", f"代码审查 {ticket}",
            "--markdown-file", md_path]
    if grant_view:
        argv += ["--grant-view", ",".join(grant_view)]
    if public_link:
        argv += ["--public-link", public_link]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              encoding="utf-8")
        try:
            out = json.loads((proc.stdout or "").strip() or "{}")
        except ValueError:
            out = {"status": "error", "stage": "create",
                   "error": (proc.stderr or proc.stdout or "")[:500]}
        return out, proc.returncode
    finally:
        try:
            os.unlink(md_path)
        except OSError:
            pass


def _load_input(args):
    if args.input_stdin:
        return json.loads(sys.stdin.read())
    with open(args.input_file, encoding="utf-8") as f:
        return json.load(f)


def _emit(text):
    w = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    w.write(text)
    if not text.endswith("\n"):
        w.write("\n")
    w.flush()


def _cli():
    ap = argparse.ArgumentParser(
        description="Assemble (and optionally create) a full-review Lark doc.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--input-file", help="Path to the input JSON.")
    src.add_argument("--input-stdin", action="store_true",
                     help="Read the input JSON from stdin.")
    ap.add_argument("--out-file", help="Write the assembled markdown here.")
    ap.add_argument("--create", action="store_true",
                    help="Create the Lark doc (overrides input 'create').")
    args = ap.parse_args()

    data = _load_input(args)

    try:
        markdown = build_body(data)
    except ValueError as exc:
        _emit(json.dumps({"status": "error", "error": str(exc)},
                         ensure_ascii=False))
        return 1

    if args.out_file:
        Path(args.out_file).write_text(markdown, encoding="utf-8")

    create = args.create or bool(data.get("create"))
    if not create:
        if args.out_file:
            _emit(json.dumps({"status": "ok", "markdown_file": args.out_file,
                              "bytes": len(markdown.encode("utf-8"))},
                             ensure_ascii=False))
        else:
            _emit(markdown)
        return 0

    out, rc = _create_doc(
        data.get("ticket_id"), markdown,
        data.get("grant_view") or [], data.get("public_link"))
    _emit(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("status") == "ok" else (rc or 1)


if __name__ == "__main__":
    sys.exit(_cli())
