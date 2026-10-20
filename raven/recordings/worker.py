"""Named local recording-maintenance child; no scheduler or arbitrary operations."""

from __future__ import annotations
import json
import os
import resource
import signal
import sys

REQUEST_LIMIT = 16384
RESULT_LIMIT = 256 << 10


def execute(request):
    if (
        not isinstance(request, dict)
        or set(request)
        - {"action", "source", "apply", "quota", "settings", "staging_token"}
        or request.get("action") not in ("migrate", "retention", "query")
        or not isinstance(request.get("source"), str)
        or len(request["source"]) > 4096
        or not os.path.isabs(request["source"])
        or "\0" in request["source"]
        or type(request.get("apply", False)) is not bool
    ):
        raise ValueError("Invalid local storage maintenance request")
    quota = request.get("quota")
    if quota is not None and (type(quota) is not int or not 0 <= quota <= (1 << 40)):
        raise ValueError("Invalid local storage quota")
    if request["action"] == "migrate":
        from raven.recordings.storage import migrate

        return migrate(
            request["source"],
            apply=request.get("apply", False),
            quota=quota,
            staging_token=request.get("staging_token"),
        )
    from raven.recordings import retention as R

    if request["action"] == "retention":
        result = R.build(
            request["source"],
            settings=request.get("settings"),
            apply=request.get("apply", False),
            quota=quota,
            staging_token=request.get("staging_token"),
        )
        # The complete coverage remains in the archive; the UI needs counts.
        result["observed_gap_count"] = len(result.pop("observed_gaps"))
        return result
    result = R.query(request["source"], limit=100)
    # Keep the UI transport bounded without claiming omitted data is absent.
    for row in result["records"]:
        if row["fidelity"] == "raw":
            row["sample"] = {"t": row["sample"]["t"]}
        else:
            if len(row["metrics"]) > 16:
                row["metrics_limited"] = True
            row["metrics"] = dict(list(row["metrics"].items())[:16])
    while (
        len(json.dumps(result, allow_nan=False).encode()) > RESULT_LIMIT - 1024
        and result["records"]
    ):
        result["records"].pop()
        result["partial"] = True
    return result


def main():
    resource.setrlimit(resource.RLIMIT_AS, (256 << 20, 256 << 20))
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 << 20, 64 << 20))
    signal.alarm(20)
    try:
        from raven.recordings.blocks import _json

        data = sys.stdin.buffer.read(REQUEST_LIMIT + 1)
        if len(data) > REQUEST_LIMIT:
            raise ValueError("Storage request exceeds its byte limit")
        result = {"ok": True, "value": execute(_json(data))}
        encoded = json.dumps(result, allow_nan=False).encode()
        if len(encoded) > RESULT_LIMIT:
            raise ValueError("Storage result exceeds its byte limit")
    except Exception as exc:
        encoded = json.dumps(
            {"ok": False, "error": str(exc)[:2048] or type(exc).__name__}
        ).encode()
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
