#!/usr/bin/env python3
"""Print a manifest pinned to this host's Python executable and example adapter.

Usage: python examples/platform/make_adapter_manifest.py > /tmp/adapter.json
Then inspect it in Raven's External adapter workbench before choosing Apply.
"""
import hashlib
import json
from pathlib import Path
import sys

executable = str(Path(sys.executable).resolve())
script = str(Path(__file__).with_name("json_adapter.py").resolve())
print(json.dumps({
    "schema": "raven.adapter/v1", "id": "example-text-hash", "version": "1",
    "protocol": "raven.adapter-protocol/v1", "argv": [executable, script],
    "environment": {}, "methods": ["describe", "validate", "run"], "permissions": [],
    "assets": [{"path": path, "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()} for path in (executable, script)],
    "limits": {"timeout": 5, "frame_bytes": 1 << 20, "total_bytes": 2 << 20, "stderr_bytes": 4096}
}, indent=2))
