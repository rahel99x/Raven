"""Selected-checkout imports remain exact across retained source namespaces."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType

import pytest


SPEC = importlib.util.spec_from_file_location(
    "benchmark_source", Path(__file__).resolve().parents[1] / "scripts/benchmark_source.py")
B = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(B)


def checkout(root, name="retained_fixture", *, declaration=None):
    root.mkdir()
    (root / "pyproject.toml").write_text(
        declaration or '[project]\nname = "unrelated-distribution"\n[tool.setuptools]\npackages = ["' + name + '"]\n')
    package = root / name
    package.mkdir()
    (package / "__init__.py").write_text('SOURCE = "selected"\n')
    (package / "cli.py").write_text('from . import SOURCE\n')
    return root


def test_static_package_declaration_uses_namespace_instead_of_distribution(tmp_path):
    source = checkout(tmp_path / "source", declaration='''[project]
name = "another-product"
[tool.setuptools]
packages = [
    "retained_fixture", # retained checkout
]
include-package-data = true
''')
    assert B.namespace(source) == "retained_fixture"
    assert B.package_directory(source) == source / "retained_fixture"


def test_metadata_free_unit_fixture_uses_current_namespace(tmp_path):
    assert B.namespace(tmp_path) == "raven"
    assert B.package_directory(tmp_path) == tmp_path / "raven"


def test_exact_subpackage_discovery_remains_source_bound(tmp_path):
    source = checkout(tmp_path / "source", declaration='''[project]
name = "another-product"
[tool.setuptools.packages.find]
include = ["retained_fixture", "retained_fixture.*"]
[tool.setuptools.package-data]
"retained_fixture.transport" = ["py.typed"]
''')
    assert B.namespace(source) == "retained_fixture"
    assert B.package_directory(source) == source / "retained_fixture"


@pytest.mark.parametrize("declaration", [
    '[project]\nname = "other"\n',
    '[tool.setuptools]\npackages = ["first", "second"]\n',
    '[tool.setuptools]\npackages = ["outside.package"]\n',
    '[tool.setuptools]\npackages = ["../outside"]\n',
    '[tool.setuptools]\npackages = "retained_fixture"\n',
    '[tool.setuptools]\npackages = ["retained_fixture"]\npackages = ["different"]\n',
    '[tool.setuptools]\npackages = ["retained_fixture"]\n[tool.setuptools]\npackages = ["different"]\n',
    '[tool.setuptools.packages.find]\nwhere = ["."]\n',
    '[tool.setuptools.packages.find]\ninclude = ["raven", "*"]\n',
    '[tool.setuptools.packages.find]\ninclude = ["raven", "raven.other"]\n',
    '[tool.setuptools.packages.find]\ninclude = ["../other", "../other.*"]\n',
    '[tool.setuptools]\npackages = ["raven"]\n[tool.setuptools.packages.find]\ninclude = ["raven", "raven.*"]\n',
])
def test_ambiguous_or_unsupported_metadata_refuses_without_fallback(tmp_path, declaration):
    (tmp_path / "pyproject.toml").write_text(declaration)
    with pytest.raises(ValueError):
        B.namespace(tmp_path)


def test_package_declaration_is_never_executed(tmp_path):
    marker = tmp_path / "executed"
    (tmp_path / "pyproject.toml").write_text(
        '[tool.setuptools]\npackages = __import__("pathlib").Path(' + repr(str(marker)) + ').touch()\n')
    with pytest.raises(ValueError):
        B.namespace(tmp_path)
    assert not marker.exists()


def test_metadata_read_is_bounded(tmp_path):
    (tmp_path / "pyproject.toml").write_bytes(b"#" * (B.MAX_METADATA_BYTES + 1))
    with pytest.raises(ValueError, match="64 KiB"):
        B.namespace(tmp_path)


def test_package_symlink_to_another_checkout_is_refused_before_import(tmp_path):
    external = checkout(tmp_path / "external")
    source = tmp_path / "selected"
    source.mkdir()
    (source / "pyproject.toml").write_text('[tool.setuptools]\npackages = ["retained_fixture"]\n')
    (source / "retained_fixture").symlink_to(external / "retained_fixture", target_is_directory=True)
    with pytest.raises(ValueError, match="beneath its source root"):
        B.package_directory(source)
    with pytest.raises(ValueError, match="beneath its source root"):
        B.load_module(source)


def test_selected_namespace_imports_its_real_relative_module(tmp_path, monkeypatch):
    source = checkout(tmp_path / "source")
    monkeypatch.setattr(sys, "path", list(sys.path))
    try:
        module = B.load_module(source, "cli")
        assert module.SOURCE == "selected"
        assert Path(module.__file__).resolve() == source / "retained_fixture/cli.py"
        assert B.load_module(source, "cli") is module
    finally:
        for name in list(sys.modules):
            if name == "retained_fixture" or name.startswith("retained_fixture."):
                del sys.modules[name]


@pytest.mark.parametrize("loaded_name", ["retained_fixture", "retained_fixture.cli"])
def test_preloaded_namespace_or_child_from_another_checkout_is_refused(tmp_path, monkeypatch, loaded_name):
    source = checkout(tmp_path / "source")
    stale = ModuleType(loaded_name)
    stale.__file__ = str(tmp_path / "different" / "retained_fixture" / "__init__.py")
    monkeypatch.setitem(sys.modules, loaded_name, stale)
    with pytest.raises(RuntimeError, match="another checkout"):
        B.load_module(source, "cli")
    assert sys.modules[loaded_name] is stale


@pytest.mark.parametrize("suffix", [".cli", "cli.", "../cli", "cli..main"])
def test_invalid_relative_module_name_is_refused(tmp_path, suffix):
    source = checkout(tmp_path / "source")
    with pytest.raises(ValueError, match="relative package module"):
        B.load_module(source, suffix)
