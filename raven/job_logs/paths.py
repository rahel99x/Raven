"""Resolve job log declarations without importing terminal presentation."""

from __future__ import annotations

import os
import re

from ..model import Finished


def stdout_path(
    job, kv: dict, files=None, which: str = "StdOut", *, probe: bool = True
) -> str:
    """Resolve declared paths, optionally checking a legacy local filename guess.

    Interactive callers defer existence checks to their background file reader.
    """
    p = kv.get(which, "") if kv else ""
    if p and p.lower() not in ("(null)", "n/a", "unknown", "none"):
        # Accounting can retain an unexpanded sbatch filename pattern. Array
        # %j is the allocation's raw ID, while %A/%a identify parent and task.
        array = re.fullmatch(r"(\d+)_(\d+)", job.id)
        raw_id = kv.get("JobIDRaw") or (job.id if not array else "")
        user = (
            kv.get("User", "")
            or kv.get("UserId", "").split("(", 1)[0]
            or getattr(job, "user", "")
        )
        values = {
            "j": raw_id,
            "A": kv.get("ArrayJobId") or (array[1] if array else raw_id),
            "a": kv.get("ArrayTaskId") or (array[2] if array else "4294967294"),
            "x": kv.get("JobName") or job.name,
            "u": user,
            "%": "%",
        }
        unresolved = False

        def expand(match):
            nonlocal unresolved
            width, token = match.groups()
            value = values.get(token, "")
            if not value or (
                width
                and (token not in ("j", "A", "a") or len(width) > 16 or int(width) > 10)
            ):
                unresolved = True
                return ""
            return str(value).zfill(int(width or 0))

        if kv.get("LogPathSource") == "sacct":
            p = re.sub(r"%([0-9]*)([A-Za-z%])", expand, p)
        if unresolved:
            return ""
        if not os.path.isabs(p):
            wd = kv.get("WorkDir") or getattr(job, "workdir", "")
            if (
                wd
                and os.path.isabs(wd)
                and wd.lower() not in ("(null)", "n/a", "unknown", "none")
            ):
                p = os.path.join(wd, p)
            elif isinstance(job, Finished) or (files is not None and files.remote):
                return ""
        return p
    if isinstance(job, Finished):
        return ""
    if files is not None and files.remote:
        return ""
    guess = os.path.join(
        "logs", f"{job.name}-{job.id}.{'err' if which == 'StdErr' else 'out'}"
    )
    return guess if not probe or os.path.exists(guess) else ""
