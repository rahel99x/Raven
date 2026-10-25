"""Bounded input JSON independent of service or scheduler composition."""

from .. import science_files as SF

MAX_REQUEST = 2 << 20


def request(path, *, cancel=None):
    _, raw = SF.snapshot(str(path), limit=MAX_REQUEST, keep=True, cancel=cancel)
    from ..evidence import loads

    document = loads(raw)
    if not isinstance(document, dict):
        raise ValueError("Request must be a JSON object")
    return document
