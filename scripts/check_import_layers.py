#!/usr/bin/env python3
"""Check every Python import depth against explicit layers and recorded debt.

The debt baseline permits existing cycles; it is not an acyclicity assertion.
New cycle edges and unresolved dynamic-import expressions fail. Literal dynamic
imports and relative compatibility aliases participate in the graph.
--require-acyclic is the stricter final architecture gate. Variable module names
remain recorded debt, rather than proof the complete runtime graph is acyclic.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "docs/reviews/import-layer-baseline.json"
PURE = frozenset(
    {
        "raven.ui.focus",
        "raven.ui.geometry",
        "raven.ui.catalog",
        "raven.research_text",
        "raven.research_context",
        "raven.planning_catalog",
        "raven.planning_records",
        "raven.recording_settings_port",
        "raven.cadence",
        "raven.refresh_preferences",
        "raven.array_ranges",
        "raven.operation_context",
        "raven.source_budget",
        "raven.ui.keys",
        "raven.view_catalog",
        "raven.input_keys",
        "raven.transport_errors",
        "raven.hostlist",
        "raven.operation_json",
    }
)


# Application readers can compose lower services and adapters, but a UI owner
# must never be reachable from them, even through function-local imports.
READ_SERVICES = frozenset(
    {
        "raven.research",
        "raven.science_analysis",
        "raven.planning_input",
        "raven.series_settings",
        "raven.projects",
        "raven.metrics",
        "raven.log_catalog",
    }
)
UI_OWNERS = frozenset(
    {
        "controller",
        "screen",
        "views",
        "workbench",
        "interaction",
        "scrollbars",
        "chart_interaction",
        "metric_live",
        "job_selection",
        "job_panels",
        "history_browser",
        "grouping_toolbar",
        "table_tools",
        "navigation_tools",
        "text_selection",
        "toolbar",
    }
)


def ui_owner(module):
    leaf = module.rsplit(".", 1)[-1]
    return module not in PURE and (
        leaf.endswith("_ui") or leaf in UI_OWNERS or module.startswith("raven.ui.")
    )


def upward_paths(graph):
    result = []
    for source in sorted(READ_SERVICES):
        queue = [(source,)]
        visited = {source}
        while queue:
            path = queue.pop(0)
            for target in sorted(graph.get(path[-1], ())):
                if target in visited:
                    continue
                visited.add(target)
                if ui_owner(target):
                    result.append(path + (target,))
                else:
                    queue.append(path + (target,))
    return result


def analyze(package):
    files = {}
    for path in package.rglob("*.py"):
        parts = path.relative_to(package).with_suffix("").parts
        name = ".".join(
            (package.name,) + (parts[:-1] if parts[-1] == "__init__" else parts)
        )
        files[name] = path
    graph = {name: set() for name in files}
    functions = Counter()
    dynamic = []
    for module, path in files.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        import_functions = {"import_module", "__import__"}
        for imported in ast.walk(tree):
            if isinstance(imported, ast.ImportFrom) and imported.module == "importlib":
                import_functions.update(
                    alias.asname or alias.name
                    for alias in imported.names
                    if alias.name == "import_module"
                )
        parents = {
            child: node
            for node in ast.walk(tree)
            for child in ast.iter_child_nodes(node)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Name)
                and node.func.id in import_functions
                or isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
            ):
                target = None
                if node.args and isinstance(node.args[0], ast.Constant):
                    target = node.args[0].value
                    if not isinstance(target, str):
                        target = None
                    elif target.startswith("."):
                        origin = node.args[1] if len(node.args) > 1 else None
                        if isinstance(origin, ast.Name) and origin.id == "__package__":
                            origin = (
                                module
                                if path.name == "__init__.py"
                                else module.rpartition(".")[0]
                            )
                        elif isinstance(origin, ast.Constant) and isinstance(
                            origin.value, str
                        ):
                            origin = origin.value
                        else:
                            origin = None
                        if origin:
                            level = len(target) - len(target.lstrip("."))
                            parts = origin.split(".")
                            target = (
                                ".".join(parts[: len(parts) - level + 1])
                                + target[level - 1 :]
                            )
                        else:
                            target = None
                if target:
                    while target and target not in files:
                        target = target.rpartition(".")[0]
                    if target and target != module:
                        graph[module].add(target)
                else:
                    dynamic.append(
                        {
                            "module": module,
                            "line": node.lineno,
                            "expression": ast.dump(node, include_attributes=False),
                        }
                    )
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            parent = node
            while parent in parents:
                parent = parents[parent]
                if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions[module] += 1
                    break
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                base = node.module or ""
                if node.level:
                    parts = (
                        module.split(".")
                        if path.name == "__init__.py"
                        else module.split(".")[:-1]
                    )
                    base = ".".join(parts[: len(parts) - node.level + 1]) + (
                        "." + base if base else ""
                    )
                names = [base] + [
                    base + "." + alias.name for alias in node.names if alias.name != "*"
                ]
            for name in names:
                while name and name not in files:
                    name = name.rpartition(".")[0]
                if name and name != module:
                    graph[module].add(name)
    index = 0
    indices, low, stack, active, components = {}, {}, [], set(), []

    def visit(node):
        nonlocal index
        indices[node] = low[node] = index
        index += 1
        stack.append(node)
        active.add(node)
        for target in sorted(graph[node]):
            if target not in indices:
                visit(target)
                low[node] = min(low[node], low[target])
            elif target in active:
                low[node] = min(low[node], indices[target])
        if low[node] == indices[node]:
            component = []
            while True:
                target = stack.pop()
                active.remove(target)
                component.append(target)
                if target == node:
                    break
            if len(component) > 1:
                components.append(sorted(component))

    for node in sorted(graph):
        if node not in indices:
            visit(node)
    components.sort(key=lambda value: (-len(value), value))
    cyclic = set()
    for component in components:
        members = set(component)
        cyclic.update(
            (source, target)
            for source in component
            for target in graph[source]
            if target in members
        )
    return {
        "schema": "raven.import-layers/v1",
        "modules": len(graph),
        "function_import_statements": sum(functions.values()),
        "modules_with_function_imports": len(functions),
        "cyclic_components": components,
        "cyclic_edges": sorted(cyclic),
        "dynamic_module_expressions": dynamic,
    }, graph


def violations(report, graph, baseline):
    allowed = {tuple(edge) for edge in baseline.get("cyclic_edges", [])}
    result = [
        "New cyclic import: " + source + " -> " + target
        for source, target in report["cyclic_edges"]
        if (source, target) not in allowed
    ]
    dynamic_allowed = Counter(
        (item["module"], item["expression"])
        for item in baseline.get("dynamic_module_expressions", [])
    )
    for item in report["dynamic_module_expressions"]:
        identity = item["module"], item["expression"]
        if dynamic_allowed[identity]:
            dynamic_allowed[identity] -= 1
        else:
            result.append(
                "New unresolved dynamic import: "
                + item["module"]
                + ":"
                + str(item["line"])
            )
    for source in sorted(PURE):
        for target in sorted(graph.get(source, ())):
            if target not in PURE and target != "raven.ui":
                result.append(
                    "Pure presentation/transport contract imports an owner: "
                    + source
                    + " -> "
                    + target
                )
    result.extend(
        "Application reader imports UI owner: " + " -> ".join(path)
        for path in upward_paths(graph)
    )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="Explicitly record existing import debt for review",
    )
    parser.add_argument(
        "--require-acyclic",
        action="store_true",
        help="Reject all remaining cyclic components",
    )
    parser.add_argument(
        "--json", type=Path, help="Write exact graph summary and remaining debt"
    )
    args = parser.parse_args(argv)
    report, graph = analyze(ROOT / "raven")
    if args.write_baseline:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(
            json.dumps(
                {
                    "schema": report["schema"],
                    "status": "Recorded architecture debt; cycle elimination remains incomplete",
                    "cyclic_edges": report["cyclic_edges"],
                    "dynamic_module_expressions": report["dynamic_module_expressions"],
                },
                indent=2,
            )
            + "\n"
        )
    baseline = json.loads(args.baseline.read_text()) if args.baseline.exists() else {}
    errors = violations(report, graph, baseline)
    if args.require_acyclic and report["cyclic_components"]:
        errors.append("Import graph still contains cyclic components")
    report["violations"] = errors
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"{report['modules']} modules; {report['function_import_statements']} function imports; "
        f"remaining SCC sizes {[len(group) for group in report['cyclic_components']]}; "
        f"{len(report['dynamic_module_expressions'])} dynamic module expressions"
    )
    for error in errors:
        print(error)
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
