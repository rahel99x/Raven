"""Recording storage has a real package and exact released-module aliases."""

import importlib
import json
from pathlib import Path
import subprocess
import sys

MODULES = (
    "archive",
    "blocks",
    "cache_limits",
    "cli",
    "identity",
    "maintenance",
    "presentation",
    "publication",
    "retention",
    "settings",
    "storage",
    "summary",
    "worker",
)


def test_recording_module_aliases_preserve_exact_owner_identity():
    for name in MODULES:
        canonical = importlib.import_module("raven.recordings." + name)
        legacy = importlib.import_module("raven.series_" + name)
        assert legacy is canonical
        assert Path(canonical.__file__).parent.name == "recordings"


def test_fresh_legacy_recording_import_uses_canonical_module():
    code = """
import json
from raven import series_blocks
from raven.recordings import blocks
print(json.dumps({'same': series_blocks is blocks, 'owner': blocks.__name__}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert json.loads(result.stdout) == {
        "same": True,
        "owner": "raven.recordings.blocks",
    }


def test_bundled_project_reader_contains_real_recording_package():
    import base64
    import lzma
    from raven.project_remote import _payload

    sources = json.loads(lzma.decompress(base64.b64decode("".join(_payload()))))
    assert "raven.recordings" in sources
    assert "raven.recordings.publication" in sources
    assert "raven.recordings.cache_limits" in sources
    assert "class PublishedSeries" in sources["raven.recordings.publication"]


def test_production_recording_consumers_import_canonical_family():
    import ast
    import importlib.util

    package = Path(__file__).resolve().parents[1] / "raven"
    legacy = {"raven.series_" + name for name in MODULES}
    failures = []
    for path in package.rglob("*.py"):
        if path.parent == package and "raven." + path.stem in legacy:
            continue
        parts = path.relative_to(package).parts[:-1]
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
                    failures.append((str(path.relative_to(package)), node.lineno))
            elif isinstance(node, ast.Import) and any(
                alias.name in legacy for alias in node.names
            ):
                failures.append((str(path.relative_to(package)), node.lineno))
    assert failures == []
