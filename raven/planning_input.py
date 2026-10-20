"""Bounded planning worker input construction without worker dispatch."""

from .execution.policy import AdmissionError, MAX_INPUT, _json
from .planning_records import record


def planning_inputs(
    view, source, *, snap=None, job=None, observations=(), overrides=None
):
    snapshot = snap or {}
    jobs, finished = snapshot.get("jobs", ()), snapshot.get("finished", ())
    if len(jobs) > 10000 or len(finished) > 10000:
        raise AdmissionError(
            "Planning exceeds the 10,000-record input window; choose an explicit bounded report"
        )
    kept = {key: snapshot.get(key, {}) for key in ("details", "nodemap", "health")}
    kept.update(
        {
            key: [record(row) for row in snapshot.get(key, [])]
            for key in ("jobs", "finished", "partitions", "share")
        }
    )
    value = {
        "view": view,
        "source": source,
        "snap": kept,
        "job": record(job) if job is not None else None,
        "observations": list(observations),
        "overrides": overrides or {},
    }
    _json(value, MAX_INPUT, "Planning input")
    return value
