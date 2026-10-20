"""Pure captured-operation cancellation and attempt identity contracts."""


def cancelled(ctx):
    return bool(ctx.cancel is not None and ctx.cancel.is_set())


def checkpoint(ctx):
    if cancelled(ctx):
        raise ValueError("Operation cancelled; prepare a new review before retrying.")


def _identity(record):
    keys = ("id", "submit", "start", "cluster", "account", "user")
    return {key: record.get(key, "") for key in keys}
