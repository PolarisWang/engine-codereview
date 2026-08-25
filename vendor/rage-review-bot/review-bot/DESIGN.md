# Review-Bot Design

Architectural reference and pitfall catalog for the `/review-bot` skill.
Read before modifying pipeline, event routing, merge logic, or schema.

`SKILL.md` is the operator-facing spec (how to run the bot, command
contract, templates). This file covers the *why* and the footguns.

**Docs kept in sync**: `SKILL.md` (English operator spec, canonical),
`SKILL_zh.md` (Chinese mirror, also uploaded to the Lark doc), and
this `DESIGN.md` (architecture + footguns). Any behavioural change
touches all three — see `SKILL.md` → "Docs to keep in sync" for the
upload helper and the review checklist.

---

## 1. Feature Inventory


### 1.1 Lifecycle commands (`parse_args.py`, `resolve_start.py`)

| Command | Effect |
|---------|--------|
| `start` | Resolve env, start listener + daemon + monitor, create Lark Base if missing, post greeting |
| `status` | Snapshot of active topics, listener, recent log lines |
| `stop` | Kill listener + daemon, print final status |
| `recover` | Force-reconcile every open topic against the GitLab API |

#### 1.1.1 The daily cycle is a restart at startup, not an overnight shutdown  (was §3.22)

**History.** The respawn race once made an external kill futile: the daemon
(`poll_dispatch.py`) runs a listener health-check that respawns a dead-PID
listener every cycle, so a naive Windows Task Scheduler job that killed the
*listener* saw it resurrected within one cycle (RAGE-12657). The original fix
routed the stop through `CronCreate` — fire `/review-bot stop` AS A PROMPT into
the parent session so the in-session handler did the teardown.

**Current design — the stop is the first half of the next morning's start.**
`scheduling/launch_review_bot.ps1` (the `ClaudeReviewBot` task, **daily 08:00**)
runs `stop_review_bot.ps1 -Silent` in-process, then starts the new session. The
separate `ClaudeReviewBotStop` task is **disabled**, not deleted — re-enable it
with `schtasks /change /tn ClaudeReviewBotStop /enable` if a true overnight
shutdown is ever wanted again.

The race is eliminated not by *where* the stop runs but by *kill order*:
`stop_review_bot.ps1` kills the **daemon first**, then the listener, monitor,
and every registered `claude.exe` session (§1.1.5). With the daemon dead before
the listener, the health-check is gone and cannot respawn anything — the same
daemon-first invariant `stop_bot.py` relies on (§1.1.2). The old session is idle
by then (its Monitor wrapper just reports a script-failed exit when its process
is killed) and is terminated via `cfg/sessions.json` + the legacy `session.pid`,
image-guarded to `claude.exe` (§1.1.5).
`-Silent` suppresses the `已停止` farewell: the greeting follows seconds later,
and from the group's point of view the bot never stopped. A failing stop is
logged to `cfg/session.restart.log` and does **not** abort the start — the
singleton pid-file guards make a survivor a no-op rather than a duplicate.

**Why the shutdown became a restart.** The nightly stop existed for one reason:
the parent session's context fills up over a day of dispatching. Measured on a
real spawn, each topic costs the parent ~3.5 K tokens it never needs again — the
Monitor notification, the echoed `render_spawn_prompt` spec, the same prompt
repeated into the `Agent` call, and the agent's result summary. Thirty spawns is
~100 K. But *stopping* was never the goal; the context reset was. Stopping also
cost real work: an overnight push, an approver reply, or a merge-queue drain sat
undone from 02:00 to 10:30 — §1.2.8 records a dev-triage reply lost to exactly
that window. Folding the stop into the start keeps the reset and gives the
overnight hours back. 08:00 was chosen as the restart hour because a review is
very unlikely to be in flight then; the window where a restart could interrupt
one is what the hour is optimizing, not the downtime (there is none).

**Why not drop the session entirely.** A daemon-spawned `claude -p` per work
item would end the context problem outright (and `-p` does run on the OAuth
subscription — verified; it does not require an API key). It was rejected
deliberately: the parent session is the operator surface. When the pipeline hits
undefined behaviour, that session is where it gets diagnosed and fixed live —
as with the phase-reset gap in §1.6.12, which presented as a silently idle
topic with no error anywhere. A bot with no attached operator would have sat
stuck indefinitely. Keeping the session means per-spawn context cost is worth
attacking directly, so the day's remaining context stays available for exactly
that debugging.

**Why drop the cron.** It added a second, session-scoped stop path that had to
be kept in sync with the Task Scheduler one, plus a `cfg/stop_cron.id` lifecycle
across `/clear`. Once the kill order made the external stop correct, the cron was
redundant. `start` no longer registers it; `stop` deletes a stale
`cfg/stop_cron.id` only as one-time cleanup (SKILL.md §2e / `stop` mode).

**Registration** (`scheduling/`, moved from the old `Desktop/Review Bot/`):
`register_review_bot_task.ps1` creates the one live task, `ClaudeReviewBot`
(daily 08:00, `StopExisting`). Run `stop_review_bot.ps1` by hand for an
emergency stop. `register_review_bot_stop_task.ps1` still exists for the
retired `ClaudeReviewBotStop` task but is not part of the loop.

Registration goes through **`schtasks.exe`, never `Register-ScheduledTask`**.
Every `*-ScheduledTask` cmdlet is CIM-backed and CIM intermittently fails here
with "The paging file is too small for this operation to complete" — the same
desktop-heap exhaustion that breaks `tasklist`/`taskkill` (§1.1.2). Observed
2026-08-04: `Register-ScheduledTask` failed with exactly that, the script
printed `[OK] ... registered`, and the task silently kept its previous trigger.
`register_review_bot_task.ps1` now writes the XML to a UTF-16 temp file, calls
`schtasks /create /xml … /f`, checks `$LASTEXITCODE`, and **reads the trigger
back out of Task Scheduler** to print it — a claim of success is only made
against observed state. `register_review_bot_stop_task.ps1` was left on the old
CIM path and carries the same latent false-OK; fix it if the stop task is ever
revived.

---

#### 1.1.2 `stop` must kill by process-scan, daemon-first — not just the pid files  (was §3.38)

The obvious `stop` ("read `listener.pid` + `daemon.pid`, `taskkill` both,
delete the files") is unreliable and silently leaves the bot running.
Observed 2026-05-30: a `/review-bot stop` killed the two recorded pids,
reported success, and the listener was still alive afterwards.

Two mechanisms defeat a pid-file-only kill:

1. **The daemon respawns the listener under a new pid.**
   `dispatcher._restart_listener` (the listener health-check) spawns a fresh
   `lark-cli event +subscribe` whenever the listener looks unhealthy and
   rewrites `listener.pid`. So the pid you read can be stale the instant you
   read it — you kill the old listener, the daemon has already started a new
   one, and `listener.pid` now points at a process you never killed.

2. **A second / orphaned daemon survives.** A prior-session
   `poll_dispatch.py --watch` (or one the singleton check didn't catch) can be
   running alongside the one recorded in `daemon.pid`. Kill the recorded
   daemon and the orphan keeps respawning the listener every health-check.

Fix: `scripts/stop_bot.py` does a **process-scan teardown**, trusting both
the pid files AND a command-line scan (`subprocess_util.iter_processes` —
Win32 `EnumProcesses` + a direct PEB read on Windows, `ps -eo pid=,args=` on
POSIX), matched against specific signatures (`poll_dispatch.py`,
`event +subscribe` / `im.message.receive_v1`, `monitor_dispatch.py`). Three
properties make it correct:

- **Kill order is load-bearing: daemons first.** Every pass kills the
  daemon(s) before the listener, so nothing can respawn the listener between
  the listener-kill and the verify scan.
- **Loop until a scan is clean.** A listener the daemon respawned just before
  the daemon died is caught on the next pass (the daemon is now gone, so no
  further respawns). It loops up to `--max-passes` (default 5) with a short
  settle between passes, then does a final verification scan and reports any
  `survivors[]` with their pids + command lines.
- **A broken scan is not an empty scan, and a survivor keeps its pid file.**
  See §1.1.4 — both were violated in the original implementation.

The signatures are deliberately narrow so unrelated `python.exe` / `node.exe`
on the machine never match. `stop_bot.py` kills the `monitor_dispatch.py`
*process*, which is enough to retire the Claude-Code Monitor *task* wrapper
too: with its script killed out from under it, the wrapper fires a benign
`Monitor script failed (exit 1)` notification and **deregisters itself**. So
the parent session needs **no** `TaskStop` — one issued afterward always
returned `No task found` (a guaranteed no-op), and the in-session stop
procedure dropped it. The scheduled-stop path can't call `TaskStop` at all
and has always relied on this same self-deregister.

(The daily auto-stop drives this same path via the Windows Task Scheduler stop
task — §1.1.1 — not an in-session cron, which was removed.)

---

#### 1.1.3 `recover.py` must scan via `iter_topic_files` and archive via `archive_topic` (RAGE-16281)  (was §3.41)

`recover.py` was the **only** scanner that walked `cfg/topics/` with a raw
`glob("om_*.json")` instead of `topic_store.iter_topic_files()`. That glob
also matches `om_<thread>.inbox.json` — a JSON **list**, not a topic dict —
and the very next line (`topic.get("identity", …)`) is **outside** the
read try/except, so a single inbox file made the entire `reconcile()` abort
with `AttributeError: 'list' object has no attribute 'get'` (exit 1, zero
topics reconciled). Inbox files exist transiently between a router write and
the dispatcher's `_drain_inboxes`, but they **accumulate** exactly when the
daemon is dead/stuck — i.e. the primary scenario recover is run in — so
recover was broken in its main use case. Fix: iterate `iter_topic_files`
(the centralized filter that skips `.inbox.json` / `.tmp-` / non-`om_`) and
defensively skip any non-dict file.

The terminal path had a second, quieter bug: it hand-rolled `_move_to_closed`
(a bare `os.replace`), which moved the file to `closed/` but left the
thread_id in `open_topic_index.json` **and** orphaned the sibling
`.inbox.json` — `archive_topic`'s own docstring warns the latter gets
"resurrected into a phantom topic" by the next scan. Fix: set the terminal
state, `write_atomic`, then `topic_store.archive_topic(TOPICS_DIR,
INDEX_PATH, thread_id)` which removes the index entry and the inbox too.
recover now also only writes a topic when something actually changed (no
mtime churn on a pure no-change reconcile).

#### 1.1.4 A failed scan must not read as "nothing is running", and a survivor must keep its pid file

Observed 2026-07-31. The nightly `ClaudeReviewBotStop` task reported
`Last Result: 0` and posted `🤖 Review Bot 已停止。` to the group while the
daemon kept running and reviewing all night. Forensics found **63 listeners**,
**5 monitor instances** from five sessions, and an orphaned daemon dating back
a week.

Three defects compounded, all of them variants of *reporting success without
verifying it*:

1. **The scan's failure mode was silence.** `stop_bot._scan_processes` wrapped
   the whole scan in `except Exception: return []`, and both it and
   `stop_review_bot.ps1` read command lines via `Get-CimInstance Win32_Process`
   / `taskkill` / `tasklist`. On this machine every one of those fails under
   desktop-heap exhaustion — `"The paging file is too small for this operation
   to complete"` — which is *exactly* the state a runaway leaves the box in.
   A broken scan therefore returned an empty list, indistinguishable from a
   clean machine, and `stop` declared `status:"ok"`. This is the same root
   cause as the zombie-listener runaway (§1.22) — the console process helpers
   are unusable here, so all process work goes through Win32 directly
   (`subprocess_util.iter_processes` / `is_process_alive` / `kill_process`; the
   standalone `.ps1` inlines the equivalent PEB read via `Add-Type`, since
   `Process.Path` is empty for most processes and never contains the `.py`
   script name).

2. **Pid files were deleted unconditionally.** The pid file is the *only*
   record of a process's identity — deleting it for a process that survived
   makes that process permanently unreachable, since no later stop can find
   what it cannot name. Each nightly stop therefore minted a fresh orphan.
   Now a pid file is removed only once its process is confirmed dead; a
   stale-but-dead file is harmless (`start` re-checks liveness).

3. **The orphaned daemon is self-amplifying.** It keeps running
   `dispatcher._restart_listener`, whose health check uses the same broken
   APIs, concludes the listener is dead, and spawns another — roughly every
   46 minutes, forever. One orphan becomes dozens of listeners. This is why
   §1.1.2's daemon-first ordering is necessary but not sufficient: the daemon
   must actually *die*.

`stop_bot.py` now returns `status:"scan_failed"` and changes nothing when the
scan breaks; `stop_review_bot.ps1` exits non-zero, lists survivors, and skips
the farewell post — so `Last Result` and the group message both stop lying.

#### 1.1.5 The restart must end every session, not only the scheduled one

`stop_review_bot.ps1` killed the parent session recorded in `cfg/session.pid`,
and **only `launch_review_bot.ps1` ever wrote that file**. The comment there
called it deliberate ("a manually started session is left alone"), but it made
the daily restart a no-op against exactly the sessions that most need ending: a
hand-started one, which is how the bot comes back after every incident recovery
(§1.1.4, §1.18). Those sessions survived every subsequent 08:00 restart.

Reconstructed from the parent-session transcripts, which record the kill as
their last write:

| session | started | last write | |
|---|---|---|---|
| `80bc0e19` / `706471bb` / `dba12e2f` / `ebbeb3b1` / `56cb84bb` | 08:00, scheduled | next day 08:00 | ended correctly |
| `5ce29e73` | 08-04 02:01, by hand | 08-06 11:33 | **outlived three restarts** |
| `096edf7d` | 08-06 15:04, by hand | 08-07 12:06 | **outlived one restart** |

Two live parent sessions is not a cosmetic leak: both receive Monitor
notifications, so they race over `monitor.pid` (the loser's Monitor exits 1 at
startup, killed by the winner), and the older one holds a context nobody reads while still
answering for the topics it was mid-way through.

**The record has to be written at `start`, not at `launch`.** `resolve_start.py`
runs on every `/review-bot start` regardless of what launched it, so that is
where `session_registry.register()` now records the session — resolved as the
nearest `claude.exe` ancestor of the python process
(`subprocess_util.find_ancestor_pid`, a `CreateToolhelp32Snapshot` walk; the
PEB scan in `iter_processes` cannot see parent links). An interactively started
session has no marker on its command line, so an ancestry walk from inside the
start flow is the only thing that can identify it — a command-line scan for
`"/review-bot start"` would have missed both rows above.

`cfg/sessions.json` is a **list**, not a scalar: overwriting one slot is the
original bug, since a second start would erase the first session's only record.
`stop_review_bot.ps1` kills every entry plus the legacy `session.pid`, each
guarded on image name `claude` (a PID recorded days ago may have been reused),
and rewrites the file with only the entries it failed to kill — clearing a
survivor's record is what made the old orphans permanently unreachable
(§1.1.4). The launcher still writes `session.pid` as the belt to that braces:
if a start aborts before reaching `resolve_start.py`, it is all the next stop
has. `session.restart.log` is appended rather than overwritten, so the question
"did yesterday's session actually die?" has more than one day of history.

#### 1.1.6 Partial restart (`restart_bot.py`)

Editing a script and getting it running used to mean a full stop + start: the
parent session is recycled, the group gets a fresh greeting, and every process
bounces — including the listener, whose respawns burn the app-wide Feishu
long-connection quota — a respawn loop once locked the app out entirely
with `1000040350`. Most edits need exactly one process restarted.
`/review-bot restart [components]` does that.

| component | process | restart mechanism | stale when |
|---|---|---|---|
| `daemon` | `pythonw poll_dispatch.py --watch` | `wscript run_poll.vbs`, detached | any `scripts/*.py` is newer than its start time |
| `listener` | `lark-cli event +subscribe` (Node) | `start_listener.py` | **never** — it runs none of our Python |
| `monitor` | `monitor_dispatch.py` under the Claude **Monitor tool** | session-only (see below) | `monitor_dispatch.py` or `subprocess_util.py` is newer |

**Default is `stale`, not `all`.** Each component's process start time is
compared against the mtime of the code it loaded, and only the ones running old
code are touched. That makes the common case (`/review-bot restart` after an
edit) both fast and minimal, and it is what keeps the listener out of it: no
Python edit can make a Node process stale, so auto mode never respawns it and
never spends quota. An explicit `--components listener` still works when the
listener is genuinely wedged.

The daemon watches all of `scripts/*.py` rather than a computed import closure.
It imports nearly the whole directory anyway, and the failure modes are not
symmetric: an unnecessary restart of a local process costs a few seconds, while
a missed one costs an afternoon debugging a fix that is on disk but not running.

**The monitor can be killed here but not restarted here.** It runs as a Claude
Code Monitor *tool* task owned by the session, not as a free-standing OS
process, so `restart_bot.py` kills it and returns
`action: "killed_needs_session_action"` with the exact tool call; SKILL.md's
`restart` mode tells the session to re-issue it. Reporting a restart it cannot
perform would leave the dispatch loop dead with a success message — the same
class of lie as §1.1.4's farewell-while-still-running.

**A relaunch is confirmed from the pid file, never from the launcher's exit
code.** `pythonw` swallows a startup traceback and exits silently, so
`_await_pid_change` waits for a *new, live* pid to appear in
`cfg/<component>.pid` before reporting success, and otherwise returns
`launch_unconfirmed` pointing at a visible-console rerun. Kill is likewise
confirmed by polling liveness before relaunching — starting a second daemon on
top of a survivor would put two of them on the same topics.

**Kill everything first, then launch — daemon killed first, started last.**
Ordering alone is not enough here, and the difference is what separates a stop
from a restart. `stop_review_bot.ps1` can simply go daemon-first because the
daemon stays dead. A restart brings it back, so a per-component
kill-then-launch loop puts a *live* daemon next to a listener that is about to
be killed: the new daemon's first `_health_check_listener` sees `listener.pid`
pointing at the process we just killed and calls `_restart_listener` itself.
`start_listener` has no singleton guard — it kills whatever pid it reads and
spawns regardless — so its spawn and ours collide, and one of the two listeners
ends up orphaned: alive, absent from the pid file, and therefore unreachable by
any later stop. That is the leak that once reached 63 listeners and burned the
app-wide Feishu long-connection quota.

So `restart_components` runs two phases: kill every requested component
(daemon first, so the health-check is gone before the listener dies), then
launch (daemon last, so it only starts supervising once the listener it would
supervise is already up). A component whose kill failed is skipped in the
launch phase — starting a second daemon on top of a survivor puts two of them
on the same topics.

**Phasing alone only covers the case where the daemon is also being
restarted.** It is dead across that window, so it cannot interfere. But the
single most likely invocation — `--components listener`, which is what SKILL.md
tells you to run for a wedged listener — deliberately leaves the daemon
running, and no reordering of a set that does not contain the daemon can stop
it from respawning the listener between our kill and our launch. Sequencing is
the wrong tool for a process we are not touching.

The restarter therefore posts a **guard**: `cfg/listener_restart.guard`, a
small JSON marker holding an expiry, written before the kill and cleared in a
`finally` after the launch. `_health_check_listener` checks it and stands down
while it is held, logging the skip rather than respawning. The daemon keeps
dispatching throughout — only its listener supervision pauses, for the couple
of seconds the swap takes.

Two failure modes are handled deliberately, both resolving toward "supervise
anyway" rather than "stay suppressed":

- **The restarter dies mid-flight.** The marker carries an expiry (90 s,
  comfortably above kill-confirm 8 s + launch-confirm 20 s); an expired marker
  counts as absent, so the health check resumes on its own. The `finally`
  handles the ordinary path, the TTL handles the crash.
- **The marker is unreadable or malformed.** Treated as absent. A guard that
  fails open costs one redundant respawn; a guard that fails closed leaves the
  listener unsupervised indefinitely, which is the failure this whole section
  exists to prevent.

**The guard prevents a decision; it cannot undo one already taken.**
`_health_check_listener` reads the marker and only *then* calls
`_restart_listener`, so a daemon that passed that read microseconds before the
marker was written still spawns. The window is small but not hypothetical, and
it is worst exactly where it matters: `--components listener` gets run when the
listener is wedged, which is precisely when the daemon is independently
concluding the same thing — both sides are primed to act at the same instant.

So the restarter reconciles after the fact. Once its listener is confirmed up,
`_reap_competing_listeners` scans by command-line signature (reusing
`stop_bot.SIGNATURES["listener"]` rather than declaring a second copy) and
kills anything else answering to it. One live listener is the invariant that
matters; a duplicate spends the app-wide long-connection quota. A failed scan
returns `None` and is reported as `listener_reap: "scan_failed"` — never
collapsed into "nothing else is running", which is the assumption that produced
orphans to begin with (§1.1.4).

Session restart is deliberately **not** a component. The parent session cannot
recreate itself, and the daily 08:00 task already owns that (§1.1.1); anything
needing a fresh session — a change to `resolve_start.py`, or reclaiming parent
context — is a `stop` + `start`, or just waiting for 08:00.

### 1.2 Event ingestion (`start_listener.py`, `router.py`, `reconcile.py`, `event_utils.py`)

- **Persistent Lark listener** — detached `lark-cli event +subscribe` (Node) writes raw event JSONs into `cfg/events/`. Daemon restarts it when PID dies, log mtime goes stale, or the log is stuck in reconnect-chatter zombie state.
- **Cold-start reconcile safety net** — `reconcile.py` fetches recent chat history via Lark API and writes any missed events into `cfg/events/`. Runs only on the daemon's first cycle / after a listener restart (§1.2.7), not every tick; stateless — de-dups by scanning existing `cfg/events/` ids against a `--since-ts` floor, not a per-topic `last_processed_ts`.
- **Router dispatch** — moves events from `cfg/events/*.json` into per-topic `<thread_id>.inbox.json`. Handles new-topic creation (root message with `RAGE-XXXXX`), per-topic event append, self-message filtering (`sender_id` is the bot's `cli_` app id OR its own `ou_` open_id — §1.2.3), withdrawn-message drop.
- **Same-ticket supersede** — a new root for an already-open ticket closes the old topic before creating a new one.
- **Event normalization** — `event_utils.normalize_listener_event` flattens the websocket payload to `{chat_id, sender_id, thread_id, root_id, content}`; HTML tags stripped; `root_id`/`thread_id` consolidated to avoid `om_` vs `omt_` duplicates.

#### 1.2.1 Event routing: inbox separation  (was §3.5)

Router and topic agent must NEVER write to the same file. The inbox design:

| Writer | File | Operation |
|--------|------|-----------|
| Router | `{thread_id}.inbox.json` | Append new events |
| Dispatcher drain | `{thread_id}.json` | Move inbox → `events.pending[]` |
| Topic agent / mechanical handler | `{thread_id}.json` | Process pending, update state |

Router dedup uses a recent-event id ring (`topic_store.is_recent_event`) plus an inbox-level event-id scan. The old `event_ts <= last_processed_ts` timestamp gate was removed — out-of-order delivery was dropping legitimate late events. Agent sets `last_processed_event_id` (exact-match anchor). `last_processed_ts` is kept for observability only, no longer a dedup gate.

**Drain-while-locked rule**: if a topic lock exists and is not stale, the drain step skips that topic — writing the topic file while an agent holds a stale snapshot would cause the agent's later write to overwrite the appended events. The inbox persists; events drain next cycle.

#### 1.2.2 `mentions[i].id` is a dict in the listener payload — extract via `_mention_open_id`  (was §3.32)

`reply_parser` matches `^@bot(\s|$)` for `dev_question` / `manual_refresh`, but
real messages lead with the bot's display name (`@沐寒蒸馏版`).
`bot_identity.normalize_bot_mention` rewrites that to literal `@bot` **iff** the
first `mentions[]` entry points at this bot — hinging on
`_mention_open_id(first) == bot_open_id`.

Footgun: `mentions[i].id` is **not a string** — the websocket payload nests it:

```json
{"key": "@_user_1", "id": {"open_id": "ou_...", "user_id": "...", "union_id": "..."}, "name": "沐寒蒸馏版"}
```

Comparing `first.get("id") != bot_open_id` against the nested dict is always
`True`, so the rewrite no-ops, the regex misses, and every `@bot …` reply lands
silently as `reply_intent_ignored` (invisible to the dev). Fix: read open_ids via
`_mention_open_id(mention)`, which handles nested dict (`id.open_id`), flat `ou_…`
string, and sibling `open_id`. **Rule: anywhere you read an open_id out of
`mentions[]`, go through `_mention_open_id`** — never compare `mention.get("id")`
to a string. (Symptom of the old bug: a `reply_intent_ignored` audit whose
content starts with `@_user_N`.)

#### 1.2.3 Self-message filter must match the bot's open_id, not just `cli_`  (was §3.43)

The bot subscribes to its own group, so the listener delivers the bot's own
outbound posts (acks, reviews) back as `im.message.receive_v1` events. The
self-message guard (`router._is_self_message`) originally skipped only
`sender_id.startswith("cli_")`. That holds for the **reconcile** path
(chat-history API reports the bot as the Lark *app* id `cli_<app>`), but NOT
for the **listener** path: `event_utils.normalize_listener_event` extracts
`event.sender.sender_id.open_id`, so a websocket self-event carries the bot
*user's* open_id (`ou_...`), which slipped past the `cli_` check.

Consequence (observed across RAGE-16360 / 16557 / 16678 and the merged-ticket
`mr_not_found` spam): the bot's own ack ("…已收到 RAGE-XXXX… chaos!NNNN") contains
a ticket id, so the router created a **phantom topic** keyed off the ack
message, re-ran `ack_new_topic` (a visible second ack), and — being a "new root
for an existing ticket" — **superseded the real topic**. When the supersede
fired after the real topic had already reached `AWAITING_APPROVAL`, the review
and the approver's reply were lost; on already-merged tickets the phantom found
no open MR and posted "未找到 MR", which the pinned reconcile floor (§1.2.6)
replayed on every startup.

Fix: `_is_self_message(sender_id, bot_open_id)` returns true for `cli_*` OR
`sender_id == bot_open_id`. `bot_open_id` (`REVIEW_BOT_OPEN_ID`) is already
loaded by `_load_env` and threaded into `route_pending_events`; the guard just
had to consult it. Empty `bot_open_id` degrades safely to the old `cli_`-only
behavior (no false positives). The daemon loads `router.py` once — restart it
after this change.

#### 1.2.4 Reconcile must skip `system` messages, or one pins the floor forever  (was §3.44)

§1.2.6 added the rule "never advance the persisted reconcile floor past a
message that failed routing-key resolution this pass" (`min_unresolved_ts − 1`)
so a *transient* resolve failure is retried, not lost. But a **permanently**
unresolvable message turns that safety rule into a deadlock: the floor sticks at
its timestamp and reconcile re-scans the full window (up to 100 msgs) every
cycle, forever.

Observed: a Lark **system message** — "刘沐寒 invited 王浩川 to the group" at
2026-06-04 22:09 — did exactly this. System messages have `msg_type: "system"`,
an `omt_` `thread_id` but no `root_id`, `deleted: false`, and an **empty sender**
(`sender.id == ""`), so reconcile's `deleted` and `cli_` filters both miss them.
They reach `_resolve_root_id`, which returns `""` (a system event has no real
thread root) — every cycle — so the message is counted `skipped_unresolved` and
`min_unresolved_ts` pins the floor at `22:08:59.999` indefinitely. This is the
recurring "floor-pin": symptom is `last_reconcile_ts` never moving and (pre-§1.2.3)
amplified phantom `mr_not_found` spam from re-scanned merged tickets on startup.

Fix: drop `msg_type == "system"` messages early in the reconcile filter loop
(alongside the `deleted` / `cli_` filters, before `_resolve_root_id`), counted
as `skipped_system`. They are never routable topics. With the system message
skipped, `min_unresolved_ts` is `None`, so the floor advances to `max_ts` and the
window stops growing. Verified against live history: `skipped_system: 1`,
`skipped_unresolved: 0`, `min_unresolved_ts: null`. The daemon loads
`reconcile.py` (via `dispatcher.py`) once — restart to apply.

Not fixed here (deferred): a general cap so *no* future permanently-unresolvable
message can pin the floor (e.g. quarantine after N unresolved cycles, or bound
floor lag behind `max_ts`).

#### 1.2.5 Reconcile root resolution: own-id default, NOT `_resolve_root_id`  (was §3.45)

THE actual source of the recurring phantom topics (16360 / 16678 / 16604 /
16557), distinct from §1.2.3. `reconcile` keys topics by a message's ROOT om_ id.
The bulk `+chat-messages-list` doesn't set `root_id` on thread members, so the
old code called `_resolve_root_id(omt_)` → `+threads-messages-list … messages[0]`
and assumed `messages[0]` is the root. It is NOT: that API returns only a
thread's **replies** (the root is absent), so `messages[0]` is the first reply —
for a freshly-acked ticket that's the **bot's own ack**. reconcile then created
a topic keyed by the ack message id; being a "new root for an existing ticket"
it **superseded the real topic**, and the ack re-ran ack_new_topic (the visible
double-ack). The operator never sees `_is_self_message` here — the reconcile
event's sender is the human poster, not the bot.

Confirmed live: for the dev post `…05acd5` (the real RAGE-16557 root), both it
and the bot ack share `omt_194756147c0f1bb8` with `root_id: None`; the bulk list
gives the dev post **no** `thread_replies` while a bot message carries them, and
`+threads-messages-list` returns only the two bot acks. So neither a
`thread_replies` check nor the thread query can recover the root.

Fix: reconcile no longer calls `_resolve_root_id`. It builds a `reply_to_root`
map from the bulk `thread_replies` arrays; a message that is genuinely listed as
someone's reply routes to that root, otherwise the message's **own id** is the
key (correct for both standalone messages and thread roots the API didn't tag).
Verified: the dev post now resolves to its own id — no phantom. `_resolve_root_id`
is left in place but unused (dead code; safe to delete later).

Caveat: down-time recovery of a *human* mechanical reply (`pass`/`ok`) still
depends on that reply appearing in some root's `thread_replies` within the fetch
window; the listener handles live replies, so this is the rare cold-start case.
Note the §1.2.4 `msg_type == "system"` skip still runs first (a system message
has no ticket id, so even keyed by own id the router would drop it, but the early
skip saves the work).

#### 1.2.6 Reconcile floor: persist `last_reconcile_ts`, never a fixed 1h window (RAGE-16281)  (was §3.42)

The history-backfill `reconcile.py` is the only path that recovers Lark
messages the (dead) websocket listener never captured. Its window was a
**fixed 1 hour**: `dispatcher._reconcile` invoked it with no `--since-ts`,
so it defaulted to `now − 1h`, and anything older was `skipped_old` —
**lost**. The `last_reconcile_ts` floor the docstring promised was never
wired: `DISPATCHER_STATE` was a dead constant, `dispatcher.state.json` never
existed, and reconcile's returned `max_ts` was discarded. So any topic
opened — or mechanical reply (`pass`/indices/`ok`/`close`) sent — while the
bot was down longer than an hour was permanently dropped (the two reported
symptoms).

Fix (three parts):
1. **Persisted floor.** `dispatcher._reconcile` computes a floor via
   `_compute_reconcile_floor`: prefer `dispatcher.state.json`'s
   `last_reconcile_ts`; on cold start seed from the newest
   `events.last_processed_ts` across open topics; clamp to a 7-day cap
   (`RECONCILE_FLOOR_CAP_MS`) so a long-dead install doesn't page the whole
   group. It passes `--since-ts <floor>`, then advances the persisted floor
   to the newest captured message (`max_ts`) — but **never past a message
   that failed routing-key resolution this pass** (`min_unresolved_ts − 1`),
   so a transient resolve failure is retried next cycle instead of being
   skipped_old.
2. **Pagination.** `fetch_history` pulled one 50-message page; a busy/long
   downtime overflowed it. It now pages (`--page-token`, sorted desc) until a
   page's oldest message predates the cutoff, `has_more` is false, or
   `MAX_RECONCILE_PAGES` (60 ⇒ 3000 msgs) is hit. Hitting the cap sets
   `capped: true`, which the dispatcher logs as a WARN — coverage truncation
   is surfaced, never silent.
3. **Recover catch-up.** `/review-bot recover` now runs one guarded
   dispatcher cycle (`recover._catch_up_messages`) before the GitLab pass, so
   missed topics/replies are ingested immediately rather than waiting for the
   daemon restart. Guard: skipped when a live daemon already holds
   `daemon.pid` (it reconciles every cycle; a concurrent one-shot would
   double-process).

Footgun: the floor lives in `dispatcher.state.json`. Deleting it is safe
(re-seeds from topics, clamped to 7 days), but manually setting it to `0`
would make every cycle page back the full 7-day cap. The daemon loads
`dispatcher.py` once — restart it after changing any of this.

#### 1.2.7 Reconcile runs on cold start / listener restart only — not every cycle  (was §3.46)

Reconcile (Lark history backfill) exists to recover messages the listener never
captured — i.e. while the bot was DOWN. Running it on **every** dispatcher cycle
(the old design) re-processed the same live messages the listener had already
handled, which is what produced the dual-ingest supersede churn (reconcile
self-roots a message the listener also routed → two topics for one post → one
supersedes the other). It also re-scanned ~100 msgs/cycle and was the surface
for the floor-pin (§1.2.6/§1.2.4) and the `_resolve_root_id` phantom (§1.2.5).

New model: **reconcile only on the moments a gap can exist** — cold start and a
mid-day listener restart. Steady-state cycles are listener-only.
- `poll_dispatch.run_one_cycle(reconcile=…)` appends `--reconcile` to the
  dispatcher subprocess. The watch loop passes `reconcile=first_cycle` (True for
  the daemon's first cycle only); single/`--loop` runs pass True.
- `dispatcher.main()` runs `_reconcile()` iff `--reconcile in sys.argv` OR
  `_health_check_listener()` reported `restarted: True` this cycle; otherwise
  `reconcile_result` is a `{"reconciled":0,"withdrawn_ids":[],"skipped":...}`
  stub so the downstream `drain_mechanical(withdrawn_ids=…)` and
  `_close_withdrawn_topics` no-op cleanly.

Re-sourced dependency: withdrawn-ROOT close previously came from reconcile's
per-cycle `withdrawn_ids` via `_close_withdrawn_topics`. With reconcile gated,
that path only fires on reconcile cycles, so `router._evict_recalled_message`
now ALSO closes a topic when the recalled message is its `root_message_id` —
driven by the listener's `im.message.recalled_v1` events in real time (the
listener subscribes to both `receive_v1` and `recalled_v1`). Withdrawn-REPLY
handling is unchanged (`mechanical_reply_handler.drain_withdrawn` mget's pending
events every cycle, independent of reconcile).

The daemon loads `poll_dispatch.py` / `dispatcher.py` / `router.py` once —
restart to apply. `_reconcile`/`reconcile.py` are unchanged internally.

#### 1.2.8 Thread replies are invisible to the bulk chat list — harvest nested `thread_replies`, page back to the oldest open root (RAGE-17195)

`im +chat-messages-list` returns only thread ROOT messages as top-level
entries, ordered by ROOT create_time; every reply lives ONLY nested inside its
root's `thread_replies[]` array. Two consequences the original reconcile
missed, which silently lost a developer's `1 2 3` dev-triage reply that landed
at 01:00 exactly while the nightly stop was tearing the listener down:

1. **The synthesis loop iterated only top-level messages**, so a thread reply
   that arrived while the listener was down was never synthesized at all —
   reconcile's "recovers mechanical replies" contract was broken for every
   reply, not just a corner case. The `reply_to_root` map (built FROM the
   nested arrays) only helped if replies also appeared top-level, which the
   current lark-cli output disproves.
2. **Pagination stopped at the event floor (`--since-ts`)**, but the list is
   ordered by root create_time — a fresh reply to a days-old root sits nested
   under a root far below the floor, so the scan never even fetched the root
   that carries it.

Fix, in `reconcile.py` + `dispatcher._reconcile`:

- The per-message gates (cutoff / existing / withdrawn / system / self) are
  extracted into one `_consider(m, root_id)` helper; the top-level pass keeps
  its root-resolution order (§1.2.5), and a second pass walks every fetched
  root's `thread_replies[]`, calling `_consider(reply, root_mid)` — the
  routing key is the enclosing root's own id, no resolution needed. Replies
  are still gated by the event floor (`cutoff_ms`), so deep paging does not
  re-ingest old traffic; the router's event-id ring dedupes the rest.
- `fetch_history` takes `page_floor_ms`: the dispatcher passes the oldest
  OPEN topic's `lifecycle.created_at` (minus 2 min of minute-precision slack,
  clamped to the 7-day cap) via `--page-floor-ts`, so pagination reaches every
  root that can still receive an actionable reply. Closed topics don't matter
  — the router drops replies on closed threads anyway (Gate 4a).
- Diagnosis breadcrumb: this also explains a floor that "sticks" at a round
  minute (§1.2.6) — `max_ts` could only ever advance to the newest ROOT,
  because replies were never written.

#### 1.2.9 Withdrawn superseding root must reopen the superseded predecessor (RAGE-20032)

Same-ticket supersede is one-way: the OLD topic gets
`closed_reason = "superseded by new topic om_<new>"` (forward pointer only);
the successor records nothing. Both withdrawn-root close paths
(`dispatcher._close_withdrawn_topics` on reconcile `withdrawn_ids`,
`router._evict_recalled_message` on live recalls) close only the duplicate.
Nothing reopened the predecessor, and router Gate 4a silently unlinks every
event whose thread sits in `closed/` — no log line, no inbox.

Incident (2026-07-10, RAGE-20032): topic A was mid-review in `FULL_REVISION`
(dev owed fixes on flagged #1/#3) when the developer posted a duplicate
"RAGE-20032" root to nudge the overnight-stopped bot. The router superseded A;
the duplicate B got a full wasteful re-review at the same SHA and landed in
`DEV_TRIAGE`; the dev withdrew the duplicate root. The 12:20 listener-restart
reconcile closed B ("root message withdrawn") — and A stayed dead. The dev's
`ok` (11:16) routed to A's thread and died at Gate 4a **twice** (live capture,
then the reconcile re-synthesis). By then `last_reconcile_ts` (11:24) had
moved past it: the floor (§1.2.6) can never re-ingest a Gate-4a-dropped
reply — only a targeted per-thread backfill can.

Fix — `topic_reopen.reopen_superseded_predecessor`, called from
`_close_withdrawn_topics` after each withdrawn-root close (passing the closed
duplicate's **thread_id**, not the withdrawn message id — `create_topic` can
record `root_message_id != thread_id`, and the supersede reason embeds the
thread_id):

1. **Purge first, unconditionally**: drop `supersede_pending.json` entries
   whose `new_thread` is the withdrawn thread BEFORE any guard/bail. With the
   supersede deferred (lock-fresh predecessor, §1.8.3), the predecessor is
   still open and the finder sees nothing in `closed/` — but
   `_retry_pending_supersedes` runs LATER in the same cycle (step 6b vs this
   hook at step 4b) and would close the predecessor for a topic that just
   died. The retry itself carries the matching invariant: it drops any
   ledger entry whose `new_thread` is no longer an open topic, because a
   multi-duplicate chain (P deferred-superseded by C, C superseded by D,
   both C and D withdrawn) leaves a stale `old=P,new=C` entry the purge
   can't see — the hook only fires for the one still-open duplicate.
2. **Reverse lookup** via `topic_store.parse_superseded_by` over `closed/`
   (newest `resolved_at` wins). Writers produce the reason literal via
   `topic_store.SUPERSEDE_REASON_FMT` so they can't drift from the regex
   this lookup keys on. The scans use a tolerant per-file reader —
   `read_or_none` only catches `FileNotFoundError`, and one corrupt JSON in
   `closed/` escaping to the blanket except would fail the reopen CLOSED,
   which the one-shot trigger turns back into the original incident. A
   crash-split pair (open copy + closed copy both present, from a crash
   between the mutate's two file operations) heals by dropping the closed
   copy.
3. **Guards fail OPEN on transient errors** — the trigger is one-shot (a
   re-reported withdrawal finds no open topic to close, so the hook never
   re-fires); a wrong reopen is visible (notice) and reversible (`close` /
   recover.py), a wrong non-reopen is silent and permanent. Only definitive
   verdicts block: another open topic already owns the ticket (checked
   before the heal, so a heal can't resurrect a rival); the predecessor's
   own root is withdrawn (`withdrawn_ids` fast path, then
   `_is_message_alive` — blocks only on `False`, `None` proceeds); an MR is
   confirmed merged/closed via `merge_tracker.check_mr` (a reopened topic
   with a merged MR is a zombie nothing terminalizes —
   `_check_approved_topics` only watches APPROVED). Restore state comes from
   the newest `topic_closed` audit entry's `from_state`; missing/terminal
   refuses (a terminal restore would just get janitor-archived next cycle).
4. **Mutate in crash-safe order**: write `topics/<id>.json` first, unlink
   the closed copy second, then `topic_index.add`. A restored `APPROVED`
   topic gets its per-MR `pipeline_status` / `pipeline_passed_at_ms` popped
   so the merge queue's hold re-arms — a stale "passed" could let this same
   cycle's merge pass beat the NEXT cycle's backfilled replies (e.g. a
   recovered approver `close`).
5. **Backfill**: list the thread's replies (`im +threads-messages-list`),
   paging to exhaustion (bounded at 40×50) — the recovery targets are the
   NEWEST replies at the tail of the asc listing, so a small page cap would
   truncate exactly the messages being recovered. Skip `deleted:true` /
   `cli_` senders / ids in `recent_event_ids` /
   `ts < last_processed_ts − 60 s` (minute-granularity slack —
   over-inclusion is free, Gate 5 dedups), and synthesize reconcile-shaped
   raw events (`_source: "reopen_backfill"`). Content must be JSON-unwrapped
   (`{"text":"ok"}` → `ok`): flat synthesized events take
   `normalize_listener_event`'s passthrough branch, which skips the unwrap,
   and `extract_text_content` leaves the braces — the `^ok$` grammar would
   silently never match. The router routes them next cycle; the normal
   drains (ack_dev_reply / no-new-commits / mechanical) take over. Accepted
   degradation: backfilled `@bot` questions carry `mentions: []`.
6. **Notice + failure isolation**: post the `topic_reopened` template
   (includes the `close` escape hatch — covers the ancestor-resurrection
   case where reopening an old predecessor wasn't what the dev wanted).
   Once the mutate has landed, a backfill or notice failure must NOT flip
   the result to `"error"`: the status stays `"reopened"` with a
   `backfill_error` field, so a partial failure is visible and recoverable
   instead of masquerading as a failed reopen.

Manual recovery / probing: `python topic_reopen.py --withdrawn-thread om_...
[--dry-run]`. Dry-run is pure — it reports `would_purge` /
`would_restore_state` without touching the ledger or moving files, because
an operator probe must not disarm a legitimately-armed deferred supersede.

Deliberately NOT hooked into `router._evict_recalled_message`: the router
holds in-memory `index` / `ticket_to_thread` / `closed_thread_ids` snapshots
and saves the index wholesale at pass end (`topic_index.save`), so an inline
reopen there gets clobbered — the reopened topic vanishes from the index, its
backfilled events then have no ticket match and are re-dropped. Re-adding
that hook requires returning the reopen result to the call site and patching
all three in-memory structures. Recalls also don't arrive in practice
(2026-07-10: three withdrawals, zero `recalled_v1` deliveries; only the
restart-reconcile caught them) — root cause: 31 orphaned
`lark-cli.exe event +subscribe` processes had accumulated across listener
health-check restarts (`stop_bot.py`'s scan doesn't match the direct
`lark-cli.exe` binary), and Lark load-balances events across an app's
websocket connections, scattering live deliveries to zombie sockets
(cleaned up 2026-07-11). `_evict_recalled_message` therefore carries a
tripwire: it WARNs `recall_path_closed_root_without_reopen` when it closes a
withdrawn root, so the first live recall is the signal to revisit hooking
the reopen there.

Observability companion: Gate 4a logs
`closed_topic_reply_dropped thread=… msg=…` (WARN) and counts
`closed_thread_drops` in the router summary — the silent drop is what let
this incident hide for two hours.

Retention caveat: the janitor prunes `closed/` after 30 days; a predecessor
older than that is unrecoverable (acceptable — a month-stale review has no
state worth restoring).

Every behavior above is pinned by a `replay.py` fixture (`reopen_*`,
`retry_supersede_skips_dead_new_thread`, `gate4a_drop_is_logged`).

### 1.3 Dispatcher cycle (`dispatcher.py`, `poll_dispatch.py`)

Each 5s daemon tick, in order:

1. **Janitor sweep** — relocate terminal topics to `closed/`, prune closed older than `CLOSED_RETENTION_DAYS` (30), clear stale lock files.
2. **Listener health check** — restart if PID dead, log stale >45 min, or zombie reconnect loop.
3. **Reconcile** (cold-start / listener-restart cycles only, §1.2.7) — fetch recent chat history, write missed events.
4. **Router** — drain `cfg/events/` into per-topic inboxes.
5. **Drain inboxes** — move `<thread_id>.inbox.json` into `events.pending[]` on the topic file (skipping topics currently locked by an agent).
6. **Withdrawn-drain** — drop pending events whose source Lark message was withdrawn (before any side effect spawns).
7. **No-new-commits drain** (§1.7) — for `*_REVISION` topics whose MRs haven't advanced past `review.last_review_commit`, post the `no_new_commits` template and drop the dev-reply event (§1.7).
8. **Mechanical reply handler** — execute approve/revision/close approver replies in-process (§1.7).
9. **Ack new topic** — mechanical ack on fresh `TRIAGING` topics; pre-compute triage / phase / version_3rd_check / per-file `+X/-Y` so the topic agent can read cached fields instead of re-deriving.
10. **Withdrawn-root close** — close topics whose root message was deleted.
11. **Merge tracker** — per APPROVED topic, check MR state + pipeline status, build merge queue(s).
12. **Find work** — collect topics with residual pending events, **pre-compute incremental log/diff** for dev_reply spawns (§1.13.1), write `dispatch_plan.json`.
13. **Trigger parent Claude** — emit trigger line only when work/merge queue non-empty.

#### 1.3.1 Parent must not respawn while topic agent is in flight  (was §3.18)

The dispatcher emits the same topic in `dispatch_plan.work[]` every cycle
until `events.pending` is drained. Complex full reviews routinely take
14+ minutes (diff fetch + full inline review + Lark doc create +
permission grants + post). During that window the parent will see
repeated dispatch notifications for the same topic.

**Do not respawn based on lock+state alone.** The per-topic lock is
acquired and released around each atomic write inside the agent, so the
lock file is missing between writes — that is not evidence the agent
crashed. Spawning a duplicate agent that races past the same preflight
window produces duplicate Lark posts (round-1 + duplicate doc + duplicate
thread reply) that have to be manually withdrawn.

The parent should track in-flight agent IDs by ticket and treat duplicate
dispatch notifications as telemetry only until the prior agent's
completion notification arrives. The dispatcher's 30-second cooldown
paces *cycles*, not same-topic emission. Janitor stale-lock thresholds
(>10 min stolen, >30 min force-cleared) are for *crashed* agents, not
slow ones — verify the prior agent is actually dead (no recent audit
writes, completion notification missing) before spawning a retry.

#### 1.3.2 Round N+1 needs its own immediate ack (`ack_dev_reply`)  (was §3.34)

Round 1 gets a fast `ack_new_topic` reply, but round N+1 (dev pushes + posts
`ok`) had none — the 30 s–15 min incremental review ran silent after the dev's
`ok`, which read as unresponsive.

`scripts/ack_dev_reply.py` mirrors `ack_new_topic.py`: a dispatcher-side
mechanical handler that races ahead of the topic agent and posts a brief Chinese
ack on the `dev_reply` path. Zero LLM tokens.

Flow:

1. Walk open topics. Skip any not in `{SIMPLE_REVISION, FULL_REVISION}`.
2. Find a pending event with `intent == "dev_reply"` and
   `role == "developer"` whose `event_id` hasn't been acked already
   (audit has no `dev_reply_ack_sent` for that id).
3. For each repo in `topic.mrs`, resolve the current branch SHA via
   `git ls-remote origin <branch>` and compute
   `git diff --numstat <last_review_commit>..<new_sha>`. Skip the
   ack entirely when no repo's SHA has advanced — the agent will
   post the `no_new_commits` template in that case, and posting two
   replies for "you pushed nothing" would be noisy.
4. Render the `ack_dev_reply` template with `TICKET_ID`, `ROUND =
   review.review_round + 1`, and per-repo paragraphs (label + MR
   link + branch + numstat tail). 3rd-party repos still get a row
   (link + branch) but no stats — we don't have a local checkout.
5. Append two audit entries (`lark_reply_sent` for the dispatcher's
   own tracking, `dev_reply_ack_sent` keyed on the event_id for
   idempotency) and the event STAYS in `events.pending` — the topic
   agent still needs it to run the actual review.

The handler holds the per-topic `.lock` for the duration of the
write, same model as `mechanical_reply_handler` — keeps the audit
mutation from racing the topic agent's own writes. If a topic agent
is already mid-flight on this topic, the lock attempt fails fast and
the ack is left to the next cycle (`skipped_locked` in the summary).

Separate template (not reused `ack_new_topic`): round-numbered title + per-repo
*incremental* stats against `last_review_commit` rather than the round-1 diff
against `target_branch`.

Wiring lives in `dispatcher.py` step 3e, right after step 3d's
`ack_new_topic.drain_ack`. The order matters: ack handlers run
before `_find_work` lists topic-agent spawns, so a single dispatcher
cycle posts the ack AND fires the agent — the agent picks up
immediately rather than waiting for the next cycle.

#### 1.3.3 `@bot` questions get an immediate ack too (`ack_dev_question`)

A developer `@bot <question>` (`dev_question`) is answered by the topic
agent — another multi-minute wait (spawn + code re-reading + freeform
reply) with the same silent-thread failure mode §1.3.2 fixed for round
N+1: until the answer lands, the dev can't tell whether the question was
even heard.

`scripts/ack_dev_question.py` mirrors `ack_dev_reply.py` minus all the
git work — a question implies no new commits, so there is no SHA
resolution or numstat, just a template render + post. Flow:

1. Walk open topics; find a pending event with `intent ==
   "dev_question"` and `role == "developer"` whose `event_id` hasn't
   been acked (audit has no `dev_question_ack_sent` for that id). No
   state filter — the agent answers `dev_question` in any state
   (`spawn_topic_agent.md` "Any state" row), so the ack is valid
   wherever the question is.
2. Render the `ack_dev_question` template (`TICKET_ID` only) and post
   to the topic root.
3. Append `lark_reply_sent` + `dev_question_ack_sent` audits; the
   event STAYS in `events.pending` — the topic agent still owns the
   actual answer.

Same cooperative per-topic `.lock` model as §1.3.2. Wiring is
`dispatcher.py` step 3f, right after step 3e, so a single cycle posts
the ack AND fires the answering agent.

#### 1.3.4 3rd-party lib MR merged ahead of its consumer must not false-close (RAGE-20767)

`ack_new_topic._discover_mrs` searches `3rd_party_cpplibs` for `state=opened`
MRs and adds them to `mrs` (for the 3rd-party review+merge phase). But a lib MR
can legitimately merge *before* its consumer chaos/rage MR — the library ships
first, then the consumer bumps `version_3rd.cmake` to the released version.
Probing only `state=opened` made `_compute_version_3rd_check` read the cmake bump
as orphaned and close the consumer topic with
`closed_reason=3rd_party_mr_not_found` — a false close on a valid workflow.

Fix: a **second `state=merged` probe** returns a `has_merged_3p` flag.
`_compute_version_3rd_check(mrs, ack_stats, has_merged_3p)` treats a merged lib MR
as satisfying the cmake→3p direction, so `cmake_without_3p` fires only when there
is **neither** an open **nor** a merged 3rd-party MR. The merged MR is deliberately
**not** added to `mrs`: it needs no review or merge, and adding it would route the
topic into the 3rd-party phase and attempt a duplicate merge — so `review_phase`
stays `null` and the topic runs the normal main-repo flow. `has_merged_3p` relaxes
only that one direction; `3p_without_cmake` still keys on an **open** MR. Kept as
its own query so an error on one state can't mask the other.

#### 1.3.5 MR discovery binds by title, not full-text `--search` (RAGE-19360)

`ack_new_topic._discover_mrs` resolves a ticket's MRs with GitLab's
`glab mr list --search "<ticket>"` (and the group `?search=<ticket>` for
3rd-party). `--search` is a **full-text substring match over title AND
description**, so it also returns any MR that merely *mentions* the ticket in
its body — including a different ticket's MR that cross-references this one.
Selecting the "first open" result then attaches the wrong MR: observed with
RAGE-19360, whose topic picked up `rage!4198` (branch
`feature/RAGE-21088_…`, title `RAGE-21088: …`) because that MR's description
named RAGE-19360, while the real `rage!4197` (`RAGE-19360: …`) was skipped and
`rage!4198` ended up double-attached to both topics.

Fix: `_title_matches_ticket(entry, ticket_id)` filters the search results to
MRs whose **title** carries the ticket as a token. The title is the
authoritative binding — the repo enforces the `RAGE-XXXXX:` title convention.
A leading verb is allowed (`Fix RAGE-19360: …`), so the ticket is matched
anywhere in the title (`(?<![A-Za-z0-9])RAGE-\d+(?![0-9])`), not anchored as a
prefix, and the trailing digit guard keeps `RAGE-19360` from matching
`RAGE-193600`. Applied to all three search sites — the rage/chaos `mr list`,
the 3rd-party `state=opened` loop, and the `state=merged` `has_merged_3p`
probe (§1.3.4) — so a body-only mention can neither attach an MR nor relax the
version_3rd check.

#### 1.3.6 A 3rd-party probe error must retry, not false-close (RAGE-20767 follow-up)

`_compute_version_3rd_check` returns `cmake_without_3p` when chaos bumps
`version_3rd.cmake` but neither an open nor a merged 3rd-party MR is found
(§1.3.4). Both facts come from the two 3rd-party group probes (`state=opened`
→ `has_3p`, `state=merged` → `has_merged_3p`). If either probe errors
transiently (glab / network), the flag it feeds stays `False`, so a real
pre-merged lib MR reads as absent → `cmake_without_3p` → the topic is closed
with "未找到对应的第三方库 MR" — the exact RAGE-20767 false-close, now driven by a
flaky API call instead of a `state=opened`-only query, and with no retry after
close.

Fix: `_discover_mrs` returns `threep_probe_errored` (True if the opened OR
merged probe failed). `_ack_one_topic` retries — does NOT close — when the
verdict is `cmake_without_3p` and `threep_probe_errored`. The `3p_without_cmake`
direction needs no guard: it requires `has_3p == True`, which a probe error can
only under-report, so a failed probe can't false-trigger it. Mirrors the
existing "any discovery error + no MRs → retry" fail-open policy (§1.3): an
uncertain 3rd-party state defers rather than closes.

#### 1.3.7 A "fix the prerequisite" close must be revivable, not a dead end (RAGE-23816)

Three ack-time closes tell the developer to go do something and come back:
`mr_not_found` ("请确认 MR 已创建"), `3rd_party_mr_not_found` ("请先创建第三方库 MR"),
and `missing_version_3rd_bump` ("version_3rd.cmake 未包含版本升级"). All three
archived the topic immediately, so the developer's follow-up in that same
thread hit router Gate 4a and was dropped with `closed_topic_reply_dropped` —
the bot asked a question it had already made itself unable to hear. Observed on
RAGE-23816: the bot closed at 23:52, the developer answered "已经创建了，重新找
&lt;link&gt;" at 23:53, and nothing happened.

Fix: `ack_new_topic.REVIVABLE_CLOSE_REASONS` marks those three reasons; the
close path appends the follow-up hint to the notice and stamps
`lifecycle.revivable = true`. Router Gate 4a, before dropping, calls
`topic_revive.try_revive` — for a revivable closed topic it moves the file back
to `topics/`, resets `state=TRIAGING` / `ack_sent=false` / `closed_reason=null`,
re-adds the index entry, and lets the event route normally, so the next
`ack_dispatch` re-runs discovery against reality. The stale root event is still
pending (the close path never drains it), so ack re-triggers off it.

Bounded by `lifecycle.revive_count` &lt; `MAX_REVIVES` (3): re-running ack
re-validates the prerequisite and re-closes if it's still unmet, so an
unbounded revive would let idle thread chatter re-post the ⚠️ notice forever.
Past the cap the drop resumes, with the count visible in the archived topic.
Non-revivable reasons (`mr_already_merged` / `mr_already_closed`, approver /
developer `close`) are unchanged — no follow-up makes a merged MR reviewable.

### 1.4 Per-topic agent (`spawn_topic_agent.md`)

Spawned once per topic with pending events. Owns the topic lock while running. Responsibilities:

- **Triage** (`TRIAGING`): discover MRs via `glab mr list`, including 3rd-party via `groups/3rd_party_cpplibs/merge_requests?search=…`; detect stale base, fetch diff, classify `simple` vs `complex`.
- **Review**: simple → inline diff review; complex → full inline review by the topic agent itself (it has no `Task` tool — there is no `/review` sub-sub-agent), create Chinese Lark doc, grant view permissions to approver + developer, post doc link.
- **Developer handling** in `*_REVISION`: incremental re-review only on `flagged_issues`, big-picture verification (re-grep current file at HEAD), answer `dev_question` without state change.
- **3rd-party phase** approval + handoff back to `TRIAGING` main phase.
- **Per-event writeback**: side effects → remove from pending → audit entry → atomic write, repeat.

#### 1.4.1 Diff base = MR `target_branch`, not hardcoded master  (was §3.16)

MRs can target integration branches (`rage/rage_pv`, feature integration
branches, etc.), not just `master`. `ack_new_topic._discover_mrs` captures
`target_branch` from the glab MR JSON and stores it on `mrs[repo]`;
`_fetch_stats` prefers `mr_obj["target_branch"]` when computing the
`git diff --numstat` base, falling back to the repo's master branch only
when the field is absent. That fallback is read from
`$RAGE_REPO_ROOT/.claude/cfg/branches.json` via `env_resolver.master_branch_for`
(which maps `rage`→`main` and carries a hardcoded default if the config is
missing). The topic agent mirrors this in `spawn_topic_agent.md` when fetching
the full diff for review content.

Observed failure mode (RAGE-13672): MR targeted `rage/rage_pv`, hardcoded
`origin/rage/master` base swept in unrelated `rage/rage_pv...master`
history, producing a fake 62-file / +1676/-267 ack post when the real
change was 1 file / +6/-0 — and would have driven the LLM review against
that wrong diff too.

#### 1.4.2 Git Bash mangles Chinese branch names; use glab API to resolve SHA  (was §3.20)

`git ls-remote origin` and `git fetch origin <branch>` invoked from Git
Bash silently fail (or return empty) when the branch name contains
Chinese (e.g. `bugfix/RAGE-14395_Buildfarm无法生成CookInfo`) — Git Bash
on Windows mangles the UTF-8 bytes before passing them to `git.exe`.
The dev_reply diff-fetch path normally relies on `ls-remote` to refresh
`mrs[repo].branch_sha`; on Chinese-named branches it returns nothing
and the agent thinks no new commits exist.

**Workaround**: resolve the head SHA via the GitLab MR API instead:
`glab api projects/<url-encoded-slug>/merge_requests/<iid>` returns
`.sha` directly, no shell-quoting hazard. Once the SHA is known,
`git fetch origin <SHA>` works fine — the SHA is plain hex.

The agent contract in `spawn_topic_agent.md` already prefers
`mrs[repo].branch_sha` from the topic file as the source of truth;
the fallback `ls-remote` step is the trap. When implementing new
branch-resolution code, default to the API path.

#### 1.4.3 Lark doc creation goes through `lark_doc_helper.py`, not inline shell  (was §3.24)

Agents that hand-rolled `lark-cli docs +create` re-derived the upload command
from training data and gambled on the Windows pipe-truncation footgun (one
RAGE-12473 spawn burned 386 s / 88.7 k tokens and posted nothing).
`scripts/lark_doc_helper.py` removes the upload mechanics from the agent. One CLI:

```
python lark_doc_helper.py create \
    --title "代码审查 RAGE-12473" \
    --markdown-file "$BODY_FILE" \
    --grant-view "$APPROVER_OPEN_ID,$creator_open_id" \
    --public-link tenant_readable
```

Internals worth knowing about (not invariants the caller upholds):

- **No shell, ever** — the helper goes through
  `subprocess_util.lark_cli_argv_prefix() + hidden_run(…)`, passing
  the markdown body as argv to `lark-cli.exe`. Bypasses cmd.exe;
  the truncation footgun is not reachable.
- **lark-cli writes API errors to stderr, not stdout** — when the
  Lark API rejects a call (e.g. `permission.members create` with code
  1063003), `result.returncode` is `1` and the JSON error blob lands
  on stderr. The helper parses both streams; an "ok=true" payload on
  stdout AND an error JSON on stderr are both first-class signals.
- **Self-grant collapses to "already_granted"** — granting view to
  the doc owner returns code 1063003 "Invalid operation". For a
  freshly-created doc, the only reason this code can fire is "this
  user already has access", so the helper rolls 1063003 / 1700101 /
  1700107 into a single `note: "already_granted"` success. This
  matters when approver and developer are the same person — the
  spawn shouldn't fail because both `--grant-view` open_ids resolve
  to the doc creator.
- **`permission.public update` is `attempted: true, applied: false`
  in steady state** — the surface was dropped from the current
  lark-cli; the helper attempts it, recognises the parent-help
  fallback, and reports `reason: "api_gone"` without failing the
  call. The explicit `permission.members create` calls are what
  actually let the developer open the doc; the public-link line is
  best-effort and **not** load-bearing. Don't add a "fail if
  applied=false" check downstream — it would break every real run.
- **Permission CLI was renamed** — old docs and the cached pattern
  file used singular `drive permission.member create`. The current
  CLI requires plural `permission.members`. The helper uses the
  current form; SKILL.md / SKILL_zh.md / `lark_cli_patterns.md`
  references should track this.

It does *not* solve "spawn that does nothing" detection in general (§1.3.1) —
just makes the most common cause unreachable.

#### 1.4.4 Spawn prompts are rendered by `render_spawn_prompt.py`, not hand-built  (was §3.47)

The parent session's only per-cycle LLM-orchestration job is spawning the
review agents — everything mechanical (routing, ack, approve/revision/close,
merge queue) already drains in-process in the daemon. But "build the spawn
prompt by hand each notification" leaked judgment into the hot path: the
`expected_phase` normalization (null → `main`), the state→model mapping, the
event→telemetry classification, and the load-cpp-conventions-but-not-on-C#
decision were all re-derived by eye every cycle, and the post-completion
`activity_logger --tokens` call was logged only when the parent remembered
(hence the `collect_session_tokens.py` back-fill). `render_spawn_prompt.py`
makes all of that deterministic: it reads `dispatch_plan.json` and emits one
`spec` per work item with `model`, `prompt`, `expected_phase`/`expected_state`,
`event_type`, a deterministic `lang_hint` (C++ → load cpp-conventions; C# →
explicitly do NOT — the bug that mislabeled `_tools` C# under C++ conventions),
and a prebuilt `telemetry.argv`. The parent just iterates `specs` and fires
`Agent(model=…, prompt=…)`. `--all` caps at `parallel_limit` and drops
lock-held-fresh topics (`skip_reason: "lock_held_fresh"`) so re-fires of an
in-flight topic don't double-spawn.

Two prompt modes. **`compact`** (default) emits the resolved inputs plus
"read & execute `spawn_topic_agent.md`", so the ~50 KB contract is read in the
*agent's* context, not the parent's. **`full`** inlines the rendered template
(placeholder substitution, fail-loud on any leftover `{{…}}`) for sessions that
want the contract on the wire. The helper forces UTF-8 stdout — a Windows
cp936 console otherwise raises `UnicodeEncodeError` on the template's non-ASCII
and corrupts the JSON the parent captures. The model map lives in
`_MODEL_BY_EVENT` (every event type → `opus` today); the hook-reminder preamble
(§ the agent treats `git commit` PreToolUse nudges as turn-ending) is prepended
to every prompt regardless of `spawn_topic_agent.md` edits.

**Why a helper and not a `/workflows` script.** The `Workflow` tool is a finite,
run-to-completion batch orchestrator — it cannot subscribe to the Monitor or
idle for the bot's ~18 h lifetime, so the *outer* event loop (daemon → Monitor
→ task-notification) can't become a workflow. The *per-cycle fan-out* could, but
the architecture already pushes ~90 % of orchestration into in-process Python;
porting the thin remaining spawn step to a workflow adds invocation overhead and
opt-in friction while a render helper captures the actual win (prompt-drift
elimination + guaranteed telemetry) with neither. Reconsider a fan-out workflow
only if cycles routinely carry 3-4 topics and schema'd parallel result
collection becomes worth it — invoked *from* the parent per notification, never
as a replacement for it.

The helper also resolves the **@-mention targets** at spawn time —
`DEVELOPER_ID` / `DEVELOPER_NAME` from `identity.creator_open_id` /
`identity.developer`, plus the fixed `APPROVER_NAME` (`审查人`) — and injects
them into the prompt with an explicit "do NOT search for a contacts cache"
line. Transcript review found topic agents spending 2-4 Bash calls per spawn
hunting for a `lark-contact-cache` file (absent in many workspaces) only to
fall back to the generic `开发者` label anyway. The display name is the ack
handler's to resolve once (into `identity.developer`); a null name yields the
generic label deterministically in the helper, not rediscovered — and wrongly
— by every spawn.

#### 1.4.5 Incremental diff base is per-repo and ancestry-checked (`incr_base.py`)

`review.last_review_commit` is the base for the next round's incremental
diff. It is a **per-repo dict** `{"rage": sha, "chaos": sha}`, not a scalar.
A scalar can only hold one repo's SHA, so on a cross-repo topic the *other*
repo's round-N diff is computed against a foreign SHA — `git diff
<rage-sha>..<chaos-head>` in the chaos repo fails (the SHA isn't there), the
repo is silently pruned, and the dev's chaos changes go un-reviewed. Observed
on RAGE-17494: round 2's chaos modifications were never diffed because
`last_review_commit` held only the rage head. `incr_base.expected_sha_for`
resolves the per-repo base (and still accepts a legacy scalar for single-repo
topics); the topic agent writes the dict via the `set_review_field`
post-action.

A stored base is only a valid two-dot base while it remains an ancestor of
the head. After a rebase / force-push it is not — and may be orphaned
(unfetchable). A naive `git diff base..head` then either fails or, if the
base is still local, silently absorbs every target-branch commit the rebase
pulled in, drowning the incremental review in unrelated churn.
`incr_base.resolve_incr_range` guards this with `git merge-base
--is-ancestor`: ancestor → two-dot `base..head` (true delta); non-ancestor or
missing base → three-dot `origin/<target>...head` (the round-1-style full
branch diff vs the merge-base with the target, correct regardless of rebase),
flagged `mode: "rebased_full"`. `incr_cache.precompute_for_topic` and
`ack_dev_reply._incremental_stats` both route through this helper, and
`spawn_topic_agent.md` mirrors it for the agent's own fetch. Flagged-issue
big-picture verification (§1.4 agent rule 4) is unaffected — it re-greps the
file at HEAD, independent of the diff base.

#### 1.4.6 Spawn prompts point at a spec file — the parent must not pay for them twice

The parent session's context is a real budget: it is the operator surface where
undefined behaviour gets diagnosed live (§1.1.1), so whatever the dispatch loop
spends is unavailable for debugging. Measured on a production spawn, each topic
cost the parent ~3.5 K tokens it never needs again:

| | tokens |
|---|---|
| Monitor notification JSON | ~150 |
| `render_spawn_prompt --all` output (full spec, incl. prompt + telemetry argv) | ~1500 |
| the same prompt repeated into the `Agent` call | ~900 |
| the agent's prose result summary | ~700 |
| per-spawn telemetry Bash call | ~150 |

The `compact` mode was already "small" by inlining only the resolved inputs
instead of the whole contract — but the parent still pays for that text
**twice**, because a prompt must be both read out of the tool result and passed
into `Agent`. Shrinking the prompt is therefore worth double its size.

`--mode ref` (the default) writes the resolved values to
`cfg/spawn/<thread>__<cycle>.json` and reduces the prompt to a pointer at that
file plus the contract path. The agent reads its own inputs — it has a whole
context of its own, and the read is one cheap tool round there instead of ~2 K
tokens here. Output is trimmed to `{thread_id, ticket_id, model, description,
prompt}`; everything dropped (topic_file, lock_file, expected_*, event_type,
triage, mrs_summary, lang_hint, developer_*, pending_count, telemetry argv) is
either in the spec file or recoverable from the plan. `compact` and `full`
remain for debugging and still emit the full spec dict.

Filenames sanitize both components — a `cycle_id` is an ISO timestamp
containing `:`, which is illegal in a Windows filename.

The other two costs are addressed alongside: the contract's §6 now requires the
agent's **final message to be the result JSON line and nothing else** (the prose
recap was written for a human reader that does not exist — the parent is a
dispatcher), and per-spawn telemetry moved out of the parent into the daemon,
which runs `collect_session_tokens.py` on an interval (§1.4.7). Together these
take a spawn from ~3.5 K to roughly ~400 tokens of parent context.

#### 1.4.7 Token telemetry is collected by the daemon, not logged by the parent

`activity_logger.py --tokens` had to be invoked by the parent right after each
Agent call — a tool round plus its output, per spawn, purely for bookkeeping.
It was also lossy by construction: if the parent forgot, or the session was
recycled between the spawn and the log call, the spawn went uncounted.

It was also **pure duplication**: the dispatcher has run
`collect_session_tokens.py` at Step 7b of every cycle since it was added, which
reconstructs the same `spawn_tokens` records losslessly from the Claude Code
subagent transcripts, deduped by agent_id via `cfg/token_ledger.json` and
bounded by `--max-age-hours`. Both paths wrote to the same log; the parent was
paying context to log what the daemon logged anyway seconds later.

So the parent simply stops calling it — no new machinery, one instruction
deleted from SKILL.md §2d. The daemon path is strictly better: zero parent
context, survives a session restart, and cannot be forgotten. Failures are
logged and never abort a cycle — telemetry is bookkeeping, not pipeline state.

The manual `activity_logger.py --tokens` path still works for ad-hoc use, and
`render_spawn_prompt --mode compact/full` still emits a ready-to-run
`telemetry.argv` for it.

#### 1.4.8 A confirmed-fixed issue is not re-verified next round

Round N verifies every entry in `review.flagged_issues[]`. Once round N has
confirmed issue #m fixed, checking it again in round N+1 buys nothing: it costs
a file re-grep per issue and it lets an unchanged fix draw a different verdict
the second time, which reads to the approver as the developer having broken
something. A settled issue is carried forward with the verdict it already
earned, and only the unsettled ones are re-verified.

**Settled** is `addressed` (the developer fixed it) or `obsolete` (the code the
issue described is gone). `not_addressed`, `partially_addressed`, and `unclear`
all re-verify — the developer has pushed since, so those verdicts describe code
that no longer exists.

**The verdicts were being erased before anything could use them.** Verdicts are
merged into the `flagged_issues` entries (`finalize_review._apply_topic_updates`),
but `flagged_issues` is *rebuilt from `review.issues`* every time the approver
replies with indices — `_handle_revision` and the arbitration tail
`_post_fix_list_and_transition` both assign a fresh list of pristine copies, and
`review.issues` holds the round-1 entries, which never carry a verdict. So the
normal loop — round N → approver picks indices → `*_REVISION` → round N+1 —
reset every verdict on the way through, and round N+1 re-verified issues round N
had already signed off. Skipping the re-check without fixing the rebuild would
have been inert; fixing the rebuild without the skip would have changed nothing
observable. Both halves are the fix:

| where | what |
|---|---|
| `review_rounds.carry_verification` | called at both rebuild sites — copies settled verdicts (and their rationale / `verified_at_sha`) onto the fresh entries |
| `review_rounds.split_flagged_for_verification` | splits `flagged_issues` into `(to_verify, carried)` |
| `render_spawn_prompt._build_spec` | puts both index lists in the spawn Context, so "which issues do I re-check?" is never the agent's judgement call |
| `spawn_topic_agent.md` rule 4 | verify only the unsettled set; carry the rest into `VERIFIED_ISSUES` with their stored verdict and never re-send them in `flagged_issue_verifications` |

The predicate lives in one module (`review_rounds.py`) because the rebuild side
and the spawn side must agree exactly — if they drifted, an issue would either
be re-checked against the ledger's word or skipped with nobody having looked.

Carried entries stay in the round-N+1 post: the 问题复查 section still lists #m
as fixed, so the approver reads one complete ledger rather than a list that
silently shrinks each round.

**The escape hatch is the approver, and it already works.** Re-flagging an index
copies a *pristine* entry out of `review.issues`, which has no verdict on it, so
the issue re-arms and gets verified again — the way back in if a developer
reverts a fix the bot already blessed. That also bounds the trade-off being
made here: between rounds, a reverted fix goes unnoticed unless someone
re-flags it.

### 1.5 State machine (`state_machine.py`)

Round 1, either phase (inverted triage, §1.23): `TRIAGING → INLINE_REVIEW | FULL_REVIEW → DEV_TRIAGE → ARBITRATION → SIMPLE_REVISION | FULL_REVISION | APPROVED`. Zero-issue round 1 and round N keep the legacy decision states: `INLINE_REVIEW | FULL_REVIEW → TRIAGE_DECISION | AWAITING_APPROVAL → APPROVED → MERGED | CLOSED`, with `SIMPLE_REVISION` / `FULL_REVISION` loops (each `dev_reply` returns to `TRIAGE_DECISION` / `AWAITING_APPROVAL`) and terminal `CLOSED`. `APPROVED` is transient — the merge queue resolves it to `MERGED` or `CLOSED`.

### 1.6 Merge queue (`process_merge_queue.py`, `merge_tracker.py`)

- Main queue (rage/chaos): sequential rebase (skip_ci) + direct merge via GitLab API PUT.
- Separate 3rd-party queue — 3rd-party MRs must not block the main FIFO.
- Per-MR pipeline polling (`check_pipeline_mr`), MR-state polling (`check_mr`).
- **Infra-failure auto-retry** (`check_infra_failure_and_retry`): fetch failed-job trace, match short-log infra patterns, retry via `POST /jobs/{id}/retry`. Falls back to `/merge_requests/{iid}/pipelines` when `head_pipeline` has moved on (superseded).
- Already-merged / externally-closed MR defensive skip.

#### 1.6.1 MR phase filtering (`_active_mrs`)  (was §3.1)

Within a single topic, `mrs` may contain entries from both phases (3rd-party entries remain after merge with `state="merged"`). Always filter by current `review_phase` before iterating:

- `review_phase == "3rd_party"` → only `mrs` keys starting with `"3rd_party/"`
- `review_phase == "main"` (or null) → only keys NOT starting with `"3rd_party/"`

This is an **MR-level** filter *within* a topic. An earlier topic-level skip (`if review_phase == "3rd_party": continue`) was too coarse and prevented 3rd-party topics from entering any merge queue.

#### 1.6.2 Merge queue post-merge transitions  (was §3.2)

Must branch on phase after a successful merge:

| Phase | Post-merge action |
|-------|-------------------|
| `"main"` | Set state `MERGED`, call `archive_topic()` (terminal) |
| `"3rd_party"` | Reset to `TRIAGING` / main, mark 3rd-party mrs as merged (non-terminal), **enqueue a synthetic pending event** (§1.6.12) |

#### 1.6.3 Already-merged MR skip (defensive)  (was §3.3)

`process_merge_queue.py` (in the per-repo merge loop) skips MRs where `state == "merged"` or `pipeline_status == "merged"`. This is NOT redundant with phase filtering — it catches MRs merged externally (someone clicked Merge in GitLab). Keep it.

#### 1.6.4 3rd-party repo pipelines  (was §3.7)

3rd-party repo (`3rd_party_cpplibs/*`) CI pipelines may not be well-maintained:

- Some repos have no pipeline at all
- Some pipelines always fail due to stale configs

**Rule**: If a 3rd-party MR has no `head_pipeline` (missing or null), treat it as `pipeline_status = "passed"` — same logic the game repo uses. If a pipeline exists, wait for it to complete normally. Do NOT block a 3rd-party MR indefinitely on a broken pipeline.

#### 1.6.5 Infra-failure retry API choice  (was §3.8)

`merge_tracker.check_infra_failure_and_retry` uses `POST /jobs/{id}/retry` — the only reliable API that matches the GitLab UI's per-row "Run again" button.

Do NOT use:
- `POST /pipelines/{id}/retry` — returns "skipped" when no failed jobs remain at that level (happens when head_pipeline is superseded).
- `POST /projects/{id}/pipeline?ref=BRANCH` — rejected with "resulting pipeline would be empty" because `.gitlab-ci.yml` restricts to `merge_request_event`.

When `head_pipeline.status != "failed"`, fall back to `/merge_requests/{iid}/pipelines` and pick the most-recent failed pipeline from history.

#### 1.6.6 Merge queue: skip_ci rebase + direct merge  (was §3.9)

The developer's commit already triggered CI. By the time the merge queue runs, the pipeline is already `"success"`. `merge_tracker.rebase_mr` uses `skip_ci=true` to avoid a redundant pipeline run, then `merge_tracker.merge_mr` uses GitLab API PUT for immediate merge.

Do NOT use:
- `glab mr merge --auto-merge` — sets `merge_when_pipeline_succeeds`, which hangs after a skip_ci rebase.
- `glab mr rebase` CLI — doesn't support skip_ci; use the API endpoint.

**30s pipeline-settle gate** (`dispatcher._check_approved_topics`):
GitLab's `detailed_merge_status` lags `head_pipeline.status=success` by
up to ~60s. Merging inside that window returns `405 Method Not Allowed`
even though the UI shows "Ready to merge". Observed repeatedly
(RAGE-13003 ×3, RAGE-13672 ×1: pipeline finished 23:26:58 → bot merged
23:27:59 → 405; manual retry at 23:31:24 → succeeded on identical
endpoint/auth).

Fix lives upstream of the merge call, not inside it:

- When `_check_approved_topics` first observes an MR's `pipeline_status`
  flip to `"passed"`, it stamps `mr_obj["pipeline_passed_at_ms"]`.
- `queue_ready` stays `False` until `now - stamp >= 30_000` ms, so the
  topic only lands on `merge_queue` one dispatcher cycle later.
- If pipeline later leaves `"passed"` (infra retry, rerun), the stamp
  is cleared so the next transition re-starts the window.

`merge_mr` still retries up to 3× with 5s spacing on 405 as a short
safety net for topics that slip through right at the edge. Other
error codes (406 conflict, non-405) fail fast.

**Post-rebase variant**: same race shape on a separate cache —
`detailed_merge_status` can read a stale `"mergeable"` from the
pre-rebase state for several seconds after `rebase_in_progress`
flips false. Observed RAGE-14156/14155 (2026-04-24): settled in
~0.5s, merge 405'd 12s later. `wait_for_mergeable` now requires
`last_sha != previous_sha` (rebase landed) AND `seen_non_mergeable`
(cache invalidated) before accepting `"mergeable"`; `previous_sha`
is captured by `rebase_mr` *before* the PUT /rebase and threaded
through `process_merge_queue`.

#### 1.6.7 Rebase conflict: park the topic, wait for dev `ok`, then resume  (was §3.29)

A merge-queue rebase **content conflict** can't self-resolve — only the dev can
rebase/push. The old behavior left the topic `APPROVED` and re-attempted the
rebase every ~90 s forever (pure waste; silent after the first notice —
RAGE-14892 / RAGE-13943).

**Flow (dev-driven, fully mechanical — no Claude spawn):**

1. **Park on first conflict.** `process_merge_queue.process_one_topic`
   sets `review.rebase_conflict_blocked = True`, records the
   conflict-time head per repo in `review.rebase_conflict_shas`, and
   posts the `rebase_conflict` template (still SHA-gated against
   duplicate *text* via the `rebase_conflict_notified` audit /
   `_has_recent_conflict_notification`). The template now tells the
   developer to rebase locally, push, **and reply `ok`**, and states
   the bot will not auto-retry until then. `_check_approved_topics`
   skips any topic with `rebase_conflict_blocked` — the exact shape of
   the `merge_manual_required` circuit breaker — so the per-cycle
   rebase storm stops.

2. **Dev rebases, pushes, replies `ok`.** The router stamps the `ok`
   as a pending `dev_reply` event even in `APPROVED` (it is not
   dropped at ingest).

3. **Mechanical `ok` handler.**
   `mechanical_reply_handler.drain_rebase_conflict_ack` runs in the
   dispatcher cycle right after `drain_no_new_commits` and before
   `_find_work`, so the parent never sees the `ok`. It `git ls-remote`s
   each parked repo and compares to `rebase_conflict_shas`:
   - **Advanced** (they pushed) → clear the flag, post `merge_resuming`
     ("流水线运行中，完成后将自动合并。"), drain the event. The topic
     re-enters the normal `APPROVED` flow next cycle, which waits for
     the push-triggered pipeline to pass, then auto-merges — exactly
     what the message promises.
   - **Not advanced** (typed `ok` without pushing) → post
     `rebase_no_push` ("未检测到新提交，请先推送…再回复 ok"), keep the
     topic parked, drain the event (the dev re-replies `ok` after
     actually pushing).
   - **ls-remote failure** → leave the event for the next cycle (never
     claim "no push" on partial info — same safe default as
     `drain_no_new_commits`).

**Why a flag, not a `MERGE_BLOCKED` state:** parking is identical in
shape to `merge_manual_required` — `APPROVED` stays the state, a
`review` flag gates merge-queue re-entry. A dedicated state would
ripple into `state_machine.py`, `recover`, `_find_work`, status, and
the §2 agent contract for no behavioral gain. `status_report` surfaces
the park as a `⛔ rebase-conflict` marker on the topic row.

**Why SHA-verify the `ok`:** the `ok` is the resume trigger (so the bot
is not polling the MR), but a bare `ok` with no push would just
re-conflict on the next merge attempt and re-park. Verifying the branch
advanced first turns that wasted round-trip into an immediate "no new
commits" reply. (This supersedes the earlier "no `dev_reply` event
needed, the SHA gate is enough" reasoning: the gate prevented duplicate
*text* but not the retry storm — parking does.)

Independent of the `merge_failed` N-strike escalation (405/403 → @mention dev +
approver for manual merge): that path is for merges the bot is *forbidden* to
perform, not content conflicts the dev must resolve.

#### 1.6.8 A 405 on `PUT /merge` may mean "already merged" — recheck before alarming  (was §3.39)

GitLab's merge endpoint (`PUT /merge_requests/:iid/merge`) returns
`405 Method Not Allowed` whenever the MR is not in an open, mergeable
state. Crucially, that includes an **already-merged** MR — the same
status code it returns for a genuine config-reject (required second
approver, unresolved threads, protected-branch rule). The PUT response
alone cannot tell the two apart.

**Symptom (RAGE-15898).** The merge queue merged chaos!2546 and posted
`✅ 已合入`, then a *second* merge attempt fired against the same,
now-already-merged MR. GitLab answered 405; `process_merge_queue`
classified 405 as non-retryable (`_NON_RETRYABLE_MERGE_ERRORS`), tripped
the `merge_manual_required` circuit breaker, posted `⚠️ 自动合并失败，
需要手动合并`, and @mentioned the dev + approver — a false alarm on an
MR that was already merged. The successful pass's `MERGED` transition was
also lost (overwritten by the failing pass's topic write — the archived
record carried `merge_manual_required: true` and **no**
`merge_queue_merged` audit), leaving the topic parked as
`APPROVED + merge_manual_required` until a `recover` run reconciled it to
`MERGED` against GitLab.

**Rule:** a failed merge PUT is not a failure until live state confirms it.
`merge_tracker.merge_mr` re-fetches MR state via `check_mr` after a failed PUT
(405 or unexpected `state`): `state == "merged"` → `{"success": True,
"already_merged": True}` (audited `merge_idempotent_already_merged`, clearing any
stale `merge_manual_required` so the topic archives clean), while a genuine
config-reject still reads `opened` and flows to the manual-merge alarm as before.
The second attempt's root cause — duplicate/concurrent merge-queue processing of
one APPROVED topic (no per-topic merge lock; the same orphan-daemon hazard §1.1.2
guards against) — is a separate follow-up.

#### 1.6.9 Pipeline Status Lifecycle  (was §4)

`mr_obj["pipeline_status"]` flows through three approval paths and one
merge-queue gate. They all MUST write the value through
`merge_tracker.recheck_pipeline_with_retry` (main) or
`merge_tracker.check_pipeline_mr` (3p) — never inline their own mapping.

**Main-phase** writer chain:

```
mechanical_reply_handler._handle_approve
  ├── glab approve
  └── post_approval.post_approval_on_topic
         └── merge_tracker.recheck_pipeline_with_retry
                ├── check_pipeline_mr       (raw status)
                ├── check_infra_failure_and_retry (if raw=="failed")
                └── maps raw → passed|running|failed, writes mr_obj

dispatcher._check_approved_topics (each cycle, APPROVED topics)
  └── merge_tracker.recheck_pipeline_with_retry   (same helper)

3rd-party topic-agent §2.1
  └── glab approve; state=APPROVED; dispatcher writes pipeline_status
      on its next cycle (see below)
```

**3rd-party** gate (dispatcher `_check_approved_topics`):

- Uses `merge_tracker.check_pipeline_mr` directly — **no infra retry**,
  because many 3rd-party repos have broken/absent CI by default.
- Status mapping: `running`/`pending`/`created` → `running` (wait);
  everything else (success, failed, canceled, none) → treat as done,
  enqueue to `3p_merge_queue` regardless of pass/fail.

**Main-phase** gate: `pipeline_status` must equal `"passed"` for every
MR before the topic joins `merge_queue`.

**Reset**: a 3p→main phase reset (`process_merge_queue.py`) pops
`pipeline_status` from every main-phase MR so the next
`_check_approved_topics` pass re-derives it from a fresh gitlab call.

**Lint guard (#14)**: no module outside the writer chain above may
assign `mr_obj["pipeline_status"]`. The `replay.py lint-pipeline-status-writes`
subcommand greps for violations.

---

#### 1.6.10 Merge Queue Invariants  (was §5)

1. **Snapshot**. The dispatcher builds `merge_queue` / `3p_merge_queue`
   entries from a point-in-time view of each APPROVED topic. Each entry
   carries `phase`, `mrs`, `approval_ts`, `ticket_id`, `topic_file`.
2. **Re-verify before acting**. `process_merge_queue.process_one_topic`
   runs `agent_preflight.check_phase(topic_file, entry["phase"])` before
   any rebase/merge. On drift (phase flipped, topic archived, developer
   pushed new revision) the entry is skipped — not merged — so the next
   dispatcher cycle can re-plan with current state.
3. **Phase ordering**. Within a topic, the merge loop sorts repos
   `3rd_party/* → chaos → rage` so the version-bump never merges before
   the library it points to.
4. **One topic, one lock**. The queue processes topics serially inside
   each cycle; cross-topic parallelism is OK but the same `thread_id`
   must never be worked on by two processes.
5. **Terminal handoff**. On full merge:
   - main-phase → `review.state = MERGED`, archive to `closed/`
   - 3rd-party phase → `review.state = TRIAGING`, `review_phase = main`,
     pop `pipeline_status`/`issues`/`flagged_issues`, append
     `phase_reset` audit, topic stays open.

#### 1.6.11 Mechanical terminal transitions must post an honest thread notice (`terminal_notice.py`)

The developer / approver usually see **only the Lark thread**, not the topic
JSON or `activity.log`. So every terminal transition (`MERGED` / `CLOSED`)
must leave a reply in the thread, or the thread silently freezes at the last
message while the real state moves on.

The *active* merge path (`process_merge_queue.process_one_topic`) always posted
a `merged` notice. But two **passive reconcile paths** transitioned the topic
and archived it **without posting anything**:

- `dispatcher._check_approved_topics` — catches merges/closes done outside the
  bot's own queue: a **manual merge** (e.g. the 01:00 stop left a topic parked
  in the merge queue and a human merged it), a **GitLab-side MR close**, or any
  state that completed during downtime. It set `MERGED`/`CLOSED`, archived, and
  logged `merge_tracker merge_detected: … -> MERGED` — but the thread saw
  nothing.
- `recover.py` — the downtime catch-up reconcile; same silent archive.

Both now call `terminal_notice.post_terminal_notice(topic, new_state)` before
the archive. The helper renders `merged` (MERGED) or `mr_closed` (CLOSED),
posts to the topic root, and is **idempotent** via
`lifecycle.terminal_notice_posted` — a retried cycle (post ok but archive
failed) or a topic the active path already notified never double-posts. The
active path routes through the same helper so it sets the flag too.

Companion correctness fix: `_check_approved_topics` previously archived with a
bare `p.rename(dest)` + manual `topic_index.remove`, which orphaned the sibling
inbox (the exact failure §1.1.3 describes for `recover.py`). It now archives via
`topic_store.archive_topic`, which removes the file, the index entry, **and** the
inbox in one call.

#### 1.6.12 The 3rd-party → main phase reset must enqueue its own work item (RAGE-23816)

Every other topic transition is driven by an inbound Lark message, so
`events.pending[]` is non-empty by construction and `dispatcher._find_work`
(which surfaces **only** topics with a pending event) picks the topic up on the
next cycle. The 3rd-party → main phase reset in §1.6.2 is the one exception: it
is triggered by a *merge the bot itself performed*, with no message behind it.
It set `state = TRIAGING` / `review_phase = "main"` / `review_round = 0` and
stopped — leaving a topic that looks ready for review but that nothing in the
pipeline ever dispatches. The main-repo review simply never started, silently
and forever.

Observed on RAGE-23816, the first topic to reach a 3rd-party merge: scaleform
`!14` merged, the thread got `✅ 第三方库 MR 已合并。正在继续审查主仓库代码…`,
and then nothing. No error, no stuck lock, no pending event — `work=0` every
cycle. Nothing distinguished it from an idle topic.

Fix: `_enqueue_phase_reset_event` appends a synthetic entry shaped like a router
inbox record (`source: "3p_phase_reset"`, `event_id:
"phase_reset:<thread>:<ms>"`, sender = the developer, content = the ticket id).
It carries **no `intent`**, so `render_spawn_prompt._classify_event` buckets it
as `new_topic` off the `TRIAGING` state and the agent runs an ordinary round-1
main-phase review. `ack_new_topic.drain_ack` stays idempotent via
`lifecycle.ack_sent`, so the ack does not re-post.

The reset also drops `review.{issues_found, lark_doc_token, lark_doc_url}`
alongside the existing `issues` / `flagged_issues` clear — those belong to the
3rd-party review that just merged, and main-phase round 1 must not inherit the
lib's review doc.

General rule: **a state transition with no inbound message must manufacture its
work item.** Writing the state alone is a no-op in an event-driven dispatcher.

##### The reset must also re-resolve the heads, not just enqueue

Enqueueing alone was still wrong. `mrs[repo].branch_sha` and
`review.ack_stats` are captured **once, at ack time**, and the 3rd-party phase
then runs for hours. The developer is usually still pushing to the consumer MRs
during exactly that window — the lib API they are adapting to is what is under
review — so the ack-time SHA is routinely several commits stale by the time the
main phase starts. Nothing between ack and the main-phase review refreshed it:
the synthetic event carries no SHA of its own, and the agent trusts the stored
`branch_sha`.

RAGE-23816 again, same night as the enqueue fix:

| | |
|---|---|
| 23:28 / 23:36 | ack captures chaos `227623d8`, rage `5b1a46be` |
| 23:46 → 01:49 | 3rd-party phase: review, approve, merge lib MR!14 |
| 00:58 / 01:00 | **developer migrates both repos to the revised lib API** |
| 02:37 | main-phase review runs — against the 23:28/23:36 SHAs |

The review's two 严重 findings were "you call `GetPlaybackFailureCount()` /
`GetLastFailedVideoUrl()`, which do not exist in the merged lib — this will not
compile" and "the count+URL polling is lossy". Both were true of the reviewed
commits and both had been fixed 1.5 h earlier, in the exact way the review went
on to recommend. The developer had to close the topic and refile it by hand.

`_refresh_main_phase_heads` runs before the enqueue: it re-runs
`ack_new_topic._discover_mrs`, refreshes `mr_iid` / `branch` / `target_branch` /
`branch_sha` / `web_url` / `state` for the non-3rd-party repos, recomputes
`ack_stats` per repo via `_fetch_stats`, and recomputes `review.triage` when any
head moved (stats drive the simple/complex thresholds, so a moved head can flip
the verdict). The merged `3rd_party/*` entries are deliberately left untouched.

Refresh failure is **reported, never swallowed**: a `None` return logs a WARN
and appends a `phase_reset_head_refresh_failed` audit entry, and the topic still
advances. Losing the topic entirely would be worse than a stale review, but a
stale review must not look like a clean one — which is the whole failure mode
here. A successful refresh that moved anything logs the before/after SHAs and
appends `phase_reset_heads_refreshed`.

This is the same family as §1.6.7's rebase-resume bug (a drain that acts on a
branch without re-reading its head) and §1.4.5's per-repo `last_review_commit`:
**any handler that reviews or merges must re-resolve the head at the moment it
acts, never trust a SHA captured by an earlier phase.**

---

### 1.7 Mechanical reply handler (`mechanical_reply_handler.py`)

Runs inside the dispatcher cycle (no Claude spawn) for main-phase approver replies:

| Current state | Intent | Mechanical steps | Next state |
|---|---|---|---|
| `TRIAGE_DECISION` / `AWAITING_APPROVAL` | `approve` | Approve each MR, check pipelines, render `approval` template | `APPROVED` |
| `TRIAGE_DECISION` | `revision(indices)` | Filter `review.issues`, populate `flagged_issues`, render `revision_request` | `SIMPLE_REVISION` |
| `AWAITING_APPROVAL` | `revision(indices)` | Same | `FULL_REVISION` |
| `AWAITING_APPROVAL` | `close` | Close each MR, post plain text, archive | `CLOSED` |
| `DEV_TRIAGE` | `dev_triage(indices)` (developer, §1.23.1) | Record `review.dev_triage` (accepted/rejected), render `dev_triage_summary` @approver | `ARBITRATION` |
| `DEV_TRIAGE` | `approve` (approver override) | Same as legacy approve | `APPROVED` |
| `ARBITRATION` | `approve` (`ok`, fix set empty, §1.23.2) | Delegate to `_handle_approve` — the OK doubles as approval | `APPROVED` |
| `ARBITRATION` | `approve` (fix set non-empty) | Refresh manual issues (§1.23.3), `flagged_issues` = accepted, render `revision_request` | `SIMPLE_REVISION` / `FULL_REVISION` by `review.triage` |
| `ARBITRATION` | `revision(indices)` (reinstate, §1.23.2) | `flagged_issues` = accepted ∪ reinstated, render `revision_request` with reinstate prefix | `SIMPLE_REVISION` / `FULL_REVISION` by `review.triage` |
| `SIMPLE_REVISION` / `FULL_REVISION` | dev reply (no bot @-mention) with every repo's `branch_sha` still at `last_review_commit` | `drain_no_new_commits`: refresh SHA via `git ls-remote`, render `no_new_commits` template, drop event | (no state change) |

Takes the topic lock the same way an agent does. Every row applies in both phases (§1.23.5). Non-mechanical intents (`escalate`, `dev_*` other than `dev_triage`, `unknown`) fall through to the Claude agent unchanged. The `no_new_commits` drain also defers to the agent on any `git ls-remote` failure — we prefer a wasted spawn to a false-positive "no new commits" reply (§1.7.3).

#### 1.7.1 Mechanical handler scope limits  (was §3.10)

`mechanical_reply_handler.drain_mechanical` intentionally handles ONLY `approve`/`revision`/`close`/`dev_triage` (plus the ARBITRATION accept/reinstate rows above), in either phase. Out of scope (all defer to Claude):

- `escalate` (`full` / `完整版`) — requires a full inline review by the agent. Applies to both `TRIAGE_DECISION` and `ARBITRATION` (simple triage, §1.23.2).
- `*_REVISION` + dev_reply / dev_question — requires code reading.
- Unknown intent — Claude clarifies.

The handler acquires the topic lock the same way an agent does; if another cycle's agent holds it, the handler skips that topic this cycle.

#### 1.7.2 Approver reply parser: `close` in both states  (was §3.17)

`reply_parser.parse_approver_reply` must accept `close`/`关闭` in BOTH
`TRIAGE_DECISION` and `AWAITING_APPROVAL` (see SKILL.md state machine).
A missing branch makes the input classify as `unknown`, so
`mechanical_reply_handler._classify` returns `None`, the mechanical
drain skips the event, and the dispatcher surfaces it to a (~$$) Opus
topic agent — which correctly refuses to handle main-phase mechanical
intents per `spawn_topic_agent.md`, leaving the event in
`events.pending` forever. The event then re-queues a new Opus spawn
every dispatcher cycle until someone notices. Any new mechanical
intent added here needs the same coverage in both states (and a
matching branch in `_MECHANICAL_STATES` / `_MECHANICAL_INTENTS`).

#### 1.7.3 No-new-commits drain (`mechanical_reply_handler.drain_no_new_commits`)  (was §7.1)

Premise: a developer pings the thread with no @-bot-mention and no new
commits pushed since `review.last_review_commit`. Spawning the topic
agent to discover this costs ~40–60K input tokens; the mechanical path
posts the `no_new_commits` template in ~2–5s.

Entry conditions (all required):

- `review.state ∈ {SIMPLE_REVISION, FULL_REVISION}`
- `review.review_phase != "3rd_party"` (the 3p flow has its own revision semantics)
- `review.last_review_commit` populated
- At least one pending event with `sender_id != APPROVER_ID`, no bot @-mention, and `state_at_arrival != TRIAGING`

SHA freshness check — **mandatory**, not optional: cached
`mrs[repo].branch_sha` is set at ack/agent time and does NOT auto-refresh,
so trusting it in isolation would falsely short-circuit whenever the
dev pushed new commits but the Lark push-notification hadn't woken the
bot. For every non-3rd-party repo on the topic, `drain_no_new_commits`
calls `git ls-remote origin <branch>`, compares that live SHA against
`_expected_sha_for_repo(repo, last_review_commit)`, and:

- **Any match failure** → not a no-op; defer to the agent. No template posted.
- **Any `ls-remote` failure (timeout, network, missing branch)** → defer to the agent. We
  prefer a wasted spawn to a false `no_new_commits` post.
- **Every repo matches** → render template via `render.py --post
  --message-id`, drop the event, append `no_new_commits_drained` +
  `lark_reply_sent` audits, push the event_id onto the recent ring for
  dedup.

`review.last_review_commit` has two on-disk shapes (string for
single-repo topics pre-schema-roll-out; `{repo: sha}` dict for newer
topics). `_expected_sha_for_repo` normalizes both; no migration is
needed. 3rd-party repo keys are filtered out of the comparison so a
post-3p topic with residual 3p MR entries doesn't spuriously defer.

#### 1.7.4 Incremental log/diff cache (`incr_cache.py`)  (was §7.2)

When the SHA check in §1.7.3 does find new commits, the dispatcher's
`_find_work` pre-computes `git log expected..current` and
`git diff expected..current` per repo and writes them to:

```
$WRITE_TMP_DIR/review_bot_incr/{thread_id}_{repo}_{current_sha}_incr.{log,diff}
```

Dispatcher emits `dispatch_plan.work[*].incr_cache = {repo: {log_path,
diff_path, sha, expected_sha}}`. The topic agent reads
`incr_cache[repo]` in its dev_reply branch, verifies
`incr_cache[repo].sha == (freshly ls-remote'd branch_sha)`, and `cat`s
the cached files instead of running `git log` / `git diff`. Saves 2–3
Bash tool rounds per spawn.

Design decisions worth preserving:

- **SHA in filename, not metadata** — the agent's check is a string compare vs a
  refreshed SHA; a dev-pushed-again race self-invalidates (filename no longer
  matches), no atomicity needed.
- **Idempotent populate** — `precompute_for_topic` skips the git round-trip if
  both files exist for `(thread, repo, sha)`; only the cycle after a push pays.
- **Never block dispatch on cache failure** — `_find_work` try/excepts + logs
  WARN; a missing `incr_cache` just means the agent runs git itself (baseline).
- **Scoped to main-phase revision** — 3rd-party / triaging return `None`; the
  cache is only for `dev_reply` incremental review.
- **No GC today** — files accumulate under `$WRITE_TMP_DIR/review_bot_incr/`; a
  handful per topic, OS-managed temp. Add a janitor if it grows.

---

### 1.8 Topic file + locking (`topic_store.py`, `topic_index.py`)

- v3 topic schema (see `SKILL.md` → Per-Topic File Schema). Auto-migrates v2 on read.
- `write_atomic` (unique-suffix tmp + `os.replace`) for every write. Normalizes `iid → mr_iid`.
- Sibling `.lock` files created via `O_CREAT|O_EXCL` (atomic on NTFS + POSIX). 10-minute stale steal. No flock.
- `close_topic` / `archive_topic` distinction (§1.8.1).
- Open-topic index at `cfg/open_topic_index.json` for O(1) "is this root already tracked?" lookups.

#### 1.8.1 `archive_topic()` vs `close_topic()`  (was §3.4)

`close_topic()` always sets state to `"CLOSED"`. The `closed/` directory is an archive for ALL terminal topics, not just `CLOSED` ones.

- `archive_topic()` — moves file + removes from index (no state change)
- `close_topic()` — sets `"CLOSED"` then archives (for non-merge terminal paths)

Callers that set a different terminal state (e.g. `"MERGED"`) must set state first, then call `archive_topic()` directly.

#### 1.8.2 Field name: `mr_iid` vs `iid` (recurring bug)  (was §3.6)

This confusion has caused bugs multiple times. The canonical field is `mr_iid`.

- **Prevention at source**: `topic_store.write_atomic()` normalizes `iid → mr_iid` on every write. `mr_iid` is always present post-write.
- **Safety net**: `merge_tracker._get_iid(mr_obj)` tolerates both field names. Use it when reading — never `mr_obj.get("mr_iid")` directly, in case data predates normalization.

#### 1.8.3 Lock keepalive + supersede-work donation  (was §3.25)

Both fix one 2026-05 failure (RAGE-14888): a ~14-min full review crossed the
10-min `STALE_LOCK_SECONDS` threshold, `_retry_pending_supersedes` archived the
topic mid-flight, and the agent's Lark doc + completed review were thrown away
(re-done by a fresh agent, two orphan docs).

**Lock keepalive** — refresh the lock mtime every 60 s (cap 30 min) so the stale
rule only fires against truly hung agents. The in-process `LockKeepalive` thread
serves single-process callers; topic agents need the detached `keepalive-spawn`
subprocess instead (§1.8.4 — the thread doesn't survive an agent's multi-Bash
flow). With keepalive holding, `_retry_pending_supersedes` defers cycle after
cycle until the agent finishes legitimately (`require_unlocked=True`).

**Work donation** (`topic_store.donate_review_to_topic` +
`agent_preflight._resolve_superseded_by` + `reply_dispatcher._attempt_artifact_donation`)
— the script-side safety net for what keepalive can't cover (crash mid-flight,
work past the 30-min cap); never spawns a Claude subagent. Two trigger surfaces:

1. **Reply-dispatcher** (main path; works even if the agent crashed): an artifact
   whose `thread_id` resolves to an archived topic → `_attempt_artifact_donation`
   parses `lifecycle.closed_reason` for the supersede pattern, rewrites the
   artifact for the canonical successor, and transfers `review.{issues,
   lark_doc_token, lark_doc_url, last_review_commit, review_round, triage}`,
   draining the canonical's pending root event.
2. **Agent-side** (`spawn_topic_agent.md` §1b; token-saver, same cycle):
   `agent_preflight` surfaces `superseded_by`; an agent that already did
   expensive work calls `donate_review_to_topic` before exiting.

**SHA gate**: donation refuses on `sha_mismatch` (canonical MR's `branch_sha` ≠
the SHA the src reviewed against) — donating stale findings would mislead the
approver; the doc is quarantined to `orphan_doc_quarantine.jsonl` for reclaim.
Donation takes the canonical lock under the normal skip-if-fresh contract; if
it's held it returns `"locked"` and retries next cycle (worst case: one
redundant review, same as pre-fix).

#### 1.8.4 `LockKeepalive` is in-process only; topic agents need a detached subprocess  (was §3.35)

`topic_store.LockKeepalive` spawns a daemon **thread** that `os.utime()`s the
lock every 60 s (cap 30 min). Fine for single-process paths
(`process_merge_queue`, `merge_tracker`, dispatcher drains) — but **not** the
topic agent: its work is dozens of separate `Bash`/`Task` subprocesses, and the
thread dies the moment each short-lived Bash process exits. So
`with LockKeepalive(LOCK_FILE):` around "the rest of your work" is structurally
impossible on the harness; the lock mtime ages past the 10-min stale threshold
mid-review (observed RAGE-15557 ~20 min, RAGE-14563 ~15 min — survived only by
luck via the no-respawn rule).

Fix: a detached **subprocess**. `topic_store.py keepalive-spawn --topic <file>`
launches the worker via `subprocess_util.detached_popen` (same machinery as
`start_listener.py`), prints `{"keepalive_pid": …}`, and returns. The worker
outlives the agent's later Bash/Task calls and `/clear`; it self-exits when the
lock disappears (agent `release_lock` — the kill signal, polled each interval;
no sentinel file) or after 30 min. The 60 s / 30 min constants are shared with
the in-process class via `_run_keepalive_loop` (single source of truth).

The janitor still force-clears any lock older than `STALE_LOCK_MINUTES` (30 min),
and `STALE_LOCK_SECONDS` (10 min) stays so the steal window survives a
keepalive-spawn failure. Only `spawn_topic_agent.md` §1 changed (the `with`
block → `keepalive-spawn`); the `LockKeepalive` class is unchanged for
in-process callers.

### 1.9 Template rendering (`templates/render.py`, `templates/*.json`)

Templates (`scripts/templates/*.json`): `review_round1`, `review_round1_dev_triage` (§1.23), `review_roundN`, `revision_request`, `dev_triage_summary` (§1.23.2), `approval`, `no_new_commits`, `merged`, `mr_closed`, `topic_reopened`, `freeform_reply`, `ack_new_topic`, `ack_dev_reply`, `ack_dev_question`, `merge_resuming`, `merge_failed`, `rebase_conflict`, `rebase_no_push`. `{{KEY}}` scalar + `"{{ARRAY_KEY}}"` paragraph-array substitution. `--post --message-id` one-shot for render-and-post.

#### 1.9.1 Lark post format  (was §3.11)

All Lark posts use `--msg-type post` with `{"zh_cn":{...}}`, bold via `"style":["bold"]`, `@` via `"tag":"at"`. Card-style messages with colored title bars are forbidden — they look inconsistent with review posts. Templates in `scripts/templates/` enforce this.

#### 1.9.2 `review_roundN` 问题复查 format pinned in `render.py`  (was §3.31)

`review_roundN.json` accepts `ISSUE_STATUS_PARAGRAPHS` as a raw
paragraph-array slot, the same shape the agent built by hand for
months. The shape drifted: some spawns emitted the verdict emoji
(`✅ 已修复`), some used plain text (`已修复`), some skipped the
severity tag, some included a one-line summary of the round-1
finding and some didn't. The operator picked one canonical shape
from the RAGE-14892 round-2 reply (see `feedback_issue_status_format`
memory) and asked for that to be the standard:

```
#N  [严重|中|轻|建议]  [Repo] file [round-1 summary] — <emoji> <verdict_zh>（rationale）
```

`render.build_issue_status_paragraphs(verified_issues)` enforces this
shape from structured input. Agents now pass `VERIFIED_ISSUES`
(list of `{index, severity, repo, file, verdict, summary?, rationale?}`)
and `expand_structured_vars` rewrites it into `ISSUE_STATUS_PARAGRAPHS`,
mirroring the `MRS`/`FILES`/`ISSUES`/`MANUAL_ISSUES` shortcuts. The
verdict marker dict (`_VERIFICATION_MARKERS`) is the canonical source
of truth for the emoji+verdict_zh pairs and is already reused by
`build_manual_issue_paragraphs` for the 人工审查 section. Passing both
`VERIFIED_ISSUES` and `ISSUE_STATUS_PARAGRAPHS` raises (same
exclusive-input contract as the other structured shortcuts).

`index` is **preserved as round-1 numbering**, not auto-renumbered.
The approver's `1,3,5` reply maps to round-1 indices; renumbering
between rounds would break that correlation. Sort is by `index`
ascending — deterministic and stable across re-runs of the same
re-verification.

#### 1.9.3 `render.py` structurally validates post content (kills the 230001 poison-loop)  (was §3.37)

A Lark post body is `{"<lang>": {"content": [[seg, ...], ...]}}` — `content`
is a list of **paragraphs**, each paragraph a list of **segment dicts**
(`{"tag":"text","text":"…"}`). The template slots `"{{ISSUE_PARAGRAPHS}}"` /
`"{{FILE_SECTION_PARAGRAPHS}}"` are spliced by `render()` directly into the
content list. So if a topic agent hand-builds `ISSUE_PARAGRAPHS` as a bare
string (`"[Chaos] foo.cpp …"`) or a list of markdown strings
(`["**#1 [中]** …"]`) instead of paragraph arrays, `render()` splices the
malformed value verbatim and produces structurally-invalid content.

The failure mode that made this expensive: the old `--check-only` only
checked `validate_required` (missing scalar vars) + the unfilled-`{{KEY}}`
regex. A *filled-but-malformed* `*_PARAGRAPHS` passes both — the artifact
lands in `cfg/replies/`, and `reply_dispatcher` posts it. Lark then rejects
with `code 230001 "content format of the post type is incorrect"`. Because
the artifact is well-formed JSON and the var is *present*, every retry fails
identically → 3 retries → quarantine, **silent on the user side** (only
`[WARN] reply_post_error` in `activity.log`). Observed 2026-05-30 on
RAGE-15898 (simple, `FILE_PARAGRAPHS` as string + `ISSUE_PARAGRAPHS: ""`) and
RAGE-13943 (complex, `ISSUE_PARAGRAPHS` as list-of-strings) — both reviews
completed correctly but never reached the group; recovery was manual
(rebuild vars with structured `ISSUES`/`FILES`, re-queue).

Fix: `render.validate_post_structure(output)` walks every `*.content` and
asserts each paragraph is a list and each segment is a dict carrying a `tag`.
It runs in **both** the `--check-only` path (so a poison artifact fails at
agent artifact-validation time, the agent audits `artifact_validation_failed`
and aborts — never writing it) and the real render/`--post` path (defence in
depth for callers that skip `--check-only`). The error message names the
likely cause and points to the structured `ISSUES`/`FILES`/`MRS` shortcuts.

Why this layer (not a `reply_dispatcher` guard): the dispatcher already
quarantines after N retries; the problem was that the failure surfaced *late*
(post time) and *silently*. Validating in `render.py` — the single chokepoint
both the agent's `--check-only` and the dispatcher's `--post` go through —
moves the failure to the earliest point where the bad data exists, and makes
the agent contract's "pre-write validation is mandatory" actually sufficient.
The companion guidance change (`spawn_topic_agent.md` §3 rule 2) tells agents
to prefer `ISSUES`/`FILES` over hand-building paragraphs, so the bad form is
avoided rather than just caught.

---

#### 1.9.4 Issue lines carry a structured location prefix (`[Repo] file:line func:`)  (was §3.40)

Round-1 review issues and the developer-facing **revision** list now render
as a single line:

```
#N  [severity]  [Repo] file[:line_range] [function: ]text
```

`render.build_issue_paragraphs` requires `repo` + `file` + `text` (line_range /
function optional) and composes the prefix via `_issue_location_text`; a
whole-file finding degrades to `[Repo] file: text`. `text` is **prose only** —
callers must NOT pre-bake the tag/filename (double-prints).

Scope is narrow: only `ISSUES` (round-1) and `FLAGGED_ISSUES` (revision). The
round-N 问题复查 (`VERIFIED_ISSUES`) keeps its verdict-marker shape (§1.9.2). The
revision list reuses the round-1 line but **preserves each issue's original
`#index`** (approver flagged `#1 #4` → dev sees `#1 #4`), so
`mechanical_reply_handler._flagged_issue_paragraphs` imports
`render._issue_location_text` rather than `build_issue_paragraphs` (which
auto-renumbers from 1).

Coordinated change: `render.py`, `mechanical_reply_handler._flagged_issue_paragraphs`,
and `spawn_topic_agent.md` must land together (else `--check-only` rejects
artifacts); restart the daemon after, since it imports the revision builder.

#### 1.9.5 Full-review Lark doc issue list is rendered, not hand-written

The complex/full-review **Lark doc** body was previously free-form markdown
the topic agent improvised — so whether each issue showed its file + line
range was non-deterministic, and the doc's `#N` numbering could drift from
the terse inline list in the thread reply.

`render.build_doc_issue_markdown` (driven by the
`scripts/templates/render_doc_issues.py` CLI) now renders the doc's `问题详情`
section deterministically from the same `review.issues[]` array that feeds the
thread reply. Each issue heading is

```
#### #N [severity] [Repo] file[:line_range] [（function）]
```

followed by the full Chinese `description`. The renderer reuses
`_SEVERITY_ORDER` + the same `enumerate(..., start=1)` numbering as
`build_issue_paragraphs`, so `#N` in the doc matches `#N` in the thread reply
verbatim (the approver's `1,3,5` reply maps to both). Line range / function
are included only for line-scoped findings — whole-file findings degrade to
`[Repo] file`, mirroring §1.9.4's `_issue_location_text`.

The thread reply needs no code change for this: `_issue_location_text` already
emits `:line_range` when present (§1.9.4). The full-review contract
(`spawn_topic_agent.md` rule #3 / #7) now requires the agent to populate
`line_range` (and `function` when applicable) on line-scoped findings so the
terse inline list still points at the code, not just the file.

The doc body markdown is a different output medium from `render.py`'s Lark
post JSON, so the formatting *function* lives in `render.py` (reusing the
location/severity helpers) but the *entry point* is the sibling
`render_doc_issues.py` — keeping `render.py`'s post-oriented `main()` and
positional `template` arg untouched.

#### 1.9.6 "请以 @bot 开头提问" renders a real bot mention (`BOT_MENTION_SEGMENTS`)

The review copy used to say `如有疑问，请以 "@bot" 开头提问` with `@bot` as
literal text — ambiguous: devs couldn't tell whether to type the literal
string `@bot` or @-mention the bot via the Lark picker. (Both actually work:
`bot_identity.normalize_bot_mention` rewrites a leading structured mention
of THIS bot to the literal `@bot` token before `reply_parser` classifies.)

`render()` now auto-injects a builtin `BOT_MENTION_SEGMENTS` variable — an
inline segment list spliced into the sentence by the existing array-splice
rule:

- `REVIEW_BOT_OPEN_ID` resolvable (env → settings.local.json →
  settings.json, checked per render call so a daemon picks up the
  auto-learned id without a restart) → a real
  `{"tag": "at", "user_id": <ou_...>}` mention. The dev sees the actual
  bot name and taps it like any mention.
- Unresolvable (fresh deployment before `bot_identity`'s auto-learn) →
  the old literal bold `@bot` text, which the parser accepts as-is.

A caller-provided `BOT_MENTION_SEGMENTS` wins over the builtin
(`setdefault`). Consumers: `revision_request` and `review_round1_dev_triage`
footers. Freeform agent copy (SKILL.md reply-instruction blocks) documents
the same convention.

### 1.10 Lark Base tracking (`SKILL.md` → Lark Base Integration)

Per-topic row in `代码审查跟踪` base, fields: ticket ID, developer, branches, MRs, triage result, rounds, issues, status, review doc, created/resolved timestamps. Upserted at state changes.

#### 1.10.1 Lark Base field names  (was §3.14)

Field names with spaces can cause `+record-upsert` failures. After each `+field-create`, verify with `+field-list`.

### 1.11 Replay harness (`replay.py`)

Sandbox the pipeline (router + drain + optional mechanical + merge-queue) against stubbed glab/Lark I/O. Seed from closed topics. Not a substitute for LLM-agent tests.

### 1.12 Cross-session durability

Listener (detached `node.exe`) and daemon (`pythonw poll_dispatch.py --watch`) survive `/clear`. Topic JSONs, index, events, logs are on disk. Only the Monitor + Claude conversation context are rebuilt on `/review-bot start`.

### 1.13 Observability

`cfg/activity.log` (dispatcher-cycle events, rotated at 1MB / 500 lines), `topic.audit[]` (per-topic state transitions + side effects + `triggered_by_event_id` for dedup), `cfg/dispatch_plan.json` (last cycle's diagnostics). The activity log also carries structured `spawn_tokens {JSON}` lines emitted by the parent after each background Agent completes — see §1.13.1 for the capture/summary workflow. The parent-emitted hook is opportunistic; for lossless accounting, `collect_session_tokens.py` walks Claude Code's `projects/<slug>/<session>/subagents/agent-*.jsonl` transcripts, sums each turn's `message.usage`, attributes by `TOPIC_FILE` / `POLL_CYCLE_ID` markers, and appends the same `spawn_tokens` format with agent-id-keyed dedup via `cfg/token_ledger.json`.

#### 1.13.1 Per-spawn token telemetry  (was §8)

Each background `Agent()` that completes in the parent `/review-bot`
workflow emits one `spawn_tokens {JSON}` line to
`cfg/activity.log` via `activity_logger.py --tokens`. The line
mixes fixed metadata (`thread_id`, `state`, `cycle_id`, `ticket_id`,
optional `event_type`) with whatever usage counters the Agent result
envelope surfaces (`input`, `output`, `cache_read`, `wall_s`). Two
design notes:

- **Structured JSON after a fixed `spawn_tokens ` prefix** — the line
  is still a valid `[ts] [LEVEL] <message>` activity.log entry, so
  existing tail/grep habits work. `status_report.py --token-summary`
  parses by locating the prefix, avoiding a ground-up NDJSON log
  format churn.
- **Token-less rows still count**. If the Agent envelope doesn't
  expose usage on a given run, the logger emits metadata-only — the
  summary skips mean/p95 math for that row but keeps the spawn count
  accurate. Better to under-report than to drop the row entirely.

`status_report.py --token-summary [--max-entries N]` groups the
entries by `event_type || state || "unknown"` and reports
`{spawns, input_mean, input_p50, input_p95, output_mean, cache_read_mean}`
per bucket — intentionally a superset of what `--event-type` alone
would cover, so topics tagged only with `--state` aren't hidden.

**Baseline snapshot workflow** (see SKILL.md for commands): grep the
`spawn_tokens` lines out of `activity.log` into
`cfg/baselines/<milestone>.jsonl` after each meaningful landing
(drain_no_new_commits, incr_cache, future T3/T4). Before/after deltas
on those snapshots are what validate whether a tactic actually moved
per-spawn spend; running `--token-summary` against a current log and
against a restored baseline gives comparable numbers without depending
on log rotation.

### 1.14 Manual review issue integration (`gitlab_threads.py`, `manual_issue_verifier.py`)

Pulls human inline review threads (`DiffNote` discussions) from the
GitLab MR into the topic and verifies fix status alongside the bot's
own findings. See §1.14.1 for the asymmetric trust model and the
non-obvious behavioural rules.

**Pipeline**:

- `gitlab_threads.fetch_for_topic` — runs at ack time
  (`ack_new_topic`), populates `review.manual_issues[]` keyed by
  `discussion_id`. Each entry: `{index, discussion_id, note_id,
  author, repo, file, line_old, line_new, base_sha, body, web_url,
  verification, verification_rationale, verified_at_sha,
  marked_resolved_at}`.
- `gitlab_threads.reconcile_manual_issues` — idempotent merge keyed
  by `discussion_id`; preserves verification verdicts when the SHA
  hasn't moved, prunes threads deleted on GitLab.
- `manual_issue_verifier.build_context` + `build_prompt` — given an
  unverified entry, slices original code at `base_sha` (±10 lines),
  current code at HEAD (±10 lines), and the per-file diff into a
  Sonnet 4.6 verification prompt. Five canonical verdicts:
  `addressed | not_addressed | partially_addressed | obsolete | unclear`.
- `gitlab_threads.mark_resolved` — write-back path: when the bot's
  verdict is `addressed` or `obsolete`, sets `resolved=true` on the
  GitLab discussion via `PUT /merge_requests/<iid>/discussions/<id>`,
  then stamps `manual_issues[i].marked_resolved_at`. One-way; never
  writes `resolved=false`.

**When verification fires**:

| Trigger | Behavior |
|---------|----------|
| Round 1 (`new_topic`) | Fetch only; display in the round-1 post with `📌 待验证` markers. No verification — there's no "after" snapshot yet. |
| Arbitration accept / reinstate (§1.23.3) | In-process fetch + reconcile only (mechanical handler) — no verification. Newly added entries are appended to the `revision_request` post with `📌 待验证` markers; verification happens at the next round-N spawn. Non-fatal on glab failure. |
| Round N (`dev_reply` after `ok` push) | Re-fetch + verify all entries with `verified_at_sha != head_sha`. The topic agent adjudicates each entry inline (no sub-agent — it has no `Task` tool). Bot's own `flagged_issues[]` re-review still runs alongside. |
| `manual_refresh` event (`@bot 同步` / `@bot refresh` / `@bot sync`) | Same fetch + verify path, but bot's own findings are NOT re-reviewed. State unchanged. |

**Display**: separate `人工审查（N 条）` section in `review_round1`
and `review_roundN`, after the bot's `问题汇总`. Each entry rendered
as `[M{i}]` (clickable link to the GitLab discussion) + verdict
marker + `[Repo] file.cpp:line — body 短句（author）`. Manual issues
use a separate `[M1]`/`[M2]` numbering — NEVER mixed with the bot's
`#N` numbering (rationale in §1.14.1).

**Cost ceiling**: ~5k tokens per manual issue × N entries per verification cycle
(`manual_refresh` is state-agnostic, open to anyone in the thread). Round-1
display path costs zero verification tokens.

#### 1.14.1 Manual review issues — pitfalls  (was §3.21)

Non-obvious rules a future maintainer will be tempted to "simplify" away:

- **`resolved` is write-only** — never READ GitLab's `resolved` flag (the team
  won't reliably set it); DO write `resolved=true` back when the verdict is
  `addressed`/`obsolete` so the UI reflects the bot. Never write `false` — a
  later regression surfaces as a new `not_addressed` verdict, not a re-opened
  thread. The schema omits a `resolved` field on purpose (only `marked_resolved_at`
  bookkeeping); storing it would invite a maintainer to read it. (RAGE-12657.)
- **Two numbering spaces — `[M1]` vs `#1`**, never mixed. The approver's `1,3,5`
  reply selects **bot** findings for `revision_request` (writes
  `flagged_issues[]`); it has no way to signal GitLab or the human reviewer, so
  indexing a manual issue there would split state between the two systems.
- **Per-issue Sonnet, not Opus, not bulk** — it's a local "does this change
  address this concern?" call. Bulk prompts dilute attention; per-issue parallel
  spawn keeps wall-clock ≈ the slowest single agent.
- **`unclear` is load-bearing** — return it on near-misses (subtle concurrency,
  code moved without obviously addressing the concern). Forcing a verdict produces
  wrong `addressed` calls. NEVER auto-promote `unclear`, NEVER `mark_resolved` on it.
- **Body truncation is display-only** — truncate `body` (~280 chars) for the post
  but verify against the FULL body (fresh API / persisted struct), and keep
  `web_url` so `[M{i}]` opens the full GitLab thread.
- **Author attribution stays GitLab-side** — the post shows the author's GitLab
  username as text only (no Lark @-mention — they may not be in the group); they
  get notified through GitLab via `web_url`.

### 1.15 Autonomous operation

The daemon/monitor/agent pipeline handles everything without user supervision. Do not narrate user actions ("you replied no"), do not say "waiting for trigger". Only respond to direct requests or when reporting agent completion results.

### 1.16 Event-driven daemon (not cron)

Fixed 2-minute cron polling wasted ~30 idle Read calls/hour. The `poll_dispatch.py --watch` daemon scans every 5s but only triggers Claude when work exists — zero idle tokens. Runs via `pythonw` + VBS to avoid the Windows console popup bug in Claude Code (anthropics/claude-code#14828: `spawn()` missing `windowsHide: true`).

### 1.17 Full-review thread reply: doc holds files, thread holds index pointers

Full/complex reviews produce two artifacts: the Lark doc (canonical detail
view, full file list + per-issue rationale) and the thread reply (what the
approver actually reads first). Earlier the thread reply duplicated both
sections inline — same FILES, same full-text ISSUES — adding nothing the
doc didn't already hold and bloating the post for large reviews
(RAGE-12249: 30 files + 15 issues, ~3 screens).

The fix is **asymmetric** because the two sections serve different
purposes:

| Section | Full review (with `DOC_LINK`) | Reason |
|---------|------------------------------|--------|
| FILES   | **OMIT** (`FILES` not passed; `FILE_SECTION_PARAGRAPHS` defaults to `[]`) | The doc already lists files. The thread reply needs no per-file context. |
| ISSUES  | **KEEP** (severity-sorted `#N` numbering, but `text` ≤ ~50 chars) | The approver replies with `1,3,5` to mark issues — that contract requires the numbering be visible in the thread, otherwise the approver has to open the doc, find the index, then come back. The terse `text` (`[Chaos] foo.cpp 路径校验缺失`) is enough to anchor the index; full rationale stays in the doc. |

`review.issues[*].description` MUST persist the **full** description for
revision lookups — only the rendered `text` in the thread post is
shortened. The revision-handler reads `description` to build
`revision_request` paragraphs.

Don't "simplify" by either:
- **Re-adding FILES to the full-review reply** — duplicates the doc; was
  removed deliberately. Simple reviews still pass FILES (no doc).
- **Dropping ISSUES from the full-review reply** ("just link the doc") —
  breaks the index→reply contract; approver has to open the doc to find
  numbering. The whole point of the thread reply is to keep index→reply
  one click away.

Implementation: `templates/review_round1.json` has a single
`{{FILE_SECTION_PARAGRAPHS}}` slot (header + per-file rows + spacer
wrapped together). `render.py` maps `FILES → FILE_SECTION_PARAGRAPHS`
via `build_file_section_paragraphs`; `_OPTIONAL_ARRAY_DEFAULTS` provides
the empty default so omitting `FILES` is valid only for templates that
declare it (currently `review_round1` and `review_round1_dev_triage`).

### 1.18 Reply intent classification (approver namelist + content tokens)

Replies in the topic thread are classified to a **(role, intent)** pair by
the `reply_parser.classify_intent` helper, which the router invokes and stamps
onto the event (`entry["role"]`/`["intent"]`/`["indices"]`) for the mechanical
handler to consume. Classification uses three cheap inputs: the sender open_id
(against the approver namelist and the topic's developer), the message content
(a small token table), and the current topic state.

**Inputs**:

- `env.approver_open_ids: list[str]` — sourced from
  `REVIEW_BOT_APPROVER_OPEN_IDS` in settings.json (comma-separated). Legacy
  single-approver `REVIEW_BOT_APPROVER_OPEN_ID` is appended automatically
  if present and not already in the list. The first entry is the "primary"
  approver and is used for the `@`-mention `user_id` in templates.
- `content` — Lark message text after `event_utils.strip_html_tags` and
  `strip_lark_at_mentions` (which removes both `<at user_id="…">…</at>`
  blocks and `@_user_N` placeholders). Trim whitespace, lowercase for
  matching.

**Decision (in `reply_parser.classify_intent`)**:

```
stripped = content.strip()                       # "" → ignored
sender_is_approver  = sender_id in env.approver_open_ids
sender_is_developer = sender_id == identity.creator_open_id

# Every branch returns a DICT: {role, intent, indices[, exclude, none]}.

# 0. DEV_TRIAGE: the topic dev triages the round-1 issues (§1.23.1), BEFORE
#    the approver block so a dev∩approver operator's indices resolve as
#    dev_triage, not approver revision.
if state == "DEV_TRIAGE" and sender_is_developer:
    rev = parse_indices_with_mode(stripped, allow_none=True)  # 1 3 5 / all / -N / none / 0 / 不修
    if rev is not None:  return {role:"developer", intent:"dev_triage", **rev}

# 1. Approver intents (gated by namelist).
if sender_is_approver:
    # `ok` is the ONLY approve verb — in every decision state
    # (_APPROVER_OK_STATES = {TRIAGE_DECISION, AWAITING_APPROVAL, ARBITRATION,
    # DEV_TRIAGE}); the *_REVISION states are excluded (there `ok` is the dev's
    # re-review trigger). No `pass`/`通过`/`lgtm`/`approved` aliases.
    if state in _APPROVER_OK_STATES and is_ok(stripped):
        return {role:"approver", intent:"approve"}
    if is_close(stripped):                              # close / 关闭
        return {role:"approver", intent:"close"}
    if is_escalate(stripped) and (state == "TRIAGE_DECISION"    # full / 完整版
            or (state == "ARBITRATION" and triage == "simple")):
        return {role:"approver", intent:"escalate"}
    if state != "DEV_TRIAGE":
        rev = parse_indices_with_mode(stripped)         # 1 3 5 / all / -N
        if rev is not None:  return {role:"approver", intent:"revision", **rev}

# 2. Developer close — only the topic's own dev may close their topic.
if sender_is_developer and is_close(stripped):
    return {role:"developer", intent:"close"}

# 3. Dev tokens (open to anyone). `ok` is NOT a dev_reply in DEV_TRIAGE/ARBITRATION.
if state not in {"DEV_TRIAGE", "ARBITRATION"} and is_ok(stripped):
    return {role:"developer", intent:"dev_reply"}
if is_manual_refresh(stripped):                         # @bot 同步 / refresh / sync
    return {role:"any", intent:"manual_refresh"}
if starts_with_bot_at(stripped):
    return {role:"developer", intent:"dev_question"}

# 4. Otherwise drop.
return {role:"ignored", intent:None}
```

`parse_indices_with_mode(stripped[, allow_none])` parses the index grammar:
space-, comma-, and Chinese-comma-separated digit runs, plus `all` (every
issue), the `-N` exclude form (all EXCEPT the listed), and — with `allow_none`
(dev-triage only) — `none`/`不修`/`0` (nothing). It returns `{indices, exclude,
none}`, or `None` on no match; a single all-digit token (`1`, `42`) is one
index. Approve / dev-reply `ok` and `close` go through the `is_ok` / `is_close`
patterns.

`starts_with_bot_at` matches either literal text starting with `@bot `
(whitespace required after, so typos like `@bottom` don't match) or a leading
Lark `<at>` block addressing the bot's open_id (which `lark-cli` exposes
pre-strip). `@bot 同步 / refresh / sync` is the more specific `manual_refresh`
match, checked first.

**Allowed intent matrix**:

| sender ∈ approver_open_ids | content                  | resulting intent  | notes |
|----------------------------|--------------------------|-------------------|-------|
| ✓ (state ∈ `_APPROVER_OK_STATES`) | `ok`              | `approver.approve` | mechanical handler approves + enqueues merge queue. In `DEV_TRIAGE` this overrides the dev-triage step (§1.23.1); in `ARBITRATION` it accepts the dev's triage (§1.23.2) — fix-set emptiness decides APPROVED vs `*_REVISION`. **Only** approve verb (`pass`/`通过`/`lgtm`/`approved` removed). |
| ✓                          | `close` / `关闭`         | `approver.close`  | mechanical handler closes all MRs |
| ✓                          | `full` / `完整版`        | `approver.escalate` | escalate `TRIAGE_DECISION` → `FULL_REVIEW`, or `ARBITRATION` (triage=simple) → full re-review → `DEV_TRIAGE`; ignored in other states |
| ✓ (state ≠ `DEV_TRIAGE`)   | indices (`1 3 5` / `all` / `-N`) | `approver.revision` | mechanical handler stores `flagged_issues` → `*_REVISION`; in `ARBITRATION` indices reinstate dev-rejected issues (§1.23.2); in `DEV_TRIAGE` ignored (wait for arbitration) |
| topic dev (state `DEV_TRIAGE`) | indices / `all` / `-N` / `none` / `0` / `不修` | `developer.dev_triage` | mechanical handler records `review.dev_triage`, posts `dev_triage_summary` → `ARBITRATION` (§1.23.1) |
| topic dev                  | `close` / `关闭` / `no`  | `developer.close` | dev closes their own topic without bouncing through the approver |
| any                        | `ok` (not `DEV_TRIAGE`/`ARBITRATION`) | `developer.dev_reply` | agent re-reviews on new SHA |
| any                        | `@bot 同步` / `refresh` / `sync` | `any.manual_refresh` | re-pull + verify human MR comments (role `any` — no approver/dev split) |
| any                        | starts with `@bot `      | `developer.dev_question` | agent answers in Chinese, no state change |
| any                        | anything else            | `ignored`         | dropped at router with audit `reply_intent_ignored` |
| ✗ (not in namelist)        | `close` / indices / `full` | falls through to dev rules | non-approver typing approver verbs is treated as text; if it doesn't also match `ok` / own-topic `close` / `@bot`, it's dropped |

**Same-person dev/approver case (e.g. self-review by the bot operator)**:
Resolved by **order + state**, not distinct tokens (`ok` now serves both
roles). The DEV_TRIAGE-dev check (step 0) runs before the approver block, so
an operator who is both dev and approver gets round-1 indices classified as
`dev_triage`. And `ok` disambiguates by state: in a decision state
(`_APPROVER_OK_STATES`) an approver `ok` is `approve`; in `*_REVISION` `ok` is
the dev's re-review trigger.

**Why approver replies in `*_REVISION` flow to the agent**:
The mechanical handler's state-table only defines approver verbs in
`TRIAGE_DECISION` / `AWAITING_APPROVAL`. In `*_REVISION` the approver
has already requested revisions; further verbs are ambiguous. Free
text from the approver in `*_REVISION` is treated as a comment and
flows to the topic agent for clarification (existing behavior,
unchanged).

**Why `ok` literal for dev_reply (not "anything from dev")**:
Legacy behavior re-reviewed on every dev thread message, burning an
Opus spawn (200–350K tokens for complex re-reviews) on every off-topic
comment. The narrow `ok` trigger is intentional cost control. Pushing
fixes without `ok` leaves the topic in `*_REVISION` (consistent with
§1.18.1 — bot has no GitLab webhook, the thread message is the trigger).

**Why `@bot ` prefix for dev_question**:
Disambiguates "dev asking the bot" from "dev commenting to humans".
The bot spends Opus tokens only when explicitly addressed. Trailing
space is required to avoid accidental matches on `@bottom`, `@boss`,
etc.

**Implementation surface**: `parse_args.py` merges `REVIEW_BOT_APPROVER_OPEN_IDS`
(CSV) + the legacy singular into `env.approver_open_ids` (`[0]` stays
`env.approver_id` for template @-mentions); `reply_parser.classify_intent` is the
pure decision function; `router.py` stamps `intent`/`indices` on the event (or
drops `ignored` with audit `reply_intent_ignored`, no spawn); `mechanical_reply_handler`
and `spawn_topic_agent.md` §2 read `event.intent` directly, never re-classifying.

**Migration**: the router only stamps newly-routed events; a legacy event already
in `pending[]` with no `intent` falls back to the content-only parser for that one
event. No migration script.

#### 1.18.1 `git push` does not trigger `dev_reply`  (was §3.19)

The bot has no GitLab webhook. Branch advances are only noticed when
the developer posts a Lark thread message after pushing — the message
becomes a `dev_reply` event, the agent then refreshes the MR HEAD via
glab and re-reviews. Pushing without a thread message leaves the topic
stuck in `*_REVISION` indefinitely.

The greeting and per-state instruction posts already implicitly tell the
developer to reply in thread, so this is rarely an issue with separate
developers and approvers. It surfaces with self-reviewed topics
(developer == approver, common with the bot operator) where the user
expects pushes alone to advance state. Operators hitting this should
post any thread message — even a single character is enough.

#### 1.18.2 Drop §2-undefined events at dispatch time, do not spawn  (was §3.30)

`router.py` stamps `intent` / `role` based on `(sender_id,
content, state)`, but the (state × intent × role) **tuple** can be
unstamped or rare-edge: e.g. a developer typed `ok` while the topic
was still in `TRIAGE_DECISION` (approver hadn't replied yet), so
intent=`dev_reply` + role=`developer` + state=`TRIAGE_DECISION` —
which has no row in `spawn_topic_agent.md` §2. The parent dispatcher
previously spawned an agent anyway, which then invented a "preemptive
re-verify" path (50–100k tokens) just to land a freeform_reply
saying "1/4 fixed, waiting on approver". Cute, but not contract-
sanctioned, and the precedent quietly widens the de-facto spec.

**Rule:** if the pending event's `(state × intent × role)` does not
match a row in §2's drain table, the dispatcher drops the event
(`events.pending` pop + audit `reply_intent_ignored_undefined_row`)
**without spawning**. The agent contract stays authoritative; new
behaviors land via SKILL.md + §2 + a real code path, not via parent
improvisation.

Today this is policy enforced by the parent's prompt logic; landing
it in `dispatcher._find_work` or `router._classify` is the natural
follow-up so the rule survives prompt drift. Track via
`reply_intent_ignored_undefined_row` audit volume — anything above
"rare" means there's a real workflow §2 should describe.

### 1.19 Reply path is mechanical: agent writes artifact, script posts

The topic agent does not call `lark-cli im +messages-reply` for thread
replies. Instead it writes a reply artifact JSON under `cfg/replies/`,
and `reply_dispatcher.drain_replies` (in the daemon's poll cycle) reads
the artifact, posts to **the artifact's own `thread_id`**, applies any
declared `post_actions`, and deletes the artifact. Mirrors the
merge_queue pattern.

**Why**: a long-running agent reasoning over multiple topic files for one ticket
can pick the wrong thread (RAGE-13296, May 2026: a FULL_REVIEW agent on T2
conflated three same-ticket topics, bailed without posting, and orphaned its
doc). Removing the post step — the agent no longer has a `--message-id` to get
wrong — makes that class of bug structurally impossible.

**Artifact schema (v1)**: `version`, `thread_id`, `ticket_id`,
`triggered_by_event_id`, `template`, `vars`, `preflight`,
`post_actions`, `orphan_doc_token`. See
`scripts/reply_dispatcher.py` docstring for the full contract.

**Strict allowlists** (`reply_dispatcher`):

- `template` ∈ {`review_round1`, `review_round1_dev_triage`,
  `review_roundN`, `freeform_reply`} — mechanical templates (`approval`,
  `revision_request`, `dev_triage_summary`, `mr_closed`, `merged`,
  `no_new_commits`) keep their direct-post path through
  `mechanical_reply_handler` / `post_approval` / `process_merge_queue`,
  so an artifact attempting them is rejected.
- `post_actions[*].type` ∈ {`set_state`, `set_review_field`}. New
  action types must add (a) the entry in the allowlist, (b) an arm in
  `_apply_post_action`, (c) a paragraph in this section explaining why
  the action belongs in this layer rather than in the agent.

**Idempotency / failure modes**:

- Schema rejection or `thread_id` not pointing at an existing topic →
  artifact deleted, audit `reply_artifact_invalid` /
  `reply_thread_missing` (no infinite retry on poison input).
- `preflight` mismatch (state or phase drifted between agent finish and
  dispatcher consume) → artifact deleted with `reply_drift_skipped`
  audit; the agent must have written it under stale assumptions.
- Lark returns `230011` (root withdrawn) → close the topic, archive,
  delete the orphan doc named in `orphan_doc_token` (operator's
  workspace stays tidy), audit `reply_root_withdrawn` +
  `topic_closed`.
- Transient post failure → leave artifact for next cycle, audit
  recorded as `reply_post_error`.
- Successful post → write `lark_reply_sent` + per-action
  `reply_post_action_applied` audits, apply state mutations, delete
  artifact, release lock.

**Why state advances live in `post_actions`, not in the topic file
write**: the agent doesn't know whether the post will succeed. Stamping
state pre-post means a 230011 rejection still leaves the topic in the
post-review state — a phantom advance. Putting the transition in
`post_actions` defers it until after Lark accepts the post; on
withdrawal we instead route to `topic_closed`.

**Mechanical handlers (`mechanical_reply_handler`, `post_approval`,
`process_merge_queue`) post inline, NOT through artifacts** —
they're already deterministic Python with no agent reasoning to
worry about, so the artifact indirection would just add latency.
Only agent-generated replies (review post, incremental re-review,
dev_question answer) take the artifact path.

#### 1.19.1 Reply-artifact retry-quarantine  (was §3.26)

`reply_dispatcher.drain_replies` previously retried failing artifacts
forever. A 2026-05 RAGE-14898 spawn omitted `SUMMARY` from the
`review_round1` artifact; `render.py validate_required` rejected the
artifact every cycle for ~16 min until the operator manually patched
it in. The dispatcher's only signal was a generic `errors: 1` counter
and a truncated `[WARN] reply_post_error` traceback in
`cfg/activity.log`.

Three orthogonal hardenings now in place:

1. **Pre-write validation in agents** (`render.py --check-only` in
   `spawn_topic_agent.md` §3 rule 2). Agents render-and-discard via
   `render.py` before atomic-renaming the artifact into
   `cfg/replies/`. A missing required var (or unfilled placeholder)
   short-circuits the agent with `status: "error"` + audit
   `artifact_validation_failed` — no poison artifact lands on disk.

2. **`failed_artifacts` diagnostics**. `drain_replies` now collects
   `{filename, ticket, template, thread_id, error}` per failure into
   `summary["failed_artifacts"]`. The dispatcher includes
   `reply_result` in `dispatch_plan.json.diagnostics.reply_dispatcher`
   so the operator can see the failing artifact set at a glance,
   without trawling activity.log for truncated tracebacks.

3. **Quarantine after `RETRY_MAX = 3`**. Per-artifact retry counts
   live in `cfg/replies/.retry_counts.json` (atomic write). On the
   third consecutive failure, the artifact is moved to
   `cfg/replies/quarantine/` and the topic gets an audit
   `reply_artifact_quarantined`. The retry counter is cleared on
   any terminal outcome (posted, drift_skipped, withdrawn,
   thread_missing). The mechanism is conservative: only `errors`
   from `_render_and_post` count as retry-eligible; transient
   `lock_skipped` does NOT increment the counter (it's not a
   failure of the artifact, just a serialization deferral).

Quarantined artifacts are NOT auto-cleaned — operators triage them
by hand (rare; usually means a buggy agent template). Restoring is a
plain `mv quarantine/<name> ../`. The retry-counts file is
self-pruning: each terminal outcome removes its key.

#### 1.19.2 Orphan Lark doc cleanup needs `drive:drive` scope  (was §3.27)

`reply_dispatcher._delete_orphan_doc` calls
`DELETE /open-apis/drive/v1/files/<token>?type=docx` via `lark-cli api
DELETE`. The previous call form (`lark-cli drive files delete --as bot
--params …`) was cargo-culted from training data — that subcommand
DOES NOT EXIST in the lark-cli build (the only `drive files`
subcommand is `copy`), so every invocation exited 1 with help text and
the doc was never deleted.

The corrected raw-API form is the documented endpoint, but the user
identity in this workspace lacks the `drive:drive` scope (the
configured grants are `drive:drive.metadata:readonly`,
`drive:file:download`, `drive:file:upload` — note the absence of plain
`drive:drive`). Without that scope, even the correct DELETE call
silently exits 1 with empty stderr (CLI swallows the 401/403 with no
visible message). Bot identity has even fewer drive scopes.

Until an operator grants `drive:drive` to the user identity:
- `_quarantine_orphan_doc` writes one line to
  `cfg/orphan_doc_quarantine.jsonl` per failed delete, with
  `{doc_token, ticket_id, thread_id, reason, ts}`. Operators grep
  this file when sweeping orphans by hand.
- The supersede orphan path (now donated rather than abandoned via
  §1.8.3) avoids creating new orphans in the common case. The
  remaining surface is root-message withdrawn AFTER the agent created
  the doc — uncommon but not impossible.

Granting the scope is an operator action via the lark-cli auth flow
(re-running `lark-cli auth login` after the app's scope set is updated
in the Lark admin console). Until then, the quarantine file is the
operational signal.

#### 1.19.3 Reply de-dup must exclude mechanical ack `reply_type`s  (was §3.36)

The agent-contract de-dup rule (`spawn_topic_agent.md` §3 rule 5): before
posting, skip if `audit[]` already has a `lark_reply_sent` with the same
`triggered_by_event_id` (stops a crash-restarted agent double-posting).

Footgun: `ack_new_topic` writes a `lark_reply_sent` with
`reply_type:"ack_new_topic"` and the **root event id** — the exact id the
round-1 `review_round1` reply uses (both triggered by the root). An unscoped
match makes the agent see the ack row and falsely drop its own round-1 review.
Same shape on the dev-reply path (`ack_dev_reply` + `review_roundN` share the
`ok` event id; §1.3.2). Observed on RAGE-14984 / RAGE-15141 — the Opus agents
caught it and posted anyway, but a literal implementation would silently drop
the round-1 review of every full-review topic.

Fix: scope the de-dup match to review/decision `reply_type`s (`review_round1`,
`review_roundN`, `revision_request`, `approval`, `close`, `merged`,
`no_new_commits`) and exclude the mechanical acks. The dispatcher-side
`reply_dispatcher` dedup is already correctly scoped; only the agent-contract
version needed the fix.

#### 1.19.4 Agents don't hand-author writeback glue (`finalize_review.py`, `build_review_doc.py`)

The reply-artifact contract used to inline ~70 lines of Python in
`spawn_topic_agent.md` §3 rule 2 (validate → write artifact) plus a §4
"writeback protocol" (drain event → persist `review.*` → audit → `write_atomic`
→ release lock) that the agent reproduced by hand each spawn. Transcript review
across a session's 9 topic agents found every completed one re-authoring this as
a bespoke 2-7 KB `writeback.py` / `finalize.py` / `process_topic.py` — and **two
of seven wrote it twice** (`finalize.py` → `finalize_v2.py`), i.e. the first
hand-derivation of the *fixed* procedure had a bug. Complex reviews additionally
hand-rolled a ~5 KB `build_doc.py` to stitch a doc head onto the (already
deterministic) `render_doc_issues` output before calling `lark_doc_helper`.

This is exactly the project rule "use the model for judgment, not deterministic
work … not format transforms where code can give a deterministic answer". The
fix moves the whole fixed tail behind two CLIs: `finalize_review.py` takes one
result JSON (artifact + `topic_updates`) and does artifact schema-validation
(reusing `reply_dispatcher._validate_artifact` so it never writes an artifact
the dispatcher would reject) + render-validation + atomic artifact write + event
drain + `review.*` persistence + audit + lock release; `build_review_doc.py`
owns the full Chinese doc body (title / 概述 / 变更概览 / `问题详情` rendered from
the same `issues[]`) + docx create + grants. The agent's residual job is pure
judgment: produce the issues array, summary, and verdicts.

**The `post_actions` / `topic_updates` split is preserved, not collapsed.**
`finalize_review` applies `topic_updates` (review work product + event drain)
immediately; `post_actions` (`set_state`, `last_review_commit`, `review_round`)
stay in the artifact and are applied by `reply_dispatcher` only after the Lark
post lands — a topic must not advance on a post that never happened (§1.19).
Artifact is written before the topic so a crash leaves a postable artifact
(deduped by `_already_posted`), never a drained event with no reply.

---

### 1.20 Assigned developer (`RAGE-XXXXX @<dev>`)

**Use case**: an MR is filed by a dev who isn't in the bot's Lark
group. The operator (an approver or any group member) posts the topic
on their behalf. Without intervention, all bot replies (revision
request, no_new_commits, merged) would @-target the operator, not the
real dev.

**Resolution rule** (`event_utils.extract_assigned_dev`):

1. Lark's structured `mentions[]` array (the picker path): pick the
   first non-bot mention, take its `open_id` (nested
   `id.open_id` or flat `open_id` — both shapes occur in
   listener payloads).
2. Literal `@<name>` text after the ticket: regex-extract the name,
   look it up in
   `~/.claude/skills/lark-contact-cache/cfg/org_contacts.md`. Exact
   Name-column match only (fuzzy matches risk pinging the wrong
   person). The cache returns Lark `user_id`, not `open_id` — Lark
   accepts both forms in `<at>` tags so this works as a degraded
   @-mention.
3. No `@` at all: fall back to the sender (preserves prior behavior).

**Identity stamping** (`ack_new_topic._ack_one_topic`): when the
resolution differs from the original sender, write
`identity.creator_open_id ← <assigned dev's open_id>`,
`identity.developer ← <display name>`, and preserve the original
sender as `identity.filed_by_open_id` for the audit trail. Audit
events: `assigned_dev_set` (resolved) /
`assigned_dev_unresolved` (literal name not in cache — display name
is shown without an @-target so the bot doesn't accidentally
@-mention the operator).

**Why we override `creator_open_id` instead of stashing the assigned
dev separately**: every downstream template
(`revision_request`, `no_new_commits`, `merged`) already keys off
`identity.creator_open_id` for the @-target and
`identity.developer` for the display name. Adding a parallel
`assigned_dev_open_id` field would mean threading "use the assigned
one if set, else fall back" through every consumer. One canonical
field is simpler and survives future template additions.

### 1.21 Approver fast-track (`RAGE-XXXXX ok`)

**Use case**: an approver merges a trivial MR (formatting, version bump) without
burning Opus on a review they already eyeballed. `RAGE-XXXXX ok` as the root
message skips the review and puts the topic straight to `APPROVED`; the merge
queue takes over. ~5 s, zero LLM spawns.

**Trigger rule** (strict literal — no synonym list):

1. `event["sender_id"]` ∈ `env.approver_open_ids` — the **original** sender, NOT
   `identity.creator_open_id` (the `@<dev>` rewrite must not let a non-approver
   fast-track on someone's behalf).
2. After stripping the ticket id and any `@<name>`, the remainder is exactly the
   lowercase ASCII token `ok`. Anything else (`pass`, `通过`, `lgtm`, `OK`, extra
   prose) falls through to the normal review flow.

**Pre-checks still run**: missing MR / merged-or-closed MR / `version_3rd.cmake`
mismatch still close the topic with the ⚠️ message. Fast-track skips the
*review*, not the safety nets.

**Implementation** (`ack_new_topic._fast_track_approve`; entry
`drain_ack(approver_open_ids=…)` — passing `None` disables it, so it's opt-in):
persist `mrs`/`ack_stats`/triage like a normal ack, `glab approve` every MR via
the shared `mechanical_reply_handler._glab_approve`, **drain the root event**
(§1.21.1), then delegate to `post_approval.post_approval_on_topic` (main phase)
or post the fixed `PIPELINE_MSG="等待合并队列处理。"` (3rd-party — those MRs may
have no pipeline) and flip to APPROVED. Reusing `post_approval` keeps the audit
shape (`fast_track_approved` + `lark_reply_sent` + `state_transition`) identical
to a normal `ok`, so reconcile / merge_tracker / recover need no fast-track
branch. It lives in `ack_new_topic.py` (not a reply intent) because only the ack
handler has both the ack-time pre-check results and the original event in hand.

---

#### 1.21.1 Non-agent paths to APPROVED must drain the root event  (was §3.28)

The dispatcher's `_find_work` treats any topic with non-empty
`events.pending` as work, regardless of state — only `MERGED` and
`CLOSED` are terminal. The normal `new_topic` flow is fine because
the topic agent drains the event as part of its work; but any
handler that takes a TRIAGING topic straight to APPROVED **without
spawning an agent** (today: fast-track in §1.21; tomorrow: any
mechanical promotion path) must drain the root event itself or the
APPROVED topic will be re-listed every dispatcher cycle until
merge_queue lands the MR. Symptom: a fast-tracked topic shows up in
the Monitor's `work` array on every trigger; no agent will pick it
up (state isn't TRIAGING / *_REVISION anymore) but the dispatcher
keeps proposing it as work.

The drain shape mirrors `mechanical_reply_handler.drain_mechanical`:

```python
pending = (topic.get("events") or {}).get("pending") or []
for idx, pe in enumerate(pending):
    if (pe.get("event_id") or pe.get("message_id")) == event_id:
        pending.pop(idx)
        break
topic.setdefault("events", {})["last_processed_event_id"] = event_id
topic_store.push_recent_event(topic, event_id)
```

Pop by event_id match (not just `pop(0)`) so a future caller that
processes a non-head event behaves correctly. `push_recent_event`
keeps the ring buffer that `router.py` consults to dedupe a
re-delivered raw event under the same id.

### 1.22 Platform & cross-cutting invariants
Gotchas that belong to no single feature — Windows process handling, lark-cli quirks, and encoding. `SKILL.md`'s Appendix: Script-Enforced Invariants points here rather than restating them.

#### 1.22.1 Detached processes on Windows  (was §3.12)

- `Start-Process` hangs in MSYS2 / Git Bash.
- `cmd.exe /c start /min` creates a visible console window.
- Use Python `subprocess.Popen` with `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`.

#### 1.22.2 lark-cli stderr output  (was §3.13)

lark-cli writes all output (including normal logs) to stderr. Listener redirects with `2>&1`; the health-check code checks both `.log` and `.err` mtime to avoid false "stale" verdicts.

#### 1.22.3 UTF-8 everywhere  (was §3.15)

Every `open()` uses `encoding='utf-8'`, every `json.dump` uses `ensure_ascii=False`. Topic files contain Chinese branch names, titles, and review content — any default-encoding write corrupts them on Windows (GBK).

#### 1.22.4 Invoke `lark-cli.exe` directly, not `node …/run.js`  (was §3.23)

The npm `@larksuite/cli` package's `run.js` bootstrap internally
re-execs `bin/lark-cli.exe` (a renamed `node.exe`) via
`child_process.spawn` **without** `windowsHide: true`. When the bot
spawns `node run.js` with `CREATE_NO_WINDOW` via
`subprocess_util.hidden_run` / `detached_popen`, the flag lands on
the parent `node.EXE` only — the grandchild `lark-cli.exe` allocates
a fresh console window every invocation. Empirically this means a
`lark-cli.exe` console pops on every dispatcher cycle (reconcile,
message-fetch, post-reply) and on every listener restart from
`_restart_listener`.

Fix: bypass `run.js` entirely. `subprocess_util.lark_cli_argv_prefix()`
returns `[<…>/bin/lark-cli.exe]` on Windows so callers do
`cmd = lark_cli_argv_prefix() + ["im", "+messages-reply", …]`. With
`CREATE_NO_WINDOW` set on this spawn, the immediate child IS the
binary that does the work — no grandchild, no console pop. Confirmed
working: `lark-cli.exe event +subscribe …` accepts the same argv as
`node run.js event +subscribe …`. POSIX falls back to
`["node", run.js]` since `lark-cli.exe` is Windows-only.

Do NOT revert any call site to `["node", lark_cli_path(), …]` thinking
it's a simplification — it pops a console every spawn.

#### 1.22.5 lark-cli exit code 0 does NOT mean the Lark post landed  (was §3.33)

`lark-cli im +messages-reply` exits 0 even when Lark **rejects** the post — the
verdict is in the JSON envelope, not the exit code:

```json
{"ok": true,  "data": {"message_id": "om_..."}}
{"ok": false, "code": 230011, "msg": "The message was withdrawn"}
{"ok": false, "code": 99999,  "msg": "rate limited / quota / etc."}
```

The 230011 path was caught by a substring match, but **any other `ok:false`**
slipped through: exit 0 → dispatcher logged `reply_posted`, consumed the
artifact, advanced state — while the Lark thread never got the post (observed on
RAGE-15558: audit said sent, `+threads-messages-list` showed no such message).

Fix: `render.py post_to_lark` parses stdout as JSON and requires `envelope.ok is
True`, else exits 1 so `reply_dispatcher` re-queues (subject to `RETRY_MAX`
quarantine). A `json.loads` failure also returns False (covers a stack-trace /
usage print on exit 0). Most lark-cli failures DO set nonzero exit; only
Lark-server rejections (rate limit, content filter) hit the `ok:false / exit 0`
path, which is why it stayed invisible until one fired.

### 1.23 Self-service dev loop: dev triages every round, approver decides once

Review is the developer's loop to run. The developer knows the code best, so
they triage the bot's issue list, push fixes, and re-run the review as many
times as they like; the approver is pulled in exactly once, at the end, and
still holds the final decision — including overriding issues the developer
pushed back on.

This started as round-1-only *inverted triage* (dev triages → approver
arbitrates immediately), which put the approver in the loop at the first
disagreement and again at the end of every subsequent round. §1.23.6–.9
extend it to the full lifecycle: triage repeats every round, dissent
accumulates instead of escalating, and one explicit developer verb ends the
loop.

- **`DEV_TRIAGE`** — where a topic waits between rounds. Entered after the
  round-1 review post *and* after every round-N post (§1.23.6). The post
  (`review_round1_dev_triage` in round 1, `review_roundN` after) @-mentions
  the developer and asks for the indices they **will fix**; unlisted issues
  are pushed back, optionally with a free-text reason (§1.23.8). The
  simple-vs-full origin is not encoded in the state name — `review.triage`
  (`"simple"`/`"complex"`, set at ack time) is authoritative.
- **`AWAITING_APPROVAL`** — the approver's court, reached only by the
  developer's explicit `done` (§1.23.7). The bot posts `handoff_summary`
  @-mentioning the approver for the first time in the topic: the fix ledger
  plus every issue the dev rejected and the reason they gave. The approver
  approves (`ok`), reinstates rejected issues (indices — §1.23.9), escalates
  (`full`, simple path only), or closes.
- **`ARBITRATION`** — **no longer entered.** Retained, with its handlers, so
  topics already parked there when the self-service loop shipped still drain
  (§1.23.4).

Every transition here is fully mechanical (parse → filter → render → post →
state flip) and runs in `mechanical_reply_handler.py` — no Claude spawn. A
**zero-issue** round-1 review bypasses triage entirely — there is nothing to
triage, so the agent posts the legacy `review_round1` and lands in
`TRIAGE_DECISION` / `AWAITING_APPROVAL` for a plain approver `ok`.

Approver approval verb is `ok` everywhere (§1.23.2); the
`pass`/`通过`/`lgtm`/`approved` aliases have been removed.

```
EVERY ROUND (either phase):
  TRIAGING → INLINE_REVIEW / FULL_REVIEW → DEV_TRIAGE
  DEV_TRIAGE:
      dev: indices / all ─────────────────→ SIMPLE_REVISION | FULL_REVISION
      dev: some rejected (-N / none) ─────→ SIMPLE_REVISION | FULL_REVISION
                                            (dissent recorded, not escalated)
      dev: nothing left to fix ───────────→ DEV_TRIAGE  (nudge: reply `done`)
      dev: done ──────────────────────────→ AWAITING_APPROVAL
      approver: ok (override) ────────────→ APPROVED
      dev / approver: close ──────────────→ CLOSED
  SIMPLE_REVISION | FULL_REVISION:
      dev: push + ok ─────────────────────→ round N+1 review → DEV_TRIAGE
      dev: done ──────────────────────────→ AWAITING_APPROVAL
      approver: ok (override) / close ────→ APPROVED | CLOSED
  AWAITING_APPROVAL:
      approver: ok ───────────────────────→ APPROVED
      approver: indices (reinstate) ──────→ SIMPLE_REVISION | FULL_REVISION
      approver: full (triage=simple only) → FULL_REVIEW → DEV_TRIAGE
      approver: close ────────────────────→ CLOSED
```

#### 1.23.1 Dev-triage reply grammar

Reuses `parse_indices_with_mode`, which already supports positive selection,
`all`, and the exclusion form. In DEV_TRIAGE the modes mean:

| Dev reply | Parsed | Meaning |
|---|---|---|
| `1 3 5` | `indices=[1,3,5], exclude=False` | fix these, reject the rest |
| `all` | `indices=[], exclude=True` | fix everything |
| `-1 -3` | `indices=[1,3], exclude=True` | **reject #1 #3**, fix the rest |
| `none` / `0` / `不修` | `indices=[], exclude=False, none=True` | reject all |

The `none` form is new: `parse_indices_with_mode(content, allow_none=True)`
accepts a sole token `none`/`0`/`不修` and returns the third encoding above —
symmetric with the existing pair (`exclude=True, indices=[]` = "all"). The
gate defaults to `allow_none=False` so `0` stays rejected in every legacy
approver path (issue indices start at 1; silently mapping `0` there would
drop intent). Mixed forms (`none 3`, `0 1`, `1 -2`) stay rejected.

Classification (`classify_intent`) checks the DEV_TRIAGE developer block
**before** the approver block, so an operator who is both the developer and
an approver gets indices resolved as `dev_triage`, not `approver.revision`.
Approver indices in DEV_TRIAGE are ignored (the approver must wait for
arbitration); approver `ok` in DEV_TRIAGE is an override straight to APPROVED
(`ok` is the unified approval verb — §1.23.2 — and DEV_TRIAGE is in
`_APPROVER_OK_STATES`). A **developer** `ok` in DEV_TRIAGE is still ignored — it
must not classify as `dev_reply`, and the dev cannot self-accept their own
triage; they must reply with indices.

#### 1.23.2 Arbitration semantics and the unified `ok` verb

**Unified approval verb.** `ok` is the single approver approval token across
every decision state — `TRIAGE_DECISION` / `AWAITING_APPROVAL` (final approve),
`DEV_TRIAGE` (override), and `ARBITRATION` (accept the dev's triage). The set is
`reply_parser._APPROVER_OK_STATES`; the `*_REVISION` states are deliberately
excluded, because there `ok` is the **developer's** "fixes pushed, re-review"
trigger and must not be hijacked into a merge — the approver has no approve
verb during revision (only `close`), and approves at the round-N decision state
instead. The legacy aliases `pass` / `通过` / `lgtm` / `approved` were removed
(no `APPROVE_PATTERNS`) and are no longer recognized in any state. The
root-message fast-track (§1.21) accepts only `ok`.

**Dev accepted everything → no arbitration.** If the dev's rejected set is
empty, `_handle_dev_triage` never enters ARBITRATION: it flags all issues, posts
`revision_request` to the dev, and transitions straight to `SIMPLE_REVISION` /
`FULL_REVISION` (audit `dev_triage_accepted_all`). The approver still has the
final say at round N — this only removes the redundant arbitration round-trip
when there's no dispute.

The approver's reply in ARBITRATION is interpreted against the dev triage
recorded in `review.dev_triage` (`accepted_indices` / `rejected_indices`):

- **`ok`** — accept the dev's triage. If the
  accepted (fix) set is **empty** (dev rejected all, approver agrees), the
  handler delegates verbatim to `_handle_approve` → APPROVED: `ok` doubles as
  approval, no pointless revision round. Otherwise `review.flagged_issues` is
  set to the accepted issues and the bot posts the final fix list
  (`revision_request`) telling the dev to reply `ok` after pushing →
  `SIMPLE_REVISION` / `FULL_REVISION` per `review.triage`.
- **Indices** (`2 4`, `all`, `-N`) — reinstate dev-rejected issues,
  interpreted against `rejected_indices`: positive = reinstate those; `all` =
  reinstate every rejected issue; `-N` = reinstate all rejected except N.
  Indices already in the accepted set are tolerated no-ops. Final fix set =
  accepted ∪ reinstated → `flagged_issues`; the `revision_request` post gets
  a prefix paragraph naming the reinstated issues → `*_REVISION`.
- **`full`** — escalate to a full review; only meaningful when
  `review.triage == "simple"`. Not mechanical — defers to the Claude agent,
  which re-runs the full round-1 path and lands back in DEV_TRIAGE (the dev
  re-triages the new full issue list).
- **`close`** — unchanged.

**Invalid indices** (matching no issue / no rejected issue) post a
plain-text error and **drop the event** — deliberately unlike
`_handle_revision`'s leave-in-pending failure mode, because a typo'd index
left pending would poison-loop the drain every cycle.

The developer cannot self-accept their own triage: dev `ok`/indices in
ARBITRATION are ignored.

#### 1.23.3 Manual-issue refresh at arbitration

The approver may also leave manual MR comments (DiffNotes) while
arbitrating. Both arbitration outcomes that lead to `*_REVISION` (accept
with non-empty fix set, reinstate) refresh manual issues **in-process**
(`gitlab_threads.fetch_for_topic` + reconcile — same precedent as
`ack_new_topic`), display-only with 待验证 markers (verification needs
Claude and happens at the next round-N spawn). Newly discovered manual
issues are appended to the `revision_request` post so the dev sees the
additional issues before fixing. A glab failure here is non-fatal: the fix
list posts without the manual section and the failure is audited
(`manual_refresh_failed_nonfatal`) — never block the fix list on a glab
blip.

#### 1.23.4 Migration & legacy-state coexistence

No migration script and no state renames. The new states are only ever
*entered* by the post-deploy round-1 agent path; every legacy row in
`reply_parser`, `state_machine`, and the mechanical handler is preserved
because round N still uses `TRIAGE_DECISION` / `AWAITING_APPROVAL`:

- Topics already in `TRIAGE_DECISION` / `AWAITING_APPROVAL` / `*_REVISION` /
  `APPROVED` at deploy finish on the old flow.
- Topics in `TRIAGING` / `INLINE_REVIEW` / `FULL_REVIEW` at deploy get the
  new flow on their next spawn (acceptable — no reply prompt has been posted
  yet).
- Deploy requires a **daemon + listener restart** (no hot reload — the
  daemon imports `mechanical_reply_handler`, `router`, `reply_parser`).

Dev-triage replies need no rapid-ack (`ack_dev_reply.py` is unchanged): the
mechanical drain handles them in the same poll tick, so the
`dev_triage_summary` post itself is the ack.

#### 1.23.5 The 3rd-party phase uses dev triage too

Dev triage was originally main-phase-only, so a 3rd-party round-1 review fell
back to `review_round1` → `TRIAGE_DECISION`, whose only `@` is `APPROVER_ID`.
That silently defeats §1.20: on a topic the operator filed for someone else
(`RAGE-XXXXX @<dev>`), the review result @-mentioned the approver — who is
usually the operator who filed it — and the developer who owns the MR was
never told a review had landed. The phase a repo lives in says nothing about
who should triage the findings; the person who wrote the code does, in both
phases. Observed on CB2N-25268 (10 issues on a `3rd_party_cpplibs/recast_detour`
MR filed for its author, zero mentions of the author).

So both phases now run the identical round-1 path: `review_round1_dev_triage`
→ `DEV_TRIAGE` → (`ARBITRATION`) → `*_REVISION`. The three
`"not supported in 3rd_party phase"` guards in `_handle_dev_triage` /
`_handle_arbitration_accept` / `_handle_arbitration_reinstate` are gone; no
other code was phase-gated, because `reply_parser` keys off state and role
only, and the 3rd-party approve path (§1.7) already ran mechanically. The
downstream 3rd-party specifics — no pipeline recheck at approval, separate
merge queue, phase reset to `main` — are untouched: they hang off approval and
merge, not off who triages.

The one deliberate asymmetry that remains is the `no_new_commits`
short-circuit (§1.7.3), which still skips 3rd-party topics: it compares
`git ls-remote` against locally-known repo roots, and 3rd-party repos have
none.

#### 1.23.6 Triage repeats every round

Round N used to land in `TRIAGE_DECISION` / `AWAITING_APPROVAL` and block on
the approver, so a developer who wanted three iterations before showing their
work dragged the approver through three decision points. Round N now lands
back in `DEV_TRIAGE` and the round-N post is addressed to the developer
(`review_roundN` carries `DEVELOPER_ID` / `DEVELOPER_NAME`; its
`{{APPROVER_ID}}` mention is gone). The developer keeps the loop running with
`ok` after each push, exactly as before.

The triage universe narrows each round. Round 1 offers every issue; later
rounds offer only what is still **open** — `_open_triage_universe` drops any
issue with a settled verdict (`addressed` / `obsolete`, §1.4.8) and any the
dev already rejected, but keeps anything the approver reinstated. Without
that narrowing a round-2 reply of `1 3` ("I'll fix these") would be read as
rejecting everything else and silently discard round-1 work. For the same
reason the fix-list post prints only the still-unfixed subset while the
ledger (`flagged_issues`) keeps every accepted issue, so settled verdicts
survive the rebuild — `_post_fix_list_and_transition(..., display=...)`.

Dissent accumulates on `review.dev_triage` rather than escalating: a later
round can flip an index from rejected to accepted (naming it accepts it), and
`rejected_indices` / `accepted_indices` are recomputed as running sets.

**Silence is not a retraction.** In round 1 "the issues you didn't name are
disputed" is the whole grammar, but from round 2 on the dev is answering
"which of the remaining ones am I fixing now" — reading their `1 3` as
"…and I retract everything I promised last round" would push work at the
approver that the dev never meant to dispute, with no notice in the thread.
So an issue already in `accepted_indices` is only demoted by an explicit
`-N` / `none`; leaving it unnamed keeps the commitment. `none` expands to
the whole open set for this purpose — it carries no indices of its own, and
reusing the (empty) index list left the one verb whose entire job is
"dispute everything" demoting nothing.

That splits what "open" means into two questions, and they have different
answers: which indices may the dev **name**, and which get partitioned into
accepted/rejected when they answer. A previously disputed issue stays
**nameable** — naming it retracts the dispute — but is not in the partitioned
set, so neither `all` nor silence resurrects it. Collapsing the two made the
retraction §1.23.6 promises impossible: the index was filtered out of the
valid set, so a dev trying to take back their own objection got
「无效的问题序号」. If a
round leaves nothing to fix (everything settled or rejected), there is no
meaningful `revision_request`, so the bot posts a plain-text nudge, holds in
`DEV_TRIAGE`, and audits `dev_triage_all_rejected`.

#### 1.23.7 The `done` hand-off

Nothing else ends the loop — without an explicit verb a self-service topic
would iterate forever. `done` / `submit` / `提审` / `完成`
(`reply_parser.HANDOFF_PATTERN`) classifies as `dev_handoff`, gated to the
topic's developer and to `_HANDOFF_STATES` = {`DEV_TRIAGE`,
`SIMPLE_REVISION`, `FULL_REVISION`} — the dev may hand off straight from a
revision state without running one more round. It is the developer's verb: a
bystander must not be able to submit someone else's branch for final review.

`_handle_dev_handoff` posts `handoff_summary` (the first and only
@-mention of the approver in the topic) and flips to `AWAITING_APPROVAL`.
The ledger section is built by `_handoff_status_paragraphs`, which is
deliberately defensive: `render.build_issue_status_paragraphs` validates hard
and raises on a missing field — right for an agent-authored artifact, wrong
here, because a malformed ledger entry must never strand the hand-off with
the event stuck in `events.pending`. Unrenderable entries fall back to the
plain indexed list.

#### 1.23.8 Optional dissent reasons

An index alone tells the approver *that* the dev disagreed, not *why*, and
the approver decides on that dissent minutes-to-days later with no other
context. So the dev-triage grammar accepts free text after the indices:
`-2 -3 这两个是误报`. `parse_indices_with_mode(..., allow_trailing_text=True)`
stops at the first token that is not an index token and returns the rest as
`reason`; at least one index token is still required, so plain prose stays
unparseable and falls through to `ignored`. Separation must be whitespace or
a comma — `-1误报` is a single non-index token, not an index plus a reason.

The flag is **off by default**, so every approver path keeps today's strict
full-match grammar; only the `dev_triage` call site enables it. Reasons are
appended per round to `review.dev_triage.reasons[]` as
`{round, rejected_indices, text, at}` and rendered under the disputed issues
in `handoff_summary`.

#### 1.23.9 Approver indices mean "reinstate"

In `AWAITING_APPROVAL` the approver is looking at a hand-off summary holding
both kinds of issue, so an index there means one of two things and the handler
splits the reply accordingly:

- the index is in `rejected_indices` → **reinstate** it (overrule the dispute);
- the index is in `accepted_indices` → **re-flag** it ("this one still isn't
  fixed"), recorded as `refixed_indices`.

Only routing the first kind was a real dead end: as soon as the dev had
disputed anything, the whole reply went to `_handle_arbitration_reinstate`,
intersected with `rejected` alone, came back empty, and drained to
「未恢复任何异议问题——如同意开发者的处理意见，请回复 ok」 — pointing the
approver at *approval* when they had just asked for more work. With nothing
rejected at all it stays a plain `_handle_revision`.

Reinstating also **settles the dispute**: the index moves out of
`rejected_indices` and into `accepted_indices` (while staying in
`reinstated_indices` for the lock below). Otherwise the next hand-off keeps
rendering it under 开发者有异议 with the dev's original reason attached, and
the approver reads a live objection to something they already overruled and
the dev already fixed.

A reinstated issue is **locked**: `_handle_dev_triage` refuses a later
rejection of it, posts a plain-text error and drops the event (audit
`dev_triage_reinstated_locked`). Otherwise the two could volley the same
index forever. Dropping rather than leaving the event pending is the same
anti-poison-loop rule as invalid indices — the dev simply re-replies.

Escalation (`full` / `完整版`, simple triage only) is reachable from
`AWAITING_APPROVAL` as well as the legacy `TRIAGE_DECISION` / `ARBITRATION`,
since `AWAITING_APPROVAL` is now where the approver first sees the topic.

### 1.24 Post-merge cherry-pick to release branches

After a topic merges to master, the approver often needs the same change on a
release branch. Doing it by hand means finding the squash commit, remembering
which repos were involved, and repeating it per branch. The bot already knows
all three, so on merge it **prompts** with the branches actually available and
takes a one-token answer:

```
bot:      已合并。需要 cherry-pick 到以下发布分支吗？
            p1 → rc_p1
            p2 → rc_next_p2
            p4 → rc_dev_p4
          回复分支代号（如 "p1" 或 "p1 p2"），或回复 "no" 跳过。
approver: p1 p2
```

**Active branches are discovered, not configured.** Both repos carry three
release families — `rc_*`, `rc_next_*`, and `rc_dev_*` — each holding several
numbered branches at once (`rc_p0` and `rc_p1`, `rc_next_p1` and
`rc_next_p2`). Only the **highest number in each family** is live; the lower ones are shipped and
must never receive a cherry-pick. `cherrypick.discover_active_branches` runs
`git ls-remote --heads`, groups by family, and keeps the max per family. A
static allow-list was considered and rejected: it goes stale exactly when a
new rc branch is cut, which is precisely when cherry-picks matter most, and a
stale list silently targets a dead branch.

**The token is a number, resolved against the active set.** `p1` means "the
active branch numbered 1", which is `rc_p1` — *not* `rc_next_p1`, which is
dead. So the prompt is not cosmetic: it is the mapping, and it is regenerated
per topic because the active set moves. When one number is active in more than
one family — `rc_next_p4` and `rc_dev_p4` both live — the token is ambiguous;
the bot refuses it and asks for the full branch name rather than guessing.
Adding a family is therefore a one-line change to `cherrypick._VARIANT` (mirrored
in `reply_parser._CP_TOKEN`): everything downstream keys off the parsed family,
not a hard-coded pair.

Three further points, each load-bearing:

**The topic must survive its own merge.** `MERGED` is terminal and
`process_merge_queue` archives to `closed/` immediately — after which router
Gate 4a drops every reply (§1.2.9). A cherry-pick command arriving *after* the
merged notice would therefore vanish. So the terminal transition now splits:
state becomes `MERGED` as before, but archiving is **deferred** while
`review.cherrypick_window_until` is in the future and at least one repo has
release branches configured. `dispatcher._archive_expired_cherrypick_windows`
archives it when the window lapses. `MERGED` stays in `TERMINAL_STATES` — the
topic is finished, it is merely still addressable.

**The merged SHA has to be captured at merge time.** `merge_tracker.check_mr`
already extracts `merge_commit_sha`/`squash_commit_sha`, but only the *passive*
reconcile path recorded it; `merge_mr` discarded the PUT response body, so a
bot-merged topic had no commit to cherry-pick. `merge_mr` now returns `sha`
and `process_merge_queue` persists `lifecycle.merge_shas[repo]`. Chaos is
`squash_option="always"` (§1.6.9), so the thing to port is the single squash
commit, never the branch head — cherry-picking the pre-squash head would
replay commits master never took.

**Direct first, MR on rejection.** `POST /repository/commits/:sha/cherry_pick`
is one call and lands instantly when the branch is unprotected. Release
branches usually *are* protected, and the same call also fails on conflict.
Both cases fall back to creating `cherry-pick-<ticket>-<branch>` from the
target and opening an MR onto it, so a rejection degrades into a reviewable
MR rather than an error the approver has to act on. The reply reports which
path each branch took.

**A token that resolves to nothing is rejected, never guessed.** Only members
of the discovered active set are accepted; anything else (a dead `rc_p0` or
`rc_dev_p3`, a
typo, `master`) is refused with the live mapping echoed back. A free-text
branch name from a chat message is one typo away from targeting `master`.

Failures are per-branch and per-repo: one blocked branch does not abort the
others, and every outcome lands in one summary reply plus an audit entry.

**Both archive paths must honour the window, not just the dedicated one.**
`MERGED` is in `TERMINAL_STATES`, so `dispatcher._janitor` step (a) — the
generic "relocate terminal topics" sweep — archived the topic ~1 minute after
the offer was posted, and Gate 4a then dropped the approver's answer. The
deferred archive in `process_merge_queue` was correct and irrelevant: a second
archiver undid it. Both now consult `dispatcher._cherrypick_window_open`, one
predicate, so they cannot diverge. Observed on RAGE-23524 (offer 03:50:49,
archived 03:51:40, `p2` reply at 03:51 dropped).

**`glab` output must be decoded as UTF-8 explicitly.** `_glab_json` used
`text=True` alone, so on a CN-Windows box the ANSI codepage decoded the
response — and a commit message with Chinese in it raised `UnicodeDecodeError`
inside subprocess's reader thread. `returncode` stays 0 while stdout comes back
*empty*, so `cherry_pick_to_branch` read a **successful** direct pick as a
failure and ran the MR fallback on top of the commit it had just landed; GitLab
then refused the second pick ("may have already been done") and the whole
operation was reported failed. The same hazard is already called out for
`discover_active_branches` — an empty stdout from this class of crash looks
exactly like a legitimate empty result, which is why it must be prevented at
the decode, not detected afterwards.

**The @-target is the release owner, not the code approver.** Deciding which
release branches a change belongs on is a release-management call, and the
person who holds it need not be the approver who reviewed the code. The offer
@-mentions `REVIEW_BOT_CHERRYPICK_DECIDER_OPEN_ID` when set, falling back to
the primary approver, so a shop where the two roles differ stops pinging the
wrong person every merge. This is the mention only — authorization is
unchanged, and any open_id in `REVIEW_BOT_APPROVER_OPEN_IDS` may answer.

#### 1.24.1 Repo roots come from `__file__`, never `os.getcwd()` (RAGE-23816)

`_repo_root_for` resolved `rage_root` as `RAGE_REPO_ROOT or os.getcwd()`, with
`chaos_root = rage_root/chaos`. The daemon inherits its working directory from
whatever launched it, and neither env var is set in the detached
`pythonw poll_dispatch.py --watch` process — so both roots were cwd-relative.

The failure is **asymmetric**, which is what let it hide:

| | resolved to | result |
|---|---|---|
| rage | `<scripts dir>` | **works** — git walks up to the enclosing repo |
| chaos | `<scripts dir>/chaos` | `[WinError 267] The directory name is invalid` |

So `discover_active_branches` raised for chaos only, `_offer_cherrypick` caught
it, logged a WARN, and `continue`d — dropping chaos from `active_by_repo` and
therefore from the token mapping. The offer still went out and still looked
healthy: `merge_shas` had both repos and the audit's `cherrypick_offered` line
recorded both, because that field is the raw merge SHAs, not what discovery
found. Only the `cherrypick_completed` results revealed a rage-only pick, and
only if you thought to compare them against `mrs`.

Observed on RAGE-23816: chaos `b78485d5` merged and was never offered, so the
release branch got the rage half of a cross-repo change. RAGE-23629 had picked
both repos correctly hours earlier — the difference was purely which directory
the daemon happened to be started from, which is exactly why a cwd fallback is
never acceptable for a path the pipeline depends on.

Fix: `_repo_roots()` derives from `PROJECT_ROOT` (computed from `__file__`,
four levels up from `scripts/`), matching what `dispatcher.py` already did.

#### 1.24.2 3rd-party repos are never offered for cherry-pick

`3rd_party_cpplibs/*` forks ship on their own release cadence; our weekly
`rc_*` / `rc_next_*` / `rc_dev_*` branches describe the game, not them. Asking
"port this to p2?" after a lib MR merges is a question with no correct answer,
so `_offer_cherrypick` skips every `3rd_party/` key in `lifecycle.merge_shas`.
A topic whose merges were all 3rd-party therefore gets no offer and archives
immediately, exactly as it did before the cherry-pick feature existed.

This is a **correctness** guard as much as a policy one. `_repo_root_for`
returns `chaos_root` for `chaos` and `rage_root` for everything else — so a
`3rd_party/recast_detour` key resolved to the **rage** checkout, and
`discover_active_branches` dutifully returned rage's live release branches and
filed them under the lib. The offer then proposed cherry-picking a
`recast_detour` commit onto `rc_p2`, and an approver answering `p2` would have
sent a lib SHA at a game release branch. Note the shape of the near-miss: the
pre-existing comment in `_offer_cherrypick` assumed a 3rd-party repo would
simply have "no release branch" and drop out of `active_by_repo` on its own.
It never did, because the discovery ran against the wrong repository.

The two-phase flow is what makes this reachable at all: a 3rd-party merge is
not terminal (§2.1), so its SHA stays in `merge_shas` while the topic resets to
the main phase, and it is still sitting there when the main-phase merge finally
opens the cherry-pick window. Skipped repos are recorded in the
`cherrypick_offered` audit as `skipped_3rd_party` rather than silently dropped
— an offer that covers less than it appears to is precisely the failure of
§1.24.1.
`_refresh_main_phase_heads` (§1.6.12) shared the same cwd fallback and is fixed
with it — there it would have silently skipped chaos's `ack_stats`, feeding the
triage thresholds a half-empty diff.

Second half of the fix — **a partial offer must say so**. Dropping a repo from
the mapping is now recorded as `cherrypick_partial` in the audit and appended to
the thread post as an explicit ⚠️ line naming the uncovered repos, so a silently
half-applied cherry-pick can't read as a complete one. The offer still goes out
for the repos that did resolve: a partial offer beats no offer, but only when
its partiality is visible.
#### 1.24.3 In `MERGED`, every reply that is not a cherry-pick answer is dropped

Adding `MERGED` to `router._REPLY_STATES` (so the 24 h offer can be answered)
re-opened the whole reply pipeline on a finished topic. `classify_intent`'s
cherry-pick block only *returned* for `cherrypick` / `cherrypick_skip`;
everything else fell through to the ordinary ladder below it, where an approver
`close` matches the state-independent close rule and a developer `ok` matches
the dev-token rule.

Neither has a drainer in `MERGED`. `mechanical_reply_handler._classify` admits
only `_MERGED_STATE_INTENTS`, so it hands the event back; the topic agent's
state×intent table has no `MERGED` row, so it writes `fallback_skipped` and
leaves the event in `events.pending`; the dispatcher sees pending work next
cycle and spawns again. An Opus spawn per cycle, for up to 24 hours, over a
reply nobody can act on — the same "no drainer → respawn loop" family as
§1.18.2, reached from a new direction.

So the block now returns unconditionally for `MERGED`: cherry-pick intents for
an approver, `ignored` for everything else. The topic is finished; the offer is
the only question it can still answer, and `reply_intent_ignored` is the honest
audit line for the rest.

#### 1.24.4 A merged repo with no captured sha is named, never dropped

`_offer_cherrypick` derives its coverage from `lifecycle.merge_shas`, but only
one of `merge_mr`'s three success paths carried a sha: the PUT whose response
parsed and reported `state == "merged"`. The unreadable-body fallback and the
`already_merged` idempotency net both returned success with no sha — and the
`already_merged` path is *common*, since it is what rescues a 405 retry.

A repo that merged through either path is simply absent from `merge_shas`, so
it silently vanishes from the offer: the approver sees an offer that looks
complete, answers `p1`, and only rage lands on the release branch while chaos
never does. That is precisely §1.24.1's failure — half a cross-repo change on a
release branch — arriving through a different door, and unlike §1.24.1 it had
no ⚠️ to warn anyone.

Both halves are fixed. `merge_mr`'s `already_merged` branch now carries the sha
that `check_mr` already fetched for it, which removes the common case
outright. For the residual case (no sha recoverable at all),
`process_merge_queue` records the repo in `lifecycle.merged_without_sha` and
the offer appends a ⚠️ line naming it, exactly as `failed_repos` does — an
offer must never read as covering more than it does.

#### 1.24.5 Branch tokens resolve case-insensitively

`_TOKEN` carries `re.IGNORECASE`, which tells the approver that case does not
matter. The lookup behind it did `branch in active`, against a dict keyed by
names straight out of `git ls-remote` — case-sensitive. So `RC_P1` passed the
regex, missed the lookup, and came back as "`RC_P1` 不是活跃发布分支（可能已停止
维护）": a factually wrong message about a branch that is very much alive.

The lookup now folds case and resolves back to the real branch name, so the
git commands still receive the exact ref and the reply echoes the canonical
spelling rather than whatever was typed. The alternative — dropping
`IGNORECASE` — would have been consistent too, but it fails a token the bot
could obviously understand, and typing a branch name in caps is not a mistake
worth a round trip.


### 1.25 The topic id is published so other agents can drive the topic

A developer increasingly has a coding agent make the fix. That agent can push a
branch, but it could not move the review forward: every verb in §1.23 is a Lark
thread reply, and nothing outside the bot knew which thread to reply to. The
thread id was in the topic filename and nowhere a human could copy it from, so
the developer stayed a manual relay — read the bot's post, type `1 3 5`, wait,
type `ok`.

So `ack_new_topic` — the first reply on every topic, and the only one guaranteed
to exist — prints `话题 ID: <thread_id>`. That is the whole mechanism: the
developer hands the id to their agent, and the agent replies into the thread like
any other participant. `reference/agent-topic-reply.md` is the contract those
agents read.

Publishing an id the bot already treats as public (it is the root message id of a
thread everyone in the review group can see) costs nothing and adds no new
authority — **who** posts still decides what a reply may do:

- `router._is_self_message` drops anything sent by the bot's own open_id before
  classification, so an agent posting `--as bot` is inert. It must post as a
  user, which needs the `im:message.send_as_user` scope.
- `dev_triage` and `dev_handoff` remain gated to `identity.creator_open_id`
  (§1.23.1, §1.23.7), so only the developer's own account can triage or hand off.
  An agent borrowing some other account can still post `ok` and `@bot`, which is
  deliberate — those are content-only verbs open to any participant.

The failure mode this avoids is the tempting one: exposing a local "reply to
topic" entry point that writes `events.pending` directly. That skips the listener,
the router's classification, and the per-topic lock, so it would race the
dispatcher and corrupt the topic file. Agents go through Lark like everyone else;
the id is the only thing they needed.

## 2. Two-Phase Topic Lifecycle

The 3rd-party / main two-phase design is **topic-scoped**, not just merge-scoped. It affects review, approval, and merge.

```
Topic with 3rd-party MR:

  3rd-party phase (review_phase == "3rd_party"):
    TRIAGING → review 3rd-party MR → approval → APPROVED
    → 3rd-party merge queue (wait for pipeline, then merge)
    → reset: state=TRIAGING, review_phase="main"

  Main phase (review_phase == "main" or null):
    TRIAGING → discover rage/chaos MRs → review → approval → APPROVED
    → main merge queue (rebase + merge) → MERGED
```

### Key rules
- 3rd-party MRs are in independent repos (`3rd_party_cpplibs/*`) — they must NOT be blocked by the main merge queue's FIFO ordering. Separate queue.
- The topic agent does NOT merge directly; both phases use their respective merge queues.
- After 3rd-party merge, the topic is NOT terminal — it resets to `TRIAGING` for main phase.
- The mechanical handler (§1.7) drains 3rd-party replies too — same states, same verbs (§1.23.5). Claude owns only the review itself.

---

### 2.1 Phase Reset Semantics (3p → main)  (was §6)

When a 3rd-party merge completes, the topic pivots rather than
terminates. The reset is a **full re-triage**, not a cosmetic state
flip:

| Field                        | Action on 3p→main |
|------------------------------|-------------------|
| `review.state`               | → `TRIAGING` |
| `review.review_phase`        | → `"main"` |
| `review.review_round`        | → `0` |
| `review.flagged_issues`      | popped |
| `review.issues`              | popped |
| `review.pending_action`      | popped |
| `mrs["3rd_party/*"]`         | `state="merged"`, `pipeline_status="merged"` |
| `mrs["chaos"/"rage"]`        | `pipeline_status` popped |
| `audit[]`                    | `phase_reset` entry appended |

Rationale: the main-phase review is about game code, not the library.
After the 3p MR merges, the chaos/rage MRs are diff-against-fresh-master
again — pipeline must re-run, triage must re-classify, issues must be
re-derived. Any stale `passed`/`failed`/`issues` from the 3p cycle
would either short-circuit a recheck in `_check_approved_topics` or
render a stale review in the main-phase post. Hence the aggressive
pop — cheap to re-derive, catastrophic to carry forward.

## Appendix: Crosswalk (old -> new)

Transition aid for one release; drop at the next reorg. `lint_design_doc.py` checks every new id resolves.

| old | new |
|---|---|
| §3.1 | §1.6.1 |
| §3.2 | §1.6.2 |
| §3.3 | §1.6.3 |
| §3.4 | §1.8.1 |
| §3.5 | §1.2.1 |
| §3.6 | §1.8.2 |
| §3.7 | §1.6.4 |
| §3.8 | §1.6.5 |
| §3.9 | §1.6.6 |
| §3.10 | §1.7.1 |
| §3.11 | §1.9.1 |
| §3.12 | §1.22.1 |
| §3.13 | §1.22.2 |
| §3.14 | §1.10.1 |
| §3.15 | §1.22.3 |
| §3.16 | §1.4.1 |
| §3.17 | §1.7.2 |
| §3.18 | §1.3.1 |
| §3.19 | §1.18.1 |
| §3.20 | §1.4.2 |
| §3.21 | §1.14.1 |
| §3.22 | §1.1.1 |
| §3.23 | §1.22.4 |
| §3.24 | §1.4.3 |
| §3.25 | §1.8.3 |
| §3.26 | §1.19.1 |
| §3.27 | §1.19.2 |
| §3.28 | §1.21.1 |
| §3.29 | §1.6.7 |
| §3.30 | §1.18.2 |
| §3.31 | §1.9.2 |
| §3.32 | §1.2.2 |
| §3.33 | §1.22.5 |
| §3.34 | §1.3.2 |
| §3.35 | §1.8.4 |
| §3.36 | §1.19.3 |
| §3.37 | §1.9.3 |
| §3.38 | §1.1.2 |
| §3.39 | §1.6.8 |
| §3.40 | §1.9.4 |
| §3.41 | §1.1.3 |
| §3.42 | §1.2.6 |
| §3.43 | §1.2.3 |
| §3.44 | §1.2.4 |
| §3.45 | §1.2.5 |
| §3.46 | §1.2.7 |
| §3.47 | §1.4.4 |
| §4 | §1.6.9 |
| §5 | §1.6.10 |
| §6 | §2.1 |
| §7 | §1.7 |
| §7.1 | §1.7.3 |
| §7.2 | §1.7.4 |
| §8 | §1.13.1 |
