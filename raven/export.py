"""Exports: a tab as text, the job or history table as CSV, the selected jobs as JSON, written under the state
directory's exports/ folder (or ./raven-exports without a state directory)."""
from __future__ import annotations

import csv
import io
import json
import os
import time
from typing import Dict, List, Optional, Sequence

from .layout import row_text, to_text
from . import clock
from .model import to_plain

JOB_FIELDS = ["id", "name", "part", "st", "where", "cpus", "gpu", "time", "left", "cpu%", "eff", "mem%", "gpu%", "flags", "info"]
FIN_FIELDS = ["id", "name", "state", "part", "elapsed", "cpus", "gpus", "ce", "me", "rss", "start", "end", "exit", "nodes"]


def export_dir(state_dir: Optional[str]) -> str:
    base = os.path.join(state_dir, "exports") if state_dir else os.path.join(os.getcwd(), "raven-exports")
    os.makedirs(base, exist_ok=True)
    return base


def write(state_dir: Optional[str], name: str, content: str) -> str:
    path = os.path.join(export_dir(state_dir), name)
    with open(path, "w") as f:
        f.write(content)
    return path


def stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def rows_to_csv(fields: Sequence[str], rows: Sequence[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(fields)
    for r in rows:
        w.writerow([r.get(k, "") for k in fields])
    return buf.getvalue()


def tab_text(views, snap: dict, app, actions, width: int) -> str:
    """The current tab rendered without a height limit, as plain text."""
    rows, _ = views.compose(snap, app, width, None, actions)
    return to_text(rows, width, color=False) + "\n"


def tab_csv(views, snap: dict, app, actions) -> Optional[str]:
    if app.tab == "jobs":
        rows = views.job_rows(snap, app, actions)
        return rows_to_csv(JOB_FIELDS, rows)
    if app.tab == "history":
        rows = [views.finished_dict(f) for f in snap["finished"]]
        return rows_to_csv(FIN_FIELDS, rows)
    if app.tab == "sources":
        hs = sorted(snap["health"].values(), key=lambda h: h.name)
        return rows_to_csv(["name", "enabled", "calls", "errors", "latency_ms", "last_ok", "error"],
                           [dict(name=h.name, enabled=h.enabled, calls=h.calls, errors=h.errors, latency_ms=round(h.latency_ms), last_ok=h.last_ok, error=h.error) for h in hs])
    if app.tab == "group":
        return rows_to_csv(["id", "user", "name", "partition", "state", "elapsed", "limit", "nodes", "cpus", "gpus", "nodelist", "reason", "priority", "submit"],
                           [dict(to_plain(j), gpus=j.gpus) for j in snap.get("group", [])])
    if app.tab == "nodes":
        return rows_to_csv(["name", "state", "cpus", "alloc", "load", "mem_total", "mem_free", "gres", "gres_used"],
                           [to_plain(n) for n in snap["nodes"].values()])
    return None


def jobs_json(snap: dict, ids: Sequence[str], series_of=None) -> str:
    """The given jobs with their live statistics, GPU samples, details and (when ``series_of`` is given) their
    recorded time series; finished jobs from the history when the id is not in the queue."""
    out = []
    jobs = {j.id: j for j in snap["jobs"]}
    fin = {f.id: f for f in snap["finished"]}
    for i in ids:
        rec: Dict = {"id": i}
        if i in jobs:
            rec["job"] = to_plain(jobs[i])
            rec["live"] = to_plain(snap["live"].get(i))
            rec["gpu"] = to_plain(snap["gpu"].get(i))
            rec["details"] = snap["details"].get(i, {})
        elif i in fin:
            rec["finished"] = to_plain(fin[i])
        if series_of is not None:
            rec["series"] = series_of(i)
        out.append(rec)
    return json.dumps(out, indent=1, default=str)


def selection_text(rows: Sequence, a: int, b: int) -> str:
    """Rows a..b (screen rows, inclusive, either order) as text."""
    lo, hi = min(a, b), max(a, b)
    lines = [row_text(r).rstrip() for r in rows[lo:hi + 1]]
    return "\n".join(lines) + ("\n" if lines else "")
