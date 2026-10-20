"""Pure exact execution owner/connection scope validation."""

from __future__ import annotations

_SCOPE_FIELDS = {
    "schema",
    "backend",
    "connection_host",
    "configured_host",
    "profile",
    "user",
    "uid",
    "cluster",
}


def validate_scope(scope):
    """A connection identity uses existing configuration, never scheduler probes."""
    if (
        not isinstance(scope, dict)
        or set(scope) != _SCOPE_FIELDS
        or scope.get("schema") != "raven.execution-scope/v1"
    ):
        raise ValueError(
            "execution connection scope is unknown; prepare a fresh review in the original connection"
        )
    for key in _SCOPE_FIELDS - {"uid"}:
        value = scope[key]
        if (
            not isinstance(value, str)
            or len(value) > 256
            or value
            and not value.isprintable()
            or key in {"backend", "connection_host", "user"}
            and not value
        ):
            raise ValueError(
                "execution connection scope is unknown; prepare a fresh review in the original connection"
            )
    if (
        isinstance(scope["uid"], bool)
        or not isinstance(scope["uid"], int)
        or not 0 <= scope["uid"] <= 4294967295
    ):
        raise ValueError(
            "execution owner scope is unknown; prepare a fresh review in the original connection"
        )
    return scope


def require_scope(review, expected_scope):
    """Reject a copied/legacy receipt before applying another cluster's job IDs."""
    validate_scope(expected_scope)
    original = validate_scope(review.get("scope"))
    if original != expected_scope:
        raise ValueError(
            "execution connection, profile, or owner scope differs; use the original connection or prepare a fresh review"
        )
    return original
