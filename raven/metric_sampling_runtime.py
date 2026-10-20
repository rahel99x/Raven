"""Sampling request/cadence orchestration over caller-published state.

No UI owner is imported. Eligibility and state are explicit integration inputs;
collectors receive the same bounded replacement snapshot as before.
"""

from __future__ import annotations

from .cadence import format_interval as format_interval
from .cadence import interval, multiplier, source, validate_rate


def sync(app, *, state, eligible):
    """Publish a bounded replacement snapshot only when demands change."""
    owner = getattr(app, "_chart_owner", app)
    if state.get("sampling_sync"):
        return
    state["sampling_sync"] = True
    try:
        requests = {}
        for identity, entry in tuple(state["entries"].items()):
            rate = entry.get("rate", 1)
            if rate != 1 and eligible(owner, identity) and source(identity):
                requests[identity] = validate_rate(rate)
        targets = tuple(getattr(owner, name, None) for name in ("sampler", "research"))
        signature = (tuple(requests.items()), tuple(id(target) for target in targets))
        if signature == state.get("sampling_signature"):
            return
        for target in targets:
            setter = getattr(target, "set_metric_sampling", None)
            if callable(setter):
                setter(requests)
        state["sampling_signature"] = signature
    finally:
        state["sampling_sync"] = False


def cadence(app, identity, rate=None):
    """Return the effective collection interval or a bounded config fallback.

    Candidate rates are used for slider endpoint labels. The current rate also
    includes faster requests from metrics sharing this job's collector.
    """
    owner = getattr(app, "_chart_owner", app)
    if not isinstance(identity, tuple) or len(identity) < 5:
        return None
    collector = source(identity)
    if collector is None:
        return None
    cfg = getattr(owner, "cfg", {}) or {}
    global_rate = multiplier(owner)
    if rate is not None:
        try:
            rate = validate_rate(rate)
        except (ValueError, TypeError, OverflowError):
            return None
    sampler = getattr(owner, "sampler", None)
    if collector != "research":
        if rate is None:
            reader = getattr(sampler, "sampling_interval", None)
            if callable(reader):
                return reader(
                    collector,
                    str(identity[1]),
                    identity[4] if len(identity) > 4 else None,
                )
        defaults = {"live": 30.0, "gpu": 5.0, "trace": 5.0}
        base = getattr(sampler, "intervals", cfg.get("intervals", {})).get(
            collector, defaults[collector]
        )
        if rate is None:
            state = getattr(owner, "metric_live_state", None) or {"entries": {}}
            rate = max(
                (
                    entry.get("rate", 1)
                    for key, entry in state["entries"].items()
                    if source(key) == collector
                    and key[1] == identity[1]
                    and key[4] == identity[4]
                    and entry.get("running")
                ),
                default=1,
            )
        value = interval(
            base,
            global_rate,
            rate,
            source=collector,
            file=collector == "trace",
            remote=bool(getattr(getattr(sampler, "files", None), "remote", False)),
        )
        collection = getattr(sampler, "collection", None) or cfg.get("raven", {}).get(
            "collection"
        )
        if collection is not None:
            from raven.collection.ledger import interval as collection_interval

            value = collection_interval(collection, collector, value)
        return value
    hub = getattr(owner, "research", None)
    if rate is None:
        reader = getattr(hub, "sampling_interval", None)
        if callable(reader):
            return reader(identity)
    settings = cfg.get("research", {}) or {}
    base = getattr(hub, "interval", settings.get("interval", 5.0))
    if rate is None:
        state = getattr(owner, "metric_live_state", None) or {"entries": {}}
        rate = max(
            (
                entry.get("rate", 1)
                for key, entry in state["entries"].items()
                if source(key) == collector
                and key[1] == identity[1]
                and key[4:] == identity[4:]
                and entry.get("running")
            ),
            default=1,
        )
    return interval(
        base,
        global_rate,
        rate,
        source="research",
        file=True,
        remote=bool(getattr(getattr(hub, "files", None), "remote", False)),
    )
