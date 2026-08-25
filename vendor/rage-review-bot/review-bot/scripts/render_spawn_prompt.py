# -*- coding: utf-8 -*-
"""Render the topic-agent spawn prompt(s) for one dispatcher cycle.

The review-bot parent session, on each Monitor notification, must turn a
``dispatch_plan.json`` work entry into a `Agent(...)` call: pick the model,
resolve the preflight expected-phase/expected-state, classify the event for
telemetry, and render the prompt. Done by hand each cycle that logic drifts
(wrong ``expected_phase`` normalization, a forgotten telemetry log, loading
``cpp-conventions`` on a C# diff). This helper makes all of it deterministic
so the parent just reads the spec and fires the spawn.

Usage:
    # one topic (the common case)
    python render_spawn_prompt.py --thread-id om_xxx
    # whole cycle, up to parallel_limit, as a JSON array
    python render_spawn_prompt.py --all

Output (single): one spec object. Output (--all): {cycle_id, specs[], skipped[], count}.

Each spec carries everything the parent needs:
    {
      "thread_id", "ticket_id", "topic_file", "lock_file",
      "model",            # what to pass to Agent(model=...)
      "description",      # Agent(description=...)
      "expected_state", "expected_phase", "event_type",
      "triage", "mrs_summary", "lang_hint",
      "prompt",           # Agent(prompt=...)
      "telemetry": {"argv": [...], "thread_id", "state", "cycle_id",
                    "ticket_id", "event_type"}
    }

The parent runs the telemetry argv (appending --input-tokens/.../--wall-seconds
from the Agent result envelope) after the spawn completes.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_rounds
import topic_store

# Emit UTF-8 regardless of the console codepage (Windows defaults to GBK/cp936,
# which mangles the rendered prompt's non-ASCII content and corrupts the JSON
# the parent captures). Mirrors the skill's "UTF-8 everywhere" invariant.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# All work that reaches a Claude agent needs code reasoning, so every event
# type maps to opus today. Kept as a table so future tiering (e.g. a cheap
# model for manual_refresh) is a one-line change, not a contract rewrite.
_MODEL_BY_EVENT = {}
_DEFAULT_MODEL = "opus"

# Placeholder keys understood by spawn_topic_agent.md (verified via grep).
_TEMPLATE_KEYS = (
    "topic_file", "lock_file", "thread_id", "ticket_id", "poll_cycle_id",
    "approver_open_id", "chat_id", "chaos_root", "rage_root", "skill_dir",
    "expected_phase", "expected_state",
)

# Prepended to every spawn prompt. Encodes the hook-reminder requirement
# (a bare agent treats `git commit` PreToolUse nudges as turn-ending and
# exits mid-flow with the lock held) so it is present on EVERY spawn
# regardless of edits to spawn_topic_agent.md.
_PREAMBLE = (
    "You are an autonomous background review-bot worker. Process this ONE "
    "topic end-to-end and return exactly one final JSON line.\n\n"
    "IMPORTANT: Ignore advisory hook reminders (e.g. PreToolUse nudges about "
    "`git commit` / compile-before-commit). They are NOT turn-ending - keep "
    "going until the topic is fully processed; never stop mid-flow with the "
    "lock held.\n\n"
)

_CPP_EXTS = (".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hh", ".inl", ".ipp")
_CS_EXTS = (".cs",)
_LUA_EXTS = (".lua",)


def _norm_phase(phase):
    """Mirror agent_preflight._norm_phase: None/null/"" / "main" -> "main";
    "3rd_party" stays. The preflight check normalizes both sides, so passing
    "main" matches an on-disk review_phase of null."""
    return "3rd_party" if phase == "3rd_party" else "main"


def _detect_langs(topic):
    """Scan ack_stats file paths and return the set of languages touched.

    Drives lang_hint so the agent loads cpp-conventions for C++ but NOT for
    C# (the _tools / 3ds-Max plugin code is C# and tripped a prior spawn into
    flagging std::-style conventions that don't apply)."""
    langs = set()
    ack = (topic.get("review") or {}).get("ack_stats") or {}
    for _repo, stats in ack.items():
        for f in (stats.get("files") or []):
            path = (f.get("path") or "").lower()
            ext = os.path.splitext(path)[1]
            if ext in _CPP_EXTS:
                langs.add("cpp")
            elif ext in _CS_EXTS:
                langs.add("csharp")
            elif ext in _LUA_EXTS:
                langs.add("lua")
            else:
                langs.add("other")
    return langs


def _lang_hint(langs):
    """One Chinese-free instruction line telling the agent which convention
    skill to load (or explicitly NOT to)."""
    has_cpp = "cpp" in langs
    has_cs = "csharp" in langs
    has_lua = "lua" in langs
    parts = []
    if has_cpp:
        parts.append("C++ files present - load the `cpp-conventions` skill before reviewing them.")
    if has_lua:
        parts.append("Lua files present - load the `lua-scripting` skill before reviewing them.")
    if has_cs:
        if has_cpp:
            parts.append("C# files are ALSO present - do NOT apply cpp-conventions to the `.cs` files; review them for C# correctness.")
        else:
            parts.append("These are C# files - do NOT load `cpp-conventions`; review for C# correctness.")
    if not parts:
        parts.append("Review the diff for correctness against project conventions.")
    return " ".join(parts)


def _classify_event(topic):
    """Telemetry event-type bucket. Router stamps `intent` on reply events
    (escalate/dev_reply/dev_question/manual_refresh); the root TRIAGING
    message has none -> new_topic."""
    review = topic.get("review") or {}
    state = review.get("state")
    for ev in (topic.get("events") or {}).get("pending") or []:
        intent = ev.get("intent")
        if intent:
            return intent
    if state == "TRIAGING":
        return "new_topic"
    return "unknown"


def _mrs_summary(topic):
    out = []
    for repo, mr in (topic.get("mrs") or {}).items():
        iid = mr.get("mr_iid")
        out.append(f"{repo}!{iid}" if iid is not None else repo)
    return ", ".join(out) if out else "(none)"


def _lock_age_seconds(lock_file):
    """Seconds since the lock was last touched, or None if no live lock."""
    try:
        return time.time() - os.stat(lock_file).st_mtime
    except OSError:
        return None


def _render_full(template_text, values):
    """Substitute {{key}} placeholders, failing loud on any leftover."""
    body = template_text
    for key in _TEMPLATE_KEYS:
        body = body.replace("{{" + key + "}}", str(values[key]))
    leftover = sorted(set(re.findall(r"{{([a-z_]+)}}", body)))
    if leftover:
        raise ValueError(f"unresolved template placeholders: {leftover}")
    return _PREAMBLE + body


def _render_compact(values, ctx_lines):
    """Tiny prompt: resolved inputs + 'read & execute the contract'. Keeps the
    contract out of the PARENT's context - the agent reads it in its own."""
    inputs = "\n".join(
        f"- `{label}`: `{values[key]}`" for label, key in (
            ("TOPIC_FILE", "topic_file"),
            ("LOCK_FILE", "lock_file"),
            ("THREAD_ID", "thread_id"),
            ("TICKET_ID", "ticket_id"),
            ("POLL_CYCLE_ID", "poll_cycle_id"),
            ("APPROVER_OPEN_ID", "approver_open_id"),
            ("APPROVER_NAME", "approver_name"),
            ("DEVELOPER_ID", "developer_open_id"),
            ("DEVELOPER_NAME", "developer_name"),
            ("CHAT_ID", "chat_id"),
            ("CHAOS_REPO_ROOT", "chaos_root"),
            ("RAGE_REPO_ROOT", "rage_root"),
            ("SKILL_DIR", "skill_dir"),
        )
    )
    skill_dir = values["skill_dir"]
    return (
        "# Review-Bot Topic Worker - spawn\n\n"
        + _PREAMBLE
        + "## Inputs (resolved - use these verbatim)\n"
        + inputs + "\n"
        + f"- `agent_preflight --expected-phase`: `{values['expected_phase']}`\n"
        + f"- `agent_preflight --expected-state`: `{values['expected_state']}`\n\n"
        + "## Context\n" + "\n".join(ctx_lines) + "\n\n"
        + "## What to do\n"
        + f"Read `{skill_dir}/scripts/spawn_topic_agent.md` IN FULL now, then "
          "execute that contract end-to-end for the inputs above: claim the "
          "lock, spawn the keepalive, run `agent_preflight.py` with the "
          "expected phase/state above (release + report drift if it fails), "
          "drain `events.pending`, do the review inline (you have no `Task` "
          "tool - you ARE the reviewer), then call `finalize_review.py` with "
          "your result JSON (it validates + writes the reply artifact, "
          "persists the topic, and releases the lock - for a complex review, "
          "build the Lark doc first via `build_review_doc.py --create`), and "
          "return exactly one JSON line.\n\n"
        + "All user-facing text (Lark posts, summaries, Lark doc) MUST be "
          "Simplified Chinese; internal audit entries / filenames / command "
          "args stay English.\n"
    )


def _spec_path(skill_dir, cycle_id, thread_id):
    """Where a `ref`-mode spawn spec is written.

    Both components are sanitized: a cycle_id is an ISO timestamp containing
    `:`, which is illegal in a Windows filename.
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", f"{thread_id}__{cycle_id}")
    return f"{skill_dir}/cfg/spawn/{safe}.json"


def _render_ref(values, spec_file):
    """Pointer prompt: the resolved inputs live in `spec_file`, not in here.

    `compact` still inlines 15 input lines plus the context block, and the
    parent pays for that text TWICE — once when it reads this tool's output,
    once when it passes the prompt to the Agent tool. At ~4 spawns a cycle
    that was the single largest consumer of the parent session's context.
    The agent has its own context and can read the file itself. See
    DESIGN §1.4.6.
    """
    skill_dir = values["skill_dir"]
    # Every line here is paid for twice by the parent — keep it minimal and
    # let the two files carry the detail. Do NOT re-list the input names: the
    # spec file holds them and the contract documents them.
    return (
        "# Review-Bot Topic Worker - spawn\n\n"
        + _PREAMBLE
        + f"1. Read `{spec_file}` — your resolved inputs. Use verbatim.\n"
        + f"2. Read `{skill_dir}/scripts/spawn_topic_agent.md` IN FULL and "
          "execute it end-to-end for those inputs.\n\n"
        + "User-facing text (Lark posts, doc) in Simplified Chinese; audit "
          "entries / filenames / command args in English.\n"
    )


def _write_spec_file(path, values, ctx_lines, extras):
    """Persist the ref-mode spec. Returns the path, or None if unwritable."""
    payload = dict(values)
    payload["context"] = ctx_lines
    payload.update(extras)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        topic_store.write_atomic(path, payload)
    except OSError:
        return None
    return path


def _build_spec(entry, plan, ctx, template_text, mode):
    """Return a spec dict for one work entry, or a {skip:...} marker."""
    thread_id = entry.get("thread_id")
    ticket_id = entry.get("ticket_id")
    topic_file = entry.get("topic_file")
    lock_file = entry.get("lock_file")

    if not topic_file or not os.path.exists(topic_file):
        return {"skip": True, "thread_id": thread_id, "ticket_id": ticket_id,
                "skip_reason": "topic_missing"}

    try:
        topic = topic_store.read(topic_file)
    except (OSError, ValueError) as exc:
        return {"skip": True, "thread_id": thread_id, "ticket_id": ticket_id,
                "skip_reason": f"topic_unreadable: {exc}"}

    review = topic.get("review") or {}
    expected_state = review.get("state") or entry.get("state")
    expected_phase = _norm_phase(review.get("review_phase"))
    triage = review.get("triage")
    event_type = _classify_event(topic)
    # Resolve the @-mention targets here so the agent never hunts for a
    # contacts cache. `ack_new_topic` already stored the developer's display
    # name + open_id; null name -> the generic label (deterministic, not
    # rediscovered by each spawn). The approver label is fixed in templates.
    identity = topic.get("identity") or {}
    developer_open_id = identity.get("creator_open_id") or ""
    developer_name = identity.get("developer") or "开发者"
    approver_name = "审查人"
    langs = _detect_langs(topic)
    lang_hint = _lang_hint(langs)
    mrs_summary = _mrs_summary(topic)
    model = _MODEL_BY_EVENT.get(event_type, _DEFAULT_MODEL)
    cycle_id = plan.get("cycle_id")
    skill_dir = ctx.get("skill_dir") or str(Path(__file__).resolve().parents[1]).replace("\\", "/")
    scripts_dir = f"{skill_dir}/scripts"
    activity_log = f"{skill_dir}/cfg/activity.log"

    values = {
        "topic_file": topic_file,
        "lock_file": lock_file,
        "thread_id": thread_id,
        "ticket_id": ticket_id,
        "poll_cycle_id": cycle_id,
        "approver_open_id": ctx.get("approver_open_id", ""),
        "approver_name": approver_name,
        "developer_open_id": developer_open_id,
        "developer_name": developer_name,
        "chat_id": ctx.get("chat_id", ""),
        "chaos_root": ctx.get("chaos_root", ""),
        "rage_root": ctx.get("rage_root", ""),
        "skill_dir": skill_dir,
        "expected_phase": expected_phase,
        "expected_state": expected_state,
    }

    pending = (topic.get("events") or {}).get("pending") or []
    ctx_lines = [
        f"- Ticket **{ticket_id}** is in state `{expected_state}` "
        f"(phase `{expected_phase}`, triage `{triage}`).",
        f"- MRs: {mrs_summary}.",
        f"- Classified event: `{event_type}` ({len(pending)} pending).",
        f"- {lang_hint}",
        "- DEVELOPER_ID / DEVELOPER_NAME / APPROVER_ID / APPROVER_NAME are "
        "resolved in Inputs above — use them verbatim for template "
        "@-mentions. Do NOT search for or read a contacts cache.",
    ]

    # Round N+1: hand the agent the already-computed split so "which issues do
    # I re-check?" is never its judgement call. Issues a previous round already
    # confirmed fixed are carried, not re-verified (DESIGN §1.4.8).
    to_verify, carried = review_rounds.split_flagged_for_verification(
        review.get("flagged_issues"))
    if carried:
        ctx_lines.append(
            "- Flagged issues already SETTLED by an earlier round — do NOT "
            "re-verify and do NOT re-grep them. Carry each into "
            "`VERIFIED_ISSUES` with its stored verdict and the rationale "
            "「已于前轮确认修复」: "
            + ", ".join("#%s (%s)" % (e.get("index"), e.get("verification"))
                        for e in carried) + ".")
        ctx_lines.append(
            "- Flagged issues to verify THIS round: "
            + (", ".join("#%s" % i for i in review_rounds.indices(to_verify)) if to_verify
               else "(none — every flagged issue is settled; say so in "
                    "SUMMARY and let the approver close the round)") + ".")

    spec_file = None
    if mode == "full":
        prompt = _render_full(template_text, values)
    elif mode == "compact":
        prompt = _render_compact(values, ctx_lines)
    else:  # ref (default)
        spec_file = _write_spec_file(
            _spec_path(skill_dir, cycle_id, thread_id), values, ctx_lines,
            {"event_type": event_type, "triage": triage,
             "mrs_summary": mrs_summary, "pending_count": len(pending)})
        if spec_file is None:
            # Unwritable spec dir — fall back to inlining rather than handing
            # the agent a prompt that points at a file that isn't there.
            prompt = _render_compact(values, ctx_lines)
        else:
            prompt = _render_ref(values, spec_file)

    telemetry_argv = [
        "python", f"{scripts_dir}/activity_logger.py",
        "--log-file", activity_log, "--tokens",
        "--thread-id", str(thread_id), "--state", str(expected_state),
        "--cycle-id", str(cycle_id), "--ticket-id", str(ticket_id),
        "--event-type", str(event_type),
    ]

    if mode == "ref" and spec_file:
        # Deliberately minimal: everything omitted here lives in spec_file or
        # the plan. This dict is read into the PARENT's context every cycle.
        return {
            "thread_id": thread_id,
            "ticket_id": ticket_id,
            "model": model,
            "description": f"Process {ticket_id}",
            "spec_file": spec_file,
            "prompt": prompt,
        }

    return {
        "thread_id": thread_id,
        "ticket_id": ticket_id,
        "topic_file": topic_file,
        "lock_file": lock_file,
        "model": model,
        "description": f"Process {ticket_id}",
        "expected_state": expected_state,
        "expected_phase": expected_phase,
        "event_type": event_type,
        "triage": triage,
        "mrs_summary": mrs_summary,
        "lang_hint": lang_hint,
        "developer_open_id": developer_open_id,
        "developer_name": developer_name,
        "approver_name": approver_name,
        "pending_count": len(pending),
        "prompt": prompt,
        "telemetry": {
            "argv": telemetry_argv,
            "thread_id": thread_id,
            "state": expected_state,
            "cycle_id": cycle_id,
            "ticket_id": ticket_id,
            "event_type": event_type,
        },
    }


def main():
    ap = argparse.ArgumentParser(description="Render review-bot spawn prompt(s)")
    default_plan = str(Path(__file__).resolve().parents[1] / "cfg" / "dispatch_plan.json")
    ap.add_argument("--plan", default=default_plan,
                    help="Path to dispatch_plan.json (default: cfg/dispatch_plan.json)")
    ap.add_argument("--thread-id", help="Render only this topic's spec")
    ap.add_argument("--all", action="store_true",
                    help="Render every work entry up to parallel_limit")
    ap.add_argument("--limit", type=int, default=None,
                    help="Override parallel_limit when used with --all")
    ap.add_argument("--mode", choices=("ref", "compact", "full"), default="ref",
                    help="ref (default): write inputs to cfg/spawn/<...>.json "
                         "and emit a pointer prompt + trimmed spec — smallest "
                         "parent context (DESIGN §1.4.6); compact: inline the "
                         "inputs; full: inline the rendered "
                         "spawn_topic_agent.md")
    ap.add_argument("--no-lock-check", action="store_true",
                    help="Do not skip entries whose .lock is held & fresh")
    args = ap.parse_args()

    if not args.thread_id and not args.all:
        print(json.dumps({"error": "pass --thread-id <id> or --all"}))
        return 1

    try:
        with open(args.plan, encoding="utf-8") as f:
            plan = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": f"cannot read plan: {exc}"}))
        return 1

    ctx = plan.get("context") or {}
    template_text = ""
    if args.mode == "full":
        sibling = Path(__file__).resolve().parent / "spawn_topic_agent.md"
        tmpl_path = plan.get("prompt_template") or str(sibling)
        # Fall back to the sibling contract if the plan's path is relative /
        # stale / missing (production plans carry an absolute path).
        if not os.path.exists(tmpl_path):
            tmpl_path = str(sibling)
        try:
            with open(tmpl_path, encoding="utf-8") as f:
                template_text = f.read()
        except OSError as exc:
            print(json.dumps({"error": f"cannot read template: {exc}"}))
            return 1

    work = plan.get("work") or []

    if args.thread_id:
        entry = next((w for w in work if w.get("thread_id") == args.thread_id), None)
        if entry is None:
            print(json.dumps({"error": f"thread_id {args.thread_id} not in plan.work"}))
            return 1
        spec = _build_spec(entry, plan, ctx, template_text, args.mode)
        print(json.dumps(spec, ensure_ascii=False, indent=2))
        return 0

    limit = args.limit if args.limit is not None else plan.get("parallel_limit", 4)
    specs, skipped = [], []
    for entry in work[:limit]:
        if not args.no_lock_check:
            age = _lock_age_seconds(entry.get("lock_file"))
            if age is not None and age <= topic_store.STALE_LOCK_SECONDS:
                skipped.append({
                    "thread_id": entry.get("thread_id"),
                    "ticket_id": entry.get("ticket_id"),
                    "skip_reason": "lock_held_fresh",
                    "lock_age_s": int(age),
                })
                continue
        spec = _build_spec(entry, plan, ctx, template_text, args.mode)
        if spec.get("skip"):
            skipped.append(spec)
        else:
            specs.append(spec)

    print(json.dumps({
        "cycle_id": plan.get("cycle_id"),
        "count": len(specs),
        "specs": specs,
        "skipped": skipped,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
