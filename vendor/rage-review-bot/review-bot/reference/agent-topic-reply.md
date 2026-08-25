# Replying to a review topic from another agent

For an agent that is **not** the review bot — a coding agent that just pushed a fix
and wants to drive the review forward without a human relaying tokens in Lark.

You need exactly one thing to start: the **topic id**. The bot prints it in its first
reply on every topic (`话题 ID: om_...`), so the developer can copy it out and hand it
to you. It is the Lark thread's root message id, and it is also the topic file's name
on disk.

```
话题 ID: om_x100b6760632fd4a8c2ee9b65c09322a
```

## The one thing that will silently break you

**Post as the user, never as the bot.** `router._is_self_message` (`scripts/router.py`)
drops any message whose sender is the bot's own open_id, before classification. A reply
sent with `--as bot` is not "ignored with a warning" — it never becomes an event at all,
and the topic sits exactly where it was.

Two further consequences of how replies are authorized:

- `dev_triage` (issue indices) and `dev_handoff` (`done`) are gated to
  `identity.creator_open_id` — the topic's developer. The account you post as **must be
  that developer**, or the reply is classified `ignored` and dropped.
- `ok` and `@bot …` are content-only and accepted from anyone, so they work from any
  account. That asymmetry is easy to mistake for "everything works" until the first
  `done` disappears.

`scripts/send_reply.py` is bot-identity by design (it is how the bot posts its own
replies). **Do not reuse it to drive a topic** — see the warning in its docstring.

## Prerequisite: one-time scope grant

Posting as the user needs the `im:message.send_as_user` scope. Without it the CLI fails
before sending, with `missing required scope(s)`.

> Granted and verified on this workspace (2026-08-20) — a user-identity send returns a
> `message_id`. The rest of this section is for a fresh machine or a re-auth.

Check first — this costs nothing and prints the request instead of sending it:

```bash
lark-cli im +messages-reply --as user --message-id om_probe \
  --reply-in-thread --text probe --dry-run
```

If it reports a missing scope, the operator must re-authorize. Two things make this
less obvious than it looks:

- **The grant has two layers.** Approving the scope in the console raises the *app*
  grant, but a token only carries the scopes it was minted with — the probe keeps
  failing until a fresh `auth login`. Seeing the scope in `auth scopes` is not evidence
  the token has it.
- **Re-running `auth login` replaces the token's whole scope set.** Logging in with just
  the two IM scopes silently drops the ~145 base/docs/task scopes other skills depend
  on. Read the current set from `lark-cli auth scopes --format json` (`userScopes`) and
  request the union.

The login is a device flow: `auth login --scope "<union>" --no-wait --json` returns a
`verification_url` + `device_code` (~10 min TTL); the operator authorizes in a browser,
then `auth login --device-code "<code>"` completes it. Don't block on `--device-code` in
the same turn that shows the URL, and mint a fresh code per attempt — restarting the
flow invalidates the previous one.

## Sending a reply

```bash
lark-cli im +messages-reply \
  --as user \
  --message-id "$TOPIC_ID" \
  --reply-in-thread \
  --msg-type text \
  --text "1 3 5"
```

`--message-id` takes the **topic id**, not the id of the message you are answering;
`--reply-in-thread` puts it in the thread stream. This is the same call the bot makes
for its own plain-text posts, so a topic id is always a valid target.

Add `--idempotency-key` if your agent might retry — a duplicate `ok` costs a whole
extra review round.

## What to say, and when

Read the state first (next section); the same token means different things — or nothing
— in different states.

| State | Token | Effect |
|---|---|---|
| `DEV_TRIAGE` | `1 3 5` | Fix these; everything else open this round is recorded as disputed |
| `DEV_TRIAGE` | `all` | Fix everything still open |
| `DEV_TRIAGE` | `-2` / `-1 -3` | Dispute just these, fix the rest |
| `DEV_TRIAGE` | `-2 <reason>` | Same, with a free-text reason carried to the approver at hand-off |
| `DEV_TRIAGE` | `none` / `不修` | Dispute everything still open |
| `SIMPLE_REVISION` / `FULL_REVISION` | `ok` | "Pushed, re-review" — **push first** (see below) |
| `DEV_TRIAGE` / `*_REVISION` | `done` | Hand off to the approver; ends the loop |
| any reply state | `@<bot> <question>` | Ask a question; does not change state |
| any reply state | `close` / `关闭` | Close the MRs and the topic |

Rules worth encoding in your agent rather than rediscovering:

- **`ok` without a new commit is wasted.** `drain_no_new_commits` compares
  `git ls-remote` against `review.last_review_commit`; if nothing advanced the bot
  replies `未检测到新提交` and drops the event. Push, *then* reply.
- **Indices are validated against the still-open set**, not the round-1 list. From
  round 2 on, issues already verified fixed and issues already disputed are not up for
  decision, and naming one is an invalid index (the bot posts a correction and drops the
  event). Read `review.flagged_issues` + `review.dev_triage` to know what is open.
- **Naming a subset does not retract the rest.** From round 2 on, an issue already in
  `accepted_indices` stays accepted unless you explicitly `-N` it — replying `1` means
  "this is the one I'm fixing now", not "I dispute the others". Round 1 is the exception:
  there, unnamed really does mean disputed.
- **You can take a dispute back by naming it.** An index in `rejected_indices` is still
  valid to name, and naming it moves it back to accepted. `all` will not do this — it
  only covers issues that are still open — so retracting is always explicit.
- **A reinstated issue cannot be disputed again.** Once the approver puts an issue back
  (`dev_triage.reinstated_indices`), replying `-N` for it is refused.
- **Anything not in the table is dropped at ingest** (`reply_intent_ignored`). Free-form
  prose is not a fallback — there is no natural-language path except `@bot`.

## Reading topic state

The topic file is the source of truth. It is JSON, named by the topic id:

```bash
cat .claude/skills/review-bot/cfg/topics/$TOPIC_ID.json
```

Fields you will actually want:

| Path | Meaning |
|---|---|
| `review.state` | Which tokens are valid right now |
| `review.review_round` | Rounds completed so far |
| `review.issues[]` | Round-1 issue list, `index` is the `#N` you reply with |
| `review.flagged_issues[]` | Live ledger; `verification` = `addressed` / `not_addressed` / … |
| `review.dev_triage` | `accepted_indices`, `rejected_indices`, `reinstated_indices`, `reasons[]` |
| `events.pending[]` | Your reply, before it has been drained |
| `identity.creator_open_id` | The developer — the account you must post as |

**Read it, never write it.** The dispatcher owns these files and takes a lock per topic;
a concurrent write corrupts state. Likewise, never invoke `dispatcher.py`,
`mechanical_reply_handler.py`, or `monitor_dispatch.py` to "make it go faster" — the
running daemon owns that loop, and a second one racing it produces duplicate Lark posts.

A topic that has finished moves to `cfg/topics/closed/`.

## Timing

Replying is asynchronous. Your message is picked up by the listener, routed on the next
dispatcher cycle, and only then drained — mechanical verbs (indices, `done`, `close`)
resolve in that cycle; a new review round takes as long as the review does, routinely
10+ minutes on a large diff.

So: reply, then poll `review.state` (or `events.pending` draining to empty) with a sane
interval. Do not re-send because nothing happened in ten seconds — a duplicate token is
a duplicate action.

## Minimal loop

```
1. read cfg/topics/$TOPIC_ID.json  → review.state
2. DEV_TRIAGE          → reply with the indices you will fix
   *_REVISION          → make the fixes, push, reply `ok`
   DEV_TRIAGE, done    → reply `done` when the ledger looks right
   AWAITING_APPROVAL   → stop; it is the approver's call now
3. poll until review.state changes, then repeat
```

Stop at `AWAITING_APPROVAL`, `APPROVED`, `MERGED`, `CLOSED`. There is no developer verb
that moves a topic out of the approver's court — if the approver sends work back, the
topic returns to `*_REVISION` on its own and the loop resumes.

See DESIGN §1.23 for the state machine and §1.25 for why the topic id is published.
