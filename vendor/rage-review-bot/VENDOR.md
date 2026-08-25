# Vendored: rage review-bot skill

Pristine copy of the upstream `review-bot` skill used as the **reference base**
for replicating review quality into this pipeline. Kept verbatim so we can
`git diff` against upstream and port changes deliberately instead of re-deriving.

## Provenance

- **Source repo**: `git@gitlab.booming-inc.com:booming/dev/projects/rage/rage.git`
- **Source path**: `.claude/skills/review-bot/`
- **Upstream commit**: `c035b1607301ac8b3d124d5529513c2f50aa0a57`
  (`auto update chaos-rage submodule`)
- **Copied verbatim**: 2026-08-25, nothing changed.

## Layout

| Path | What it is |
|---|---|
| `SKILL.md` | Operator spec — commands, templates, runtime contract (canonical) |
| `SKILL_zh.md` | Chinese mirror of SKILL.md (also uploaded to a Lark doc) |
| `DESIGN.md` | Architecture + footguns — the *why* behind the pipeline |
| `reference/` | agent-topic-reply / commands / lark_base_api |
| `scheduling/` | Windows Task Scheduler + VBS launcher (NOT portable to our Linux bot) |
| `scripts/` | ~50 python modules + `spawn_topic_agent.md` (the agent contract) + templates |
| `scripts/templates/` | Lark `post` templates + `render.py` |

## Porting policy (方案D)

- **Reuse verbatim** (pure logic, platform-agnostic): `state_machine.py`,
  `reply_parser.py` (indices grammar), `review_rounds.py` (settled-issue ledger),
  severity rubric, `render.py` builders, `spawn_topic_agent.md` *prompt*
  (restructured, not copied raw).
- **Platform-bound, must be rewritten for our Linux/http bot**:
  `lark-cli` → our feishu http + `feishu_notifier`;
  `glab` → GitLab API (already partially in `jira_parser` / `gitlab_ci`);
  Windows daemon/VBS/taskkill → our `event_server` + Jenkins;
  `lark_doc_helper.py` → new http adapter (M4, scope-gated).
- **Deliberately NOT porting**: merge queue (`process_merge_queue`),
  cherry-pick (`cherrypick.py`), 3rd-party phase, `replay.py` harness.

The plan/decisions live in `docs/review-bot-replication-plan.md` (P0 decision
gate). Any change to vendored files must keep this dir diff-able against
upstream.