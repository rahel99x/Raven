"""The Heavy package keeps import and subprocess compatibility boundaries."""

import importlib
import io
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "name",
    [
        "allocation",
        "allocation_adapters",
        "allocation_exec",
        "allocation_helper",
        "allocation_identity",
        "allocation_ownership",
        "allocation_recovery",
        "allocation_reuse",
        "worker",
        "worker_client",
        "protocol",
        "resources",
        "fork_task",
    ],
)
def test_legacy_heavy_imports_are_exact_canonical_module_aliases(name):
    assert importlib.import_module("raven.raven_" + name) is importlib.import_module(
        "raven.heavy." + name
    )


@pytest.mark.parametrize(
    "source",
    [
        ("heavy", "worker.py"),
        ("execution", "worker.py"),
        ("execution", "registry.py"),
        ("execution", "admission.py"),
        ("operation_adapters", "cluster.py"),
        ("operation_adapters", "science.py"),
        ("configuration", "policy.py"),
    ],
)
def test_worker_build_digest_covers_moved_implementation(monkeypatch, source):
    from raven.heavy.protocol import build_digest, worker_argv

    baseline = build_digest()
    original = Path.open

    def changed(path, *args, **kwargs):
        if path.parts[-2:] == source:
            return io.BytesIO(b"# changed Heavy worker implementation\n")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", changed)
    assert build_digest() != baseline
    argv = worker_argv()
    assert (Path(argv[-1]) / "raven" / "heavy" / "worker.py").is_file()
