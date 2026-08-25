# -*- coding: utf-8 -*-
"""Mechanical acknowledgement for brand-new topics (no Claude agent).

When a root message carrying RAGE-XXXXX lands in the group, the router
writes a `new_topic` event into the topic's pending queue. Previously the
topic agent owned the full cycle: MR discovery → diff fetch → triage →
review → Lark post. The user saw silence for 2–15 minutes.

This handler runs inside the dispatcher cycle (same slot as
`mechanical_reply_handler.drain_mechanical`) and races ahead of the
agent to post a lightweight ack containing the ticket id, per-repo MR
link + branch, and diff-stat summary. The full review still follows,
posted by the agent once triage finishes. Goal: ~5s feedback instead of
minutes.

Scope:
    pending has `new_topic` AND lifecycle.ack_sent is falsy
      → discover MRs, fetch diff-stats, post ack template, mark sent
      → the `new_topic` event stays in pending (agent still needs it)
    No MRs found                → post ⚠️ close template, CLOSED
    All MRs merged/closed       → post status, CLOSED

Deferred to the Claude agent (unchanged):
    review post, triage decisions, 3rd-party vs main phase selection,
    version_3rd.cmake dependency-pair check.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import event_utils
import gitlab_threads
import mechanical_reply_handler
import merge_tracker
import post_approval
import subprocess_util
import topic_store


SKILL_DIR = SCRIPT_DIR.parent

# Fast-track trigger: root sender ∈ approver namelist AND root content
# (after stripping ticket id + any @dev mention + whitespace) is exactly the
# lowercase ASCII `ok`. Lets the approver fast-track a trivial MR straight to
# APPROVED without going through the review flow. `ok` is the only accepted
# token (the `pass` alias was removed). See SKILL.md / DESIGN §1.21.
_FAST_TRACK_TOKENS = frozenset({"ok"})
RENDER_PY = SCRIPT_DIR / "templates" / "render.py"
_ACTIVITY_LOG = SKILL_DIR / "cfg" / "activity.log"

# Ticket pattern lives in event_utils.TICKET_RE (shared with router.py's
# root-message matcher) — do not re-declare it here, it drifts.

# Triage thresholds — EITHER exceeded WITHIN a single repo flips a topic
# to `complex`. Schema/codegen files always force `complex`.
_TRIAGE_COMPLEX_FILES = 5
_TRIAGE_COMPLEX_LINES = 100
_SCHEMA_EXT_RE = re.compile(r'\.(rsd|nsd|gsd|csd)$', re.IGNORECASE)

# 3rd-party dependency sentinel: chaos-side bump of this file must be
# paired with a 3rd-party library MR (and vice versa).
_VERSION_3RD_FILE = "version_3rd.cmake"


# ---- Activity log ----------------------------------------------------

def _log(level, msg):
    try:
        with open(_ACTIVITY_LOG, "a", encoding="utf-8") as f:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{ts}] [{level}] ack_new_topic: {msg}\n")
    except OSError:
        pass


# ---- Helpers ---------------------------------------------------------

def _already_posted(topic, event_id):
    """Mirror mechanical_reply_handler._already_posted — de-dup on event_id."""
    for entry in reversed(topic.get("audit") or []):
        if entry.get("triggered_by_event_id") == event_id:
            if entry.get("event") in ("lark_reply_sent",
                                      "lark_reply_duplicate_suppressed"):
                return True
    return False


def _render_and_post(template_name, variables, message_id):
    """Invoke templates/render.py with --post. Returns (ok, response_text)."""
    vars_blob = json.dumps(variables, ensure_ascii=False)
    cmd = ["python", str(RENDER_PY), template_name,
           "--vars", vars_blob, "--post", "--message-id", message_id]
    try:
        proc = subprocess_util.hidden_run(
            cmd, capture_output=True, text=True,
            timeout=30, encoding="utf-8", errors="replace")
    except (OSError, Exception) as exc:  # noqa: BLE001
        return False, str(exc)[:200]
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()[:300]
    return True, (proc.stdout or "").strip()


def _post_plain_text(text, message_id):
    """Post a plain text reply via lark-cli."""
    cmd = subprocess_util.lark_cli_argv_prefix() + [
           "im", "+messages-reply",
           "--message-id", message_id, "--reply-in-thread",
           "--as", "bot", "--msg-type", "text", "--text", text]
    try:
        proc = subprocess_util.hidden_run(
            cmd, capture_output=True, text=True,
            timeout=30, encoding="utf-8", errors="replace")
    except (OSError, Exception) as exc:  # noqa: BLE001
        return False, str(exc)[:200]
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()[:300]
    return True, (proc.stdout or "").strip()


def _post_close_notice(text, message_id, mention_id=None, mention_name=None):
    """Post an ack-time close notice, @-mentioning the requester at the end.

    When a developer open_id is known, render a Lark post (pure post format,
    never a card — DESIGN §1.9.1) whose single paragraph ends with an `at`
    segment so the person who filed the review is pinged. When no open_id is
    available (e.g. the assigned dev couldn't be resolved to one), fall back
    to plain text so the close never silently fails.
    """
    if not mention_id:
        return _post_plain_text(text, message_id)
    post = {"zh_cn": {"title": "", "content": [[
        {"tag": "text", "text": text + " "},
        {"tag": "at", "user_id": mention_id,
         "user_name": mention_name or "开发者"},
    ]]}}
    cmd = subprocess_util.lark_cli_argv_prefix() + [
           "im", "+messages-reply",
           "--message-id", message_id, "--reply-in-thread",
           "--as", "bot", "--msg-type", "post",
           "--content", json.dumps(post, ensure_ascii=False)]
    try:
        proc = subprocess_util.hidden_run(
            cmd, capture_output=True, text=True,
            timeout=30, encoding="utf-8", errors="replace")
    except (OSError, Exception) as exc:  # noqa: BLE001
        return False, str(exc)[:200]
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()[:300]
    return True, (proc.stdout or "").strip()


# ---- MR discovery ----------------------------------------------------

def _glab_json(args, timeout_s=30):
    """Run glab, return parsed JSON or None."""
    stdout, stderr, rc = merge_tracker._run_glab(args, timeout_s=timeout_s)
    if rc != 0:
        return None, (stderr or stdout or "").strip()[:200]
    try:
        return json.loads(stdout), None
    except (ValueError, json.JSONDecodeError) as e:
        return None, f"bad json: {e}"


def _title_matches_ticket(entry, ticket_id):
    """True iff the MR title carries the ticket as a token.

    GitLab `mr list --search` / group `?search=` match the ticket against the
    MR *title AND description*, so an unrelated MR that merely mentions the
    ticket in its body — or a different ticket's MR that cross-references this
    one — is also returned. Selecting the "first open" result then attaches
    the wrong MR to the topic (observed: RAGE-19360 picking up a RAGE-21088
    branch whose description named RAGE-19360). The MR title is the
    authoritative binding — this repo enforces the `RAGE-XXXXX:` title
    convention. A leading verb like "Fix " is allowed (e.g. "Fix RAGE-19360:
    ..."), so match the token anywhere in the title, not just as a prefix; the
    trailing `(?![0-9])` guard keeps `RAGE-19360` from matching `RAGE-193600`.
    See DESIGN §1.3.5.
    """
    title = entry.get("title") or ""
    return re.search(r"(?<![A-Za-z0-9])" + re.escape(ticket_id) + r"(?![0-9])",
                     title) is not None


def _project_slug_from_entry(entry):
    """Full `group/project` path of the project an MR entry belongs to.

    The *project*-level MR API returns `project_path_with_namespace`, but the
    **group**-level endpoint (`groups/<g>/merge_requests`) does NOT — it only
    carries `project_id`, `references.full` and `web_url`. Reading the missing
    field yielded `""`, which silently dropped every open 3rd-party MR: the
    topic then looked like it had no lib MR at all, so a chaos
    `version_3rd.cmake` bump false-closed with `cmake_without_3p` and the
    3rd-party review phase could never trigger (observed: RAGE-23816 closed
    while `3rd_party_cpplibs/scaleform_4_5_0!14` was open). Fall back to
    `references.full` (`"group/project!14"`) and then to the `web_url` path.
    """
    slug = entry.get("project_path_with_namespace") or ""
    if slug:
        return slug
    full_ref = ((entry.get("references") or {}).get("full")) or ""
    if "!" in full_ref:
        candidate = full_ref.split("!", 1)[0].strip()
        if "/" in candidate:
            return candidate
    web_url = entry.get("web_url") or ""
    marker = "/-/merge_requests/"
    if marker in web_url:
        path = web_url.split(marker, 1)[0]
        # https://host/group/project → group/project
        parts = path.split("//", 1)[-1].split("/", 1)
        if len(parts) == 2 and "/" in parts[1]:
            return parts[1]
    return ""


def _discover_mrs(ticket_id):
    """Search rage/chaos/3rd_party_cpplibs for MRs whose *title* names ticket.

    Returns (mrs_dict, errors_list, has_merged_3p, threep_probe_errored).
    mrs_dict keys: "rage", "chaos", "3rd_party/<project>". Each value includes
    mr_iid, branch, target_branch, branch_sha, web_url, state,
    pipeline_status=null. 3rd-party entries additionally carry repo_slug for
    merge_tracker. `threep_probe_errored` is True when the 3rd-party
    `state=opened` OR `state=merged` probe failed — the caller must NOT act on a
    `cmake_without_3p` verdict then (has_3p / has_merged_3p may be falsely False
    → the exact RAGE-20767 false-close; §1.3.6).

    Candidates come from GitLab's full-text `--search` / `?search=` (which
    matches title AND description), then are filtered to title matches via
    `_title_matches_ticket` so a body-only mention of the ticket can't attach
    the wrong MR. See DESIGN §1.3.5.
    """
    mrs = {}
    errors = []

    for repo, slug in (("rage", merge_tracker.REPO_SLUGS["rage"]),
                       ("chaos", merge_tracker.REPO_SLUGS["chaos"])):
        data, err = _glab_json(
            ["glab", "mr", "list", "--search", ticket_id, "-R", slug,
             "-F", "json"])
        if err:
            errors.append(f"{repo}: {err}")
            continue
        if not data:
            continue
        # Bind by title, not the full-text --search (which also matches the MR
        # *description*) — a body-only mention of the ticket, or a sibling
        # ticket's MR that cross-references it, must not attach here.
        data = [e for e in data if _title_matches_ticket(e, ticket_id)]
        if not data:
            continue
        # Prefer the first open MR; fall back to any MR if none are open
        # (caller uses state to decide close-path).
        pick = None
        for entry in data:
            if entry.get("state") == "opened":
                pick = entry
                break
        if pick is None and data:
            pick = data[0]
        if pick:
            mrs[repo] = {
                "mr_iid":          pick.get("iid"),
                "branch":          pick.get("source_branch"),
                "target_branch":   pick.get("target_branch"),
                "branch_sha":      pick.get("sha"),
                "web_url":         pick.get("web_url"),
                "state":           pick.get("state"),
                "pipeline_status": None,
            }

    # 3rd-party group search (opened) — these enter `mrs` for the
    # review+merge 3rd-party phase. A failed opened/merged probe makes
    # has_3p / has_merged_3p unreliable, so track it (§1.3.6).
    threep_probe_errored = False
    data, err = _glab_json(
        ["glab", "api",
         f"groups/3rd_party_cpplibs/merge_requests?search={ticket_id}&state=opened"])
    if err:
        errors.append(f"3rd_party: {err}")
        threep_probe_errored = True
    elif data:
        for entry in data:
            # Title-bind (see _title_matches_ticket) — a lib MR that only
            # mentions the ticket in its body must not enter the 3rd-party phase.
            if not _title_matches_ticket(entry, ticket_id):
                continue
            # → "3rd_party_cpplibs/renderdoc" (group API omits
            # project_path_with_namespace — see _project_slug_from_entry)
            slug = _project_slug_from_entry(entry)
            name = slug.split("/", 1)[1] if "/" in slug else slug
            if not name:
                continue
            mrs[f"3rd_party/{name}"] = {
                "mr_iid":          entry.get("iid"),
                "repo_slug":       slug,
                "branch":          entry.get("source_branch"),
                "target_branch":   entry.get("target_branch"),
                "branch_sha":      entry.get("sha"),
                "web_url":         entry.get("web_url"),
                "state":           entry.get("state"),
                "pipeline_status": None,
            }

    # Separate probe for *merged* 3rd-party MRs. A lib MR that landed
    # ahead of its consumer chaos/rage MR is a legitimate workflow: its
    # existence still satisfies the `version_3rd.cmake` bidirectional
    # check (so we must not false-close the consumer topic), but it is
    # NOT added to `mrs` — it is already merged, so there is nothing to
    # review or merge, and adding it would wrongly route the topic into
    # the 3rd-party review phase. Kept as its own query so an error on
    # one state does not mask the other.
    has_merged_3p = False
    merged_data, merged_err = _glab_json(
        ["glab", "api",
         f"groups/3rd_party_cpplibs/merge_requests?search={ticket_id}&state=merged"])
    if merged_err:
        errors.append(f"3rd_party_merged: {merged_err}")
        threep_probe_errored = True
    elif merged_data:
        # Title-bind (see _title_matches_ticket) — a body-only mention must not
        # relax the version_3rd cmake→3p check (§1.3.4).
        has_merged_3p = any(
            _title_matches_ticket(e, ticket_id) for e in merged_data)

    return mrs, errors, has_merged_3p, threep_probe_errored


# ---- Diff-stat fetch -------------------------------------------------

def _master_branch(repo, rage_root):
    """Repo's master branch from rage_root/.claude/cfg/branches.json (committed),
    via the shared env_resolver helper resolved against the repo under review
    (not the bot's deployed location). Falls back to rage=master /
    chaos=rage/master if the config or helper is unavailable.

    env_resolver lives under the *reviewed* repo's .claude/scripts, which differs
    from the bot's own scripts dir, so it is loaded by explicit path rather than
    via sys.path. spec_from_file_location (without sys.modules caching) rebinds to
    the given rage_root every call — a stale sys.path entry would otherwise pin the
    first rage_root for the life of the process."""
    try:
        import importlib.util
        entry = os.path.join(rage_root, ".claude", "scripts", "env_resolver.py")
        spec = importlib.util.spec_from_file_location("env_resolver", entry)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.master_branch_for(rage_root, repo)
    except Exception:
        return "rage/master" if repo == "chaos" else "master"


def _repo_root(repo, rage_root, chaos_root):
    if repo == "rage":
        return rage_root
    if repo == "chaos":
        return chaos_root
    return None  # 3rd-party — caller must skip


def _run_git(args, cwd, timeout_s=60):
    try:
        proc = subprocess_util.hidden_run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True,
            timeout=timeout_s, encoding="utf-8", errors="replace")
        return proc.stdout, proc.stderr, proc.returncode
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return "", str(e), -1


def _fetch_stats(repo, mr_obj, rage_root, chaos_root):
    """Compute per-file + aggregate stats for one MR via `git diff --numstat`.

    Returns dict `{file_count, insertions, deletions, files[]}` or None
    on failure. `files[]` is a list of `{path, insertions, deletions}`
    entries used downstream for triage, version_3rd check, and the
    agent's FILE_PARAGRAPHS. 3rd-party repos are skipped — we don't
    have them checked out locally. The ack post still lists the
    3rd-party MR link, just without stats.
    """
    root = _repo_root(repo, rage_root, chaos_root)
    if root is None or not os.path.isdir(root):
        return None
    # Prefer the MR's actual target_branch (from GitLab); fall back to the
    # repo's master branch only when GitLab didn't return it (legacy or offline
    # replay). _master_branch reads rage_root/.claude/cfg/branches.json, keeping
    # diffs correct for MRs that target a non-master integration branch
    # (e.g. rage/rage_pv).
    base = mr_obj.get("target_branch") or _master_branch(repo, rage_root)
    sha = mr_obj.get("branch_sha")
    if not sha:
        return None

    # Fetch base then the MR SHA. Best-effort; we fall back to cached
    # refs if the network is unreachable.
    _run_git(["fetch", "origin", base], cwd=root, timeout_s=90)
    _run_git(["fetch", "origin", sha], cwd=root, timeout_s=90)

    stdout, stderr, rc = _run_git(
        ["diff", "--numstat", f"origin/{base}...{sha}"],
        cwd=root, timeout_s=60)
    if rc != 0:
        return None

    files = []
    total_ins = 0
    total_del = 0
    for line in (stdout or "").splitlines():
        parts = line.rstrip().split("\t")
        if len(parts) < 3:
            continue
        ins_s, del_s, path = parts[0], parts[1], parts[2]
        # numstat renders binary files as "-\t-\t<path>". Keep the
        # filename (downstream file-list checks care about paths even
        # for binaries) but count 0 lines toward the triage thresholds.
        try:
            ins = 0 if ins_s == "-" else int(ins_s)
            dels = 0 if del_s == "-" else int(del_s)
        except ValueError:
            continue
        files.append({"path": path, "insertions": ins, "deletions": dels})
        total_ins += ins
        total_del += dels

    return {
        "file_count": len(files),
        "insertions": total_ins,
        "deletions":  total_del,
        "files":      files,
    }


# ---- Deterministic triage / phase / version_3rd helpers --------------
#
# Three pure functions of `mrs` + `ack_stats` — moving them here keeps
# the topic agent's prompt from re-applying the same rules on every
# new-topic spawn. All three run AFTER the ack happy-path stat fetch.


def _is_version_3rd_file(path):
    """Match the canonical `version_3rd.cmake` at any directory depth."""
    return path == _VERSION_3RD_FILE or path.endswith("/" + _VERSION_3RD_FILE)


def _compute_triage(ack_stats):
    """Return "simple" or "complex" from per-repo stats.

    Rule (mirrors spawn_topic_agent.md): `complex` if any single repo
    has more than `_TRIAGE_COMPLEX_FILES` files OR more than
    `_TRIAGE_COMPLEX_LINES` total insertions+deletions, OR if any
    changed path is a schema/codegen file (.rsd/.nsd/.gsd/.csd).
    Otherwise `simple`. Architectural judgment stays with the agent.
    """
    for stats in (ack_stats or {}).values():
        if not stats:
            continue
        file_count = stats.get("file_count") or 0
        total_lines = (stats.get("insertions") or 0) + (stats.get("deletions") or 0)
        if file_count > _TRIAGE_COMPLEX_FILES or total_lines > _TRIAGE_COMPLEX_LINES:
            return "complex"
        for entry in stats.get("files") or []:
            if _SCHEMA_EXT_RE.search(entry.get("path") or ""):
                return "complex"
    return "simple"


def _compute_review_phase(mrs):
    """Return "3rd_party" if any MR key starts with `3rd_party/`, else None."""
    for key in mrs or {}:
        if key.startswith("3rd_party/"):
            return "3rd_party"
    return None


def _compute_version_3rd_check(mrs, ack_stats, has_merged_3p=False):
    """Bidirectional consistency check between chaos `version_3rd.cmake`
    bumps and the presence of 3rd-party library MRs.

    `has_merged_3p` marks an already-merged 3rd-party lib MR for the
    ticket (landed ahead of its consumer). It satisfies the cmake-bump
    requirement just like an open one, so a pre-merged lib does not
    false-close the consumer topic.

    Returns:
      "ok"                — consistent, or no chaos MR (nothing to check)
      "cmake_without_3p"  — chaos bumps `version_3rd.cmake` but no
                            `3rd_party/*` MR exists (open or merged)
      "3p_without_cmake"  — an *open* `3rd_party/*` MR exists but the
                            chaos MR does not bump `version_3rd.cmake`
    """
    has_3p = any(k.startswith("3rd_party/") for k in (mrs or {}))
    chaos_stats = (ack_stats or {}).get("chaos") or {}
    chaos_files = chaos_stats.get("files") or []
    cmake_bumped = any(_is_version_3rd_file(entry.get("path") or "")
                       for entry in chaos_files)
    chaos_present = "chaos" in (mrs or {})

    if cmake_bumped and not has_3p and not has_merged_3p:
        return "cmake_without_3p"
    if has_3p and chaos_present and not cmake_bumped:
        return "3p_without_cmake"
    return "ok"


# ---- Repo-paragraph builder ------------------------------------------

_REPO_LABEL = {"rage": "Game", "chaos": "Chaos"}


def _label_for(repo):
    if repo.startswith("3rd_party/"):
        return "3rd-party"
    return _REPO_LABEL.get(repo, repo)


def _link_text(repo, iid):
    if repo.startswith("3rd_party/"):
        return f"{repo.split('/', 1)[1]}!{iid}"
    return f"{repo}!{iid}"


def _repo_paragraphs(mrs, ack_stats):
    """Build REPO_PARAGRAPHS for the ack template — one paragraph per repo.

    Shape: `[<Label> 仓库] [<repo>!iid](url) <branch> · N 个文件变更 +X/-Y`
    3rd-party entries omit the stats tail (no local checkout to diff).
    """
    paragraphs = []
    for repo, mr in mrs.items():
        iid = mr.get("mr_iid")
        url = mr.get("web_url") or ""
        branch = mr.get("branch") or ""
        stats = ack_stats.get(repo) if ack_stats else None
        tail = f" {branch}" if branch else ""
        if stats:
            tail += (f" · {stats['file_count']} 个文件变更 "
                     f"+{stats['insertions']} / -{stats['deletions']}")
        paragraphs.append([
            {"tag": "text", "text": f"{_label_for(repo)} 仓库 ",
             "style": ["bold"]},
            {"tag": "a", "text": _link_text(repo, iid), "href": url},
            {"tag": "text", "text": tail},
        ])
    return paragraphs


# ---- Per-topic action ------------------------------------------------

def _post_text_nodes(content):
    """Walk a Lark post-format payload and concat every node's `text`.

    Root messages carrying a Jira link arrive as a JSON-serialized
    post structure
    (`{"title":"","content":[[{"tag":"a","text":"RAGE-…","href":"…"}, …]]}`).
    `event_utils.extract_text_content` strips HTML but does NOT unpack
    the JSON wrapper — leaving `"text":"ok"` substrings entangled
    with quotes / commas. For the fast-track check we need the pure
    rendered text. Returns "" on parse failure.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (ValueError, json.JSONDecodeError):
            return content  # plain text — return as-is
    if not isinstance(content, dict):
        return ""
    paragraphs = content.get("content") or []
    parts = []
    for para in paragraphs:
        if not isinstance(para, list):
            continue
        for node in para:
            if isinstance(node, dict):
                t = node.get("text") or ""
                if t:
                    parts.append(str(t))
    return " ".join(parts)


def _is_fast_track_root(event, sender_id, approver_open_ids, ticket_id):
    """Decide if this root message is an approver fast-track.

    Returns True iff:
      - sender_id ∈ approver_open_ids (namelist gate — non-approvers
        cannot fast-track, and the literal token reverts to noise),
      - the root content, after stripping the ticket id and any `@<name>`
        mention and surrounding whitespace, is exactly `ok`.

    The match is strict on purpose: any extra prose
    (`RAGE-12345 ok please`) falls through to the normal review
    flow rather than silently fast-tracking. See SKILL.md.
    """
    if not approver_open_ids or sender_id not in approver_open_ids:
        return False
    raw = event.get("content") if isinstance(event, dict) else None
    text = _post_text_nodes(raw)
    if not text:
        # Fall back to extract_text_content for pure-text msg_type
        text = event_utils.extract_text_content(event) if isinstance(event, dict) else ""
    if not text:
        return False
    # Strip the ticket id (case-insensitive, with optional separators).
    if ticket_id:
        text = re.sub(re.escape(ticket_id), "", text, flags=re.IGNORECASE)
    # Strip any `@<name>` mention (same shape extract_assigned_dev consumes).
    text = re.sub(r"@[^\s@]{1,32}", "", text)
    # Strip Lark's link-text artifacts that may slip past the JSON walker.
    text = text.strip()
    return text in _FAST_TRACK_TOKENS


def _fast_track_approve(topic, topic_path, event, thread_id, ticket_id,
                        review_phase):
    """Approve every MR + post the approval template + transition to APPROVED.

    Runs after the standard ack-time checks (MR discovery, MR state,
    version_3rd consistency, ack_stats fetch) have passed. The caller
    has already persisted `mrs` / `ack_stats` / `triage` /
    `review_phase` / `version_3rd_check` on the topic dict and is
    holding the topic lock.

    Skips the normal `ack_new_topic` template (no point posting "found
    your MRs, reviewing now…" right before the approval). Instead
    posts the canonical `approval` template via
    `post_approval.post_approval_on_topic`, which handles the
    pipeline-status recheck + infra-retry + state flip + audit trail
    identically to the mechanical approver-reply path. Returns the
    bucket key consumed by `drain_ack`'s summary.
    """
    event_id = event.get("event_id") or event.get("message_id")
    mrs = topic.get("mrs") or {}

    for repo, mr_obj in mrs.items():
        ok, err = mechanical_reply_handler._glab_approve(repo, mr_obj)
        if not ok:
            _log("WARN", f"{ticket_id}: fast-track glab approve failed "
                         f"({repo}): {err}")
            return "errors"

    lifecycle = topic.setdefault("lifecycle", {})
    lifecycle["ack_sent"] = True
    lifecycle["ack_sent_at"] = topic_store.now_ms()

    # Drain the root new_topic event from pending — no agent will
    # process it on the fast-track path, and the dispatcher will
    # otherwise keep re-listing the APPROVED topic as work every
    # cycle (event remains, state isn't terminal until MERGED).
    pending = (topic.get("events") or {}).get("pending") or []
    for idx, pe in enumerate(pending):
        if (pe.get("event_id") or pe.get("message_id")) == event_id:
            pending.pop(idx)
            break
    topic.setdefault("events", {})["last_processed_event_id"] = event_id
    topic_store.push_recent_event(topic, event_id)

    topic_store.append_audit(topic,
        event="fast_track_approved",
        triggered_by_event_id=event_id,
        sender_id=event.get("sender_id"),
        repos=list(mrs.keys()),
        review_phase=review_phase,
    )

    if review_phase == "3rd_party":
        # 3rd-party MRs may have no pipeline; mirror the mechanical
        # 3rd-party-approve path (skip pipeline recheck, fixed copy,
        # let process_merge_queue drive merge + phase reset to main).
        if not _already_posted(topic, event_id):
            ok, resp = _render_and_post("approval", {
                "TICKET_ID": ticket_id,
                "PIPELINE_MSG": "等待合并队列处理。",
            }, thread_id)
            if not ok:
                _log("WARN", f"{ticket_id}: fast-track 3p approval post "
                             f"failed: {resp}")
                # Don't unwind — MR is approved on GitLab; persist what we
                # have so the next cycle's reconcile can repost.
                topic_store.write_atomic(topic_path, topic)
                return "errors"
            topic_store.append_audit(topic,
                event="lark_reply_sent",
                reply_type="approval",
                triggered_by_event_id=event_id,
                phase="3rd_party",
                fast_track=True,
            )
        review = topic.setdefault("review", {})
        from_state = review.get("state")
        review["state"] = "APPROVED"
        topic_store.append_audit(topic,
            event="state_transition",
            from_state=from_state,
            to_state="APPROVED",
            triggered_by_event_id=event_id,
            fast_track=True,
        )
        topic_store.write_atomic(topic_path, topic)
        return "fast_tracked"

    # Main phase — delegate to post_approval (pipeline recheck + post + flip).
    from_state = (topic.get("review") or {}).get("state")
    result = post_approval.post_approval_on_topic(
        topic, topic_path, event_id, from_state=from_state)
    if not result.get("ok"):
        _log("WARN", f"{ticket_id}: fast-track post_approval failed: "
                     f"{result.get('reason')}")
        return "errors"
    return "fast_tracked"


def _needs_ack(topic):
    """True iff this topic needs a brand-new-topic ack.

    The router doesn't tag events with `event_type: "new_topic"`; per
    spawn_topic_agent.md the `new_topic` action is inferred from
    `review.state == "TRIAGING"` (the initial state set by
    `topic_store.empty_topic`). So we gate on state + `lifecycle.ack_sent`
    falsy + at least one pending event to anchor the ack to an event_id.
    """
    if (topic.get("lifecycle") or {}).get("ack_sent"):
        return False
    if (topic.get("review") or {}).get("state") != "TRIAGING":
        return False
    pending = (topic.get("events") or {}).get("pending") or []
    return bool(pending)


def _find_new_topic_event(topic):
    """Return the first (oldest) pending event — this is the new_topic.

    The router processes events in receive order and a TRIAGING topic's
    first pending item is always the root-message event that opened the
    topic. Later events can only arrive after the topic exists.
    """
    pending = (topic.get("events") or {}).get("pending") or []
    return pending[0] if pending else None


# Ack-time closes whose cause the developer can fix and then continue in
# the SAME thread (create the missing MR, push the version_3rd bump). These
# are marked revivable so a follow-up reply reopens the topic instead of
# being dropped by router Gate 4a — the close copy explicitly asks for the
# follow-up, so silently swallowing it is a dead end (DESIGN §1.3.7).
# `mr_already_merged` / `mr_already_closed` are NOT revivable: nothing the
# developer says makes a merged MR reviewable again.
REVIVABLE_CLOSE_REASONS = frozenset({
    "mr_not_found",
    "3rd_party_mr_not_found",
    "missing_version_3rd_bump",
})

_REVIVE_HINT = "补齐后在本话题回复即可继续审查。"


def _close_with_message(topic, topic_path, topics_dir, index_path,
                        thread_id, event, reason, text,
                        mention_id=None, mention_name=None):
    """Shared close path: post the notice (@-mentioning the requester when
    an open_id is known), set state=CLOSED, archive.

    Revivable reasons additionally get the follow-up hint appended to the
    notice and `lifecycle.revivable` set, so the router can reopen the
    topic when the developer answers.
    """
    revivable = reason in REVIVABLE_CLOSE_REASONS
    if revivable:
        text = f"{text}{_REVIVE_HINT}"
    event_id = event.get("event_id") or event.get("message_id")
    if not _already_posted(topic, event_id):
        ok, resp = _post_close_notice(text, thread_id, mention_id, mention_name)
        if ok:
            topic_store.append_audit(topic,
                event="lark_reply_sent",
                reply_type="ack_new_topic_close",
                reason=reason,
                triggered_by_event_id=event_id)
        else:
            _log("WARN", f"{thread_id}: close-message post failed: {resp}")
    review = topic.setdefault("review", {})
    from_state = review.get("state")
    review["state"] = "CLOSED"
    lifecycle = topic.setdefault("lifecycle", {})
    lifecycle["resolved_at"] = topic_store.now_ms()
    lifecycle["closed_reason"] = reason
    lifecycle["revivable"] = revivable
    lifecycle["ack_sent"] = True
    lifecycle["ack_sent_at"] = topic_store.now_ms()
    topic_store.append_audit(topic,
        event="topic_closed",
        from_state=from_state,
        to_state="CLOSED",
        reason=reason,
        triggered_by_event_id=event_id)
    topic_store.write_atomic(topic_path, topic)
    topic_store.archive_topic(topics_dir, index_path, thread_id)


def _ack_one_topic(topic, topic_path, topics_dir, index_path,
                   rage_root, chaos_root, approver_open_ids=None):
    """Process one topic. Returns result string for summary counter."""
    thread_id = topic.get("thread_id") or topic.get("root_message_id")
    if not thread_id:
        return "errors"
    event = _find_new_topic_event(topic)
    if event is None:
        return None
    ticket_id = (topic.get("identity") or {}).get("ticket_id") or ""
    if not ticket_id:
        return "errors"

    # Resolve "assigned developer" from the root message — operator can
    # file on behalf of someone not in the group via `RAGE-XXXXX @<dev>`.
    # Lark @-mention via picker is the preferred input; literal text
    # falls back to the org-contacts cache. See
    # `event_utils.extract_assigned_dev` for the resolution rules.
    identity = topic.setdefault("identity", {})
    original_sender_id = identity.get("creator_open_id") or ""
    assigned_id, assigned_name, assigned_source = (
        event_utils.extract_assigned_dev(
            event,
            fallback_sender_id=original_sender_id,
            fallback_sender_name=identity.get("developer") or "",
        ))
    if assigned_source != "sender" and assigned_id != original_sender_id:
        identity["filed_by_open_id"] = original_sender_id
        identity["creator_open_id"] = assigned_id
        if assigned_name:
            identity["developer"] = assigned_name
        topic_store.append_audit(topic,
            event="assigned_dev_set",
            assigned_dev_open_id=assigned_id,
            assigned_dev_name=assigned_name,
            source=assigned_source,
            filed_by=original_sender_id,
        )
    elif assigned_source == "text_unresolved" and assigned_name:
        # Display the literal name even when we couldn't resolve to an
        # open_id — better than the bot misattributing to the operator.
        identity["developer"] = assigned_name
        topic_store.append_audit(topic,
            event="assigned_dev_unresolved",
            literal_name=assigned_name,
            filed_by=original_sender_id,
        )

    # Requester to @-mention on any ack-time close (creator_open_id may have
    # been rewritten to the assigned dev above). Empty → close falls back to
    # plain text. Name defaults to a generic label so an unresolved contact
    # never blocks the close.
    mention_id = identity.get("creator_open_id") or ""
    mention_name = identity.get("developer") or "开发者"

    mrs, discovery_errors, has_merged_3p, threep_probe_errored = _discover_mrs(ticket_id)
    if discovery_errors:
        _log("WARN", f"{ticket_id}: MR discovery partial — {discovery_errors}")
        # Any error + no MRs at all → retry next cycle, don't falsely close.
        if not mrs:
            return "retry"

    if not mrs:
        _close_with_message(topic, topic_path, topics_dir, index_path,
                            thread_id, event,
                            reason="mr_not_found",
                            text=f"⚠️ 未找到 {ticket_id} 对应的 MR。"
                                 f"请确认 MR 已创建并包含 {ticket_id}。",
                            mention_id=mention_id, mention_name=mention_name)
        return "closed_no_mr"

    # If every MR is non-opened (merged/closed), there's nothing to review.
    states = [mr.get("state") for mr in mrs.values()]
    if all(s in ("merged", "closed") for s in states):
        final_state = "merged" if all(s == "merged" for s in states) else "closed"
        cn = "已合并" if final_state == "merged" else "已关闭"
        _close_with_message(topic, topic_path, topics_dir, index_path,
                            thread_id, event,
                            reason=f"mr_already_{final_state}",
                            text=f"该 MR {cn}，无需审查。",
                            mention_id=mention_id, mention_name=mention_name)
        return f"closed_already_{final_state}"

    # Happy path — fetch stats first so triage / version_3rd / phase
    # helpers can consult the per-file list they produce.
    ack_stats = {}
    for repo, mr in mrs.items():
        stats = _fetch_stats(repo, mr, rage_root, chaos_root)
        if stats is not None:
            ack_stats[repo] = stats

    # Bidirectional dependency check: chaos bumping `version_3rd.cmake`
    # without a 3rd-party library MR (or vice versa) is inconsistent —
    # close the topic with the existing warning copy before posting ack.
    version_3rd_check = _compute_version_3rd_check(mrs, ack_stats,
                                                   has_merged_3p)
    if version_3rd_check == "cmake_without_3p":
        # The cmake→3p verdict depends on the state=opened AND state=merged
        # 3rd-party probes; a transient failure of either leaves has_3p /
        # has_merged_3p falsely False and would re-trigger the exact RAGE-20767
        # false-close. Treat the verdict as uncertain and retry, not close
        # (§1.3.6).
        if threep_probe_errored:
            _log("WARN", f"{ticket_id}: version_3rd cmake_without_3p, but a "
                         f"3rd-party probe errored — retry, not close")
            return "retry"
        _close_with_message(topic, topic_path, topics_dir, index_path,
                            thread_id, event,
                            reason="3rd_party_mr_not_found",
                            text="⚠️ 检测到 version_3rd.cmake 版本升级，"
                                 "但未找到对应的第三方库 MR。请先创建第三方库 MR。",
                            mention_id=mention_id, mention_name=mention_name)
        return "closed_version_3rd_missing_3p"
    if version_3rd_check == "3p_without_cmake":
        _close_with_message(topic, topic_path, topics_dir, index_path,
                            thread_id, event,
                            reason="missing_version_3rd_bump",
                            text="⚠️ 第三方库 MR 已存在，但 chaos 仓库的 "
                                 "version_3rd.cmake 未包含版本升级。",
                            mention_id=mention_id, mention_name=mention_name)
        return "closed_missing_cmake_bump"

    # Deterministic triage + phase routing — moves this reasoning out
    # of the topic agent so Opus doesn't re-apply the same rules on
    # every new-topic spawn.
    triage = _compute_triage(ack_stats)
    review_phase = _compute_review_phase(mrs)

    # Persist discovered MRs + stats + pre-computed review fields
    # before the fast-track / normal-ack branch — both paths rely on
    # `topic["mrs"]` (fast-track for glab approve / pipeline recheck,
    # normal ack for the agent's downstream review).
    topic["mrs"] = mrs
    review = topic.setdefault("review", {})
    review["ack_stats"] = ack_stats
    review["triage"] = triage
    review["review_phase"] = review_phase
    review["version_3rd_check"] = version_3rd_check

    # Fast-track: approver typed `RAGE-XXXXX ok` as the root message.
    # Approve every MR + post the approval template + state→APPROVED so
    # the merge queue picks it up the next cycle. Authorization is
    # gated on the ORIGINAL sender (event["sender_id"]) — the
    # assigned-dev resolution above may have rewritten
    # identity.creator_open_id, so we must not trust that field for
    # the namelist check.
    sender_id = event.get("sender_id") or ""
    if _is_fast_track_root(event, sender_id, approver_open_ids, ticket_id):
        return _fast_track_approve(topic, topic_path, event,
                                    thread_id, ticket_id, review_phase)

    variables = {
        "TICKET_ID":        ticket_id,
        "REPO_PARAGRAPHS":  _repo_paragraphs(mrs, ack_stats),
        # The topic id is the thread's root message id. Printing it in the
        # first reply is the only place a human can copy it from — handing
        # it to another agent is what lets that agent drive the review
        # (reference/agent-topic-reply.md, DESIGN §1.25).
        "TOPIC_ID":         thread_id,
    }
    event_id = event.get("event_id") or event.get("message_id")
    if _already_posted(topic, event_id):
        posted_ok = True
    else:
        posted_ok, resp = _render_and_post("ack_new_topic", variables, thread_id)
        if not posted_ok:
            _log("WARN", f"{ticket_id}: ack post failed: {resp}")
            return "errors"
        topic_store.append_audit(topic,
            event="lark_reply_sent",
            reply_type="ack_new_topic",
            triggered_by_event_id=event_id)

    # Fetch manual review threads from GitLab (DiffNote discussions).
    # See DESIGN §1.14.1. Best-effort — a transient API failure should
    # not block the ack; manual issues are refreshable on round N or
    # via the `manual_refresh` command.
    fetched_manual, manual_errors = gitlab_threads.fetch_for_topic(topic)
    if manual_errors:
        _log("WARN",
             f"{ticket_id}: manual-issue fetch partial — {manual_errors}")
    existing_manual = review.get("manual_issues") or []
    merged_manual, manual_summary = gitlab_threads.reconcile_manual_issues(
        existing_manual, fetched_manual)
    review["manual_issues"] = merged_manual
    if manual_summary["added"] or manual_summary["pruned"]:
        topic_store.append_audit(topic,
            event="manual_issues_synced",
            triggered_by_event_id=event_id,
            summary=manual_summary,
            errors=manual_errors[:5] if manual_errors else None)
    lifecycle = topic.setdefault("lifecycle", {})
    lifecycle["ack_sent"] = True
    lifecycle["ack_sent_at"] = topic_store.now_ms()
    topic_store.append_audit(topic,
        event="ack_sent",
        reason="ack_new_topic",
        triggered_by_event_id=event_id,
        repos=list(mrs.keys()),
        triage=triage,
        review_phase=review_phase)
    topic_store.write_atomic(topic_path, topic)
    return "posted"


# ---- Drain entry point (dispatcher calls this) -----------------------

def drain_ack(topics_dir, index_path, cycle_id, rage_root, chaos_root,
              approver_open_ids=None):
    """Run the ack handler across all open topics.

    Mirrors mechanical_reply_handler.drain_mechanical signature so the
    dispatcher step can slot it in alongside existing mechanical drains.
    `approver_open_ids` is the authorized-approver namelist; the
    handler uses it to gate the fast-track shortcut
    (`RAGE-XXXXX ok` root message → straight to APPROVED).
    Pass `None` to disable fast-track entirely (test/replay).
    """
    topics_dir = Path(topics_dir)
    summary = {"checked": 0, "posted": 0,
               "closed_no_mr": 0,
               "closed_already_merged": 0,
               "closed_already_closed": 0,
               "closed_version_3rd_missing_3p": 0,
               "closed_missing_cmake_bump": 0,
               "fast_tracked": 0,
               "errors": 0, "skipped_locked": 0,
               "retry": 0, "topics_touched": []}
    if not topics_dir.exists():
        return summary

    holder = f"ack-{cycle_id}"
    for topic_path in topic_store.iter_topic_files(topics_dir):
        try:
            topic = topic_store.read(topic_path)
        except (OSError, ValueError):
            continue
        if not _needs_ack(topic):
            continue
        summary["checked"] += 1

        if not topic_store.acquire_lock(topic_path, holder, cycle_id):
            summary["skipped_locked"] += 1
            continue

        try:
            # Re-read under lock — the pre-lock read may be stale.
            topic = topic_store.read(topic_path)
            if not _needs_ack(topic):
                continue
            result = _ack_one_topic(
                topic, topic_path, topics_dir, index_path,
                rage_root, chaos_root,
                approver_open_ids=approver_open_ids)
            if result is None:
                continue
            if result == "retry":
                summary["retry"] += 1
                continue
            key = result if result in summary else "errors"
            summary[key] = summary.get(key, 0) + 1
            summary["topics_touched"].append(
                topic.get("thread_id") or topic_path.stem)
        except Exception as exc:  # noqa: BLE001
            summary["errors"] += 1
            _log("ERROR", f"{topic_path.name}: {type(exc).__name__}: {exc}")
        finally:
            topic_store.release_lock(topic_path)

    return summary
