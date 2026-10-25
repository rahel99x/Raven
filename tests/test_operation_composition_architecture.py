"""Operation adapter packaging keeps public identities and lower contracts."""

import importlib
import json
from pathlib import Path
import subprocess
import sys

import pytest


ADAPTERS = (
    "campaign_policy",
    "campaigns",
    "cluster",
    "engineering",
    "evidence_queries",
    "health",
    "investigation",
    "platform",
    "process_profile",
    "queue_models",
    "queue_order",
    "runtime",
    "scale",
    "science",
    "services",
    "signal_analysis",
    "trace",
    "workflow",
    "workspace",
)


@pytest.mark.parametrize("name", ADAPTERS)
def test_released_adapter_imports_keep_the_canonical_module_identity(name):
    canonical = importlib.import_module("raven.operation_adapters." + name)
    released = importlib.import_module("raven.ops_" + name)
    assert released is canonical
    assert released.run is canonical.run
    assert released.SPECIFICATIONS is canonical.SPECIFICATIONS
    assert all(item["key"] for item in canonical.SPECIFICATIONS)


def test_lower_operation_contract_import_does_not_load_adapters_or_ui():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import raven.operation_support,sys,json;print(json.dumps(sorted(sys.modules)))",
        ],
        cwd=Path(__file__).parents[1],
        check=True,
        text=True,
        capture_output=True,
        timeout=10,
    )
    loaded = json.loads(result.stdout)
    assert "raven.operations" not in loaded
    assert not any(name.startswith("raven.operation_adapters.") for name in loaded)
    assert not {"raven.controller", "raven.views", "raven.workbench"} & set(loaded)


def test_registry_has_one_explicit_binding_for_each_shipped_adapter():
    from raven import operations

    modules = operations._modules()
    assert {module.__name__ for module in modules} == {
        "raven.operation_adapters." + name for name in ADAPTERS
    }
    keys = [spec["key"] for module in modules for spec in module.SPECIFICATIONS]
    assert len(keys) == len(set(keys))
    assert set(operations._registry()) == set(keys)
    with pytest.raises(ValueError, match="Unknown operation"):
        operations.specification("untrusted.module.name")
