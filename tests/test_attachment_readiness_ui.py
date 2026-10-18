"""Automatic attachment declarations and per-process views preserve evidence boundaries."""
from __future__ import annotations

import json
import os
import threading
from types import SimpleNamespace

import pytest

from raven import connect_data, launch_features, launch_setup_ui, layout
from raven import trace_analysis, trace_ui
from raven.trace_recorder import Recorder


def test_metrics_readiness_scans_only_declared_literal_python_targets(tmp_path):
    (tmp_path / "solve.py").write_text('import raven\nraise RuntimeError("Never execute readiness input")\n')
    (tmp_path / "comment.py").write_text('# import raven\ntext = "import raven"\n')
    (tmp_path / "not-launched.py").write_text('import raven\n')
    script = tmp_path / "job.sh"
    script.write_text('#!/bin/bash\npython3 -u solve.py\npython comment.py\npython "$dynamic.py"\n')
    evidence = launch_features.detect_metric_client(script, tmp_path)
    assert evidence == {"targets": ["solve.py"], "warnings": []}
    rows = launch_features.readiness([], {}, trace=True, metric_client=evidence)
    assert any("@raven client detected in solve.py" in row["reason"] for row in rows)
    trace = next(row for row in rows if row["feature"] == "trace")
    assert trace["status"] == "will-verify" and "no script hook is required" in trace["reason"]
    assert "explicit python-legacy-profile consent" in trace["reason"]


def test_metrics_readiness_refuses_symlinks_special_files_and_escape(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside.py")
    outside.write_text('import raven\n')
    (tmp_path / "linked.py").symlink_to(outside)
    os.mkfifo(tmp_path / "pipe.py")
    script = tmp_path / "job.sh"
    script.write_text(f'python linked.py\npython pipe.py\npython {outside}\n')
    assert launch_features.detect_metric_client(script, tmp_path)["targets"] == []
    outside.unlink()


def test_metrics_readiness_limits_targets_and_reports_shadowing(tmp_path):
    (tmp_path / "raven.py").write_text('__raven_vendored__ = True\n')
    for index in range(35):
        (tmp_path / f"work{index}.py").write_text('from raven import metric\n')
    script = tmp_path / "job.sh"
    script.write_text('\n'.join(f'python work{index}.py' for index in range(35)))
    evidence = launch_features.detect_metric_client(script, tmp_path)
    assert len(evidence["targets"]) == 32
    assert any("at most 32" in warning for warning in evidence["warnings"])
    assert any("takes import precedence" in warning for warning in evidence["warnings"])


def test_setup_automatic_trace_needs_no_hook_but_keeps_legacy_explicit():
    state = {"setup_form": {"trace": True}, "setup_fields": {}}
    assert launch_setup_ui._resolved(state)["trace_adapter"] == "python-monitoring"
    state["setup_form"]["trace_adapter"] = "python-legacy-profile"
    assert launch_setup_ui._resolved(state)["trace_adapter"] == "python-legacy-profile"


def test_snippets_are_three_line_api_examples_and_manual_bash_remains():
    assert len(connect_data.snippet("python").splitlines()) == 3
    assert '@raven.measure' in connect_data.snippet("python")
    assert 'with raven.phase' in connect_data.snippet("phase")
    assert 'reduce="sum"' in connect_data.snippet("reduce")
    assert 'every rank' in connect_data.snippet("reduce")
    assert 'RAVEN_REPORT_HELPER' in connect_data.snippet("bash")


def test_preview_labels_kind_and_coalescing_without_losing_values():
    result = {"series": {"residual": [{"t": 1, "value": .1}, {"t": 2, "value": .01}]},
              "kinds": {"residual": "residual"}, "coalesced": {"residual": 12}}
    lines = connect_data.preview_lines(result, layout.Glyphs(False))
    assert any("kind: residual" in line and "log" in line for line in lines)
    assert any("coalesced ×12" in line for line in lines)
    assert any("last 0.01" in line for line in lines)


def test_residual_preview_uses_log_coordinates_and_preserves_nonpositive_gaps(monkeypatch):
    plotted = []
    monkeypatch.setattr(layout, "spark", lambda glyphs, values, width: plotted.extend(values) or "preview")
    result = {"series": {"r": [{"t": index, "value": value} for index, value in enumerate((100, 10, 0, 1))]},
              "kinds": {"r": "residual"}}
    lines = connect_data.preview_lines(result, layout.Glyphs(False))
    assert plotted == [1, .5, None, 0]
    assert any("1 nonpositive" in line for line in lines)


def _ctx(root):
    return dict(remote=False, root=str(root), job_id="123", submit="submit", start="start",
                cluster="local", run_id="", key=("123", "attempt"))


def _record(root, role, rank=None):
    extra = {"role": role, "parent_pid": os.getpid()}
    if rank is not None:
        extra["rank"] = rank
    recorder = Recorder(root, job_id="123", attempt="submit/start", background_flush=False, identity_extra=extra)
    recorder.emit("science", {"name": "solve"})
    recorder.close()
    return recorder


def test_trace_process_rows_filter_helpers_without_dropping_their_recordings(tmp_path):
    helper = _record(tmp_path, "helper")
    main = _record(tmp_path, "main", rank=0)
    result = trace_ui._read(_ctx(tmp_path), "", 0, "", None, threading.Event())
    assert len(result["sources"]) == 2 and len(result["processes"]) == 2
    assert result["path"] == str(main.path)
    assert next(row for row in result["processes"] if row["path"] == str(helper.path))["events"] >= 2
    visible = trace_ui._read(dict(_ctx(tmp_path), show_helpers=True), "", 0, "", None, threading.Event())
    assert visible["path"] == str(helper.path)


def test_trace_run_index_retains_observed_gap_without_inventing_process_identity(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    (root / "trace").mkdir()
    (root / "trace/index.json").write_text(json.dumps({"schema": "raven.trace-index/v1", "sources": [
        {"schema": "raven.trace-gap/v1", "run_id": "run-1", "status": "unavailable",
         "process": {"role": "child"}, "reason": "Observed python -I command; PID unavailable"}]}))
    context = dict(_ctx(tmp_path), run_root=str(root), run_id="run-1")
    result = trace_ui._read(context, "", 0, "", None, threading.Event())
    assert result["snapshot"] is None and len(result["processes"]) == 1
    assert result["processes"][0].get("pid") is None
    assert "-I" in result["processes"][0]["reason"]


def test_trace_process_paging_and_helper_preference_are_read_only(tmp_path, monkeypatch):
    recorder = _record(tmp_path, "main", rank=0)
    context = _ctx(tmp_path)
    result = trace_ui._read(context, "", 0, "", None, threading.Event())
    app = SimpleNamespace(save=lambda: None)
    state = trace_ui.initialize(app)
    state.update(context=context, result=result["snapshot"], status="ready", verified=True,
                 sources=result["sources"], processes=result["processes"] + [
                     dict(role="rank", rank=i, pid=1000+i, events=i, status="declared", path="", reason="")
                     for i in range(70)])
    monkeypatch.setattr(trace_ui, "_context", lambda app: context)
    from raven import trace_protocol
    monkeypatch.setattr(trace_protocol, "read_json", lambda *a, **k: pytest.fail("paint performed filesystem I/O"))
    views, job = SimpleNamespace(g=layout.Glyphs(False)), SimpleNamespace(id="123")
    rows, hits = trace_ui.panel(views, app, job, 80)
    assert any("page 1 / 3" in layout.row_text(row) for row in rows)
    assert any("processes-next" in str(hit) for hit in hits)
    trace_ui.run_command(app, ["trace", "processes-next"])
    rows, _ = trace_ui.panel(views, app, job, 80)
    assert any("page 2 / 3" in layout.row_text(row) for row in rows)
    trace_ui.run_command(app, ["trace", "helpers"])
    restored = SimpleNamespace()
    trace_ui.restore(restored, trace_ui.save(app))
    assert trace_ui.initialize(restored)["show_helpers"] is True
    assert recorder.path.exists()


def test_causal_paths_keep_per_process_durations_and_only_explicit_cross_links():
    def span(stream, name, start, end, sequence):
        common = dict(stream=stream, thread="main", job_id="1", attempt="one", node=stream, pid=stream)
        return [dict(common, seq=sequence, mono_ns=start, kind="span_start", data={"span_id": name}),
                dict(common, seq=sequence+1, mono_ns=end, kind="span_end", data={"span_id": name})]
    events = span("a", "one", 0, 10, 1) + span("a", "two", 20, 40, 3) + span("b", "three", 0, 30, 1)
    events += [dict(stream="links", thread="main", job_id="1", attempt="one", node="a", pid="a",
                    seq=index, mono_ns=index, kind="link", data={"source": source, "target": target})
               for index, (source, target) in enumerate((("a:one", "a:two"), ("a:two", "b:three")), 1)]
    result = trace_analysis.analyze_causality(events)
    assert result["data"]["duration_ns"] == 60
    paths = {row["stream"]: row for row in result["data"]["processes"]}
    assert paths["a"]["duration_ns"] == 30 and paths["b"]["duration_ns"] == 30
    assert paths["a"]["path"] == ["a:one", "a:two"] and paths["b"]["path"] == ["b:three"]
