"""Shared bounded scientific analysis cache below operation orchestration."""

from __future__ import annotations

from . import evidence
from .derived_results import RESULTS, ResultKey
from .operation_context import checkpoint, _identity
from .science_readers import READERS as _READERS

VERSION = "1"


def analyze(
    operation,
    document,
    ctx,
    *,
    identity=None,
    settings=None,
    cache=None,
    version=VERSION,
):
    """The standalone form and staged workflow use the identical pure analysis."""
    function = _READERS[operation][2]
    checkpoint(ctx)
    # Recommendation expiry is a wall-clock predicate. Never retain it under a
    # source-only revision, even when its underlying evidence has not changed.
    if operation == "recommendation":
        return function(document)
    revision = evidence.digest(document)
    selection = [
        _identity(job)
        for job in ctx.jobs + ctx.finished
        if ctx.selected and str(job.get("id")) == ctx.selected
    ]
    source_identity = {
        "source": identity or {"document_sha256": revision},
        "selected_scheduler_attempts": selection,
    }
    key = ResultKey.create(
        scope=ctx.scope,
        identity=source_identity,
        revision=revision,
        algorithm=operation,
        version=version,
        settings=settings,
    )
    return (RESULTS if cache is None else cache).get_or_compute(
        key,
        lambda: (
            function(document, cancel=ctx.cancel)
            if operation == "scientific-impact"
            else function(document)
        ),
        cancel=ctx.cancel,
    )
