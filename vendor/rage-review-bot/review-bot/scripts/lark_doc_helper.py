"""Deterministic Lark doc creation + permission grants for review-bot agents.

Removes the "shell pipes truncate large markdown on Windows" footgun by
passing all arguments via Python subprocess argv directly to lark-cli
(no cmd.exe interpolation, no shell=True). Replaces the multi-block
inline shell snippet that topic agents previously had to construct
themselves.

CLI:
    python lark_doc_helper.py create \\
        --title "代码审查 RAGE-12473" \\
        --markdown-file D:/tmp/RAGE-12473_review.md \\
        --grant-view ou_AAA,ou_BBB \\
        [--public-link tenant_readable]

Output: a single JSON line on stdout.
    {"status":"ok","docx_token":"...","url":"...",
     "perms_granted":[...],"perms_failed":[...],"public_link":{...}}
or
    {"status":"error","stage":"create|grant|public","error":"..."}.

Exit 0 if the doc was created (perm grant failures still status=ok with
non-empty perms_failed); 1 if doc creation failed.
"""

import argparse
import json
import sys
from pathlib import Path

import subprocess_util


def _run_lark(argv_tail, timeout=120):
    """Run lark-cli (via lark_cli_argv_prefix + hidden_run) with the given
    sub-args. No shell. Returns the completed subprocess result.
    """
    argv = subprocess_util.lark_cli_argv_prefix() + argv_tail
    return subprocess_util.hidden_run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )


def _parse_create_response(stdout):
    """Extract docx_token + url from a `docs +create` stdout payload."""
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") or {}
    doc_id = data.get("doc_id") or data.get("docx_token") or data.get("token")
    doc_url = data.get("doc_url") or data.get("url")
    if not doc_id:
        return None
    if not doc_url:
        doc_url = f"https://www.feishu.cn/docx/{doc_id}"
    return {"docx_token": doc_id, "url": doc_url}


def _create_doc(title, markdown):
    result = _run_lark([
        "docs", "+create",
        "--title", title,
        "--markdown", markdown,
    ])
    # Success JSON lands on stdout; API-error JSON lands on stderr with rc=1.
    parsed = (_parse_create_response(result.stdout)
              or _parse_create_response(result.stderr))
    if parsed:
        return {"status": "ok", **parsed}
    raw = ((result.stderr or "") + (result.stdout or "")).strip()
    return {"status": "error", "stage": "create",
            "error": raw[:500] or f"non-zero exit {result.returncode}"}


# Permission codes treated as "already has access" (no-op success):
#   1700101 / 1700107 — already_granted (older drive API)
#   1063003           — "Invalid operation" returned when adding the
#                       doc owner / existing member as a viewer; for a
#                       freshly-created doc the only path to this code is
#                       "member already has access".
_ALREADY_GRANTED_CODES = {1700101, 1700107, 1063003}


def _grant_view(docx_token, open_id):
    params = json.dumps(
        {"token": docx_token, "type": "docx", "need_notification": "false"},
        ensure_ascii=False)
    data = json.dumps(
        {"member_id": open_id, "member_type": "openid",
         "perm": "view", "type": "user"},
        ensure_ascii=False)
    result = _run_lark([
        "drive", "permission.members", "create",
        "--params", params,
        "--data", data,
    ])
    # lark-cli writes API-error JSON to stderr (not stdout) and exits 1.
    # Try stdout first, fall back to stderr; only treat as a non-JSON
    # failure if neither parses.
    payload = None
    for blob in (result.stdout, result.stderr):
        if not blob:
            continue
        try:
            cand = json.loads(blob)
        except (TypeError, ValueError):
            continue
        if isinstance(cand, dict):
            payload = cand
            break
    if payload is None:
        raw = ((result.stderr or "") + (result.stdout or "")).strip()
        return {"ok": False,
                "error": raw[:300] or f"exit {result.returncode}"}
    if payload.get("ok"):
        return {"ok": True}
    # lark-cli sometimes passes the raw Lark API envelope straight through
    # (no top-level `ok` wrapper): `{"code":0,"msg":"Success","data":{...}}`.
    # A `code` of 0 IS success — without this branch a genuine first-time
    # grant (which returns code:0) gets mislabeled as a failure with the
    # useless error string "None: " (0 is falsy, so the code/message lookup
    # below yields None). The already-granted no-op path masked this because
    # repeat grants return 1700101/1063003, not 0.
    if payload.get("code") == 0:
        return {"ok": True}
    err = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    inner = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    code = err.get("code") or inner.get("code")
    msg = (err.get("message") or inner.get("msg") or "").lower()
    if code in _ALREADY_GRANTED_CODES or "already" in msg:
        return {"ok": True, "note": "already_granted"}
    detail = err.get("message") or inner.get("msg") or msg
    return {"ok": False, "error": f"{code}: {detail}"[:300]}


def _try_public_link(docx_token, link_share_entity):
    """Best-effort org link sharing via `drive permission.public patch` (1.0.48;
    the old `update` verb was removed). Sets ONLY the link-share level (e.g.
    tenant_readable) — bundling security_entity/share_entity/external_access (as
    the old code did) gets rejected with API 91012 on locked-down tenants, and
    isn't needed for org-readable. Other public-permission fields are left
    untouched. Wrapped best-effort — never fails the caller. The reliable
    per-user path is _grant_view (permission.members create)."""
    params = json.dumps({"token": docx_token, "type": "docx"},
                        ensure_ascii=False)
    data = json.dumps({"link_share_entity": link_share_entity}, ensure_ascii=False)
    try:
        result = _run_lark([
            "drive", "permission.public", "patch",
            "--params", params,
            "--data", data,
            "--yes",  # patch is flagged high-risk-write; confirm non-interactively
        ], timeout=30)
    except Exception as e:
        return {"attempted": True, "applied": False,
                "reason": f"spawn_failed: {e}"[:120]}
    payload = None
    for blob in (result.stdout, result.stderr):
        if not blob:
            continue
        try:
            cand = json.loads(blob)
        except (TypeError, ValueError):
            continue
        if isinstance(cand, dict):
            payload = cand
            break
    if payload is None:
        # No JSON on either stream — unexpected (patch is a valid verb);
        # treat as not-applied rather than crash.
        return {"attempted": True, "applied": False, "reason": "no_json_response"}
    if payload.get("ok") or payload.get("code") == 0:
        return {"attempted": True, "applied": True}
    return {"attempted": True, "applied": False, "reason": "api_rejected"}


def _cmd_create(args):
    md_path = Path(args.markdown_file)
    if not md_path.is_file():
        return {"status": "error", "stage": "create",
                "error": f"markdown file not found: {md_path}"}
    try:
        markdown = md_path.read_text(encoding="utf-8")
    except OSError as e:
        return {"status": "error", "stage": "create",
                "error": f"reading markdown: {e}"}
    if not markdown.strip():
        return {"status": "error", "stage": "create",
                "error": "markdown file is empty"}

    created = _create_doc(args.title, markdown)
    if created.get("status") != "ok":
        return created
    docx_token = created["docx_token"]
    url = created["url"]

    perms_granted = []
    perms_failed = []
    seen = set()
    for raw in (args.grant_view or "").split(","):
        oid = raw.strip()
        if not oid or oid in seen:
            continue
        seen.add(oid)
        r = _grant_view(docx_token, oid)
        if r["ok"]:
            entry = {"open_id": oid}
            if r.get("note"):
                entry["note"] = r["note"]
            perms_granted.append(entry)
        else:
            perms_failed.append({"open_id": oid, "error": r.get("error")})

    public_link = (_try_public_link(docx_token, args.public_link)
                   if args.public_link else None)

    out = {
        "status": "ok",
        "docx_token": docx_token,
        "url": url,
        "perms_granted": perms_granted,
        "perms_failed": perms_failed,
    }
    if public_link is not None:
        out["public_link"] = public_link
    return out


def _cli():
    ap = argparse.ArgumentParser(description="lark_doc_helper CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create",
                       help="Create a Lark docx + grant view perms")
    c.add_argument("--title", required=True)
    c.add_argument("--markdown-file", required=True,
                   help="Path to UTF-8 markdown body")
    c.add_argument("--grant-view", default="",
                   help="Comma-separated open_ids to grant view perm")
    c.add_argument("--public-link", default=None,
                   choices=["tenant_readable", "anyone_readable"],
                   help="Best-effort public link sharing")

    args = ap.parse_args()
    if args.cmd == "create":
        result = _cmd_create(args)
    else:
        result = {"status": "error", "error": f"unknown cmd: {args.cmd}"}

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(_cli())
