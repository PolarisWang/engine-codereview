#!/usr/bin/env python3
"""
Shared helpers for the code-review pipeline scripts.

Single source of truth for:
  - Project/LLM/workspace config (from config.yaml at repo root)
  - Jira URL pattern
  - HTTP requests with retry / backoff (urllib, no external deps)

Used by: jira_parser.py, feishu_scanner.py, code_reviewer.py, feishu_notifier.py
"""
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

# Config file location: <repo_root>/config.yaml
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
CONFIG_PATH = os.path.join(REPO_ROOT, "config.yaml")

# ── Jira URL pattern (single canonical copy) ────────────────────────────────
# Matches https://host/browse/ISSUE-123 or https://host/issues/ISSUE-123
JIRA_URL_PATTERN = re.compile(
    r'https?://[\w.-]+/(?:browse|issues)/([A-Za-z][A-Za-z0-9]+-\d+)'
)

# GitLab MR URL pattern: https://host/group/sub/project/-/merge_requests/123
MR_URL_PATTERN = re.compile(r'https?://[\w.-]+/(?:.+?)/-/merge_requests/(\d+)')

# ── Config loading ──────────────────────────────────────────────────────────

_CONFIG_CACHE = None


def _parse_yaml_scalar(val):
    """Coerce a scalar value: null/bool/int are handled; strings stay strings."""
    v = val.strip()
    if v in ("", "null", "Null", "NULL", "~"):
        return None
    if v in ("true", "True", "TRUE"):
        return True
    if v in ("false", "False", "FALSE"):
        return False
    if v.startswith('"') and v.endswith('"') and len(v) >= 2:
        return v[1:-1]
    if v.startswith("'") and v.endswith("'") and len(v) >= 2:
        return v[1:-1]
    # YAML plain int
    try:
        return int(v)
    except ValueError:
        pass
    return v


def _parse_yaml_minimal(text):
    """
    Minimal YAML-subset parser for config.yaml WITHOUT PyYAML.

    Deliberately supports the syntax this repo actually uses:
      - flat and block-nested mappings (2-space indentation)
      - scalar values: quoted ('...' / "..."), bool, int, plain strings
      - block scalars: `key: |` and `key: >` (multi-line literal / folded)
      - comments (starts with #) and blank lines

    Full YAML (anchors, lists, complex nesting) is left to PyYAML when present;
    if this parser meets syntax it cannot handle it raises ValueError so the
    caller surfaces a clear, actionable error instead of misparsing silently.
    """
    result = {}
    current = result
    stack = []                # (indent, dict)
    lines = text.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        # strip comments (careful not to strip # inside quotes — config doesn't use them)
        line = raw.split("#", 1)[0] if "#" in raw else raw
        line = line.rstrip()
        if not line.strip():
            i += 1
            continue
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if stripped.startswith("- "):
            raise ValueError("list items are unsupported by the minimal YAML parser; "
                             "install PyYAML on the agent instead")
        if ":" not in stripped:
            raise ValueError(f"unsupported line in config without ':' at line {i+1}: {stripped[:40]}")
        key, _, val = stripped.partition(":")
        key = key.strip()
        val = val.strip()
        # Rebuild stack for this indent level.
        while stack and stack[-1][0] >= indent:
            stack.pop()
            current = stack[-1][1] if stack else result

        if not val:
            # Nested mapping — but first check for a block scalar introducer on
            # the SAME key is impossible (block scalars have a marker after ':').
            child = {}
            current[key] = child
            stack.append((indent, child))
            current = child
            i += 1
        elif val in ("|", ">"):
            # Block scalar: absorb subsequent more-indented lines.
            block_lines = []
            i += 1
            while i < n:
                inner = lines[i].rstrip()
                if not inner.strip():
                    # blank line inside block — keep, unless it's the terminator
                    # (we stop only when a line is less/equal indented with content)
                    if not lines[i].strip():
                        # peek ahead: if next non-blank is <= this key's indent, end block
                        j = i
                        while j < n and not lines[j].strip():
                            j += 1
                        if j >= n or (len(lines[j]) - len(lines[j].lstrip())) <= indent:
                            break
                        block_lines.append("")
                        i += 1
                        continue
                    block_lines.append("")
                    i += 1
                    continue
                inner_indent = len(inner) - len(inner.lstrip())
                if inner_indent <= indent:
                    break
                block_lines.append(inner[inner_indent:])
                i += 1
            while block_lines and block_lines[-1] == "":
                block_lines.pop()
            folded = (val == ">")
            body = "\n" if not folded else " "
            block_val = body.join(block_lines)
            current[key] = block_val
        else:
            current[key] = _parse_yaml_scalar(val)
            i += 1
    return result


def load_config():
    """Load config.yaml once. Uses PyYAML if available, else a minimal built-in
    parser so the scripts never hard-depend on a yaml module being installed on
    the Jenkins agent. Returns {} on any error."""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    raw = None
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        _CONFIG_CACHE = {}
        print(f"[config] Failed to read {CONFIG_PATH}: {e}", file=sys.stderr)
        return _CONFIG_CACHE
    try:
        import yaml
        _CONFIG_CACHE = yaml.safe_load(raw) or {}
    except ImportError:
        # PyYAML not installed on this agent — use the minimal parser.
        try:
            _CONFIG_CACHE = _parse_yaml_minimal(raw)
            print("[config] PyYAML not found; used minimal built-in YAML parser", file=sys.stderr)
        except ValueError as e:
            _CONFIG_CACHE = {}
            print(f"[config] Minimal YAML parse failed for {CONFIG_PATH}: {e}", file=sys.stderr)
    except Exception as e:
        _CONFIG_CACHE = {}
        print(f"[config] Failed to load {CONFIG_PATH}: {e}", file=sys.stderr)
    return _CONFIG_CACHE


def get_projects():
    """Return the projects mapping (id -> config dict)."""
    return load_config().get("projects", {})


def get_project(project_id):
    """Return a single project config (normalized), or None if unknown."""
    return get_projects().get(project_id)


def get_claude_config():
    """Return the claude config block (model/max_tokens/review_instructions)."""
    return load_config().get("claude", {})


def get_workspace_config():
    """Return the workspace config block (base_dir/cache_repos)."""
    return load_config().get("workspace", {})


# ── 方案B 中央配置访问器：单一事实源 config.yaml，敏感/部署点可用 env 覆盖 ──
# 优先级：config.yaml 值 > env > 代码默认。secret(app_id/secret/token) 只在 env。

def _env(key, default=""):
    return os.environ.get(key, default)


def get_paths():
    """Return the paths: block {workspace, state_file, reviewed_msg_ids}. 'workspace'
    falls back to workspace.base_dir (kept in sync)."""
    cfg = load_config()
    p = cfg.get("paths") or {}
    if not p.get("workspace"):
        p["workspace"] = (cfg.get("workspace") or {}).get("base_dir", "")
    return p


def c_workspace(env_key="REVIEW_WORKSPACE"):
    """Resolve the review workspace: env(REVIEW_WORKSPACE) > paths.workspace >
    workspace.base_dir > default cr-workspace."""
    return _env(env_key, "") or get_paths().get("workspace", "") or "/var/lib/report-server/daily/cr-workspace"


def c_state_file():
    """Resolve the shared topic state file: env(PIPELINE_STATE_FILE) > paths.state_file."""
    return _env("PIPELINE_STATE_FILE", "") or get_paths().get("state_file", "") \
        or "/root/.codereview-pipeline-state.json"


def c_reviewed_msg_ids_file():
    return _env("REVIEWED_MSG_IDS_FILE", "") or get_paths().get("reviewed_msg_ids", "") \
        or os.path.expanduser("~/.codereview-processed-msg-ids.json")


def c_gitlab_host():
    """Resolve the GitLab API host: env(GITLAB_HOST) > gitlab.host."""
    return _env("GITLAB_HOST", "") or (load_config().get("gitlab") or {}).get("host", "") \
        or "gitlab.booming-inc.com"


def c_feishu_chat_id():
    """Resolve the target Feishu chat id: env(FEISHU_CHAT_ID) > feishu.chat_id."""
    return _env("FEISHU_CHAT_ID", "") or (load_config().get("feishu") or {}).get("chat_id", "")


def c_feishu_bot_name():
    return _env("FEISHU_BOT_NAME", "") or (load_config().get("feishu") or {}).get("bot_name", "")


def c_feishu_bot_open_id():
    return _env("FEISHU_BOT_OPEN_ID", "") or (load_config().get("feishu") or {}).get("bot_open_id", "")


def c_claude_model():
    cl = load_config().get("claude", {})
    return cl.get("model") or _env("ANTHROPIC_MODEL", "") or "deepseek-v4-flash"


def c_claude_base_url():
    cl = load_config().get("claude", {})
    return cl.get("base_url") or _env("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")


# ── HTTP with retry (shared by all external API callers) ────────────────────

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


def http_request(method, url, data=None, headers=None, timeout=30,
                 retries=3, backoff=1.0, raw_body=None,
                 retryable_status=RETRYABLE_STATUS):
    """
    POST/GET with exponential backoff retry on retryable statuses, timeouts,
    and transient connection errors.

    - method:  'GET' / 'POST' / 'PATCH'
    - data:    dict -> serialized as JSON (utf-8)
    - raw_body: pre-serialized string (takes precedence over data)
    - headers: dict of extra headers
    - retries: number of attempts (>=1); with retries=1 it behaves like one shot
    - retryable_status: status codes that trigger a retry

    Returns parsed JSON (dict/list) on success, or None on final failure.
    Retries only happen for retryable conditions; deterministic 4xx client
    errors (e.g. 401/403/404) are NOT retried and return None immediately.
    """
    if headers is None:
        headers = {}
    headers.setdefault("Content-Type", "application/json")
    if raw_body:
        body = raw_body.encode("utf-8")
    elif data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    else:
        body = None

    req = urllib.request.Request(url, data=body, method=method, headers=headers)

    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                try:
                    return json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    return {"code": -1, "msg": "non-json response",
                            "raw": raw.decode("utf-8", errors="replace")[:500]}
        except urllib.error.HTTPError as e:
            if e.code in retryable_status and attempt < retries:
                last_exc = e
                _sleep_backoff(backoff, attempt)
                continue
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_exc = e
            if attempt < retries:
                _sleep_backoff(backoff, attempt)
                continue
            return None
        except Exception as e:
            # Unexpected error — log and bail (don't mask with retries)
            print(f"[http] Unexpected error on {method} {url[:80]}: {e}",
                  file=sys.stderr)
            return None
    return None


def _sleep_backoff(backoff, attempt):
    """Sleep with exponential backoff + jitter for a failed attempt."""
    delay = backoff * (2 ** (attempt - 1))
    # small deterministic-ish jitter (clamped to [0, 1)) to avoid thundering herd
    jitter = (time.time() * 1000) % 1000 / 1000.0
    time.sleep(min(delay + jitter, 30))