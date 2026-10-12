"""Explicit state boundary for a document embedded in another workspace.

An embedded renderer reads the owner's published services without copying the
whole application. Its assignments and controller methods operate on this
context. Mutable presentation fields are supplied by the pane, while drawing
registries and collectors deliberately remain owned by the application.
"""
from __future__ import annotations

from types import MethodType


class PaneContext:
    """A small, isolated presentation overlay over published application data.

Only pane overrides are retained. Methods are rebound to the overlay so
``clamp_cursor`` and explicit pane commands cannot silently mutate the owner's
selection. Application-only playback state must be reached through
``_chart_owner``; sharing a copied reference here would outlive replay seeks.
    """

    _OWNER_ONLY = frozenset(("metric_playback_state",))

    def __init__(self, owner, *, identity, **presentation):
        self._pane_owner = owner
        self._chart_owner = getattr(owner, "_chart_owner", owner)
        self._pointer_owner_app = getattr(owner, "_pointer_owner_app", self._chart_owner)
        self.pane_identity = identity
        self.__dict__.update(presentation)

    def __getattr__(self, name):
        if name in self._OWNER_ONLY:
            raise AttributeError(name)
        owner = self._pane_owner
        # Resolve properties against pane state, rather than reading a value
        # computed from the owning Jobs/History workspace's active selection.
        descriptor = getattr(type(owner), name, None)
        if isinstance(descriptor, property):
            return descriptor.__get__(self, type(self))
        value = getattr(owner, name)
        if isinstance(value, MethodType) and value.__self__ is owner:
            value = MethodType(value.__func__, self)
        return value

    def save(self):
        """Pane rendering and local controls do not persist a synthetic App."""


def job_identity(app, job):
    """Identity of one published execution, including requeued job IDs."""
    if job is None:
        return None
    jid = str(job.id)
    store = getattr(app, "store", None)
    generation = getattr(store, "job_attempt", None)
    return (jid, getattr(job, "submit", None), getattr(job, "start", None),
            generation(jid) if callable(generation) else None)
