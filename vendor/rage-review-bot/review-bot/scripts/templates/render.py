# -*- coding: utf-8 -*-
"""Render a Lark post JSON template by filling {{PLACEHOLDER}} values.

Usage:
    python render.py <template_name> --vars '{"KEY":"value",...}'
    python render.py <template_name> --vars-file /path/to/vars.json

    # Render AND post in one shot (agents MUST use this for all Lark posts):
    python render.py <template_name> --vars-file vars.json --post --message-id om_xxx

Outputs the filled JSON to stdout. With --post, also posts via lark-cli
and prints the lark-cli response JSON to stdout instead.

Placeholder types:
  {{KEY}}              — simple string substitution
  "{{ARRAY_KEY}}"      — replaced by a JSON array of paragraph arrays
                         (the value in vars must be a list of lists)

Builtin vars (auto-injected by render(); a caller-provided value wins):
  BOT_MENTION_SEGMENTS — inline segment list telling the dev how to address
                         the bot: a real @-mention when REVIEW_BOT_OPEN_ID
                         resolves, else the literal bold `@bot` text.
                         See DESIGN §1.9.6.

Structured shortcuts (preferred over hand-rolled paragraph arrays):
  MRS            → MR_LINKS_PARAGRAPHS (list of {repo, iid, branch, url})
  FILES          → FILE_SECTION_PARAGRAPHS (header + per-file rows + spacer; full reviews omit FILES, doc holds the file list)
  ISSUES         → ISSUE_PARAGRAPHS    (list of {severity, repo, file, text, line_range?, function?};
                                        repo/file/text required, line_range/function optional;
                                        `text` is prose only — location prefix added by render;
                                        index auto-assigned, sorted by severity)
  FLAGGED_ISSUES → FLAGGED_ISSUE_PARAGRAPHS (same shape as ISSUES)
  DOC_LINK       → DOC_LINK_PARAGRAPHS (one {title, url} dict; used by review_round1 for full reviews)

Validation:
  - Required scalar vars (e.g. FILE_COUNT, SUMMARY) are enforced per-template.
  - Severities must be one of {严重, 中, 轻, 建议} — non-canonical labels
    (e.g. [suggestion], [信息], [Nit]) are rejected.
  - Unfilled {{KEY}} placeholders after substitution raise a clear error
    instead of silently rendering as empty.

The "_comment" and "_usage" keys are stripped from output.
"""
import json
import sys
import os
import re
import argparse
import subprocess
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import subprocess_util


TEMPLATE_DIR = Path(__file__).resolve().parent

# Severity enum: must match ordering for sort priority
VALID_SEVERITIES = ["严重", "中", "轻", "建议"]
_SEVERITY_ORDER = {sev: idx for idx, sev in enumerate(VALID_SEVERITIES)}

# Required scalar variables per template — empty string / None triggers ValueError.
# Array placeholders (e.g. ISSUE_PARAGRAPHS) are NOT listed here; their absence
# is caught by the unfilled-placeholder check after rendering.
REQUIRED_VARS = {
    # FILE_COUNT / INSERTIONS / DELETIONS / MR_LINKS_PARAGRAPHS moved to
    # ack_new_topic — the round-1 review no longer re-states them.
    "review_round1": ["TICKET_ID", "ROUND", "SUMMARY", "APPROVER_ID"],
    "review_round1_dev_triage": ["TICKET_ID", "ROUND", "SUMMARY",
                                 "DEVELOPER_ID", "DEVELOPER_NAME"],
    # review_roundN is addressed to the developer under the self-service
    # loop (DESIGN §1.23.6) — the approver is only mentioned at hand-off.
    "review_roundN": ["TICKET_ID", "ROUND", "SUMMARY",
                      "DEVELOPER_ID", "DEVELOPER_NAME"],
    "handoff_summary": ["TICKET_ID", "APPROVER_ID", "DEVELOPER_NAME",
                        "ROUND"],
    "revision_request": ["TICKET_ID", "DEVELOPER_ID", "DEVELOPER_NAME"],
    "dev_triage_summary": ["TICKET_ID", "APPROVER_ID"],
    "approval": ["TICKET_ID", "PIPELINE_MSG"],
    "merged": ["TICKET_ID"],
    "no_new_commits": ["TICKET_ID"],
    "rebase_conflict": ["TICKET_ID", "DEVELOPER_ID", "DEVELOPER_NAME"],
    "ack_new_topic": ["TICKET_ID", "TOPIC_ID"],
    "ack_dev_question": ["TICKET_ID"],
    "topic_reopened": ["TICKET_ID"],
}


def _repo_label(repo):
    """Map repo slug to display label."""
    if repo == "rage":
        return "Game"
    if repo == "chaos":
        return "Chaos"
    if repo.startswith("3rd_party/"):
        return repo.split("/", 1)[1]
    return repo


def _repo_tag(repo):
    """Map repo slug to bracketed file-prefix tag."""
    return f"[{_repo_label(repo)}] "


def _issue_location_text(repo, file, line_range, function, text):
    """Compose the location-prefixed issue text shared by round-1 review
    issues AND the developer-facing revision list:

        [Repo] file[:line_range] [function: ]text

    `repo` + `file` are required by the callers (enforced in
    build_issue_paragraphs / the revision builder); `line_range` and
    `function` are optional and omitted gracefully — a whole-file finding
    renders as ``[Repo] file: text`` (no colon-range, no function).

    NOTE: the round-N 问题复查 section does NOT use this — it keeps its
    verdict-marker shape (`build_issue_status_paragraphs`). See DESIGN §1.9.4.
    """
    loc = f"{_repo_tag(repo)}{file}"
    if line_range:
        loc += f":{line_range}"
    if function:
        return f"{loc} {function}: {text}"
    return f"{loc}: {text}"


def _link_text(repo, iid):
    """Format MR link text: chaos!2153, renderdoc!3, etc."""
    if repo.startswith("3rd_party/"):
        return f"{repo.split('/', 1)[1]}!{iid}"
    return f"{repo}!{iid}"


def build_mr_link_paragraphs(mrs):
    """Build MR link paragraphs from structured data.

    Input: list of {"repo": str, "iid": int, "branch": str, "url": str}
    Output: list of paragraph arrays (one per MR), each on its own line.
    """
    if not isinstance(mrs, list) or not mrs:
        raise ValueError("MRS must be a non-empty list")
    out = []
    for mr in mrs:
        for key in ("repo", "iid", "branch", "url"):
            if key not in mr or mr[key] in ("", None):
                raise ValueError(f"MRS entry missing required field: {key}")
        out.append([
            {"tag": "text", "text": f"{_repo_label(mr['repo'])} 仓库 ",
             "style": ["bold"]},
            {"tag": "a", "text": _link_text(mr["repo"], mr["iid"]),
             "href": mr["url"]},
            {"tag": "text", "text": f" {mr['branch']}"},
        ])
    return out


def build_file_paragraphs(files):
    """Build per-file change paragraphs from structured data.

    Input: list of {"repo": str, "path": str, "insertions": int,
                    "deletions": int, "description": str}
    Output: list of paragraph arrays — each enforces:
        [Repo] **path** +X/-Y — description
    """
    if not isinstance(files, list) or not files:
        raise ValueError("FILES must be a non-empty list")
    out = []
    for entry in files:
        for key in ("repo", "path"):
            if key not in entry or entry[key] in ("", None):
                raise ValueError(f"FILES entry missing required field: {key}")
        insertions = entry.get("insertions", 0)
        deletions = entry.get("deletions", 0)
        description = entry.get("description", "")
        paragraph = [
            {"tag": "text", "text": _repo_tag(entry["repo"])},
            {"tag": "text", "text": entry["path"], "style": ["bold"]},
            {"tag": "text", "text": f" +{insertions}/-{deletions}"},
        ]
        if description:
            paragraph.append({"tag": "text", "text": f" — {description}"})
        out.append(paragraph)
    return out


def build_file_section_paragraphs(files):
    """Build the entire 变更文件 section (header + per-file paragraphs + spacer).

    Wraps `build_file_paragraphs` so the whole section can be spliced into a
    single template slot. Full-review thread replies omit FILES entirely
    (the Lark doc holds the file list); the empty-default in
    `_OPTIONAL_ARRAY_DEFAULTS` causes the section to vanish without leaving
    an orphan header.
    """
    body = build_file_paragraphs(files)
    return [
        [{"tag": "text", "text": "变更文件", "style": ["bold"]}],
        *body,
        [{"tag": "text", "text": "\n"}],
    ]


def build_repo_commit_paragraphs(repo_commits):
    """Build per-repo commit paragraphs for round-N incremental reviews.

    Input: list of {"repo": str, "sha_short": str}
    Output: list of paragraph arrays — each formatted as:
        **<Repo> 仓库** / 提交: <sha_short>

    Needed because topics with both rage + chaos MRs revised between rounds
    must show each repo's new SHA on its own line; the old single-line
    REPO_LABEL/COMMIT_SHA_SHORT placeholders collapsed both repos into one
    ambiguous string.
    """
    if not isinstance(repo_commits, list) or not repo_commits:
        raise ValueError("REPO_COMMITS must be a non-empty list")
    out = []
    for entry in repo_commits:
        for key in ("repo", "sha_short"):
            if key not in entry or entry[key] in ("", None):
                raise ValueError(f"REPO_COMMITS entry missing required field: {key}")
        out.append([
            {"tag": "text", "text": f"{_repo_label(entry['repo'])} 仓库",
             "style": ["bold"]},
            {"tag": "text", "text": f" / 提交: {entry['sha_short']}"},
        ])
    return out


def build_issue_paragraphs(issues):
    """Build issue paragraphs, sorted by severity, with auto-assigned #N.

    Input: list of {"severity", "repo", "file", "text",
                    "line_range"?, "function"?}
        - severity, repo, file, text are REQUIRED (non-empty).
        - line_range, function are OPTIONAL (omitted gracefully — a
          whole-file finding renders as ``[Repo] file: text``).
        - `text` is prose ONLY; the location prefix
          (``[Repo] file:line_range function:``) is composed here, so
          callers must NOT pre-bake it into `text`.
    Output: list of paragraph arrays — each enforces:
        **#N  [severity]** [Repo] file[:line_range] [function: ]text
    Used for BOTH ISSUES (round-1 review) and FLAGGED_ISSUES. The round-N
    问题复查 section uses build_issue_status_paragraphs instead (verdict-
    marker shape, deliberately NOT location-prefixed). See DESIGN §1.9.4.
    Raises ValueError on non-canonical severity or missing repo/file/text.
    """
    if not isinstance(issues, list):
        raise ValueError("ISSUES must be a list")
    for entry in issues:
        sev = entry.get("severity", "")
        if sev not in VALID_SEVERITIES:
            raise ValueError(
                f"Invalid severity '{sev}'. "
                f"Must be one of {VALID_SEVERITIES}."
            )
        for key in ("repo", "file", "text"):
            if not entry.get(key):
                raise ValueError(
                    f"Each issue must have non-empty '{key}'")
    ordered = sorted(issues, key=lambda it: _SEVERITY_ORDER[it["severity"]])
    out = []
    for idx, entry in enumerate(ordered, start=1):
        loc_text = _issue_location_text(
            entry["repo"], entry["file"],
            entry.get("line_range"), entry.get("function"),
            entry["text"])
        out.append([
            {"tag": "text",
             "text": f"#{idx}  [{entry['severity']}] ",
             "style": ["bold"]},
            {"tag": "text", "text": loc_text},
        ])
    return out


def _doc_issue_location(repo, file, line_range, function):
    """Compose the markdown heading location string for a full-review doc
    issue: ``[Repo] file[:line_range] [（function）]``. Mirrors
    `_issue_location_text`'s line-range/function omission so the doc heading
    and the thread-reply prefix point at the same place. The function name is
    parenthesised （…）rather than appended with a colon because it sits in a
    markdown heading, not inline prose.
    """
    loc = f"{_repo_tag(repo)}{file}"
    if line_range:
        loc += f":{line_range}"
    if function:
        loc += f" （{function}）"
    return loc


def build_doc_issue_markdown(issues):
    """Render the 问题详情 section of a full-review Lark doc as markdown.

    Input: `review.issues[]`-shaped entries
        {"severity", "repo", "file", "description" (or "text"),
         "line_range"?, "function"?}
        - severity, repo, file, description are REQUIRED (non-empty).
        - line_range / function are OPTIONAL — included only for line-scoped /
          function-scoped findings; whole-file findings render as `[Repo] file`.
    Output: a markdown string. Issues are sorted by severity and numbered
    `#N` using the SAME key + start-1 enumeration as `build_issue_paragraphs`,
    so the doc's `#N` matches the thread reply's inline list verbatim (both
    derive from the same `review.issues[]` array). Each issue renders as:

        #### #N [严重] [Chaos] foo.cpp:120-145 （bar）

        <full Chinese description>

    Raises ValueError on non-canonical severity or missing repo/file/description.
    """
    if not isinstance(issues, list):
        raise ValueError("ISSUES must be a list")
    for entry in issues:
        sev = entry.get("severity", "")
        if sev not in VALID_SEVERITIES:
            raise ValueError(
                f"Invalid severity '{sev}'. "
                f"Must be one of {VALID_SEVERITIES}."
            )
        for key in ("repo", "file"):
            if not entry.get(key):
                raise ValueError(
                    f"Each issue must have non-empty '{key}'")
        if not (entry.get("description") or entry.get("text")):
            raise ValueError(
                "Each issue must have non-empty 'description'")
    ordered = sorted(issues, key=lambda it: _SEVERITY_ORDER[it["severity"]])
    blocks = []
    for idx, entry in enumerate(ordered, start=1):
        loc = _doc_issue_location(
            entry["repo"], entry["file"],
            entry.get("line_range"), entry.get("function"))
        description = (entry.get("description") or entry.get("text") or "").strip()
        blocks.append(
            f"#### #{idx} [{entry['severity']}] {loc}\n\n{description}")
    return "\n\n".join(blocks)


def build_indexed_issue_paragraphs(issues):
    """Issue lines that PRESERVE the original round-1 `#index` — no re-sort,
    no renumber — so humans correlate by the number they saw in round 1.

    Input: `review.issues[]`-shaped entries:
        {"index", "severity", "repo", "file",
         "description" (or "text"), "line_range"?, "function"?}
    Output: same single-line shape as build_issue_paragraphs:
        **#N  [severity]** [Repo] file[:line_range] [function: ]text

    Used by the mechanical handler for FLAGGED_ISSUE_PARAGRAPHS
    (revision_request) and the dev-triage summary sections. See DESIGN §1.9.4.
    """
    out = []
    for issue in issues:
        text = (issue.get("description") or issue.get("text") or "").strip()
        loc_text = _issue_location_text(
            issue.get("repo", ""), issue.get("file", ""),
            issue.get("line_range"), issue.get("function"), text)
        out.append([
            {"tag": "text",
             "text": (f"#{issue.get('index', '?')}  "
                      f"[{issue.get('severity', '?')}] "),
             "style": ["bold"]},
            {"tag": "text", "text": loc_text},
        ])
    return out


def build_triage_section_paragraphs(header, issues):
    """One dev-triage summary section: bold `<header>（N 条）` line +
    preserved-#N issue lines + spacer. Empty input yields [] so the
    section vanishes from the post (mirrors build_manual_issue_paragraphs).
    Used for AGREED/REJECTED sections of dev_triage_summary (DESIGN §1.23.2).
    """
    if not issues:
        return []
    return [
        [{"tag": "text", "text": f"{header}（{len(issues)} 条）",
          "style": ["bold"]}],
        *build_indexed_issue_paragraphs(issues),
        [{"tag": "text", "text": "\n"}],
    ]


def build_issue_status_paragraphs(verified_issues):
    """Build the 问题复查 section of a round-N incremental review.

    Input: list of {"index": int, "severity": str, "repo": str,
                    "file": str, "verdict": str,
                    "summary": str (optional — 1-line round-1
                                    description, surfaced so the
                                    approver doesn't have to scroll
                                    back to round 1),
                    "rationale": str}.
        - `verdict` must be a key of `_VERIFICATION_MARKERS`.
        - `index` is the round-1 issue index (preserved so the
          approver can correlate `#3` in round 2 with `#3` in round 1).
    Output: one paragraph per issue, sorted by `index` ascending, each:
        **#N**  **[severity]**  [Repo] <file> [<summary>] — <marker>（<rationale>）

    Mirrors `build_manual_issue_paragraphs` (which renders the human-
    comment section in the same post). Agents used to hand-build these
    paragraphs, so the shape drifted across spawns — this helper pins
    the format the operator picked from the RAGE-14892 round-2 reply.
    Raises ValueError on missing required fields, invalid severity, or
    invalid verdict.
    """
    if not isinstance(verified_issues, list):
        raise ValueError("VERIFIED_ISSUES must be a list")
    for entry in verified_issues:
        for key in ("index", "severity", "repo", "file", "verdict"):
            if entry.get(key) in ("", None):
                raise ValueError(
                    f"VERIFIED_ISSUES entry missing required field: {key}"
                )
        if entry["severity"] not in VALID_SEVERITIES:
            raise ValueError(
                f"Invalid severity '{entry['severity']}'. "
                f"Must be one of {VALID_SEVERITIES}."
            )
        if entry["verdict"] not in _VERIFICATION_MARKERS:
            raise ValueError(
                f"Invalid verdict '{entry['verdict']}'. "
                f"Must be one of {list(_VERIFICATION_MARKERS.keys())}."
            )

    ordered = sorted(verified_issues, key=lambda it: it["index"])
    out = []
    for entry in ordered:
        marker = _VERIFICATION_MARKERS[entry["verdict"]]
        summary = (entry.get("summary") or "").strip()
        rationale = (entry.get("rationale") or "").strip()
        parts = [
            {"tag": "text", "text": f"#{entry['index']}",
             "style": ["bold"]},
            {"tag": "text", "text": "  "},
            {"tag": "text", "text": f"[{entry['severity']}]",
             "style": ["bold"]},
            {"tag": "text",
             "text": f"  {_repo_tag(entry['repo'])}{entry['file']}"},
        ]
        if summary:
            parts.append({"tag": "text", "text": f" {summary}"})
        parts.append({"tag": "text", "text": f" — {marker}"})
        if rationale:
            parts.append({"tag": "text", "text": f"（{rationale}）"})
        out.append(parts)
    return out


def build_manual_issue_paragraphs(manual_issues):
    """Build the 人工审查 section from `review.manual_issues[]`.

    Input: list of dicts shaped per DESIGN §1.14.1 — each carries
        {index, repo, file, line_new, line_old, body, web_url,
         verification, verification_rationale}.
    Output: paragraph arrays — one section header, one or two
            paragraphs per manual issue:
        [<count> 条] header
        [M1] <verdict> [Repo] file.cpp:line — body 短句   ← clickable
            └ <Chinese rationale>                          ← only if verification != null
        ...

    `[M1]` is rendered as a Lark `<a href>` to the GitLab discussion
    so clicking opens the source thread directly. Empty input yields
    [] (the slot disappears entirely from the post).
    """
    if not manual_issues:
        return []
    if not isinstance(manual_issues, list):
        raise ValueError("MANUAL_ISSUES must be a list")

    out = []
    out.append([
        {"tag": "text",
         "text": f"人工审查（{len(manual_issues)} 条）",
         "style": ["bold"]},
    ])

    for entry in manual_issues:
        idx = entry.get("index") or 0
        verdict = entry.get("verification")
        marker = _VERIFICATION_MARKERS.get(verdict, _VERIFICATION_MARKERS[None])
        repo = entry.get("repo") or ""
        file = entry.get("file") or ""
        line = entry.get("line_new") or entry.get("line_old") or ""
        body = entry.get("body") or ""
        web_url = entry.get("web_url") or ""
        author = entry.get("author") or ""

        line_suffix = f":{line}" if line else ""
        location = f"{_repo_tag(repo)}{file}{line_suffix}"

        m_label = f"[M{idx}]"
        head = []
        if web_url:
            head.append({"tag": "a", "text": m_label, "href": web_url,
                         "style": ["bold"]})
        else:
            head.append({"tag": "text", "text": m_label, "style": ["bold"]})
        head.append({"tag": "text", "text": f" {marker} {location}"})
        if body:
            head.append({"tag": "text", "text": f" — {body}"})
        if author:
            head.append({"tag": "text", "text": f"（{author}）"})
        out.append(head)

        rationale = entry.get("verification_rationale") or ""
        if verdict and verdict != "pending" and rationale:
            out.append([
                {"tag": "text", "text": f"      └ {rationale}"},
            ])

    out.append([{"tag": "text", "text": "\n"}])
    return out


# Display markers for the five verification verdicts. `None` is the
# pre-verification state (round 1 display, before any dev push).
_VERIFICATION_MARKERS = {
    None:                   "📌 待验证",
    "pending":              "📌 待验证",
    "addressed":            "✅ 已修复",
    "not_addressed":        "⚠️ 未修复",
    "partially_addressed":  "🟡 部分修复",
    "obsolete":             "📝 代码已删除/重构",
    "unclear":              "❓ 无法判断",
}


def build_doc_link_paragraphs(doc_link):
    """Build the Lark doc link header for full/complex reviews.

    Input: dict {"title": str, "url": str}  (title defaults to "代码审查报告")
    Output: list of paragraph arrays — a bold intro line, the hyperlink, and
            a blank spacer — to be prepended to review_round1 content. Simple
            reviews omit DOC_LINK entirely, leaving DOC_LINK_PARAGRAPHS = [].
    """
    if not isinstance(doc_link, dict):
        raise ValueError("DOC_LINK must be a dict with 'title' and 'url'")
    url = doc_link.get("url") or ""
    if not url:
        raise ValueError("DOC_LINK missing 'url'")
    title = doc_link.get("title") or "代码审查报告"
    return [
        [{"tag": "text", "text": "完整审查报告（飞书文档）：",
          "style": ["bold"]}],
        [{"tag": "a", "text": title, "href": url}],
        [{"tag": "text", "text": "\n"}],
    ]


# Structured-input keys → (output placeholder key, builder function)
_STRUCTURED_BUILDERS = {
    "MRS": ("MR_LINKS_PARAGRAPHS", build_mr_link_paragraphs),
    "FILES": ("FILE_SECTION_PARAGRAPHS", build_file_section_paragraphs),
    "ISSUES": ("ISSUE_PARAGRAPHS", build_issue_paragraphs),
    "FLAGGED_ISSUES": ("FLAGGED_ISSUE_PARAGRAPHS", build_issue_paragraphs),
    "REPO_COMMITS": ("REPO_COMMIT_PARAGRAPHS", build_repo_commit_paragraphs),
    "DOC_LINK": ("DOC_LINK_PARAGRAPHS", build_doc_link_paragraphs),
    "MANUAL_ISSUES": ("MANUAL_ISSUE_PARAGRAPHS", build_manual_issue_paragraphs),
    "VERIFIED_ISSUES": ("ISSUE_STATUS_PARAGRAPHS", build_issue_status_paragraphs),
}

# Array placeholders that default to [] when the caller omits them.
# Used to keep optional slots (e.g. the Lark doc header for simple reviews)
# from tripping the unfilled-placeholder check.
_OPTIONAL_ARRAY_DEFAULTS = {
    # FILE_SECTION_PARAGRAPHS is omitted by full/complex reviews — the
    # Lark doc is the canonical file-list view, the thread reply only
    # carries DOC_LINK + the (terse) issue list.
    # MANUAL_ISSUE_PARAGRAPHS is omitted when there are no human review
    # threads on the MR (the common case at round-1 spawn time).
    "review_round1": ["DOC_LINK_PARAGRAPHS", "FILE_SECTION_PARAGRAPHS",
                      "MANUAL_ISSUE_PARAGRAPHS"],
    "review_round1_dev_triage": ["DOC_LINK_PARAGRAPHS",
                                 "FILE_SECTION_PARAGRAPHS",
                                 "MANUAL_ISSUE_PARAGRAPHS"],
    "review_roundN": ["MANUAL_ISSUE_PARAGRAPHS"],
    # REJECTED_SECTION_PARAGRAPHS vanishes when the dev disputed nothing;
    # ESCALATE_INSTRUCTION_PARAGRAPHS only appears for simple reviews.
    "handoff_summary": ["REJECTED_SECTION_PARAGRAPHS",
                        "ESCALATE_INSTRUCTION_PARAGRAPHS"],
    # PREFIX_PARAGRAPHS carries the arbitration reinstate note ("审查人恢复了
    # …"); MANUAL_ISSUE_PARAGRAPHS any newly discovered human MR comments
    # (DESIGN §1.23.3). Legacy revision callers omit both — defaults keep
    # their renders byte-identical.
    "revision_request": ["PREFIX_PARAGRAPHS", "MANUAL_ISSUE_PARAGRAPHS"],
    "dev_triage_summary": ["AGREED_SECTION_PARAGRAPHS",
                           "REJECTED_SECTION_PARAGRAPHS",
                           "ESCALATE_INSTRUCTION_PARAGRAPHS"],
    # DEVELOPER_MENTION_SEGMENTS: trailing @-dev segments (an `at` node, or
    # [] when the dev open_id is unknown). Mirrors the BOT_MENTION_SEGMENTS
    # idiom, and sidesteps validate_required's pinyin guard on DEVELOPER_NAME.
    "no_new_commits": ["DEVELOPER_MENTION_SEGMENTS"],
}

# Array placeholders whose default is a NON-empty paragraph block (unlike
# _OPTIONAL_ARRAY_DEFAULTS which default to []). Keyed by template, then by
# placeholder. The default preserves legacy behavior for callers that omit
# the slot; callers that want it gone pass an explicit [].
#
# ESCALATE_INSTRUCTION_PARAGRAPHS: the "回复 full/完整版 进行完整审查" reply
# instruction. `full`/`完整版` only escalates from TRIAGE_DECISION (round-N
# decision state) or ARBITRATION when `review.triage == "simple"` — see
# reply_parser.classify_intent. Elsewhere a `full` reply is dropped as
# `ignored`. So the line is correct (and the default) for simple-review
# posts, but a dead instruction after a full review has already run. The
# full-review path passes `ESCALATE_INSTRUCTION_PARAGRAPHS: []` to suppress
# it; dev_triage_summary always passes the slot explicitly (simple → the
# line, complex → []), so its default lives in _OPTIONAL_ARRAY_DEFAULTS.
_NONEMPTY_ARRAY_DEFAULTS = {
    "review_round1": {
        "ESCALATE_INSTRUCTION_PARAGRAPHS": [
            [{"tag": "text",
              "text": "· 回复 \"full\" 或 \"完整版\" 进行完整审查"}]
        ],
    },
}


# Pinyin fallback pattern: lowercase ASCII with a dot separator
# (e.g. "yu.cheng", "muhan.liu"). This is what glab/git return when the
# open_id → Chinese name resolution fails. Reject it in user-facing
# DEVELOPER_NAME slots so a failed contact-cache lookup doesn't silently
# leak into the revision_request post.
_PINYIN_FALLBACK_RE = re.compile(r'^[a-z]+(\.[a-z]+)+$')


def expand_structured_vars(variables):
    """Expand structured shortcuts (MRS/FILES/ISSUES/FLAGGED_ISSUES) into
    their corresponding *_PARAGRAPHS arrays. Returns a new dict.

    Rejects caller providing BOTH the structured key and the pre-built
    paragraphs for the same target — pick one.
    """
    expanded = dict(variables)
    for src_key, (target_key, builder) in _STRUCTURED_BUILDERS.items():
        if src_key in expanded:
            if target_key in expanded:
                raise ValueError(
                    f"Provide either '{src_key}' (structured) or "
                    f"'{target_key}' (pre-built), not both."
                )
            expanded[target_key] = builder(expanded.pop(src_key))
    return expanded


def validate_required(template_name, variables):
    """Raise if any required scalar var is missing, None, or empty string."""
    required = REQUIRED_VARS.get(template_name, [])
    missing = [
        key for key in required
        if variables.get(key) is None or variables.get(key) == ""
    ]
    if missing:
        raise ValueError(
            f"Template '{template_name}' missing required variables: {missing}"
        )
    dev_name = variables.get("DEVELOPER_NAME")
    if isinstance(dev_name, str) and _PINYIN_FALLBACK_RE.match(dev_name):
        raise ValueError(
            f"DEVELOPER_NAME looks like a pinyin fallback ({dev_name!r}). "
            f"Resolve the developer's Chinese name via lark-contact-cache "
            f"before rendering — a pinyin leak means the contact lookup "
            f"failed and the user-facing post would show "
            f"'{dev_name}' instead of a readable name."
        )


def load_template(name):
    """Load a template JSON file by name (without .json extension)."""
    path = TEMPLATE_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Template not found: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


_PLACEHOLDER_RE = re.compile(r'\{\{([A-Z_]+)\}\}')


def _resolve_bot_open_id():
    """REVIEW_BOT_OPEN_ID from env, else the settings files (local first).

    Same sources as `dispatcher._load_env`. Resolved per render call (not
    cached at import) so a long-lived daemon that auto-learns the id
    mid-life (`bot_identity.maybe_learn_bot_open_id` writes it to
    settings.local.json) picks it up without a restart. Returns "" when
    unconfigured.
    """
    open_id = os.environ.get("REVIEW_BOT_OPEN_ID", "").strip()
    if open_id:
        return open_id
    claude_dir = Path(__file__).resolve().parents[4]
    for fname in ("settings.local.json", "settings.json"):
        path = claude_dir / fname
        if not path.is_file():
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        open_id = (data.get("env") or {}).get("REVIEW_BOT_OPEN_ID", "").strip()
        if open_id:
            return open_id
    return ""


def _bot_mention_segments():
    """Builtin BOT_MENTION_SEGMENTS: how review copy tells the dev to
    address the bot. A real @-mention when the deployed bot's open_id is
    known — unambiguous and tab-completable, and `bot_identity.
    normalize_bot_mention` rewrites it back to the literal `@bot` token
    before `reply_parser` sees it. Falls back to the literal bold `@bot`
    text when REVIEW_BOT_OPEN_ID is unresolvable (fresh deployment before
    the auto-learn). See DESIGN §1.9.6.
    """
    open_id = _resolve_bot_open_id()
    if open_id:
        return [{"tag": "at", "user_id": open_id, "user_name": "bot"}]
    return [{"tag": "text", "text": "@bot", "style": ["bold"]}]


def render(template, variables):
    """Recursively fill placeholders in the template structure.

    Injects builtin vars (BOT_MENTION_SEGMENTS) unless the caller passed
    its own value. Raises ValueError if any {{KEY}} remains unfilled.
    """
    merged = dict(variables)
    merged.setdefault("BOT_MENTION_SEGMENTS", _bot_mention_segments())
    return _render_node(template, merged)


def _render_node(template, variables):
    if isinstance(template, str):
        # Full-array placeholder "{{ARRAY_KEY}}" — return the list directly
        stripped = template.strip()
        if stripped.startswith("{{") and stripped.endswith("}}"):
            key = stripped[2:-2]
            if key in variables:
                val = variables[key]
                if isinstance(val, list):
                    return val
                return str(val)
        # Inline scalar substitution. Coerce non-str non-list scalars
        # (e.g. int ROUND=2) so callers don't have to remember to stringify.
        result = template
        for key, val in variables.items():
            if isinstance(val, list):
                continue
            result = result.replace("{{" + key + "}}", str(val))
        # Fail loudly on any remaining unfilled placeholder
        remaining = _PLACEHOLDER_RE.findall(result)
        if remaining:
            raise ValueError(
                f"Unfilled placeholders {sorted(set(remaining))} "
                f"in template string: {template!r}"
            )
        return result
    if isinstance(template, dict):
        return {
            key: _render_node(val, variables)
            for key, val in template.items()
            if not key.startswith("_")
        }
    if isinstance(template, list):
        result = []
        for item in template:
            rendered = _render_node(item, variables)
            # Splice expanded array placeholders into the parent list
            if (isinstance(item, str)
                    and item.strip().startswith("{{")
                    and isinstance(rendered, list)):
                result.extend(rendered)
            else:
                result.append(rendered)
        return result
    return template


def validate_post_structure(output):
    """Assert the rendered Lark post content is structurally well-formed.

    A Lark post body is ``{"<lang>": {"content": [[seg, ...], ...]}}`` — content
    is a list of paragraphs, each paragraph a list of segment dicts, each segment
    carrying a ``tag``. ``render()`` splices ``*_PARAGRAPHS`` var values into the
    content list verbatim, so a caller who hand-builds ``ISSUE_PARAGRAPHS`` /
    ``FILE_PARAGRAPHS`` as a bare string or a list-of-strings produces malformed
    content that passes the unfilled-placeholder check but Lark rejects with
    ``code 230001 "content format of the post type is incorrect"`` — a silent
    poison-loop until the artifact quarantines. This validator turns that into a
    loud failure at ``--check-only`` time. See DESIGN §1.9.3.

    Raises ValueError on the first malformed element; no-op for non-post output.
    """
    if not isinstance(output, dict):
        return
    for lang_block in output.values():
        if not isinstance(lang_block, dict):
            continue
        content = lang_block.get("content")
        if content is None:
            continue
        if not isinstance(content, list):
            raise ValueError(
                f"post content must be a list of paragraphs, got "
                f"{type(content).__name__}")
        for i, para in enumerate(content):
            if not isinstance(para, list):
                raise ValueError(
                    f"post content[{i}] must be a paragraph (list of segments), "
                    f"got {type(para).__name__}: {str(para)[:120]}. Likely a "
                    f"*_PARAGRAPHS var passed as a string/scalar — use the "
                    f"structured ISSUES/FILES/MRS shortcut instead of "
                    f"hand-building paragraph arrays.")
            for j, seg in enumerate(para):
                if not isinstance(seg, dict) or "tag" not in seg:
                    raise ValueError(
                        f"post content[{i}][{j}] must be a segment dict with a "
                        f"'tag' key, got {type(seg).__name__}: {str(seg)[:120]}. "
                        f"Likely a *_PARAGRAPHS var passed as a list of strings — "
                        f"use the structured ISSUES/FILES shortcut.")


def post_to_lark(rendered_json, message_id):
    """Render to temp file and post via lark-cli. Returns (ok, response_text).

    `ok` is True only when the lark-cli subprocess succeeded AND the Lark
    API response carries ``{"ok": true}``. Without the JSON check, Lark
    can return ``{"ok": false, "code": ..., "msg": "..."}`` with subprocess
    exit code 0 — the caller would then think the post landed when in
    fact Lark silently rejected it. See DESIGN §1.22.5.
    """
    tmp_dir = os.environ.get(
        "WRITE_TMP_DIR",
        str(Path(__file__).resolve().parent.parent.parent / "cfg"))
    post_file = os.path.join(tmp_dir, "post.json")
    with open(post_file, "w", encoding="utf-8") as f:
        json.dump(rendered_json, f, ensure_ascii=False)

    cmd = subprocess_util.lark_cli_argv_prefix() + [
        "im", "+messages-reply",
        "--message-id", message_id,
        "--reply-in-thread",
        "--as", "bot",
        "--msg-type", "post",
        "--content", json.dumps(rendered_json, ensure_ascii=False),
    ]
    result = subprocess_util.hidden_run(
        cmd, capture_output=True, text=True, timeout=30, encoding="utf-8")
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()

    if result.returncode != 0:
        return False, stdout or stderr or f"exit_code={result.returncode}"

    # Subprocess succeeded; parse the Lark JSON envelope. lark-cli wraps
    # responses as ``{"ok": true|false, "identity": ..., "data": {...}}``
    # or ``{"ok": false, "error": "..."}``. Either way ``ok`` is the
    # authoritative field — exit code 0 alone is not.
    try:
        envelope = json.loads(stdout) if stdout else {}
    except ValueError:
        # Non-JSON stdout — return the raw text so the caller can decide
        # (rare; happens when lark-cli prints a usage line or stack trace
        # but still exits 0).
        return False, stdout or stderr or "non_json_response"
    if not isinstance(envelope, dict) or envelope.get("ok") is not True:
        # Lark rejected the post. Surface the full envelope so the caller
        # can quarantine the artifact with a useful audit string.
        return False, stdout

    return True, stdout


def main():
    parser = argparse.ArgumentParser(description="Render a Lark post template")
    parser.add_argument("template", help="Template name (e.g. review_round1)")
    parser.add_argument("--vars", help="JSON string of variables")
    parser.add_argument("--vars-file", help="Path to JSON file with variables")
    parser.add_argument("--post", action="store_true",
                        help="Render AND post to Lark in one shot")
    parser.add_argument("--message-id",
                        help="Thread root message ID (required with --post)")
    parser.add_argument("--check-only", action="store_true",
                        help="Validate required vars + structural placeholders "
                             "without rendering output. Exits 0 on success, "
                             "1 with the ValueError message on stderr otherwise. "
                             "Used by the topic-agent contract for pre-write "
                             "artifact validation (catches missing SUMMARY etc. "
                             "BEFORE the artifact lands in cfg/replies/).")
    args = parser.parse_args()

    template = load_template(args.template)

    if args.vars_file:
        with open(args.vars_file, encoding="utf-8") as f:
            variables = json.load(f)
    elif args.vars:
        variables = json.loads(args.vars)
    else:
        variables = {}

    # Expand structured shortcuts → *_PARAGRAPHS arrays
    variables = expand_structured_vars(variables)
    # Fill in [] defaults for optional array slots the caller omitted,
    # so the unfilled-placeholder check doesn't reject valid simple posts.
    for optional_key in _OPTIONAL_ARRAY_DEFAULTS.get(args.template, []):
        variables.setdefault(optional_key, [])
    # Fill in non-empty defaults (e.g. the escalate reply instruction) for
    # callers that omit the slot, preserving legacy behavior.
    for opt_key, default_val in _NONEMPTY_ARRAY_DEFAULTS.get(
            args.template, {}).items():
        variables.setdefault(opt_key, default_val)
    # Validate required scalar vars before rendering
    validate_required(args.template, variables)

    if args.check_only:
        # Render too — catches unfilled-placeholder errors that
        # validate_required can't see (e.g. missing array slots, typo'd keys
        # in nested paragraphs). Discard the output, but validate its
        # structure first so a hand-built *_PARAGRAPHS string/list-of-strings
        # (which renders fine but Lark rejects with 230001) fails HERE rather
        # than poison-looping in reply_dispatcher. See DESIGN §1.9.3.
        validate_post_structure(render(template, variables))
        return 0

    output = render(template, variables)
    # Loud structural validation on every real render/post too — defence in
    # depth for callers that skip --check-only.
    validate_post_structure(output)

    if args.post:
        if not args.message_id:
            print(json.dumps({"error": "--post requires --message-id"}),
                  file=sys.stderr)
            return 1
        ok, response = post_to_lark(output, args.message_id)
        print(response)
        # Exit nonzero on Lark rejection so reply_dispatcher's
        # subprocess returncode check picks up the failure. Exit code 0
        # alone is no longer enough — see post_to_lark docstring.
        return 0 if ok else 1

    # Default: just output the rendered JSON
    # Force UTF-8 on Windows to avoid GBK encoding errors
    import io
    stdout_utf8 = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    json.dump(output, stdout_utf8, ensure_ascii=False)
    stdout_utf8.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
