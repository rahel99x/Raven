#!/usr/bin/env python3
"""Standalone demonstration adapter: explicit text hashing, no file/network access."""
import hashlib
import json
import sys

LIMIT = 1 << 20
line = sys.stdin.buffer.readline(LIMIT + 1)
if len(line) > LIMIT or not line.endswith(b"\n"):
    raise SystemExit(2)
request = json.loads(line)
response = {"schema": "tower.adapter-protocol/v1", "id": request.get("id")}
if request.get("schema") != response["schema"]:
    raise SystemExit(2)
method = request.get("method")
if method == "describe":
    result = {"name": "example text hash", "methods": ["describe", "validate", "run"], "permissions": []}
else:
    params = request.get("params", {})
    text = params.get("text")
    if not isinstance(text, str) or len(text.encode("utf-8")) > 256 << 10:
        response.update(type="error", error="text must be a UTF-8 string of at most 256 KiB")
        result = None
    elif method == "validate":
        result = {"valid": True, "bytes": len(text.encode("utf-8"))}
    elif method == "run":
        result = {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "bytes": len(text.encode("utf-8"))}
    else:
        response.update(type="error", error="Unsupported method")
        result = None
if result is not None:
    response.update(type="result", result=result)
print(json.dumps(response, allow_nan=False), flush=True)
