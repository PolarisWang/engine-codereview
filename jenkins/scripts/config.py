#!/usr/bin/env python3
"""
config.py — 集中配置中心 for the chaos code-review pipeline.

All tunable runtime parameters, USER-FACING copy (messages), and command words live
in config.yaml so operators/admins can adjust them in ONE place without touching the
business code (方案C: 配置统一化)。This module loads config.yaml and exposes:

    IDLE_CLOSE_DAYS / AUTO_CLOSE_MR / MAX_CONCURRENT_REVIEWS / ...  参数
    MSG        dict: user-facing copy templates (from config.yaml messages:)
    CMD        dict: command word lists (from config.yaml commands:)

Values imported into orchestrate.py / code_reviewer.py via `from config import *`.
The values below are DEFAULTS; config.yaml overrides them.
"""
import os
# Defaults first; load_config() later overrides from config.yaml.
IDLE_CLOSE_DAYS = 2
AUTO_CLOSE_MR = True
MAX_CONCURRENT_REVIEWS = 6
DEFAULT_WORKSPACE = "/var/lib/report-server/daily/cr-workspace"
CHECKOUT_RESET_ON_REUSE = True
EDIT_MODEL = "deepseek-v4-flash[1m]"
AGENT_MAX_ROUNDS = 6
AGENT_MAX_TOKEN = 1000

# 用户可调文案(方案C): 集中到 config.yaml messages:, 这里仅默认值。
MSG = {
    "queued": "⚠️ 并发 Review 已达上限，本话题已进入排队，稍后自动开始。",
    "queued_position": "⚠️ 并发 Review 已达上限，当前排队第 {pos} 位，稍后自动开始。",
    "started": "↗️ 轮到本话题了，开始自动 Review...",
    "review_progress": "⏳ 处理中 · 正在 Review...\n已收到 {key}，正在拉取代码并进行 AI 审查，请稍候。\n（这是处理中提示，非最终审查结果）",
    "review_done": "✅ Review 完成。",
    "optimize_started": "⏳ 已开始优化：AI 将自动修复关键问题，改码完成后自动推送修复分支并创建/更新 MR。",
    "optimize_note": "（可再次 `优化` 更新已有 MR，无需单独重申。）",
    "confirm_no_pending": "⛔ 当前没有待确认的自动修改。请先回复 `优化` 生成修改。",
    "close_confirmed": "🔒 本话题已关闭，不再处理。",
    "autoclose_reason": "{days}天无新回复自动关闭",
    "autoclose_notice": "🔒 本话题长时间无新回复，已自动关闭。如需重新审查请新开话题。",
}
# 命令词(方案C): 集中到 config.yaml commands:, 这里仅默认值。
CMD = {
    "optimize": ["优化", "自动优化", "优化代码", "优化并提交"],
    "autofix": ["改码", "自动修改", "自动修复", "autofix", "改码并提交"],
    "guidance": ["指引", "修改指引", "怎么改"],
    "mr": ["mr", "生成mr", "出mr单", "mr单", "更新mr", "更新mr单"],
    "status": ["状态", "/状态", "status"],
    "close": ["4", "关闭", "关闭话题"],
}


def _load_yaml_opt(path=None):
    """Attempt to parse config.yaml (via common.load_config if available). Returns
    dict or {} on absence. We build our own minimal read so config.py doesn't hard-depend
    on common being importable first."""
    try:
        from common import load_config
        return load_config() or {}
    except Exception:
        return {}


def load_config_merged(path=None):
    """Merge config.yaml overrides into this module's globals (MSG/CMD/params).
    Called at import time so `from config import *` sees merged values."""
    global IDLE_CLOSE_DAYS, AUTO_CLOSE_MR, MAX_CONCURRENT_REVIEWS, DEFAULT_WORKSPACE
    global CHECKOUT_RESET_ON_REUSE, EDIT_MODEL, AGENT_MAX_ROUNDS, AGENT_MAX_TOKEN
    global MSG, CMD
    cfg = _load_yaml_opt(path)
    life = cfg.get("lifecycle") or {}
    concurrency = cfg.get("concurrency") or {}
    ws = cfg.get("workspace") or {}
    llm = cfg.get("claude") or {}
    if life.get("idle_close_days") is not None:
        IDLE_CLOSE_DAYS = int(life["idle_close_days"])
    if life.get("auto_close_mr") is not None:
        AUTO_CLOSE_MR = bool(life["auto_close_mr"])
    if concurrency.get("max_reviews") is not None:
        MAX_CONCURRENT_REVIEWS = int(concurrency["max_reviews"])
    if ws.get("base_dir"):
        DEFAULT_WORKSPACE = ws["base_dir"]
    if llm.get("model"):
        EDIT_MODEL = llm["model"]
    # messages/commands 覆盖
    m = cfg.get("messages") or {}
    if isinstance(m, dict):
        MSG = {**MSG, **{k: (str(v) if v is not None else MSG.get(k, "")) for k, v in m.items()}}
    c = cfg.get("commands") or {}
    if isinstance(c, dict):
        merged = dict(CMD)
        for k, v in c.items():
            if isinstance(v, list):
                merged[k] = [str(x) for x in v]
        CMD = merged


load_config_merged()


def M(_k, **kw):
    """Fetch a user-facing message from config.MSG and .format() it. Template uses
    `{name}` placeholders (str.format style) — NOT f-string — so it's safe both in
    config.yaml and here. First positional arg is the message key; all kwargs go to
    .format(), so a template placeholder may be named `key` (e.g. `{key}`) without
    colliding with the function arg. Falls back to the literal key if message missing."""
    tpl = MSG.get(_k)
    if tpl is None:
        return _k
    try:
        return tpl.format(**kw)
    except Exception:
        return tpl
