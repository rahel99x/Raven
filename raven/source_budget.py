"""Task-local artifact read allowance, independent of execution adapters."""

from contextvars import ContextVar

_SOURCE_BUDGET = ContextVar("raven_source_budget", default=None)
_TASK_ROUTER = ContextVar("raven_task_router", default=None)


class AdmissionError(ValueError):
    """A managed request has no admitted execution target."""


def source_limit(requested):
    state = _SOURCE_BUDGET.get()
    if state is None:
        return requested
    if state["files"] <= 0 or state["remaining"] <= 0:
        raise AdmissionError("Task artifact read budget exhausted")
    return min(requested, state["remaining"])


def source_read(amount):
    state = _SOURCE_BUDGET.get()
    if state is not None:
        if amount < 0 or amount > state["remaining"] or state["files"] <= 0:
            raise AdmissionError("Task artifact read budget exhausted")
        state["remaining"] -= amount
        state["files"] -= 1
