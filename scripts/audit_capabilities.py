#!/usr/bin/env python3
"""Record current runtime declarations without upgrading them to qualification."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from raven.raven_execution import capability_ledger


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--refresh-ledger", action="store_true")
    args = parser.parse_args(argv)
    ledger = capability_ledger()
    sources = sorted(str(path.relative_to(ROOT)) for path in (ROOT / "raven").rglob("*.py"))
    schemas = sorted((ROOT / "docs/schemas").glob("*.schema.json"))
    result = {"schema": "raven.capability-audit/v1",
              "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "working_tree_modified": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)),
              "source_sha256": {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in sources},
              "counts": {key: len(ledger[key]) for key in ("operations", "proposals", "worker_operations", "deferred_paths")},
              "published_schemas": len(schemas),
              "schema_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in schemas},
              "deferred_paths": ledger["deferred_paths"],
              "operation_routes": [{key: row[key] for key in ("operation", "availability", "apply", "implemented_worker_adapter")}
                                   for row in ledger["operations"]],
              "qualification": "declarations only; no runtime, scheduler, device or usability result inferred",
              "test_count": "not collected by this source inventory; use final revision regression evidence"}
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    if args.refresh_ledger:
        (ROOT / "docs/raven/capability-ledger.json").write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result["counts"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
