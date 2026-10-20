"""The canonical cadence domain cannot depend on its UI compatibility adapters."""

import ast
from pathlib import Path

from raven import cadence, metric_sampling, refresh_rate


def imports(name):
    path = Path(cadence.__file__).with_name(name + ".py")
    return [
        node.module
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.ImportFrom) and node.level
    ]


def test_pure_cadence_leaf_has_no_application_imports():
    assert imports("cadence") == []
    for module in ("refresh_preferences", "metric_sampling_runtime"):
        assert not set(imports(module)) & {
            "refresh_rate",
            "metric_sampling",
            "metric_live",
            "telemetry_settings",
            "setup_ui",
        }


def test_compatibility_names_keep_the_canonical_function_identity():
    for name in (
        "poll_interval",
        "poll_position",
        "source_interval",
        "file_interval",
        "validate_multiplier",
        "multiplier",
        "recordings_interval",
    ):
        assert getattr(refresh_rate, name) is getattr(cadence, name)
    for name in (
        "interval",
        "format_interval",
        "validate_rate",
        "source",
        "attempt",
        "matches",
        "research_matches",
    ):
        assert getattr(metric_sampling, name) is getattr(cadence, name)


def test_presentation_and_collectors_use_one_way_domain_edges():
    assert not set(imports("metric_live")) & {"refresh_rate", "metric_sampling"}
    assert "refresh_rate" not in imports("telemetry_settings")
    for module in ("sampler", "research", "log_catalog", "logs", "telemetry"):
        assert not set(imports(module)) & {"refresh_rate", "metric_sampling"}
