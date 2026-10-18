#!/usr/bin/env python3
"""Static dependency inventory; conservative reachability is not runtime loading."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path


def inventory(root):
    package = root / "raven"
    modules, files = {}, {}
    for path in sorted(package.rglob("*.py")):
        relative = path.relative_to(root).with_suffix("")
        name = ".".join(relative.parts).removesuffix(".__init__")
        files[name] = path
        modules[name] = set()
    functions = annotated = long_lines = 0
    literals = {}
    validators = {}
    for name, path in files.items():
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        literals[name] = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        current = name if path.name == "__init__.py" else name.rsplit(".", 1)[0]
        local_validators = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions += 1
                annotated += node.returns is not None
                if node.name.startswith(("validate", "_validate")):
                    local_validators.append({"symbol": node.name, "line": node.lineno})
            targets = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    parts = current.split(".")
                    base = ".".join(parts[:len(parts) - node.level + 1])
                    target = base + ("." + node.module if node.module else "")
                else:
                    target = node.module or ""
                targets = [target] if node.module else [target + "." + alias.name for alias in node.names]
            modules[name].update(target for target in targets if target in modules)
        long_lines += sum(len(line) > 200 for line in text.splitlines())
        if local_validators:
            validators[name] = local_validators
    def reachable(start):
        seen, pending = set(), [start]
        while pending:
            item = pending.pop()
            if item in seen or item not in modules:
                continue
            seen.add(item)
            pending.extend(modules[item] - seen)
        return sorted(seen)
    schemas = []
    for path in sorted((root / "docs/schemas").glob("*.json")):
        schema = json.loads(path.read_text())
        constants, references, pending = set(), set(), [schema]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                constant = item.get("const")
                if isinstance(constant, str) and constant.startswith("raven."):
                    constants.add(constant)
                if isinstance(item.get("$ref"), str):
                    references.add(item["$ref"])
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
        matches = sorted(name for name, values in literals.items() if values & constants)
        schemas.append({"path": str(path.relative_to(root)), "schema_constants": sorted(constants),
                        "literal_reference_modules": matches,
                        "validator_candidates": {name: validators[name] for name in matches if name in validators},
                        "references": sorted(references),
                        "binding": "generated: raven.protocol_contracts.worker_control" if path.name == "worker-control.v1.schema.json"
                                   else "manual review required; literal references are not validation proof"})
    return {"schema": "raven.architecture-audit/v1", "module_count": len(modules),
            "source_sha256": {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files.values()},
            "python_bytes": sum(path.stat().st_size for path in files.values()),
            "functions": functions, "return_annotated_functions": annotated, "lines_over_200_columns": long_lines,
            "edges": {name: sorted(edges) for name, edges in modules.items()}, "hand_validator_inventory": validators,
            "schema_files": [str(path.relative_to(root)) for path in sorted((root / "docs/schemas").glob("*.json"))],
            "schema_runtime_inventory": schemas,
            "conservative_reachable": {name: reachable(name) for name in (
                "raven.raven_launch", "raven.controller", "raven.raven_worker", "raven.launch_runtime", "raven.transport.framing")},
            "limitations": ["Static imports include conditional/function-local imports; this is not measured startup loading.",
                            "Dynamic/plugin imports require separate runtime qualification.",
                            "Validator symbols are an inventory, not proof that a published schema captures domain semantics."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    print(json.dumps(inventory(args.source.resolve()), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
