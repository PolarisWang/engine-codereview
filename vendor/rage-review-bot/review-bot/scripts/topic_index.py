"""Maintain the open-topic index (cfg/open_topic_index.json).

The index is a flat map {thread_id: ticket_id} covering only OPEN
topics (anything under cfg/topics/*.json, not under closed/). It's
a cache — the ground truth is the actual topic files — but the
router reads it on every event so rebuilding is cheap when needed.
"""

import json
import os
from pathlib import Path

import topic_store


def _index_stale(index_path, topics_dir):
    """Return True if the index doesn't exist or is older than any
    topic file under topics_dir (not recursive — excludes closed/)."""
    if not index_path.exists():
        return True
    idx_mtime = index_path.stat().st_mtime
    try:
        for p in topic_store.iter_topic_files(topics_dir):
            if p.stat().st_mtime > idx_mtime:
                return True
    except FileNotFoundError:
        return True
    return False


def rebuild(topics_dir, index_path):
    """Scan topics_dir (flat) and write a fresh index to index_path."""
    topics_dir = Path(topics_dir)
    index_path = Path(index_path)
    index = {}
    for p in topic_store.iter_topic_files(topics_dir):
        try:
            topic = topic_store.read(p)
        except (OSError, ValueError):
            continue
        tid = topic.get("thread_id")
        ticket = (topic.get("identity") or {}).get("ticket_id")
        if tid and ticket:
            index[tid] = ticket
    topic_store.write_atomic(index_path, index)
    return index


def load(index_path):
    """Load the index as a dict. Returns empty dict if missing."""
    try:
        return topic_store.read(index_path)
    except FileNotFoundError:
        return {}


def load_or_rebuild(topics_dir, index_path):
    """Return the index, rebuilding it from cfg/topics/*.json if stale.

    Staleness check is cheap: compare index mtime to max topic file
    mtime. A missing index counts as stale.
    """
    topics_dir = Path(topics_dir)
    index_path = Path(index_path)
    if _index_stale(index_path, topics_dir):
        return rebuild(topics_dir, index_path)
    return load(index_path)


def add(index_path, thread_id, ticket_id):
    """Add or update an entry and persist."""
    idx = load(index_path)
    idx[thread_id] = ticket_id
    topic_store.write_atomic(index_path, idx)
    return idx


def remove(index_path, thread_id):
    """Drop an entry (idempotent) and persist."""
    idx = load(index_path)
    if thread_id in idx:
        del idx[thread_id]
        topic_store.write_atomic(index_path, idx)
    return idx


def save(index_path, index):
    """Persist a whole index dict."""
    topic_store.write_atomic(index_path, index)
