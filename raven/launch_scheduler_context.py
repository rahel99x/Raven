"""Exact retained scheduler connection identity for launch lifecycle actions."""

from __future__ import annotations

import re

from .connection_scope import configured_collection_scope, selection_digest
from .evidence import digest


def configured(policy, *, environ=None):
    """Record explicit preview context; actual receipt cluster must still agree."""
    if (
        not isinstance(policy, dict)
        or policy.get("connection", {}).get("transport") != "local"
    ):
        return None
    cluster = policy.get("site", {}).get("cluster_id", "")
    if not isinstance(cluster, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]{0,128}", cluster
    ):
        return None
    try:
        scope = configured_collection_scope(policy, environ=environ)
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "schema": "raven.scheduler-context/v1",
        "connection": digest(scope),
        "user": scope["user"],
        "cluster": cluster,
        "selection_sha256": selection_digest(environ),
    }


def capture(backend, policy):
    """Capture configured context, without probing or guessing a cluster.

    Existing-script submission executes on the configured local submission
    host. A filesystem origin or an SSH alias is not proof of that identity.
    """
    if (
        backend is None
        or getattr(backend, "ledger", None) is None
        or not isinstance(policy, dict)
    ):
        return None
    current = configured(policy)
    scope = getattr(backend, "scope", None)
    if current is None or not isinstance(scope, dict):
        return None
    connection, user = scope.get("connection"), scope.get("user")
    if (
        not isinstance(connection, str)
        or not re.fullmatch(r"[0-9a-f]{64}", connection)
        or not isinstance(user, str)
        or not user
        or len(user) > 256
        or not user.isprintable()
    ):
        return None
    return (
        current
        if current["connection"] == connection and current["user"] == user
        else None
    )


def validated(context):
    """Action metadata has a closed contract; arbitrary extensions cannot act."""
    fields = {"schema", "connection", "user", "cluster", "selection_sha256"}
    if not isinstance(context, dict) or set(context) != fields:
        return False
    if context["schema"] != "raven.scheduler-context/v1":
        return False
    if any(
        not isinstance(context[key], str)
        or not re.fullmatch(r"[0-9a-f]{64}", context[key])
        for key in ("connection", "selection_sha256")
    ):
        return False
    user, cluster = context["user"], context["cluster"]
    return (
        isinstance(user, str)
        and 0 < len(user) <= 256
        and user.isprintable()
        and isinstance(cluster, str)
        and re.fullmatch(r"[A-Za-z0-9_.-]{0,128}", cluster) is not None
    )


def matches(retained, backend, policy, cluster):
    current = capture(backend, policy)
    if (
        current is None
        or not validated(retained)
        or not isinstance(cluster, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", cluster)
    ):
        return False
    if any(
        retained[key] != current[key]
        for key in ("schema", "connection", "user", "selection_sha256")
    ):
        return False
    # A default-cluster selection is retained as unknown until a fresh governed
    # ClusterName read verifies it. Never fill it in from the current hostname.
    return all(
        not context["cluster"] or context["cluster"] == cluster
        for context in (retained, current)
    )
