"""Architecture gates include imports hidden in callbacks and annotations."""

import importlib.util
from pathlib import Path
import pytest


spec = importlib.util.spec_from_file_location(
    "raven_import_layers", Path(__file__).parents[1] / "scripts/check_import_layers.py"
)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def test_function_and_type_checking_imports_both_participate_in_cycles(tmp_path):
    package = tmp_path / "raven"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "one.py").write_text("def operation():\n    from .two import value\n")
    (package / "two.py").write_text(
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from .one import operation\nvalue=1\n"
    )
    result, graph = checker.analyze(package)
    assert result["cyclic_components"] == [["raven.one", "raven.two"]]
    assert result["function_import_statements"] == 1
    assert len(checker.violations(result, graph, {})) == 2
    assert not checker.violations(result, graph, result)


def test_package_relative_imports_and_unknown_dynamic_names_are_visible(tmp_path):
    package = tmp_path / "raven"
    (package / "ui").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "ui/__init__.py").write_text("")
    (package / "ui/focus.py").write_text(
        "def capture():\n    from .. import controller\n"
    )
    (package / "controller.py").write_text(
        "from .ui.focus import capture\nimport importlib\nimportlib.import_module(name)\n"
    )
    report, graph = checker.analyze(package)
    assert graph["raven.ui.focus"] == {"raven.controller", "raven"}
    assert len(report["dynamic_module_expressions"]) == 1
    dynamic = report["dynamic_module_expressions"][0]
    assert dynamic["module"] == "raven.controller"
    assert dynamic["line"] == 3
    assert dynamic["expression"].startswith("Call(")
    assert any("Pure" in error for error in checker.violations(report, graph, report))


def test_literal_dynamic_imports_and_aliased_relative_shims_are_real_edges(tmp_path):
    package = tmp_path / "raven"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "one.py").write_text(
        "from importlib import import_module as load\n"
        "implementation=load('.two', __package__)\n"
    )
    (package / "two.py").write_text(
        "import importlib\n"
        "def read():\n    return importlib.import_module('raven.one')\n"
    )
    result, graph = checker.analyze(package)
    assert graph["raven.one"] == {"raven.two"}
    assert graph["raven.two"] == {"raven.one"}
    assert result["cyclic_components"] == [["raven.one", "raven.two"]]
    assert result["dynamic_module_expressions"] == []
    assert len(checker.violations(result, graph, {})) == 2


def test_new_unresolved_imports_fail_but_line_number_changes_do_not(tmp_path):
    package = tmp_path / "raven"
    package.mkdir()
    (package / "__init__.py").write_text("")
    source = package / "one.py"
    source.write_text("import importlib\nimportlib.import_module(module)\n")
    baseline, _ = checker.analyze(package)
    source.write_text("\n\nimport importlib\nimportlib.import_module(module)\n")
    result, graph = checker.analyze(package)
    assert checker.violations(result, graph, baseline) == []
    source.write_text(source.read_text() + "importlib.import_module(other)\n")
    result, graph = checker.analyze(package)
    assert checker.violations(result, graph, baseline) == [
        "New unresolved dynamic import: raven.one:5"
    ]


def test_builtin_import_aliases_and_attribute_calls_are_visible(tmp_path):
    package = tmp_path / "raven"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "one.py").write_text(
        "from builtins import __import__ as load\n"
        "load('raven.two')\n"
        "import builtins\n"
        "builtins.__import__(runtime_target)\n"
    )
    (package / "two.py").write_text("from .one import load\n")
    report, graph = checker.analyze(package)
    assert graph["raven.one"] == {"raven.two"}
    assert report["cyclic_components"] == [["raven.one", "raven.two"]]
    assert len(report["dynamic_module_expressions"]) == 1
    assert report["dynamic_module_expressions"][0]["line"] == 4


@pytest.mark.parametrize("unresolved", [False, True])
def test_strict_gate_cannot_be_waived_by_rewriting_a_debt_baseline(
    tmp_path, monkeypatch, unresolved
):
    package = tmp_path / "raven"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "one.py").write_text(
        "import importlib\nimportlib.import_module(target)\n"
        if unresolved
        else "def read():\n    from .two import value\n"
    )
    (package / "two.py").write_text(
        "from .one import read\n" if not unresolved else "value=1\n"
    )
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    assert checker.main(
        [
            "--baseline",
            str(tmp_path / "debt.json"),
            "--write-baseline",
            "--require-acyclic",
        ]
    )
