"""closure.py — rage-style review closure, ported from rage's self-service dev loop.

Pure, self-contained decision layer (no pipeline_state / feishu coupling): given a
topic's current review state + the raw reply + sender identity, decide:

  - the resolved intent (role / intent / indices), via rage's `reply_parser`
  - the next topic state, via rage's `state_machine`
  - what to persist on the topic (dev_triage sets, flagged issues, round)
  - what to tell the user (approver/dev routing)

Everything here is unit-testable without a live MR or Feishu. The event_server /
orchestrate layers read `config`) for per-project `approver_open_ids` and apply the
dict this returns.

Port policy (对齐 rage): the closure semantics match rage DESIGN §1.5 / §1.23 —
DEV_TRIAGE (dev triages first, every round) → SIMPLE/FULL_REVISION → AWAITING_APPROVAL
(dev `done` hands off) → APPROVED/CLOSED. ARBITRATION is retained-but-unentered for
drain of in-flight topics. APPROVED is transient (merge train).
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

import reply_parser as rp
import state_machine as sm


# States we can be in while the review is actionable (dev/approver).
REVIEW_STATES = {
    "TRIAGING", "INLINE_REVIEW", "FULL_REVIEW", "TRIAGE_DECISION",
    "DEV_TRIAGE", "ARBITRATION", "AWAITING_APPROVAL",
    "SIMPLE_REVISION", "FULL_REVISION", "APPROVED",
}


def parse_open_ids(raw):
    """归一化 approver_open_ids 到真 open_id 列表。

    minimal YAML(prod 无 PyYAML) 把内联列表 `[ "ou_a", "ou_b" ]` 解析成**字符串**
    `'["ou_a", "ou_b"]'` 而非真 list。这里兼容:
      - list   -> 原样(逐项去空白/去引号)
      - JSON 数组字符串 `["a","b"]` / `'["a"]'` -> 解析
      - 逗号分隔字符串 `a,b` -> split
    返回去空白/去引号/去空 的去重 open_id 列表(可能为空)。"""
    import re as _re, json as _json
    raw = raw or []
    if isinstance(raw, str):
        s = raw.strip()
        # 形如 ["a", "b"] / ["a"] 的 JSON 数组字符串(minimal YAML 产物)
        if s.startswith("[") and s.endswith("]"):
            try:
                arr = _json.loads(s)
                if isinstance(arr, list):
                    return parse_open_ids(arr)
            except Exception:
                pass
            # 非严格 JSON 但带引号 -> 摘引号
            return [_re.sub(r'^["\']|["\']$', '', t).strip()
                    for t in _re.findall(r'["\']([^"\']+)["\']', s)]
        # 逗号/空白分隔
        return [t for t in (_re.split(r'[\s,，]+', s) if s else []) if t]
    if isinstance(raw, (list, tuple)):
        out = []
        for x in raw:
            if isinstance(x, str):
                x = x.strip().strip('"').strip("'")
            if x:
                out.append(str(x))
        return out
    return []


def approver_ids_for(project_cfg, policy_admins):
    """Resolve the project's approver open_ids.

    Per-project `approver_open_ids` from config.project.<id> wins; fall back to
    policy.yaml `policy.agent.admins` when empty. Returns a list of open_id
    strings (never None).
    """
    ap = parse_open_ids((project_cfg or {}).get("approver_open_ids"))
    ap = [x for x in ap if x]
    if ap:
        return ap
    return [a for a in (policy_admins or []) if a]


def classify(reply_text, sender_id, state, approver_ids, developer_id=None,
             triage="simple"):
    """Classify a thread reply into rage intent (thin wrapper over reply_parser).

    Returns reply_parser.classify_intent result (role/intent/indices/...).
    """
    return rp.classify_intent(reply_text, sender_id, state, approver_ids,
                              developer_id=developer_id, triage=triage)


def next_state(state, action):
    """Apply a state_machine transition; None if invalid."""
    try:
        return sm.transition(state, action)
    except Exception:
        return None


def plan_dev_triage(accepted_indices, rejected_indices, existing=None, triage="simple"):
    """Merge a developer's triage reply into the topic's running dev_triage set.

    existing: {accepted_indices, rejected_indices, reinstated_indices, reasons,
               round} or None.
    Returns updated dev_triage dict (rage DESIGN §1.23.1/.8).
    """
    existing = existing or {}
    acc = set(existing.get("accepted_indices") or [])
    rej = set(existing.get("rejected_indices") or [])
    reasons = dict(existing.get("reasons") or {})
    acc.update(accepted_indices)
    for i in accepted_indices:
        rej.discard(i)   # naming an index retracts an earlier dispute
        reasons.pop(i, None)
    for i in rejected_indices:
        if i in (existing.get("reinstated_indices") or []):
            continue     # a reinstated issue cannot be disputed again (§1.23.9)
        rej.add(i)
        acc.discard(i)
        if i not in reasons:
            reasons[i] = ""
    return {
        "accepted_indices": sorted(acc),
        "rejected_indices": sorted(rej),
        "reinstated_indices": existing.get("reinstated_indices") or [],
        "reasons": reasons,
        "triage": triage,
    }


def reconcil(
        reply_text, sender_id, state, approver_ids, developer_id,
        issue_count, triage="simple", dev_triage=None, reinstate=False):
    """The single entry the event layer calls for a reply to an active review.

    classifies → transitions state → returns `{intent, role, indices, exclude,
    none, next_state, post, persist, reason}`. `post` describes what to tell the
    user (a template name + vars); `persist` is a summary of topic field updates
    the caller should apply to pipeline_state.

    Pure: no I/O. `issue_count` is used to build the revision/dev-triage messaging.
    `reinstate` is set by the caller when the approver is reinstating dev-rejected
    issues in AWAITING_APPROVAL.
    """
    cls = classify(reply_text, sender_id, state, approver_ids,
                   developer_id=developer_id, triage=triage)
    role = cls.get("role", "ignored")
    intent = cls.get("intent")
    indices = cls.get("indices") or []
    exclude = cls.get("exclude", False)
    none = cls.get("none", False)

    out = {"intent": intent, "role": role, "indices": indices,
           "exclude": exclude, "none": none,
           "next_state": None, "post": None, "persist": {}, "reason": ""}

    if role == "ignored":
        out["reason"] = "忽略（无匹配意图）"
        return out

    if intent == "dev_triage":
        # dev says which issues they'll fix; unlisted = disputed (round 1).
        if none:
            acc, rej = [], list(range(1, issue_count + 1))
        elif exclude:
            acc = [i for i in range(1, issue_count + 1) if i not in indices]
            rej = sorted(i for i in indices if 1 <= i <= issue_count)
        else:
            acc, rej = sorted(i for i in indices if 1 <= i <= issue_count), []
        out["persist"]["dev_triage"] = plan_dev_triage(
            acc, rej, existing=dev_triage, triage=triage)
        to = "revision_simple" if triage == "simple" else "revision_full"
        out["next_state"] = next_state("DEV_TRIAGE", to)
        out["post"] = {"template": "revision_request", "vars": {
            "accepted": acc, "rejected": rej, "triage": triage}}
        return out

    if intent == "approve":
        # approver `ok` — the only approval verb. Override anywhere it's allowed.
        out["next_state"] = next_state(state, "approve")
        out["post"] = {"template": "approval", "vars": {}}
        out["persist"]["approved"] = True
        return out

    if intent == "close":
        out["next_state"] = next_state(state, "close")
        out["post"] = {"template": "closed", "vars": {}}
        out["persist"]["closed"] = True
        return out

    if intent == "escalate":
        out["next_state"] = next_state(state, "escalate")
        out["post"] = {"template": "escalated", "vars": {}}
        return out

    if intent == "revision":
        # approver marks issues (or, in AWAITING_APPROVAL, reinstates).
        out["next_state"] = next_state(state, "revision_simple" if triage == "simple"
                                       else "revision_full")
        out["persist"]["flagged_issues"] = indices
        out["post"] = {"template": "revision_request", "vars": {"indices": indices}}
        return out

    if intent == "dev_handoff":
        out["next_state"] = next_state(state, "handoff")
        out["post"] = {"template": "handoff_summary", "vars": {
            "dev_triage": dev_triage or {}}}
        return out

    if intent == "dev_reply":
        # dev pushed fixes; re-review next round. next_state trips the revision
        # loop (SIMPLE_REVISION -> TRIAGE_DECISION legacy, or stays), but the
        # caller triggers a NEW review round; we mark that the loop continued.
        out["persist"]["re_review"] = True
        out["post"] = {"template": "re_review", "vars": {}}
        return out

    if intent == "manual_refresh":
        out["post"] = {"template": "manual_refresh", "vars": {}}
        return out

    if intent == "dev_question":
        out["post"] = {"template": "dev_question", "vars": {}}
        return out

    out["reason"] = f"未处理 intent={intent} role={role}"
    return out


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="rage closure decision")
    p.add_argument("--reply", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--sender", required=True)
    p.add_argument("--approvers", default="")
    p.add_argument("--dev", default="")
    p.add_argument("--issues", type=int, default=0)
    p.add_argument("--triage", default="simple")
    a = p.parse_args()
    apro = [x for x in a.approvers.split(",") if x]
    r = reconcil(a.reply, a.sender, a.state, apro, a.dev, a.issues, a.triage)
    import json
    r["approvers"] = apro
    print(json.dumps(r, ensure_ascii=False, indent=2))
