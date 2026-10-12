"""Unknown node telemetry stays unknown; measured zero remains meaningful."""
import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from raven.model import Node
from raven.slurm import parse_node
from raven.config import Config
from raven.layout import Glyphs, row_text
from raven.views import Views


def test_node_defaults_preserve_unknown_telemetry():
    node = Node("new-node")
    assert node.load is None and node.mem_free is None
    assert json.loads(json.dumps(asdict(node)))["mem_free"] is None


@pytest.mark.parametrize("value", ["N/A", "Unknown", "(null)", "nan", "inf", "-inf", "-1"])
def test_unavailable_or_invalid_node_measurements_are_not_zero(value):
    node = parse_node(f"NodeName=missing CPUTot=64 RealMemory=256000 CPULoad={value} FreeMem={value}")
    assert node.load is None and node.mem_free is None


def test_absent_node_fields_are_unknown_and_real_zero_is_retained():
    missing = parse_node("NodeName=missing CPUTot=64 RealMemory=256000")
    assert missing.load is None and missing.mem_free is None
    zero = parse_node("NodeName=zero CPUTot=64 RealMemory=256000 CPULoad=0 FreeMem=0")
    assert zero.load == 0.0 and zero.mem_free == 0.0


def test_known_node_measurements_remain_exact_and_bad_capacity_is_unknown():
    node = parse_node("NodeName=known CPUTot=64 RealMemory=256000 CPULoad=3.25 FreeMem=128000")
    assert (node.load, node.mem_total, node.mem_free) == (3.25, 256000, 128000)
    node = parse_node("NodeName=unknown RealMemory=nan")
    assert node.mem_total == 0


def test_node_resource_matrix_keeps_unknown_distinct_from_real_zero():
    views = Views(Glyphs(False), Config())
    missing = parse_node("NodeName=missing CPUTot=64 RealMemory=256000")
    zero = parse_node("NodeName=zero CPUTot=64 RealMemory=256000 CPULoad=0 FreeMem=0")
    unknown_row = row_text(views.node_resource_rows([missing], 120)[2])
    zero_row = row_text(views.node_resource_rows([zero], 120)[2])
    assert unknown_row.count("?") >= 2
    assert "100%" not in unknown_row
    assert "0%" in zero_row and "100%" in zero_row
    assert "?" not in zero_row


@pytest.mark.parametrize("reverse", [False, True])
def test_nodes_table_sorts_and_formats_missing_telemetry(reverse):
    missing = parse_node("NodeName=missing CPUTot=64 RealMemory=256000")
    known = parse_node("NodeName=known CPUTot=64 RealMemory=256000 CPULoad=1 FreeMem=256000")
    app = SimpleNamespace(sort={"nodes": "load"}, reverse={"nodes": reverse})
    snap = {"nodes": {"missing": missing, "known": known}, "jobs": [], "gpu": {}, "hist_gpu": {}, "gpu_mean": {}}
    rows, _ = Views(Glyphs(True), Config()).my_nodes(snap, app, 150, None)
    missing_row = next(row_text(row) for row in rows if "missing" in row_text(row))
    assert missing_row.count("n/a") == 3
