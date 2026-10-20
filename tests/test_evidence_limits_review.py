"""Nested SQL processes preserve the Heavy task's tighter resource limits."""

import json
import resource
import subprocess
import sys

from raven import evidence_index as I
from test_evidence_index import capture as capture


def test_query_limits_never_raise_an_inherited_soft_or_hard_limit(monkeypatch):
    inherited = {
        resource.RLIMIT_AS: (128 << 20, 512 << 20),
        resource.RLIMIT_CPU: (2, 5),
        resource.RLIMIT_FSIZE: (8 << 20, 8 << 20),
    }
    applied = {}
    monkeypatch.setattr(resource, "getrlimit", inherited.__getitem__)
    monkeypatch.setattr(resource, "setrlimit", applied.__setitem__)
    I._apply_query_limits()
    assert applied == {
        resource.RLIMIT_AS: (128 << 20, 256 << 20),
        resource.RLIMIT_CPU: (2, 5),
        resource.RLIMIT_FSIZE: (8 << 20, 8 << 20),
    }


def test_actual_sql_child_runs_under_five_second_inherited_hard_cpu_cap(capture):
    project, bundle, index = capture
    I.ingest(index, bundle, project)
    script = """
import resource
resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
from raven.evidence_index import _child
_child()
"""
    request = {
        "index": str(index),
        "sql": "SELECT count(*) FROM measurements",
        "options": {},
    }
    completed = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        timeout=8,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["rows"] == [[6]]
