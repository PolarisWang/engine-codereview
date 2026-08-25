"""Post-merge cherry-pick of a merged commit onto active release branches.

Two responsibilities, deliberately separated so the resolution half stays
pure and testable without a network:

1. **Discovery / resolution** (`discover_active_branches`, `resolve_tokens`) —
   which release branches are live right now, and what the approver's `p1`
   means today.
2. **Execution** (`cherry_pick_to_branch`) — land the commit, direct first,
   MR on rejection.

Release branches come in numbered families (`rc_p0`, `rc_p1`, …;
`rc_next_p1`, `rc_next_p2`, …; `rc_dev_p4`, …). Only the highest number per
family is live; the rest have shipped and must never receive a cherry-pick.
See DESIGN §1.24.
"""
import json
import re
import subprocess

import subprocess_util as su

# rc_p<N> / rc_next_p<N> / rc_dev_p<N>, optionally namespaced: chaos carries
# its release branches under `rage/` (same convention as its `rage/master`
# default), so the same release is `rc_p1` in rage and `rage/rc_p1` in chaos.
# The namespace is part of the family — `rage/rc_` and `rc_` never share a max.
# The variant is too: `rc_`, `rc_next_`, and `rc_dev_` are three independent
# families, each with its own live maximum (DESIGN §1.24).
# `_VARIANT` must stay in sync with `reply_parser._CP_TOKEN`.
_VARIANT = r"(?:next_|dev_)?"
_RELEASE_BRANCH = re.compile(
    r"^(?P<prefix>(?:[\w.\-]+/)*)rc_(?P<variant>%s)p(?P<number>\d+)$" % _VARIANT)
# What the approver may type: `p1`, or a full branch name (any spelling).
_TOKEN = re.compile(
    r"^(?:p(?P<number>\d+)|(?P<branch>(?:[\w.\-]+/)*rc_%sp\d+))$" % _VARIANT,
    re.IGNORECASE)

_LS_REMOTE_TIMEOUT = 60
_API_TIMEOUT = 120


def parse_release_branch(name):
    """Return (family, number) for a release branch, else None.

    The family carries both the namespace and the variant, so `rage/rc_p1`
    (chaos) and `rc_p1` (rage) are different families, as are `rc_p4`,
    `rc_next_p4`, and `rc_dev_p4` — each max is computed against its own
    naming, and no branch can mask another family's live head.
    """
    match = _RELEASE_BRANCH.match(name.strip())
    if not match:
        return None
    family = "%src_%s" % (match.group("prefix"), match.group("variant") or "")
    return family, int(match.group("number"))


def active_from_names(names):
    """Reduce branch names to the active one per family.

    Pure — no I/O — so the max-per-family rule is unit-testable. Returns
    {branch_name: number} ordered by family then number.
    """
    best = {}
    for name in names:
        parsed = parse_release_branch(name)
        if not parsed:
            continue
        family, number = parsed
        if family not in best or number > best[family][1]:
            best[family] = (name.strip(), number)
    return {name: number for name, number in
            sorted(best.values(), key=lambda item: item[0])}


def discover_active_branches(repo_root):
    """Return {branch_name: number} of live release branches for one repo.

    `git ls-remote --heads` rather than a local branch listing: the workspace
    may never have fetched a freshly cut rc branch, and a cherry-pick target
    that exists only on the remote is still a valid target.
    """
    # No refspec filter: chaos namespaces its release branches under `rage/`,
    # so `refs/heads/rc_*` silently returns nothing there. List every head and
    # filter in Python, where the namespace-aware pattern decides.
    # encoding= is mandatory, not decoration: `text=True` alone decodes with
    # the locale codec (GBK on this machine) and BOTH repos carry branches
    # with non-ASCII names. That raises UnicodeDecodeError inside subprocess's
    # reader thread, which surfaces as *empty stdout* — so discovery silently
    # returned "no release branches" and the offer never appeared.
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", "origin"],
            cwd=repo_root, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=_LS_REMOTE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("ls-remote failed in %s: %s" % (repo_root, exc))
    if result.returncode != 0:
        raise RuntimeError("ls-remote failed in %s: %s"
                           % (repo_root, (result.stderr or "").strip()[:200]))

    names = []
    for line in (result.stdout or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            names.append(parts[1][len("refs/heads/"):])
    return active_from_names(names)


def build_prompt_mapping(active_by_repo):
    """Merge per-repo active sets into the token map shown to the approver.

    `{token: {"targets": {repo: branch}, "ambiguous": bool}}`. The token is a
    release *number*, and the branch name for that number is per-repo —
    release 1 is `rc_p1` in rage but `rage/rc_p1` in chaos, so a single
    branch string cannot represent it. A number live in two families **within
    one repo** is ambiguous; the approver spells that branch out instead
    (DESIGN §1.24).
    """
    by_number = {}
    for repo, active in sorted(active_by_repo.items()):
        for branch, number in active.items():
            by_number.setdefault(number, {}).setdefault(repo, []).append(branch)

    mapping = {}
    for number, per_repo in sorted(by_number.items()):
        token = "p%d" % number
        collisions = {repo: sorted(branches)
                      for repo, branches in per_repo.items()
                      if len(branches) > 1}
        if collisions:
            mapping[token] = {"targets": {}, "ambiguous": True,
                              "candidates": collisions}
        else:
            mapping[token] = {
                "targets": {repo: branches[0]
                            for repo, branches in sorted(per_repo.items())},
                "ambiguous": False,
            }
    return mapping


def describe_token(token, entry):
    """One human-readable line for the offer post."""
    targets = entry.get("targets") or {}
    # Collapse the common case where every repo uses the same branch name.
    names = set(targets.values())
    if len(names) == 1:
        return "%s → %s（%s）" % (token, next(iter(names)),
                                 "/".join(sorted(targets)))
    parts = ["%s（%s）" % (branch, repo)
             for repo, branch in sorted(targets.items())]
    return "%s → %s" % (token, "，".join(parts))


def resolve_tokens(tokens, mapping, active_by_repo):
    """Resolve approver tokens to [(label, {repo: branch})].

    Returns (resolved, errors). Every token is either resolved or reported —
    a token is never silently dropped, because a dropped token reads to the
    approver as "cherry-picked" when nothing happened.
    """
    resolved = []
    errors = []
    seen = set()
    for raw in tokens:
        token = raw.strip()
        match = _TOKEN.match(token)
        if not match:
            errors.append("`%s` 不是有效的分支代号" % token)
            continue

        if match.group("number") is not None:
            label = "p%d" % int(match.group("number"))
            entry = mapping.get(label)
            if entry is None:
                errors.append("`%s` 不对应任何活跃发布分支" % token)
                continue
            if entry.get("ambiguous"):
                flat = sorted({b for branches in
                               (entry.get("candidates") or {}).values()
                               for b in branches})
                errors.append("`%s` 有歧义（%s），请直接写完整分支名"
                              % (token, " / ".join(flat)))
                continue
            targets = dict(entry["targets"])
        else:
            branch = match.group("branch")
            # `_TOKEN` carries re.IGNORECASE, which promises the approver that
            # case does not matter — but the active set is keyed by names from
            # `git ls-remote`, which is case-SENSITIVE. Matching those directly
            # meant `RC_P1` passed the regex, missed the lookup, and came back
            # as "not an active release branch (possibly retired)" — a message
            # that is simply untrue. Fold case for the lookup and resolve back
            # to the real branch name so the git commands still get the exact
            # ref (DESIGN §1.24.5).
            needle = branch.lower()
            targets = {}
            for repo, active in active_by_repo.items():
                for real in active:
                    if real.lower() == needle:
                        targets[repo] = real
                        break
            if not targets:
                errors.append("`%s` 不是活跃发布分支（可能已停止维护）" % branch)
                continue
            # Label with the real spelling, not what was typed: it is echoed
            # back in the result reply and used as the dedup key.
            label = sorted(set(targets.values()))[0]

        if label in seen:
            continue
        seen.add(label)
        resolved.append((label, targets))
    return resolved, errors


# ── Execution ────────────────────────────────────────────────────────

def _glab_json(args, timeout_s=_API_TIMEOUT):
    """Run a glab api call, returning (data, error)."""
    try:
        # encoding= is mandatory (same hazard as discover_active_branches):
        # `text=True` alone decodes with the ANSI codepage, and a commit
        # message with Chinese in the JSON body raises UnicodeDecodeError
        # inside subprocess's reader thread — stdout comes back EMPTY while
        # returncode stays 0, so a successful cherry-pick reads as a failure
        # and the caller runs the MR fallback on top of the pick it just
        # landed. See DESIGN §1.24.
        result = su.hidden_run(["glab"] + args, capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)[:200]
    if result.returncode != 0:
        return None, (result.stderr or result.stdout or "").strip()[:300]
    try:
        return json.loads(result.stdout or "{}"), None
    except json.JSONDecodeError:
        return {}, None


def _urlenc(slug):
    return slug.replace("/", "%2F")


def cherry_pick_to_branch(repo_slug, sha, branch, ticket_id):
    """Cherry-pick *sha* onto *branch*, falling back to an MR.

    Returns {"mode": "direct"|"mr"|"failed", "branch", "url", "error"}.
    Direct is tried first because it is one call and needs no follow-up; it
    fails on a protected branch (the normal case for a release branch) and on
    conflict, and both degrade to a reviewable MR rather than an error the
    approver has to chase (DESIGN §1.24).
    """
    project = _urlenc(repo_slug)
    data, err = _glab_json([
        "api", "--method", "POST",
        "projects/%s/repository/commits/%s/cherry_pick" % (project, sha),
        "--field", "branch=%s" % branch,
    ])
    if err is None and data:
        return {"mode": "direct", "branch": branch,
                "url": data.get("web_url") or "", "error": None}
    direct_error = err or "cherry_pick returned no commit"

    # Fallback: cherry-pick onto a fresh branch cut from the target, then
    # open an MR. The API cherry-picks onto the new branch, so a conflict
    # here is the same conflict — reported, not silently merged.
    work_branch = "cherry-pick-%s-%s" % (ticket_id, branch)
    _, branch_err = _glab_json([
        "api", "--method", "POST",
        "projects/%s/repository/branches" % project,
        "--field", "branch=%s" % work_branch,
        "--field", "ref=%s" % branch,
    ])
    # An existing work branch (retry of the same request) is not fatal.
    if branch_err and "already exists" not in branch_err.lower():
        return {"mode": "failed", "branch": branch, "url": "",
                "error": "%s; branch create failed: %s"
                         % (direct_error, branch_err)}

    _, pick_err = _glab_json([
        "api", "--method", "POST",
        "projects/%s/repository/commits/%s/cherry_pick" % (project, sha),
        "--field", "branch=%s" % work_branch,
    ])
    if pick_err:
        return {"mode": "failed", "branch": branch, "url": "",
                "error": "%s; conflict on %s: %s"
                         % (direct_error, work_branch, pick_err)}

    mr, mr_err = _glab_json([
        "api", "--method", "POST",
        "projects/%s/merge_requests" % project,
        "--field", "source_branch=%s" % work_branch,
        "--field", "target_branch=%s" % branch,
        "--field", "title=%s: cherry-pick to %s" % (ticket_id, branch),
        "--field", "remove_source_branch=true",
    ])
    if mr_err:
        return {"mode": "failed", "branch": branch, "url": "",
                "error": "%s; MR create failed: %s" % (direct_error, mr_err)}
    return {"mode": "mr", "branch": branch,
            "url": (mr or {}).get("web_url") or "", "error": None}
