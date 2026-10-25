"""Optional actual-client wire qualification, independent of a live cluster."""

import os
import runpy
import shutil
from pathlib import Path

import pytest


def test_actual_kubectl_preserves_reviewed_uid_preconditions_on_wire():
    binary = os.environ.get("RAVEN_KUBECTL") or shutil.which("kubectl")
    if not binary:
        pytest.skip("Actual kubectl is not installed; native format fixture tests remain separate")
    source = Path(__file__).resolve().parents[1] / "scripts/qualify_kubernetes_client.py"
    module = runpy.run_path(str(source))
    result = module["qualify"](binary)
    assert result["checks"] and all(result["checks"].values())
    assert result["qualification"].startswith("Actual kubectl against loopback")
    assert (
        len([request for request in result["http_requests"] if request["method"] == "DELETE"]) == 2
    )
