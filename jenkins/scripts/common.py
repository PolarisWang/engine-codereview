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

# ── Config loading ──────────────────────────────────────────────────────────

_CONFIG_CACHE = None


def _parse_yaml_minimal(text):
    """
    Minimal YAML-subset parser for config.yaml WITHOUT PyYAML.

    Handles the structure actually used by this repo's config.yaml: a flat
    mapping, block-nested mappings (2-space indent), and scalar string/int
    values (with optional quotes and trailing # comments). Comments and blank
    lines are ignored. This is deliberately limited — full YAML is handled by
    PyYAML when available.

    Returns a dict, or raises ValueError on unsupported syntax.
    """
    result = {}
    current = result          # the dict we're currently filling
    stack = []                # stack of (indent, dict) for nesting
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip() if "#" in raw else raw.rstrip()
        if not line.strip():
            continue
        # strip indentation
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if stripped.startswith("- "):
            # list item (only used for flat symbol lists if ever) — not in our config
            raise ValueError("list items unsupported by minimal YAML parser")
        # split key: value
        if ":" not in stripped:
            continue
        key, _, val = stripped.partition(":")
        key = key.strip()
        val = val.strip()
        # Rebuild stack for the new indent level.
        while stack and stack[-1][0] >= indent:
            stack.pop()
            current = stack[-1][1] if stack else result
        if not val:
            # block -> a nested mapping
            child = {}
            current[key] = child
            stack.append((indent, child))
            current = child
        else:
            # scalar
            if val in ("true", "True"):
                val = True
            elif val in ("false", "False"):
                val = False
            elif val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            elif val.startswith("'") and val.endswith("'") and len(val) >= 2:
                val = val[1:-1]
            else:
                # keep as string (numbers stay strings; fine for our config)
                pass
            current[key] = val
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