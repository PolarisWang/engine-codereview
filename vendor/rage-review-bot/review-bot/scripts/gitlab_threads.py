# -*- coding: utf-8 -*-
"""Fetch and normalize GitLab MR review threads (DiffNote discussions).

Manual code-review comments left on the MR by humans become
`review.manual_issues[]` entries on the topic. The bot integrates them
into round-1 display and round-N verification (see DESIGN §1.14.1).

Scope:
- Only `DiffNote` discussions (line-anchored review comments). System
  notes (label changes, approvals, milestones) and free-form
  conversation notes (no `position`) are dropped.
- The team has chosen NOT to use GitLab's `resolved` flag — the bot is
  the sole arbiter of fix status. We don't surface or read it.

Usage (CLI for replay/debugging):
    python gitlab_threads.py fetch --repo-slug booming/dev/projects/rage/chaos --mr-iid 2337
    python gitlab_threads.py reconcile --topic <topic.json> --rage-root <path> --chaos-root <path>
"""
import argparse
import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import merge_tracker
import topic_store


# Body truncation — full text is one click away via web_url.
MAX_BODY_CHARS = 280


def _urlenc(s):
    return s.replace("/", "%2F")


def fetch_discussions(repo_slug, mr_iid, timeout_s=30):
    """Fetch all discussions for a given MR.

    Returns (list[dict], error_str). Empty list with None error means no
    threads. Non-empty error indicates the API call failed; callers
    should treat manual-issue state as "unknown, retry later" rather
    than "no manual issues".
    """
    args = ["glab", "api",
            f"projects/{_urlenc(repo_slug)}/merge_requests/{mr_iid}/discussions"]
    stdout, stderr, rc = merge_tracker._run_glab(args, timeout_s=timeout_s)
    if rc != 0:
        return [], (stderr or stdout or "").strip()[:300]
    try:
        return json.loads(stdout), None
    except (ValueError, json.JSONDecodeError) as exc:
        return [], f"bad json: {exc}"


def _normalize_diffnote(thread, mr_web_url):
    """Pull the canonical fields out of a discussion's first note.

    Returns a dict shaped per the `manual_issues[]` schema (see
    DESIGN §1.14.1), or None if the thread is not a DiffNote.
    """
    notes = thread.get("notes") or []
    if not notes:
        return None
    first = notes[0]
    if first.get("type") != "DiffNote":
        return None
    pos = first.get("position") or {}
    if not pos:
        return None  # somehow a DiffNote without position — skip

    body_full = first.get("body") or ""
    body = body_full[:MAX_BODY_CHARS]
    if len(body_full) > MAX_BODY_CHARS:
        body = body[:-1] + "…"

    note_id = first.get("id")
    web_url = f"{mr_web_url}#note_{note_id}" if mr_web_url and note_id else ""

    author = (first.get("author") or {}).get("username") or "unknown"

    return {
        "discussion_id": thread.get("id") or "",
        "note_id": note_id,
        "author": author,
        "file": pos.get("new_path") or pos.get("old_path") or "",
        "line_old": pos.get("old_line"),
        "line_new": pos.get("new_line"),
        "base_sha": pos.get("base_sha") or "",
        "body": body,
        "body_full_length": len(body_full),
        "web_url": web_url,
        "created_at": first.get("created_at") or "",
        # Verification-side fields — populated later by the verifier.
        "verification": None,
        "verification_rationale": "",
        "verified_at_sha": "",
    }


def fetch_manual_issues(repo, mr_obj, timeout_s=30):
    """Fetch + normalize manual review issues for one MR.

    Returns (list[dict], error_str). Each entry:
        {discussion_id, note_id, author, file, line_old, line_new,
         base_sha, body, body_full_length, web_url, created_at,
         verification, verification_rationale, verified_at_sha,
         repo (added by caller)}

    `repo` is filled in by the caller from the topic's mrs key, since
    the GitLab API doesn't carry our internal repo slug.
    """
    repo_slug = mr_obj.get("repo_slug")
    if not repo_slug:
        repo_slug = merge_tracker.REPO_SLUGS.get(repo)
    if not repo_slug:
        return [], f"unknown repo slug for {repo!r}"
    mr_iid = merge_tracker._get_iid(mr_obj)
    if not mr_iid:
        return [], f"no mr_iid on {repo!r}"

    threads, err = fetch_discussions(repo_slug, mr_iid, timeout_s=timeout_s)
    if err:
        return [], err

    mr_web_url = mr_obj.get("web_url") or ""
    out = []
    for thread in threads:
        entry = _normalize_diffnote(thread, mr_web_url)
        if entry is None:
            continue
        entry["repo"] = repo
        out.append(entry)
    # Stable order: by created_at then discussion_id
    out.sort(key=lambda e: (e.get("created_at") or "", e.get("discussion_id") or ""))
    return out, None


def reconcile_manual_issues(existing, fetched):
    """Merge a fresh fetch into the existing `manual_issues[]` array.

    Idempotent merge keyed by `discussion_id`:
      - Existing entries keep their `verification`,
        `verification_rationale`, `verified_at_sha` (verification is
        SHA-pinned; refetch alone doesn't invalidate it).
      - New entries are appended with `verification=None`.
      - Threads no longer in the fetch result are dropped (the human
        deleted them on GitLab); their verification verdicts are lost.
        This is rare; the audit trail records the prune.

    Re-numbers `index` (1..N) by stable sort order so display labels
    are dense even after prunes. The labels carry no semantic meaning
    beyond display, so re-numbering across cycles is safe.

    Returns (merged_list, summary_dict) where summary tracks
    {added, kept, pruned, refreshed_metadata}. `refreshed_metadata`
    counts entries where the body / line numbers changed (e.g.
    reviewer edited their comment) — verification is NOT auto-cleared
    in that case; SHA-based invalidation is the only trigger.
    """
    by_id = {e.get("discussion_id"): e for e in existing if e.get("discussion_id")}
    summary = {"added": 0, "kept": 0, "pruned": 0, "refreshed_metadata": 0}

    seen_ids = set()
    merged = []
    for fresh in fetched:
        did = fresh.get("discussion_id")
        if not did:
            continue
        seen_ids.add(did)
        if did in by_id:
            old = by_id[did]
            # Preserve verification fields; refresh display fields.
            metadata_changed = (
                old.get("body") != fresh.get("body")
                or old.get("line_new") != fresh.get("line_new")
                or old.get("line_old") != fresh.get("line_old")
                or old.get("file") != fresh.get("file")
            )
            merged_entry = dict(fresh)
            merged_entry["verification"] = old.get("verification")
            merged_entry["verification_rationale"] = old.get("verification_rationale", "")
            merged_entry["verified_at_sha"] = old.get("verified_at_sha", "")
            merged.append(merged_entry)
            summary["kept"] += 1
            if metadata_changed:
                summary["refreshed_metadata"] += 1
        else:
            merged.append(fresh)
            summary["added"] += 1

    summary["pruned"] = sum(1 for e in existing
                            if e.get("discussion_id") not in seen_ids)

    # Re-number by sort order
    for idx, entry in enumerate(merged, start=1):
        entry["index"] = idx

    return merged, summary


def mark_resolved(repo, mr_obj, discussion_id, timeout_s=15):
    """Set `resolved=true` on a GitLab discussion thread.

    Called by the topic agent after a verification verdict of
    `addressed` or `obsolete` — the bot is the source of truth for
    fix status, and writing back keeps the GitLab UI in sync with
    the bot's judgment so reviewers don't have to manually click
    "Resolved" on every thread.

    Returns (ok, error_str). Idempotent on GitLab's side — calling
    with resolved=true on an already-resolved thread is a no-op (HTTP
    200 with the unchanged thread).

    Never set `resolved=false` from here. Once the bot decides a
    concern is addressed, a later regression should surface as a NEW
    `not_addressed` verdict in the Lark thread + audit, not as a
    re-opened GitLab thread (which would conflict with any follow-up
    notes the reviewer left).
    """
    repo_slug = mr_obj.get("repo_slug")
    if not repo_slug:
        repo_slug = merge_tracker.REPO_SLUGS.get(repo)
    if not repo_slug:
        return False, f"unknown repo slug for {repo!r}"
    mr_iid = merge_tracker._get_iid(mr_obj)
    if not mr_iid:
        return False, f"no mr_iid on {repo!r}"
    if not discussion_id:
        return False, "no discussion_id"

    args = ["glab", "api", "--method", "PUT",
            f"projects/{_urlenc(repo_slug)}/merge_requests/{mr_iid}"
            f"/discussions/{discussion_id}?resolved=true"]
    stdout, stderr, rc = merge_tracker._run_glab(args, timeout_s=timeout_s)
    if rc != 0:
        return False, (stderr or stdout or "").strip()[:200]
    return True, None


def fetch_for_topic(topic, only_main_phase=True):
    """Fetch manual issues for every active MR on the topic.

    Returns (list[dict], errors_list). Aggregated entries from all
    repos. Skips 3rd-party repos by default (their threads are
    out-of-scope for the main review flow; can be enabled later).

    Caller is responsible for calling `reconcile_manual_issues` to
    merge with the topic's existing array, then writing back.
    """
    mrs = topic.get("mrs") or {}
    review_phase = (topic.get("review") or {}).get("review_phase")

    out = []
    errors = []
    for repo, mr in mrs.items():
        # Skip repos not in the current phase. In main phase we only
        # care about rage/chaos. In 3rd-party phase we'd fetch from
        # the 3rd-party MR but that path isn't enabled yet.
        if only_main_phase:
            if review_phase == "3rd_party":
                if not repo.startswith("3rd_party/"):
                    continue
            else:
                if repo.startswith("3rd_party/"):
                    continue
        if mr.get("state") in ("merged", "closed"):
            continue
        entries, err = fetch_manual_issues(repo, mr)
        if err:
            errors.append(f"{repo}: {err}")
            continue
        out.extend(entries)
    return out, errors


# -----------------------------------------------------------------
# CLI
# -----------------------------------------------------------------

def _utf8_stdout():
    """Force UTF-8 stdout on Windows so Chinese strings round-trip."""
    import io
    return io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")


def _cmd_fetch(args):
    threads, err = fetch_discussions(args.repo_slug, args.mr_iid)
    if err:
        json.dump({"error": err}, _utf8_stdout(), ensure_ascii=False)
        return 1
    mr_web_url = args.mr_web_url or ""
    out = []
    for t in threads:
        e = _normalize_diffnote(t, mr_web_url)
        if e:
            out.append(e)
    json.dump({"count": len(out), "issues": out},
              _utf8_stdout(), ensure_ascii=False)
    return 0


def _cmd_mark_resolved(args):
    topic = topic_store.read(Path(args.topic))
    manual_issues = (topic.get("review") or {}).get("manual_issues") or []
    target = next((m for m in manual_issues
                   if m.get("index") == args.index), None)
    if target is None:
        json.dump({"error": f"no manual issue with index {args.index}"},
                  _utf8_stdout(), ensure_ascii=False)
        return 1
    repo = target.get("repo") or ""
    mr = (topic.get("mrs") or {}).get(repo) or {}
    ok, err = mark_resolved(repo, mr, target.get("discussion_id"))
    json.dump({"ok": ok, "error": err}, _utf8_stdout(), ensure_ascii=False)
    return 0 if ok else 1


def _cmd_reconcile(args):
    topic = topic_store.read(Path(args.topic))
    fetched, errors = fetch_for_topic(topic)
    existing = (topic.get("review") or {}).get("manual_issues") or []
    merged, summary = reconcile_manual_issues(existing, fetched)
    json.dump({
        "summary": summary,
        "errors": errors,
        "manual_issues": merged,
    }, _utf8_stdout(), ensure_ascii=False)
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="Raw fetch + normalize from GitLab")
    f.add_argument("--repo-slug", required=True,
                   help="e.g. booming/dev/projects/rage/chaos")
    f.add_argument("--mr-iid", required=True, type=int)
    f.add_argument("--mr-web-url",
                   help="Base MR URL — used to build per-thread web_urls")
    f.set_defaults(func=_cmd_fetch)

    r = sub.add_parser("reconcile",
                       help="Fetch + merge into a topic's manual_issues[]")
    r.add_argument("--topic", required=True, help="Path to topic JSON")
    r.set_defaults(func=_cmd_reconcile)

    m = sub.add_parser("mark-resolved",
                       help="Set resolved=true on the GitLab discussion")
    m.add_argument("--topic", required=True, help="Path to topic JSON")
    m.add_argument("--index", required=True, type=int,
                   help="manual_issues[].index to mark resolved")
    m.set_defaults(func=_cmd_mark_resolved)

    args = p.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
