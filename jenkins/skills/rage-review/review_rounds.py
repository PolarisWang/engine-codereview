# -*- coding: utf-8 -*-
"""Which flagged issues does round N+1 still have to look at?

One home for the "settled" predicate, because two very separate places need to
agree on it: `mechanical_reply_handler` (rebuilding `review.flagged_issues`
when the approver replies with indices) and `render_spawn_prompt` (telling the
agent what to verify). If they disagreed, the agent would either re-check an
issue the ledger calls fixed or skip one nobody has looked at.

See DESIGN §1.4.8.
"""

# Verdicts that settle an issue for good. `addressed` = the developer fixed it;
# `obsolete` = the code it described is gone (refactored away), which the agent
# records rather than forcing a push-back. The other three — `not_addressed`,
# `partially_addressed`, `unclear` — all still need looking at next round.
SETTLED_VERDICTS = ("addressed", "obsolete")

# Fields that make up a verdict, copied as a unit when carrying one forward.
_VERDICT_FIELDS = ("verification", "verification_rationale",
                   "verified_at_sha", "marked_resolved_at")


def is_settled(entry):
    """True if this flagged-issue entry already earned a final verdict."""
    return (isinstance(entry, dict)
            and entry.get("verification") in SETTLED_VERDICTS)


def _by_index(entries):
    out = {}
    for entry in (entries or []):
        if isinstance(entry, dict) and entry.get("index") is not None:
            out[entry["index"]] = entry
    return out


def carry_verification(new_flagged, prev_flagged):
    """Copy settled verdicts from the previous flagged set onto a rebuilt one.

    `flagged_issues` is rebuilt from `review.issues` every time the approver
    replies with indices, and `review.issues` holds the pristine round-1 entries
    with no verdict on them — so without this merge, a rebuild silently reset
    every verdict and round N+1 re-verified issues round N had already confirmed
    fixed. That reset is the bug; this merge is the fix (DESIGN §1.4.8).

    Only *settled* verdicts carry. An unsettled one (`not_addressed`,
    `partially_addressed`, `unclear`) is deliberately dropped: the developer has
    pushed since, so the old verdict describes stale code and the issue is due
    for a fresh look anyway.

    Mutates and returns `new_flagged` (entries are already fresh copies).
    """
    previous = _by_index(prev_flagged)
    for entry in (new_flagged or []):
        if not isinstance(entry, dict):
            continue
        old = previous.get(entry.get("index"))
        if old is None or not is_settled(old):
            continue
        for field in _VERDICT_FIELDS:
            if field in old:
                entry[field] = old[field]
    return new_flagged


def split_flagged_for_verification(flagged):
    """Split `review.flagged_issues` into (to_verify, carried) for round N+1.

    Once a round confirms issue #m fixed, re-checking it every subsequent round
    costs a file re-grep and invites a verdict flip on unchanged code. The
    settled ones are carried forward with the verdict they already earned; only
    the unsettled ones are re-verified.

    Pure and index-ordered so the spawn spec, the post, and the audit all agree
    on the same split. An entry with no `verification` key is unverified (round
    1's output, or an issue the approver flagged for the first time) and always
    re-verifies.
    """
    to_verify, carried = [], []
    for entry in (flagged or []):
        if not isinstance(entry, dict):
            continue
        (carried if is_settled(entry) else to_verify).append(entry)

    def key(entry):
        return (entry.get("index") is None, entry.get("index"))

    return sorted(to_verify, key=key), sorted(carried, key=key)


def indices(entries):
    """Index list for display, skipping entries that somehow lack one."""
    return [e.get("index") for e in (entries or [])
            if isinstance(e, dict) and e.get("index") is not None]
