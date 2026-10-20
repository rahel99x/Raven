"""Strict native Slurm process-membership evidence for one exact local step."""

from __future__ import annotations

import re

_HEADER = ("PID", "JOBID", "STEPID", "LOCALID", "GLOBALID")


def parse(text, job, step):
    """Return scheduler-observed PIDs; foreign or ambiguous rows fail closed."""
    if (
        not isinstance(text, str)
        or len(text.encode("utf-8")) > 65536
        or not re.fullmatch(r"[0-9]{1,20}", job)
        or not re.fullmatch(r"(?:[0-9]{1,20}|batch|extern)", step)
    ):
        raise ValueError(
            "Worker process placement is unverified: invalid bounded Slurm PID-membership response"
        )
    rows = text.splitlines()
    if not rows or tuple(rows[0].split()) != _HEADER:
        description = (
            "".join(ch for ch in rows[0] if ch.isprintable())[:256]
            if rows
            else "empty response"
        )
        raise ValueError(
            "Worker process placement is unverified: Slurm PID membership is unavailable on this host; "
            + description
        )
    if len(rows) > 4097:
        raise ValueError(
            "Worker process placement is unverified: Slurm PID membership exceeds the step process bound"
        )
    pids = set()
    for row in rows[1:]:
        fields = row.split()
        if (
            len(fields) != 5
            or not re.fullmatch(r"[1-9][0-9]{0,9}", fields[0])
            or fields[1] != job
            or fields[2] != step
            or any(
                not re.fullmatch(r"(?:[0-9]{1,10}|-)", value) for value in fields[3:]
            )
        ):
            raise ValueError(
                "Worker process placement is unverified: Slurm PID membership contains an ambiguous or foreign job/step row"
            )
        pid = int(fields[0])
        if pid > 2147483647 or pid in pids:
            raise ValueError(
                "Worker process placement is unverified: Slurm PID membership contains an invalid or repeated process identity"
            )
        pids.add(pid)
    return sorted(pids)
