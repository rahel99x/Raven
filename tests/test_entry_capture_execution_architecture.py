"""Canonical entry/capture/admission layers and finite worker bindings."""

import ast
import importlib
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from raven import cli, launch, raven_launch, trace_recorder
from raven.entry import grammar, launch_options, onboarding_cli
from raven.execution import policy, registry
from raven.tracing import capture, document, provenance


ALIASES = (
    ("command_options", "entry.command_options"),
    ("config", "configuration.dashboard"),
    ("entry_options", "entry.grammar"),
    ("execution_admission", "execution.admission"),
    ("execution_policy", "execution.policy"),
    ("launch", "entry.workload"),
    ("launch_autotrace", "launching.autotrace"),
    ("launch_bootstrap", "launching.bootstrap"),
    ("launch_contract", "launching.contract"),
    ("launch_diagnostics", "launching.diagnostics"),
    ("launch_features", "launching.features"),
    ("launch_io", "launching.io"),
    ("launch_options", "entry.launch_options"),
    ("launch_results", "launching.results"),
    ("launch_runtime", "launching.runtime"),
    ("launch_runtime_cache", "launching.runtime_cache"),
    ("launch_scheduler_context", "launching.scheduler_context"),
    ("launch_settings", "launching.settings"),
    ("launch_setup_ui", "launching.setup_ui"),
    ("launch_trace", "launching.trace"),
    ("launch_wizard", "launching.wizard"),
    ("launch_wizard_ui", "launching.wizard_ui"),
    ("log_export_admission", "execution.log_export"),
    ("onboarding", "entry.onboarding_cli"),
    ("onboarding_service", "entry.onboarding_service"),
    ("planning_command_service", "execution.planning"),
    ("raven_config", "configuration.policy"),
    ("raven_launch", "entry.launcher"),
    ("raven_qualification", "entry.qualification"),
    ("setup_config", "configuration.allocation"),
    ("setup_profiles", "configuration.profiles"),
    ("trace_analysis", "tracing.analysis"),
    ("trace_browser", "tracing.browser"),
    ("trace_capabilities", "tracing.capabilities"),
    ("trace_capture", "tracing.capture"),
    ("trace_cli", "tracing.cli"),
    ("trace_cpu", "tracing.cpu"),
    ("trace_cpu_timeline", "tracing.cpu_timeline"),
    ("trace_document", "tracing.document"),
    ("trace_export", "tracing.export"),
    ("trace_mpi", "tracing.mpi"),
    ("trace_native", "tracing.native"),
    ("trace_protocol", "tracing.protocol"),
    ("trace_provenance", "tracing.provenance"),
    ("trace_snapshot", "tracing.snapshot"),
    ("trace_stream", "tracing.stream"),
    ("trace_stream_export", "tracing.stream_export"),
    ("trace_summary", "tracing.summary"),
    ("trace_timeline", "tracing.timeline"),
    ("trace_timeline_ui", "tracing.timeline_ui"),
    ("trace_ui", "tracing.ui"),
    ("worker_operations", "execution.worker"),
    ("worker_registry", "execution.registry"),
)


def imports(path):
    return {
        node.module
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.ImportFrom)
    }


def test_released_aliases_keep_canonical_module_and_callable_identity():
    for old, canonical in ALIASES:
        assert importlib.import_module("raven." + old) is importlib.import_module(
            "raven." + canonical
        )
    assert cli.parse is grammar.parse
    assert raven_launch._parser is grammar.bootstrap_parser
    assert launch.parser is launch_options.parser
    assert launch.parse is launch_options.parse
    assert trace_recorder.Recorder is capture.Recorder
    assert trace_recorder.PythonCapture is capture.PythonCapture
    from raven import trace_browser, trace_export

    assert trace_browser.render is document.render
    assert trace_browser._browser_model is document._browser_model
    assert trace_export._pairs is provenance._pairs


def test_capture_and_launch_leaves_do_not_import_dispatch_or_orchestration():
    package = Path(capture.__file__).parents[1]
    for path in (
        package / "tracing/capture.py",
        package / "tracing/provenance.py",
        package / "launching/io.py",
        package / "launching/contract.py",
        package / "launching/diagnostics.py",
        package / "launching/runtime_cache.py",
    ):
        assert not imports(path) & {
            "cli",
            "raven_launch",
            "launch_runtime",
            "launch_trace",
            "tracing.cli",
            "launching.runtime",
            "raven_execution",
        }, path
    for name in ("autotrace", "bootstrap"):
        assert "runtime" not in imports(package / "launching" / (name + ".py"))
    assert not hasattr(capture, "main")


def test_worker_callable_registry_covers_every_admitted_pure_operation():
    assert set(policy._PURE) <= registry.LOADERS.keys()
    for operation, declaration in policy._PURE.items():
        binding = registry.pure_binding(operation)
        assert callable(binding.function) and binding.fields == declaration[2]
        assert registry.pure_binding(operation) is binding
    with pytest.raises(ValueError, match="Unknown worker operation"):
        registry.pure_binding("os.system")


def test_worker_registry_import_is_lazy_and_one_binding_does_not_load_others():
    program = """
import sys
from raven.execution.registry import pure_binding
assert 'raven.science_stats' not in sys.modules
assert 'raven.science_diagnostics' not in sys.modules
binding = pure_binding('statistics')
assert binding.function.__module__ == 'raven.science_stats'
assert 'raven.science_diagnostics' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr


def test_onboarding_demo_uses_explicit_entry_composition_callback():
    seen = []
    assert (
        onboarding_cli.run_setup(["--demo"], demo=lambda argv: seen.append(argv) or 19)
        == 19
    )
    assert seen == [onboarding_cli.DEMO_ARGS]


def test_heavy_plan_does_not_construct_owned_runtime_resources(
    tmp_path, monkeypatch, capsys
):
    import json

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("RAVEN_CONFIG", raising=False)
    monkeypatch.setattr(
        raven_launch,
        "_allocation",
        lambda *_: pytest.fail("plan constructed an allocation manager/socketpair"),
    )
    assert raven_launch.heavy_main(["--plan"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["schema"] == "raven.launch-plan/v1"
    assert plan["intent"] == "heavy" and plan["mutations"] == 0
    assert plan["dashboard_imported"] is False and plan["allocation"]


def test_production_entry_capture_consumers_use_canonical_implementations():
    root = Path(capture.__file__).parents[1]
    legacy = {"raven." + old for old, _ in ALIASES}
    legacy.update({"raven.raven_execution", "raven.trace_recorder"})
    failures = []
    for path in root.rglob("*.py"):
        if path.parent == root and "raven." + path.stem in legacy:
            continue
        owner = "raven" + (
            "." + ".".join(path.relative_to(root).parts[:-1])
            if path.parent != root
            else ""
        )
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
