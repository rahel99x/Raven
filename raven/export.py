"""Private, durable exports independent of ephemeral scheduler state storage."""
from __future__ import annotations

import csv
import io
import json
import os
import time
import stat
import secrets
from pathlib import Path
from typing import Dict, Optional, Sequence

from .layout import row_text, to_text
from .model import to_plain

JOB_FIELDS = ["id", "name", "part", "st", "where", "cpus", "gpu", "time", "left", "cpu%", "eff", "mem%", "gpu%", "flags", "info"]
FIN_FIELDS = ["id", "name", "state", "part", "elapsed", "cpus", "gpus", "ce", "me", "rss", "start", "end", "exit", "nodes"]


def export_dir(state_dir: Optional[str], *, directory=None) -> str:
    """A durable user-owned export root, independent of node-local state."""
    configured = directory or os.environ.get('RAVEN_EXPORT_DIR')
    base = Path(configured).expanduser() if configured else Path(os.environ.get('XDG_DATA_HOME') or Path.home()/'.local'/'share')/'raven'/'exports'
    if not base.is_absolute():
        raise ValueError('Export directory must be absolute')
    # Resolve an existing user-chosen prefix once. Missing components are
    # created relative to a pinned descriptor; a raced symlink is never followed.
    missing = []
    prefix = base
    while not os.path.lexists(prefix):
        missing.append(prefix.name)
        prefix = prefix.parent
    prefix = prefix.resolve(strict=True)
    parent = os.open(prefix, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        for component in reversed(missing):
            try:
                os.mkdir(component, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=parent)
            os.close(parent)
            parent = child
            prefix = prefix / component
    finally:
        os.close(parent)
    base = prefix
    info = base.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError('Export directory must be user-owned and not writable by another user: ' + str(base))
    return str(base)


def write(state_dir: Optional[str], name: str, content: str) -> str:
    if not isinstance(name,str) or not name or name in ('.','..') or os.path.basename(name)!=name or '\0' in name:
        raise ValueError('Export requires a plain new filename')
    folder=export_dir(state_dir)
    parent=os.open(folder,os.O_RDONLY|os.O_DIRECTORY|getattr(os,'O_NOFOLLOW',0))
    temporary='.raven-export-'+secrets.token_hex(16)+'.tmp'
    created=False
    try:
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600,dir_fd=parent)
        created=True
        try:
            with os.fdopen(fd,'w',encoding='utf-8',newline='') as stream:
                stream.write(content);stream.flush();os.fsync(stream.fileno())
            # Link publishes the complete, flushed inode in one exclusive
            # operation. A pre-existing file or symlink is never replaced.
            os.link(temporary,name,src_dir_fd=parent,dst_dir_fd=parent,follow_symlinks=False)
            os.fsync(parent)
        finally:
            if created:
                os.unlink(temporary,dir_fd=parent)
    finally:
        os.close(parent)
    return os.path.join(folder,name)


def stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")+'-'+secrets.token_hex(4)


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
