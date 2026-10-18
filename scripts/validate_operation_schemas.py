#!/usr/bin/env python3
"""Optional developer audit of operation schemas and their published examples.

Requires jsonschema >= 4 and referencing in the auditing Python environment;
neither package is a Raven runtime dependency. Never contacts a scheduler.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys


def markers(schema):
    result = []
    if isinstance(schema, dict):
        marker = schema.get("properties", {}).get("schema", {}).get("const")
        if marker:
            result.append(marker)
        for group in ("oneOf", "anyOf", "allOf"):
            for child in schema.get(group, ()):
                result.extend(markers(child))
        for child in schema.get("$defs", {}).values():
            result.extend(markers(child))
    return result


def main():
    try:
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
    except ImportError:
        print("Use a developer Python environment with jsonschema >= 4 installed.", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parents[1]
    folder = root / "docs/schemas"
    schemas = {path.name: json.loads(path.read_text()) for path in sorted(folder.glob("*.json"))}
    resources = []
    for name, value in schemas.items():
        Draft202012Validator.check_schema(value)
        resource = Resource.from_contents(value)
        resources.append(((folder / name).as_uri(), resource))
        if value.get("$id"):
            resources.append((value["$id"], resource))
    registry = Registry().with_resources(resources)
    schema_names = {marker: name for name, schema in schemas.items() for marker in markers(schema)}
    passed, errors = [], []
    examples = sorted((root / "examples").rglob("*.json")) + sorted((root / "docs/examples").rglob("*.json"))
    # Versioned metric metadata participates through the same schema marker
    # discovery as operation examples; it is never a JSONL header record.
    # A scientific identity is deliberately reusable without a schema marker.
    explicit = {"examples/operations-science/identity.json": "science-identity.schema.json"}
    for path in examples:
        value = json.loads(path.read_text())
        relative = str(path.relative_to(root))
        marker = value.get("schema") if isinstance(value, dict) else None
        if not marker and relative not in explicit:
            continue
        name = explicit.get(relative) or schema_names.get(marker)
        if name is None:
            errors.append({"example": relative, "error": "No schema for " + str(marker)})
            continue
        validator = Draft202012Validator(schemas[name], registry=registry)
        failures = list(validator.iter_errors(value))
        if failures:
            errors.append({"example": relative, "schema": name,
                           "errors": [error.message for error in failures[:8]]})
        else:
            passed.append(relative)
    print(json.dumps({"schemas": len(schemas), "examples": len(passed), "passed": passed, "errors": errors}, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
