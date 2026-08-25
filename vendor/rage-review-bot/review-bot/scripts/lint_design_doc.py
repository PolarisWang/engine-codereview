# -*- coding: utf-8 -*-
"""Structural lint for the review-bot DESIGN.md / SKILL.md hierarchy.

Keeps the feature-anchored structure from decaying back into a flat,
EOF-appended pitfall catalog. Run before committing doc changes (the SKILL.md
"Docs to keep in sync" checklist) and wired into hooks (PostToolUse on doc
edits, PreToolUse on `git commit`).

Hard checks (exit 1 on violation):
  1. Top-level `## ` allowlist in DESIGN.md — only "1. Feature Inventory",
     "2. Two-Phase Topic Lifecycle", and "Appendix:*". A new `## 3.`/`## 9.`
     catalog fails, so a new concern has nowhere to go but a `#### 1.x.y` child.
  2. Hierarchy: every `#### a.b.c` sits under an existing `### a.b` parent;
     child numbers are contiguous + unique per parent.
  3. No dangling `§` refs: every `§<dotted>` across review-bot *.py/*.md
     resolves to a DESIGN heading. Excludes `(was §X)` tags, the crosswalk
     appendix, and SKILL step refs like `§2d` (digit+letter).
  4. Crosswalk appendix: every `old -> new` row's new id is a live heading.

Advisory (reported, does not fail): SKILL.md "## Design Notes" bullets that
carry no `DESIGN §` pointer (possible re-duplicated prose).

`--json` emits machine-readable output for hooks.
"""
import json
import os
import re
import sys

# Emit UTF-8 regardless of console codepage — the JSON/text output carries `§`,
# which a Windows cp936 stdout would mangle (and a UTF-8 subprocess reader, e.g.
# the doc-lint hook, would then fail to decode).
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.normpath(os.path.join(HERE, ".."))
DESIGN = os.path.join(SKILL_DIR, "DESIGN.md")
SKILL = os.path.join(SKILL_DIR, "SKILL.md")
APPENDIX = "## Appendix: Crosswalk"
ALLOWED_TOP = ("1. Feature Inventory", "2. Two-Phase Topic Lifecycle")
HEAD_RE = re.compile(r"^(#{2,4})\s+(.*)$")
NUM_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(.*)$")
# DESIGN ref: dotted number, NOT a `(was §X)` tag, NOT a `§2d` step ref.
REF_RE = re.compile(r"(?<!\(was )§(\d+(?:\.\d+)*)(?![A-Za-z])")


def iter_headings(text):
    """Yield (level, num_or_None, title), skipping fenced code blocks."""
    in_code = False
    for ln in text.splitlines():
        if ln.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = HEAD_RE.match(ln)
        if not m:
            continue
        level = len(m.group(1))
        nm = NUM_RE.match(m.group(2).strip())
        yield level, (nm.group(1) if nm else None), m.group(2).strip()


def source_files():
    for dp, dn, fns in os.walk(SKILL_DIR):
        if "cfg" in dn:
            dn.remove("cfg")
        for fn in fns:
            if not (fn.endswith(".py") or fn.endswith(".md")):
                continue
            if fn.startswith("_reorg") or fn == "lint_design_doc.py":
                continue
            yield os.path.join(dp, fn)


def main():
    as_json = "--json" in sys.argv
    errors, warnings = [], []
    design = open(DESIGN, encoding="utf-8").read()
    heads = list(iter_headings(design))
    heading_nums = {num for _l, num, _t in heads if num}

    # 1. top-level allowlist
    for level, _num, title in heads:
        if level == 2 and title not in ALLOWED_TOP and not title.startswith("Appendix"):
            errors.append(f"[top-allowlist] unexpected `## {title}` — new concerns "
                          f"must be `#### 1.x.y` children, not new top sections")

    # 2. hierarchy: #### under existing ### parent, contiguous children
    sec3 = {num for _l, num, _t in heads if _l == 3 and num}
    kids = {}
    for level, num, title in heads:
        if level == 4:
            if not num or num.count(".") != 2:
                errors.append(f"[hierarchy] `#### {title}` is not a 3-part `a.b.c` id")
                continue
            parent = num.rsplit(".", 1)[0]
            if parent not in sec3:
                errors.append(f"[hierarchy] `#### {num}` has no `### {parent}` parent")
            kids.setdefault(parent, []).append(int(num.rsplit(".", 1)[1]))
    for parent, nums in kids.items():
        expect = list(range(1, len(nums) + 1))
        if sorted(nums) != expect:
            errors.append(f"[hierarchy] {parent} children {sorted(nums)} "
                          f"not contiguous/unique (want {expect})")

    # 3. dangling refs
    for path in source_files():
        text = open(path, encoding="utf-8").read()
        if os.path.basename(path) == "DESIGN.md":
            text = text.split(APPENDIX, 1)[0]
        rel = os.path.relpath(path, SKILL_DIR)
        for tok in set(REF_RE.findall(text)):
            # Only dotted ids (§1.x, §1.x.y, §2.1) are unambiguous DESIGN refs.
            # Bare single-number refs are intra-/cross-doc section pointers
            # (spawn_topic_agent.md §3, SKILL.md §2 table / §2d step) — not
            # DESIGN headings, so don't validate them here.
            if "." not in tok:
                continue
            if tok not in heading_nums:
                errors.append(f"[dangling-ref] §{tok} in {rel} resolves to no DESIGN heading")

    # 4. crosswalk appendix new-ids resolve
    if APPENDIX in design:
        tail = design.split(APPENDIX, 1)[1]
        for old, new in re.findall(r"\|\s*§([\d.]+)\s*\|\s*§([\d.]+)\s*\|", tail):
            if new not in heading_nums:
                errors.append(f"[crosswalk] §{old} -> §{new}: §{new} is not a live heading")

    # advisory: SKILL Design Notes bullets without a DESIGN pointer
    skill = open(SKILL, encoding="utf-8").read()
    m = re.search(r"\n## Design Notes\n(.*?)\n## ", skill, re.S)
    if m:
        for line in m.group(1).splitlines():
            if line.startswith("- ") and "DESIGN §" not in line:
                warnings.append(f"[dedup] Design Notes bullet without DESIGN pointer: "
                                f"{line.strip()[:70]}")

    if as_json:
        print(json.dumps({"ok": not errors, "errors": errors, "warnings": warnings},
                         ensure_ascii=False))
    else:
        for e in errors:
            print("ERROR " + e)
        for w in warnings:
            print("warn  " + w)
        print(f"\n{'FAIL' if errors else 'OK'}: {len(errors)} errors, {len(warnings)} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
