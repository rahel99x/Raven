"""Pure bounded producer, thread, task and node-lifetime identities."""

from __future__ import annotations


def _label(value, default="unknown", limit=1024):
    from ..job_logs.text import display_text

    return display_text(value[:limit]) if isinstance(value, str) and value else default


def _component(value, default="unknown"):
    if value is None:
        return default
    if type(value) is int:
        return str(value)
    return _label(value, default, 256)


def _row_source(row, sources):
    from .workspace_model_clocks import text

    source_id = text(row.get("source_id"), "source ID")
    clock_id = text(row.get("clock_id"), "clock domain ID")
    source = sources.get(source_id, {})
    identity = source.get("identity", {})
    if not isinstance(identity, dict):
        raise ValueError("Trace source identity must be an object")
    return source_id, clock_id, source, identity


def _data(row):
    value = row.get("data", {})
    if not isinstance(value, dict):
        raise ValueError("Trace interval data must be an object")
    return value


def _logical_task(data):
    """Validate declared lifetime keys; an arbitrary extension is not an ID."""
    for field in ("logical_task_id", "task_id", "task_generation"):
        value = data.get(field)
        if value is not None and not (
            type(value) is int
            and 0 <= value < 1 << 64
            or isinstance(value, str)
            and 0 < len(value) <= 256
            and value.isprintable()
        ):
            raise ValueError(
                "Trace " + field + " must be a bounded text or integer identity"
            )
    if (
        data.get("logical_task_id") is not None
        and data.get("task_id") is not None
        and data["logical_task_id"] != data["task_id"]
    ):
        raise ValueError("Trace logical task identity aliases disagree")
    return data.get("logical_task_id", data.get("task_id")), data.get("task_generation")


def _producer_location(row, source, identity):
    """Separate a report producer's lifetime from the importing recorder."""
    from .workspace_model_clocks import key, known

    data = _data(row)
    node = _component(data.get("source_node", row.get("node", identity.get("node"))))
    clock = source.get("clock", {})
    clock = clock if isinstance(clock, dict) else {}
    producer = clock.get("producer", {})
    producer = producer if isinstance(producer, dict) else {}
    if "source_boot_id" in data:
        boot = _component(data["source_boot_id"])
    elif clock.get("kind") == "producer-session" and node == producer.get("node"):
        boot = _component(producer.get("boot_id"))
    elif node == identity.get("node") and (
        clock.get("kind") == "monotonic"
        or clock.get("kind") is None
        and "source_pid" not in data
        and "source_node" not in data
    ):
        boot = _component(identity.get("boot_id"))
    else:
        boot = "unknown"
    complete = known(node) and known(boot)
    lifetime = key(
        "node-lifetime",
        identity.get("cluster"),
        node,
        boot,
        None if complete else row.get("source_id"),
    )
    label = (
        "Node "
        + node
        + (
            " · boot " + boot[:12]
            if complete
            else " · lifetime unknown ("
            + str(row.get("source_id", "unknown"))[-8:]
            + ")"
        )
    )
    return dict(node=node, boot_id=boot, id=lifetime, label=label, complete=complete)
