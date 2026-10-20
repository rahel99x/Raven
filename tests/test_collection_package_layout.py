"""Collection owners have canonical modules without changing scoped capabilities."""

import ast
import importlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace


def test_collection_legacy_paths_preserve_exact_module_and_class_identity():
    for legacy, canonical in (
        ("raven_collection", "ledger"),
        ("raven_limits", "retention"),
        ("raven_diagnostics", "diagnostics"),
    ):
        actual = importlib.import_module("raven.collection." + canonical)
        assert importlib.import_module("raven." + legacy) is actual
        assert Path(actual.__file__).parent.name == "collection"
    from raven import raven_collection, raven_limits
    from raven.collection import ledger, retention

    assert raven_collection.Ledger is ledger.Ledger
    assert raven_collection.GovernedFiles is ledger.GovernedFiles
    assert raven_limits.LightLimits is retention.LightLimits


def test_confined_local_capability_keeps_exact_registered_wrapper_type():
    from raven.collection.ledger import GovernedFiles
    from raven.local_files import LocalFiles, local_supported
    from raven.raven_collection import GovernedFiles as LegacyFiles

    backend = SimpleNamespace(ledger=object())
    assert local_supported(LegacyFiles(LocalFiles(), backend))

    class ForeignWrapper:
        inner = LocalFiles()
        remote = False

    class Derived(GovernedFiles):
        pass

    assert not local_supported(ForeignWrapper())
    assert not local_supported(Derived(LocalFiles(), backend))


def test_production_collection_consumers_import_canonical_family():
    root = Path(__file__).resolve().parents[1] / "raven"
    legacy = {"raven.raven_collection", "raven.raven_limits", "raven.raven_diagnostics"}
    failures = []
    for path in root.rglob("*.py"):
        if path.parent == root and "raven." + path.stem in legacy:
            continue
        parts = path.relative_to(root).parts[:-1]
        owner = "raven" + ("." + ".".join(parts) if parts else "")
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                target = (
                    importlib.util.resolve_name(
                        "." * node.level + (node.module or ""), owner
                    )
                    if node.level
                    else node.module or ""
                )
                imported = {
                    target,
                    *(target + "." + alias.name for alias in node.names),
                }
                if imported & legacy:
                    failures.append((str(path.relative_to(root)), node.lineno))
            elif isinstance(node, ast.Import) and any(
                alias.name in legacy for alias in node.names
            ):
                failures.append((str(path.relative_to(root)), node.lineno))
    assert failures == []
