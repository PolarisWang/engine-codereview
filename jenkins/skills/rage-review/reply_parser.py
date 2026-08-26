"""
Parse reply content into a structured intent.

Two entry points:

- `classify_intent(content, sender_id, state, approver_open_ids)` — the
  routing-time classifier. Returns `(role, intent[, indices])` from
  {approver.approve (`ok`), approver.close, approver.full, approver.revision,
  developer.dev_triage, developer.dev_handoff, developer.dev_reply,
  developer.dev_question, ignored}. Approver verbs are gated by the
  namelist; dev tokens (`ok`, `@bot ...`) are content-only. `dev_triage`
  is gated to the DEV_TRIAGE state and the topic's developer
  (DESIGN §1.23.1); `dev_handoff` (`done`) is the developer's explicit
  "I'm finished, over to the approver" verb (DESIGN §1.23.7).

- `parse_approver_reply(content, state)` — back-compat wrapper used by
  `mechanical_reply_handler._classify` after the namelist gate. Same
  return shape as before.

Indices accept whitespace + ASCII comma + Chinese comma as separators:
`1 3 5`, `1,3,5`, `1，3，5`, `1, 3, 5` all parse to `[1, 3, 5]`.

Usage:
    python reply_parser.py --content "1 3 5" --state TRIAGE_DECISION
"""

import argparse
import json
import re
import sys


# Patterns (case-insensitive). Anchored to full-match for tight intent gates.
# Approval has a single verb: `ok` (DEV_REPLY_PATTERN below), gated to
# `_APPROVER_OK_STATES`. The legacy aliases `pass`/`通过`/`lgtm`/`approved`
# were removed (DESIGN §1.23.2) — they are no longer recognized.
ESCALATE_PATTERNS = re.compile(r"^(yes|完整审查|完整版|full\s*review|full)$", re.IGNORECASE)
CLOSE_PATTERNS = re.compile(r"^(no|close|关闭)$", re.IGNORECASE)
# Indices: digit-runs separated by whitespace, ASCII comma, or 中文逗号.
INDICES_PATTERN = re.compile(r"^\s*\d+(?:[\s,，]+\d+)*\s*$")
# Extended indices: also accepts `all`, `-N` (exclusions), and an optional `#`
# prefix (`#1`, `#1 3`, `#-2`) — users naturally write `#1` to refer to issue #1.
# Tokenized and validated by `parse_indices_with_mode` because the regex alone
# can't reject the ambiguous mixed-mode `1 -2` form.
_INDICES_EXT_TOKEN = r"#?(?:all|-?\d+)"
INDICES_EXT_PATTERN = re.compile(
    rf"^\s*{_INDICES_EXT_TOKEN}(?:[\s,，]+{_INDICES_EXT_TOKEN})*\s*$",
    re.IGNORECASE,
)
_TOKEN_SPLIT = re.compile(r"[\s,，]+")
# A single index-grammar token, used to find where the indices stop and a
# free-text reason begins (`allow_trailing_text`, DESIGN §1.23.8).
_INDEX_TOKEN = re.compile(r"^#?(?:all|-?\d+)$", re.IGNORECASE)
_NON_SEPARATOR_RUN = re.compile(r"[^\s,，]+")
_NONE_WITH_REASON = re.compile(
    r"^(none|0|不修)[\s,，]+(.*)$", re.IGNORECASE | re.DOTALL)
# Leading junk to strip off a captured reason (the separator that ended the
# index run is not part of the reason).
_REASON_LSTRIP = " 	，,、：:"
# Sole-token "reject all issues" form for dev triage (DESIGN §1.23.1).
# Only honored when `parse_indices_with_mode(..., allow_none=True)` —
# legacy approver paths keep rejecting `0` (issue indices start at 1).
NONE_PATTERN = re.compile(r"^(none|0|不修)$", re.IGNORECASE)
# Dev tokens.
DEV_REPLY_PATTERN = re.compile(r"^ok$", re.IGNORECASE)
# The developer's explicit hand-off to the approver (DESIGN §1.23.7). Under
# the self-service loop the dev drives rounds N+1 themselves, so nothing else
# ends the loop — without this verb a topic would iterate forever.
HANDOFF_PATTERN = re.compile(r"^(done|submit|提审|完成)$", re.IGNORECASE)
# States where `done` is meaningful: the dev is either sitting on a fresh
# round's issue list or part-way through fixing one. Excluded everywhere
# else (the topic is already in the approver's court, or terminal).
_HANDOFF_STATES = frozenset({
    "DEV_TRIAGE", "SIMPLE_REVISION", "FULL_REVISION",
})
# States where an approver `ok` means "approve" (the unified approval verb,
# DESIGN §1.23.2). In ARBITRATION it accepts the dev's triage; in
# TRIAGE_DECISION / AWAITING_APPROVAL / DEV_TRIAGE it is the final approval
# (or override). The *_REVISION states are deliberately excluded — there `ok`
# is the developer's "fixes pushed, re-review" trigger, so an approver `ok`
# must not be hijacked into a merge.
_APPROVER_OK_STATES = frozenset({
    "TRIAGE_DECISION", "AWAITING_APPROVAL", "ARBITRATION", "DEV_TRIAGE",
})
# `@bot ` prefix (trailing whitespace required to avoid `@bottom`-style false matches).
DEV_QUESTION_PREFIX = re.compile(r"^@bot(\s|$)", re.IGNORECASE)
# `@bot 同步` / `@bot refresh` / `@bot sync` — fetches manual review
# threads from GitLab + verifies against current HEAD. Open to anyone
# who can post (developer or approver). State-agnostic. See DESIGN §1.14.1.
MANUAL_REFRESH_PATTERN = re.compile(
    r"^@bot\s+(同步|refresh|sync)\s*$", re.IGNORECASE)
# Post-merge cherry-pick reply (approver, MERGED state only). The bot posts
# the live token→branch mapping on merge, so the approver answers with bare
# tokens: `p1`, `p1 p2`, or a full `rc_next_p2` / `rc_dev_p4`. An optional `cherrypick`
# verb is tolerated but never required — the prompt asks for the token.
# Resolution against the *active* branch set happens in `cherrypick.py`;
# this module stays I/O-free, so it only shapes the tokens.
_CHERRYPICK_VERB = re.compile(r"^(?:cherry-?pick|cp|摘取)\s+", re.IGNORECASE)
# One token: `p1`, `rc_p1`, `rc_next_p1`, `rc_dev_p1`, or the namespaced
# `rage/rc_p1` (chaos keeps its release branches under `rage/`, matching its
# `rage/master` default). Keep the variant list in sync with
# `cherrypick._VARIANT` — a spelling this accepts but that one rejects would
# reach `resolve_tokens` as an unknown token.
_CP_TOKEN = r"(?:p\d+|(?:[\w.\-]+/)*rc_(?:next_|dev_)?p\d+)"
_CHERRYPICK_TOKENS = re.compile(
    r"^%s(?:[\s,，]+%s)*$" % (_CP_TOKEN, _CP_TOKEN), re.IGNORECASE)
# Declining the offer. Kept separate from CLOSE_PATTERNS: in MERGED the MRs
# are already merged, so routing `no` to the close handler would try to close
# merged MRs and post a close notice on a merged topic.
CHERRYPICK_SKIP = re.compile(
    r"^(?:no|skip|不用|不需要|跳过)$", re.IGNORECASE)
_TOKEN_SEPARATORS = re.compile(r"[\s,，]+")


def parse_cherrypick_branches(content):
    """Return the token list from a cherry-pick reply, or None if not one.

    De-duplicated, order-preserving. Tokens are returned verbatim (`p1`,
    `rc_next_p2`) — mapping a number onto a branch needs the live active set,
    which only `cherrypick.resolve_tokens` has.
    """
    if not content:
        return None
    body = _CHERRYPICK_VERB.sub("", content.strip()).strip()
    if not body or not _CHERRYPICK_TOKENS.match(body):
        return None
    seen = []
    for token in _TOKEN_SEPARATORS.split(body):
        token = token.strip()
        if token and token not in seen:
            seen.append(token)
    return seen or None


def parse_indices(content):
    """Extract issue indices from content like '1 3 5', '1,3,5', '1，3，5'."""
    nums = re.findall(r"\d+", content)
    return sorted(set(int(n) for n in nums))


def _clean_reason(text):
    """Strip the separator that ended the index run off a captured reason."""
    return (text or "").lstrip(_REASON_LSTRIP).strip()


def parse_indices_with_mode(content, allow_none=False,
                            allow_trailing_text=False):
    """Parse extended revision input — `1 3 5`, `all`, `-1 -3`, `all -2 -4`.

    Returns one of:
        {"indices": [int, ...], "exclude": False, "none": False, "reason": str}
        {"indices": [int, ...], "exclude": True,  "none": False, "reason": str}
        {"indices": [],         "exclude": False, "none": True,  "reason": str}
        None                                       # not a valid revision input

    `exclude=True, indices=[]` means "fix every issue" (the `all` form).
    `exclude=True, indices=[3, 5]` means "fix every issue except #3 and #5"
    (whether spelled `-3 -5` or `all -3 -5`).

    `allow_none=True` (dev triage, DESIGN §1.23.1) additionally accepts a
    sole token `none` / `0` / `不修` meaning "reject every issue" — the
    symmetric counterpart of `all`. Default off so legacy approver paths
    keep rejecting `0`.

    `allow_trailing_text=True` (dev triage, DESIGN §1.23.8) lets the
    developer append a free-text reason after the indices — `-2 -3 这两个是
    误报`. The index grammar itself is unchanged: parsing stops at the first
    token that is not an index token and everything after it comes back as
    `reason` (empty string when absent). At least one index token is still
    required, so plain prose stays unparseable and falls through to
    `ignored`. Separation must be whitespace or a comma — `-1误报` is a
    single non-index token, not an index plus a reason. Off by default so
    every approver path keeps today's strict full-match behaviour.

    Rejects ambiguous mixed-mode inputs (positives + negatives like
    `1 -2`, or `all` combined with positives like `all 1`) — those
    return None so the caller falls through to the unknown path. Zero
    or negative-zero (`0` / `-0`) are rejected because issue indices
    start at 1; mixing them in would silently drop the intent.
    """
    if content is None:
        return None
    stripped = content.strip()
    if not stripped:
        return None

    # `none` is a sole token, so it has to be recognised before the index
    # scan below - `0` is a valid index token to the scanner and would be
    # consumed there, then rejected as a non-positive index.
    if allow_none:
        if NONE_PATTERN.match(stripped):
            return {"indices": [], "exclude": False, "none": True,
                    "reason": ""}
        if allow_trailing_text:
            m = _NONE_WITH_REASON.match(stripped)
            if m:
                return {"indices": [], "exclude": False, "none": True,
                        "reason": _clean_reason(m.group(2))}

    reason = ""
    if allow_trailing_text:
        head_end = 0
        count = 0
        for m in _NON_SEPARATOR_RUN.finditer(stripped):
            if not _INDEX_TOKEN.match(m.group(0)):
                break
            head_end = m.end()
            count += 1
        if not count:
            return None
        reason = _clean_reason(stripped[head_end:])
        stripped = stripped[:head_end]

    if not INDICES_EXT_PATTERN.match(stripped):
        return None
    has_all = False
    positives = []
    negatives = []
    for tok in _TOKEN_SPLIT.split(stripped):
        if not tok:
            continue
        # strip optional `#` prefix (users write #1 / #1 3)
        if tok.startswith("#"):
            tok = tok[1:]
        low = tok.lower()
        if low == "all":
            has_all = True
            continue
        if tok.startswith("-"):
            try:
                n = int(tok[1:])
            except ValueError:
                return None
            if n <= 0:
                return None
            negatives.append(n)
        else:
            try:
                n = int(tok)
            except ValueError:
                return None
            if n <= 0:
                return None
            positives.append(n)
    if positives and negatives:
        return None  # `1 -2` is ambiguous — caller falls through
    if has_all and positives:
        return None  # `all 1` is contradictory
    if negatives or has_all:
        return {"indices": sorted(set(negatives)), "exclude": True,
                "none": False, "reason": reason}
    if positives:
        return {"indices": sorted(set(positives)), "exclude": False,
                "none": False, "reason": reason}
    return None


def classify_intent(content, sender_id, state, approver_open_ids,
                    developer_id=None, triage=None):
    """Classify a reply by content (and approver namelist + topic dev).

    Args:
        content: Raw message text (Lark @-mentions already stripped by router).
        sender_id: Lark open_id of the sender.
        state: Current topic state. Gates `full` (TRIAGE_DECISION or
            ARBITRATION-on-simple), `dev_triage` (DEV_TRIAGE), and the
            unified `ok`-as-approve verb (any state in
            `_APPROVER_OK_STATES`). See DESIGN §1.23.1–.2.
        approver_open_ids: list[str] of authorized approver open_ids.
        developer_id: open_id of the topic's dev (identity.creator_open_id).
            Used to authorize `close` from the dev (so the dev who filed the
            topic can also close it) and `dev_triage`. None disables both.
        triage: review.triage ("simple"|"complex") — only consulted to gate
            `full` escalation from ARBITRATION.

    Returns:
        dict with keys: role ("approver"|"developer"|"ignored"),
                         intent (str|None),
                         indices (list[int], only for revision/dev_triage;
                         those two also carry exclude/none flags).
    """
    if content is None:
        return {"role": "ignored", "intent": None, "indices": []}

    stripped = content.strip()
    if not stripped:
        return {"role": "ignored", "intent": None, "indices": []}

    sender_is_approver = bool(approver_open_ids) and sender_id in approver_open_ids
    sender_is_developer = bool(developer_id) and sender_id == developer_id

    # 0. DEV_TRIAGE — the developer triages the round-1 issue list
    #    (DESIGN §1.23.1). Checked BEFORE the approver block so an operator
    #    who is both the dev and an approver gets indices resolved as
    #    dev_triage, not approver revision.
    if state == "DEV_TRIAGE" and sender_is_developer:
        rev = parse_indices_with_mode(stripped, allow_none=True,
                                      allow_trailing_text=True)
        if rev is not None:
            return {"role": "developer", "intent": "dev_triage",
                    "indices": rev["indices"], "exclude": rev["exclude"],
                    "none": rev["none"], "reason": rev.get("reason", "")}

    # 0b. MERGED — the post-merge cherry-pick window (DESIGN §1.24). Cherry-pick
    #     is approver-only: it writes to release branches, so the dev cannot
    #     self-serve it.
    #
    #     Everything else in MERGED is DROPPED here, and that exit is
    #     load-bearing (DESIGN §1.24.3). MERGED is in `router._REPLY_STATES`
    #     for the 24 h window, so without it an approver `close` would fall
    #     through to the state-independent close below and a dev `ok` to
    #     `dev_reply` — both then fail `mechanical_reply_handler`'s
    #     `_MERGED_STATE_INTENTS` gate, bounce to the topic agent, find no
    #     MERGED row in its state×intent table, and sit in `events.pending`
    #     re-spawning an agent every dispatch cycle until the window expires.
    #     The topic is finished; the only question it can still answer is the
    #     cherry-pick offer.
    if state == "MERGED":
        if sender_is_approver:
            if CHERRYPICK_SKIP.match(stripped):
                return {"role": "approver", "intent": "cherrypick_skip",
                        "indices": [], "branches": []}
            branches = parse_cherrypick_branches(stripped)
            if branches:
                return {"role": "approver", "intent": "cherrypick",
                        "indices": [], "branches": branches}
        return {"role": "ignored", "intent": None, "indices": []}

    # 1. Approver intents — gated by namelist.
    if sender_is_approver:
        # `ok` is the ONLY approver approval verb (DESIGN §1.23.2). It resolves
        # to `approve` in every decision state (`_APPROVER_OK_STATES`); the
        # revision states are excluded (there `ok` is the dev's re-review
        # trigger). No `pass`/`通过`/`lgtm` aliases.
        if state in _APPROVER_OK_STATES and DEV_REPLY_PATTERN.match(stripped):
            return {"role": "approver", "intent": "approve", "indices": []}
        if CLOSE_PATTERNS.match(stripped):
            return {"role": "approver", "intent": "close", "indices": []}
        # Under the self-service loop the approver first sees the topic in
        # AWAITING_APPROVAL, so escalation has to be reachable from there
        # too (DESIGN §1.23.9). ARBITRATION is retained for in-flight topics.
        if ESCALATE_PATTERNS.match(stripped) and (
                state == "TRIAGE_DECISION"
                or (state in ("ARBITRATION", "AWAITING_APPROVAL")
                    and triage == "simple")):
            return {"role": "approver", "intent": "escalate", "indices": []}
        # Approver indices in DEV_TRIAGE are ignored — the approver must
        # wait for arbitration (DESIGN §1.23.1).
        if state != "DEV_TRIAGE":
            rev = parse_indices_with_mode(stripped)
            if rev is not None:
                return {"role": "approver", "intent": "revision",
                        "indices": rev["indices"], "exclude": rev["exclude"]}

    # 2. Developer `close` — only from the topic's dev (not random senders).
    #    Lets the developer close their own topic without bouncing through
    #    the approver. Approver-close already handled above when sender is
    #    in the approver namelist.
    if sender_is_developer and CLOSE_PATTERNS.match(stripped):
        return {"role": "developer", "intent": "close", "indices": []}

    # 2b. Dev hand-off (`done`) - ends the self-service loop and puts the
    #     topic in the approver's court (DESIGN §1.23.7). Gated to the
    #     topic's developer: a bystander must not be able to submit
    #     someone else's work for final review. Checked before the generic
    #     dev tokens so it wins over nothing, but ordering is kept explicit
    #     for the same reason `dev_triage` sits at the top.
    if (sender_is_developer and state in _HANDOFF_STATES
            and HANDOFF_PATTERN.match(stripped)):
        return {"role": "developer", "intent": "dev_handoff", "indices": []}

    # 3. Dev tokens (ok / @bot ...) — open to anyone (tokens are
    #    self-evident; an approver typing `ok` triggers a re-review,
    #    that's fine). In DEV_TRIAGE/ARBITRATION `ok` is NOT a dev_reply:
    #    the dev must triage with indices / the approver must arbitrate
    #    (a dev `ok` must not self-accept their own triage, §1.23.2).
    if state not in ("DEV_TRIAGE", "ARBITRATION") \
            and DEV_REPLY_PATTERN.match(stripped):
        return {"role": "developer", "intent": "dev_reply", "indices": []}
    # `@bot 同步` is a more specific match than the general `@bot ...`
    # question prefix, so it must be checked first.
    if MANUAL_REFRESH_PATTERN.match(stripped):
        # role="any" so the consumer can grant access without going
        # through the developer-vs-approver authorization split.
        return {"role": "any", "intent": "manual_refresh", "indices": []}
    if DEV_QUESTION_PREFIX.match(stripped):
        return {"role": "developer", "intent": "dev_question", "indices": []}

    # 4. Otherwise drop.
    return {"role": "ignored", "intent": None, "indices": []}


def parse_approver_reply(content, state):
    """
    Parse approver reply into intent.

    Args:
        content: Message text from the approver.
        state: Current topic state (TRIAGE_DECISION or AWAITING_APPROVAL).

    Returns:
        dict with keys:
            intent: "approve" | "escalate" | "revision" | "close" | "unknown"
            indices: list of ints (only for revision intent)
    """
    content = content.strip()

    # `close`/`no`/`关闭` is a state-independent hard-terminate — valid in any
    # non-terminal state. Check it before the per-state branches so a legacy
    # (router-unstamped) close queued in an earlier state (e.g. an approver
    # typing `close` during TRIAGING, carried forward into DEV_TRIAGE) still
    # drains mechanically instead of hitting the `unknown` fallback and
    # re-listing every cycle. The decision states below already returned close
    # for CLOSE_PATTERNS, so this only changes the DEV_TRIAGE/ARBITRATION path.
    if CLOSE_PATTERNS.match(content):
        return {"intent": "close", "indices": []}

    if state == "TRIAGE_DECISION":
        if DEV_REPLY_PATTERN.match(content):
            return {"intent": "approve", "indices": []}
        if ESCALATE_PATTERNS.match(content):
            return {"intent": "escalate", "indices": []}
        if CLOSE_PATTERNS.match(content):
            return {"intent": "close", "indices": []}
        rev = parse_indices_with_mode(content)
        if rev is not None:
            return {"intent": "revision",
                    "indices": rev["indices"], "exclude": rev["exclude"]}
        return {"intent": "unknown", "indices": []}

    elif state == "AWAITING_APPROVAL":
        if DEV_REPLY_PATTERN.match(content):
            return {"intent": "approve", "indices": []}
        if CLOSE_PATTERNS.match(content):
            return {"intent": "close", "indices": []}
        rev = parse_indices_with_mode(content)
        if rev is not None:
            return {"intent": "revision",
                    "indices": rev["indices"], "exclude": rev["exclude"]}
        return {"intent": "unknown", "indices": []}

    elif state in ("SIMPLE_REVISION", "FULL_REVISION"):
        # Approver override during revision: only `close` is honored.
        # There is no approve verb here — `ok` is the developer's "fixes
        # pushed, re-review" trigger, and the `pass` alias was removed
        # (DESIGN §1.23.2). The approver approves at the round-N decision
        # state instead.
        if CLOSE_PATTERNS.match(content):
            return {"intent": "close", "indices": []}
        return {"intent": "unknown", "indices": []}

    # DEV_TRIAGE / ARBITRATION events are always stamped by the router
    # (the states postdate ingest-time stamping), so this legacy fallback
    # never sees them — return unknown defensively rather than guess.
    return {"intent": "unknown", "indices": []}


def main():
    p = argparse.ArgumentParser(description="Parse approver reply intent")
    p.add_argument("--content", required=True, help="Approver message content")
    p.add_argument("--state", required=True, help="Current topic state")
    args = p.parse_args()

    result = parse_approver_reply(args.content, args.state)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()