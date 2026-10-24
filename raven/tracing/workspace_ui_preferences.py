"""Bounded per-attempt view history, separate from immutable trace evidence."""

from __future__ import annotations

import json
import re

MAX_RUNS = 32
MAX_RUN_BYTES = 64 << 10
MAX_HISTORY_BYTES = 512 << 10


def _snapshot(view, keys):
    raw = json.dumps(
        {key: view[key] for key in keys}, ensure_ascii=True, allow_nan=False
    )
    return json.loads(raw), len(raw.encode())


def remember(state, view, keys):
    key = view.get("run_key")
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
        return
    history = state.setdefault("workspace_runs", {})
    snapshot, size = _snapshot(view, keys)
    if size > MAX_RUN_BYTES:
        state["workspace_preferences_notice"] = (
            "This view exceeds the 64 KiB per-run preference history budget; its captured evidence is unaffected. Simplify saved ranges to retain this view after switching runs."
        )
        history.pop(key, None)
        return
    history.pop(key, None)
    history[key] = snapshot
    total = sum(
        len(json.dumps(value, ensure_ascii=True).encode()) for value in history.values()
    )
    while history and (len(history) > MAX_RUNS or total > MAX_HISTORY_BYTES):
        oldest = next(iter(history))
        total -= len(json.dumps(history.pop(oldest), ensure_ascii=True).encode())
    state.pop("workspace_preferences_notice", None)


def restore_history(value, keys, initialize):
    result = {}
    if not isinstance(value, dict):
        return result
    total = 0
    for key, entry in list(value.items())[-MAX_RUNS:]:
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[0-9a-f]{64}", key)
            or not isinstance(entry, dict)
            or entry.get("run_key") != key
        ):
            continue
        state = {
            "workspace_view": {name: entry[name] for name in keys if name in entry}
        }
        snapshot, size = _snapshot(initialize(state), keys)
        if size > MAX_RUN_BYTES or total + size > MAX_HISTORY_BYTES:
            continue
        total += size
        result[key] = snapshot
    return result
