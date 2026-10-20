"""Generated extraction projects retain the production build trust boundary."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from build import ProjectBuilder


def test_every_staged_distribution_reuses_the_pinned_source_backend(tmp_path):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "raven_distribution_prototype_test", root / "scripts/distribution_prototype.py"
    )
    prototype = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prototype)
    profile, source_hashes = prototype.stage(tmp_path)
    source = ProjectBuilder(root)
    assert source.build_system_requires
    assert all("==" in requirement for requirement in source.build_system_requires)
    for kind in ("full", "core", "lab"):
        staged = ProjectBuilder(tmp_path / kind)
        assert staged.build_system_requires == source.build_system_requires
        assert (
            staged._build_system["build-backend"]
            == source._build_system["build-backend"]
        )
    assert profile["profile"] == "core-experiment" and source_hashes
