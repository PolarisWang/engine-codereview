#!/usr/bin/env python3
"""
config.py — 集中配置中心 for the chaos code-review pipeline.

All tunable runtime parameters live here so operators can adjust them in one
place without hunting through orchestrate.py. Values are imported into
orchestrate.py and code_reviewer.py via `from config import *`.

Edit this file directly; changes take effect on the next pipeline invocation.
"""

# ── 生命周期 (topic lifecycle) ──────────────────────────────────────────
# 话题多久无新回复自动关闭（天）。用户可在 Feishu 用 `4 关闭` 提前手动关闭。
IDLE_CLOSE_DAYS = 2
# 自动关闭时是否一并关闭本轮创建的 fix-branch MR + 删除 fix 分支，释放资源。
AUTO_CLOSE_MR = True

# ── 并发 (concurrency admission) ─────────────────────────────────────────
# 同时允许 running 的独立 review 子进程数量上限。每个 topic 的 review 是独立
# 子进程（独立 clone、独立跑，互不竞争），超出上限的新请求进入队列并提示。
MAX_CONCURRENT_REVIEWS = 6

# ── 共享 checkout（arch-D）───────────────────────────────────────────────
# 持久化 workspace：topics 的 checkout + result 文件都在这。
DEFAULT_WORKSPACE = "/var/lib/report-server/daily/cr-workspace"
# 复用已有 checkout 目录时是否强制 fetch + reset --hard + clean，避免陈旧/残留
# SHA 污染下一次 review（曾导致 push 超大 pack / HTTP 413 与损坏改动泄漏）。
CHECKOUT_RESET_ON_REUSE = True

# ── LLM / agent 编辑 ─────────────────────────────────────────────────────
# local `claude -p` 用的模型（此为能触达 [1m] 模型、且原始 HTTP 不 503 的通道）。
EDIT_MODEL = "deepseek-v4-flash[1m]"
# 每次用户消息最多多少轮 tool-call。
AGENT_MAX_ROUNDS = 6
# 每次 agent LLM 回合最大输出 tokens。
AGENT_MAX_TOKEN = 1000
