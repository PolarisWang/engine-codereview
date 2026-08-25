# -*- coding: utf-8 -*-
"""Render the 问题详情 (issue detail) section of a full-review Lark doc as
Simplified-Chinese markdown.

The full-review Lark doc's issue list is rendered deterministically — NOT
hand-written — so every issue heading carries `[Repo] file[:line_range]
[（function）]` and the `#N` numbering stays in lockstep with the thread
reply's inline list (both derive from the same `review.issues[]` array). See
DESIGN §1.9.5 and `spawn_topic_agent.md` §7.

Usage:
    python render_doc_issues.py --vars-file doc_issues.json > issues.md
    python render_doc_issues.py --vars-file doc_issues.json --check-only

`--vars-file` is a JSON file shaped `{"ISSUES": [ <review.issues-shaped> ]}`,
i.e. each entry has {severity, repo, file, description (or text),
line_range?, function?}. `--check-only` validates the input and exits 0 on
success, 1 with the ValueError message on stderr otherwise (mirrors
render.py's --check-only contract for pre-write validation).

Writes UTF-8 markdown to stdout. Splice it into the doc body file before
calling lark_doc_helper.py create.
"""
import argparse
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render import build_doc_issue_markdown


def main():
    parser = argparse.ArgumentParser(
        description="Render full-review Lark doc 问题详情 markdown")
    parser.add_argument("--vars-file", required=True,
                        help='JSON file shaped {"ISSUES": [...]}')
    parser.add_argument("--check-only", action="store_true",
                        help="Validate input without emitting. Exits 0 on "
                             "success, 1 with the ValueError on stderr.")
    args = parser.parse_args()

    with open(args.vars_file, encoding="utf-8") as f:
        variables = json.load(f)
    issues = variables.get("ISSUES")
    if issues is None:
        print("vars-file must contain an 'ISSUES' key", file=sys.stderr)
        return 1

    try:
        markdown = build_doc_issue_markdown(issues)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.check_only:
        return 0

    # Force UTF-8 on Windows to avoid GBK encoding errors.
    stdout_utf8 = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    stdout_utf8.write(markdown)
    stdout_utf8.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
