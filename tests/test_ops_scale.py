"""Scale diagnostics: attribution, malformed inputs, boundedness, isolation."""
from dataclasses import replace
import json
import math
import sys
import threading
import time

import pytest

from raven.operations import Context, command
from raven.remote import LocalFiles
from raven.slurm import Slurm, FakeBackend, Backend
from raven import ops_scale as S, scale_bottlenecks as B, scale_clusters as C, scale_energy as E, scale_incidents as I
from raven.scale_common import timestamp, number


@pytest.fixture
def ctx():
    return Context(slurm=Slurm(FakeBackend(), "alex"), files=LocalFiles(), cfg={},
                   jobs=({"id": "7", "submit": "2026-10-01T01:00:00", "start": "2026-10-01T02:00:00+00:00", "hosts": ["n1"], "cluster": "alpha"},),
                   selected="7", scope={"cluster": "alpha"}, cancel=threading.Event())


def profiler():
    return {"schema": "raven.profiler/v1", "cluster": "alpha", "job_id": "7", "attempt": "a1", "expected_ranks": [0, 1],
            "ordered_barrier_phases": True,
            "records": [{"rank": 0, "node": "n1", "phase": "solve", "elapsed_s": 2, "cpu_s": 4, "mpi_s": 1, "io_s": .5, "read_bytes": 100, "write_bytes": 0},
                        {"rank": 1, "node": "n2", "phase": "solve", "elapsed_s": 4, "mpi_s": 2}]}


def energy(kind="total"):
    base = {"scope": "job", "attribution": "exclusive", "source": "job accounting", "kind": kind,
            "unit": "J", "value": 100, "coverage_complete": True}
    if kind != "total":
        base.update(samples=[{"t": 0, "value": 0}, {"t": 10, "value": 100}], window_start=0, window_end=10)
        if kind == "power":
            base["unit"] = "W"
    return base


def campaign():
    return {"schema": "raven.energy/v1", "work_unit": "accepted samples", "attempts": [
        {"cluster": "alpha", "job_id": "7", "attempt": "a1", "state": "FAILED", "accepted_work": 0, "energy": energy()},
        {"cluster": "alpha", "job_id": "8", "attempt": "a2", "state": "COMPLETED", "accepted_work": 2, "energy": energy()}]}


def events():
    return {"schema": "raven.incidents/v1", "events": [dict(cluster="alpha", nodes=["n1"], start=100, end=200, state="DOWN", reason="power failure")]}


@pytest.mark.parametrize("bad", [True, -1, float("nan"), float("inf"), 1e308, 1e-300, "1", {}])
def test_invalid_numbers(bad):
    with pytest.raises(ValueError):
        number(bad, "counter")


@pytest.mark.parametrize("bad", ["2026-01-01T00:00:00", "bad", True, -1, 1e99, "1960-01-01T00:00:00Z"])
def test_timestamp_requires_unambiguous_bounded_time(bad):
    with pytest.raises(ValueError):
        timestamp(bad)


def test_timestamp_offset_equivalence():
    assert timestamp("2026-01-01T00:00:00Z") == timestamp("2025-12-31T16:00:00-08:00")


def test_rank_summary_and_threaded_cpu():
    result = B.analyze(profiler(), job_id="7")
    assert result["barrier_envelope_s"] == 4
    assert result["phases"][0]["slowest_rank"] == 1
    assert result["phases"][0]["skew"] == pytest.approx(4 / 3)
    assert result["records"][0]["io_bytes_per_s"] == 200
    assert result["records"][1]["io_s"] is None


@pytest.mark.parametrize("change", ["duplicate", "wrong_job", "mpi_exceeds", "negative_rank", "control", "bad_schema", "unexpected_rank"])
def test_profiler_rejects_bad_evidence(change):
    value = profiler()
    if change == "duplicate": value["records"].append(value["records"][0].copy())
    if change == "wrong_job": value["job_id"] = "8"
    if change == "mpi_exceeds": value["records"][0]["mpi_s"] = 3
    if change == "negative_rank": value["records"][0]["rank"] = -1
    if change == "control": value["records"][0]["node"] = "n\x1b[1m"
    if change == "bad_schema": value["schema"] = "other"
    if change == "unexpected_rank": value["expected_ranks"] = [0]
    with pytest.raises(ValueError): B.analyze(value, job_id="7")


def test_unobserved_rank_never_complete():
    value = profiler()
    value["expected_ranks"].append(2)
    result = B.analyze(value)
    assert result["barrier_envelope_s"] is None
    assert result["phases"][0]["missing_ranks_sample"] == [2]
    del value["expected_ranks"]
    assert B.analyze(value)["barrier_envelope_s"] is None


def test_rank_phase_cross_product_is_not_materialized():
    value = profiler()
    value.pop("expected_ranks")
    value["records"] = [dict(rank=n, phase=f"p{n}", elapsed_s=1) for n in range(5000)]
    start = time.monotonic()
    result = B.analyze(value)
    assert all(len(phase["missing_ranks_sample"]) == 32 for phase in result["phases"])
    assert all(phase["missing_rank_count"] == 4999 for phase in result["phases"])
    assert time.monotonic() - start < 5


def test_darshan_import_counters_and_unknown_elapsed():
    lines = "\n".join(f"POSIX 0 123 {key} {val} file /fs fs" for key, val in (
        ("POSIX_BYTES_READ", 100), ("POSIX_BYTES_WRITTEN", 20), ("POSIX_F_READ_TIME", 2), ("POSIX_F_WRITE_TIME", 1), ("POSIX_F_META_TIME", .5)))
    value = B.import_darshan(lines, cluster="alpha", job_id="7", attempt="a1")
    result = B.analyze(value)
    assert result["records"][0]["io_s"] == 3.5
    assert result["records"][0]["elapsed_s"] is None
    assert result["barrier_envelope_s"] is None


def test_darshan_partial_file_counter_keeps_total_unknown():
    raw = "POSIX 0 1 POSIX_BYTES_READ 10\nPOSIX 0 1 POSIX_BYTES_WRITTEN 5\nPOSIX 0 2 POSIX_BYTES_READ 20"
    result = B.analyze(B.import_darshan(raw, cluster="alpha", job_id="7", attempt="a1"))
    assert result["records"][0]["read_bytes"] == 30
    assert result["records"][0]["write_bytes"] is None


@pytest.mark.parametrize("raw", ["POSIX -1 123 POSIX_BYTES_READ 5", "POSIX 0 123 POSIX_BYTES_READ nan", "POSIX 0", "POSIX 0 1 POSIX_BYTES_READ 5\nPOSIX 0 1 POSIX_BYTES_READ 5"])
def test_darshan_rejects_aggregate_or_bad_data(raw):
    with pytest.raises(ValueError): B.import_darshan(raw, cluster="a", job_id="7", attempt="a1")


def test_energy_counts_failed_attempts():
    result = E.analyze(campaign())
    assert result["joules_per_work"] == 100
    assert result["failed_attempt_joules"] == 100
    assert result["complete"]


@pytest.mark.parametrize("kind,expected", [("total", 100), ("counter", 100), ("power", 500)])
def test_energy_measurement_kinds(kind, expected):
    result = E.integrate(energy(kind))
    assert result["joules"] == expected
    assert result["complete"]


@pytest.mark.parametrize("unit,value", [("J", 1), ("kJ", 1000), ("Wh", 3600), ("kWh", 3600000)])
def test_energy_units(unit, value):
    series = energy(); series.update(value=1, unit=unit)
    assert E.integrate(series)["joules"] == value


@pytest.mark.parametrize("change", ["shared", "missing", "zero_work", "unknown_work", "mixed_scope", "reset", "gap", "coverage"])
def test_partial_energy_never_claims_efficiency(change):
    value = campaign()
    item = value["attempts"][0]
    if change == "shared": item["energy"]["attribution"] = "shared"
    if change == "missing": item["energy"] = None
    if change == "zero_work": value["attempts"][1]["accepted_work"] = 0
    if change == "unknown_work": item["accepted_work"] = None
    if change == "mixed_scope": item["energy"]["scope"] = "gpu"
    if change == "coverage": item["energy"].pop("coverage_complete")
    if change in ("reset", "gap"):
        item["energy"] = energy("counter")
        if change == "reset": item["energy"]["samples"] = [{"t": 0, "value": 100}, {"t": 10, "value": 0}]
        else: item["energy"]["max_gap_s"] = 5
    assert E.analyze(value)["joules_per_work"] is None


def test_counter_reset_continues_without_counting_unknown_interval():
    series = energy("counter")
    series.update(window_end=30, samples=[{"t": 0, "value": 100}, {"t": 10, "value": 200}, {"t": 20, "value": 0}, {"t": 30, "value": 30}])
    result = E.integrate(series)
    assert result["joules"] == 130
    assert result["coverage_s"] == 20
    assert result["resets"] == 1
    assert not result["complete"]


def test_partial_sample_window_not_full_attempt():
    series = energy("counter"); series["window_end"] = 20
    assert not E.integrate(series)["complete"]
    series.pop("window_end"); series.pop("window_start")
    assert not E.integrate(series)["complete"]


@pytest.mark.parametrize("mutation", [lambda e: e.update(unit="mJ"), lambda e: e.update(attribution="estimated"), lambda e: e.update(attribution="allocated_fraction", fraction=2), lambda e: e.update(value=-1)])
def test_bad_energy_contract(mutation):
    value = energy(); mutation(value)
    with pytest.raises(ValueError): E.integrate(value)


def test_attempt_collision_rejected_cluster_collision_preserved():
    value = campaign(); value["attempts"].append(value["attempts"][0].copy())
    with pytest.raises(ValueError): E.analyze(value)
    value["attempts"][-1]["cluster"] = "beta"
    assert len(E.analyze(value)["attempts"]) == 3


def test_incidents_require_both_node_and_time():
    event = I.normalize(events())
    hit = I.correlate(event, cluster="alpha", job_id="7", hosts=["n1"], start=150, end=300)
    assert hit["events"][0]["overlap_s"] == 50
    assert "not proof" in hit["warnings"][0]
    assert not I.correlate(event, cluster="beta", job_id="7", hosts=["n1"], start=150, end=300)["events"]
    assert not I.correlate(event, cluster="alpha", job_id="7", hosts=["n2"], start=150, end=300)["events"]
    assert not I.correlate(event, cluster="alpha", job_id="7", hosts=["n1"], start=200, end=300)["events"]


def test_incident_open_end_and_duplicates():
    value = events(); value["events"][0]["end"] = None; value["events"] *= 2
    parsed = I.normalize(value)
    assert len(parsed) == 1
    assert I.correlate(parsed, cluster="alpha", job_id="7", hosts=["n1"], start=150, end=300)["events"][0]["overlap_s"] == 150


@pytest.mark.parametrize("mutation", [lambda e: e.update(nodes=["n[1-3]"]), lambda e: e.update(end=100), lambda e: e.update(start="2026-01-01"), lambda e: e.update(reason="a\x1bb")])
def test_incident_malformed(mutation):
    value = events(); mutation(value["events"][0])
    with pytest.raises(ValueError): I.normalize(value)


def test_sacctmgr_event_parser_is_strict():
    raw = "alpha|n1|2026-10-01T01:00:00|Unknown|DOWN|power|\nother|n1|bad|bad|DOWN|x|"
    result = I.parse_sacctmgr(raw, cluster="alpha")
    assert len(result) == 1 and result[0]["end"] is None
    with pytest.raises(ValueError): I.parse_sacctmgr("not|enough", cluster="alpha")


def test_cluster_config_profiles_and_endpoint_identity():
    cfg = {"profiles": {"a": {"host": "login-a"}, "b": {"host": "login-b"}}}
    values = C.configuration(None, cfg)
    assert len(values) == 2 and values[0]["cluster_key"] != values[1]["cluster_key"]
    changed = C.configuration({"schema": "raven.cluster-workspace/v1", "clusters": [{"name": "a", "host": "changed"}]})
    assert changed[0]["cluster_key"] != values[0]["cluster_key"]


def test_cluster_profiles_inherit_remote_host():
    values = C.configuration(None, {"host": "login-a", "cluster_name": "alpha", "profiles": {"lab": {"account": "lab"}}})
    assert values[0]["host"] == "login-a"
    assert values[0]["cluster"] == "alpha"


@pytest.mark.parametrize("entry", [{"name": "x", "host": "-evil"}, {"name": "x", "host": "host;cmd"}, {"name": "x", "profile": "missing"}, {"name": "x", "cluster": "a,b"}])
def test_cluster_rejects_bad_addresses(entry):
    with pytest.raises(ValueError): C.configuration({"schema": "raven.cluster-workspace/v1", "clusters": [entry]})


def test_cluster_failures_isolated_and_ids_scoped(ctx, monkeypatch):
    sources = C.configuration({"schema": "raven.cluster-workspace/v1", "clusters": [{"name": n} for n in ("a", "b", "offline")]})
    barrier = threading.Barrier(3)
    def collect_one(context, source, user, hours):
        barrier.wait(timeout=2)
        if source["name"] == "offline": raise OSError("disconnected")
        job = dict(id="7", job_id="7", cluster_name=source["name"], submit="a", state="RUNNING", name="same", identity=[source["cluster_key"], "7", "a"], attempt="a")
        return dict(source=source, jobs=[job], errors=[], status="ok")
    monkeypatch.setattr(C, "collect_one", collect_one)
    result = C.collect(ctx, sources)
    assert len(result["jobs"]) == 2
    assert result["jobs"][0]["identity"] != result["jobs"][1]["identity"]
    assert len(result["warnings"]) == 1
    assert ctx.selected == "7" and ctx.slurm.user == "alex"


def test_cluster_single_mode_honors_one_worker(ctx, monkeypatch):
    sources = C.configuration({"schema": "raven.cluster-workspace/v1", "clusters": [{"name": n} for n in ("a", "b")]})
    monkeypatch.setattr(C, "collect_one", lambda context, source, user, hours: dict(source=source, jobs=[], errors=[], status="ok"))
    assert C.collect(replace(ctx, cfg={"worker_mode": "single"}), sources)["workers"] == 1


def test_cluster_collect_one_keeps_partial_queue_on_history_error(ctx, monkeypatch):
    from raven import operation_support as operations
    backend = FakeBackend()
    captured = []
    def run(context, argv, **kwargs):
        captured.append((context, argv))
        if argv[0] == "sacct": raise OSError("accounting unavailable")
        return backend.run(argv)[0]
    monkeypatch.setattr(operations, "command", run)
    source = C.configuration({"schema": "raven.cluster-workspace/v1", "clusters": [{"name": "remote", "host": "login.example"}]})[0]
    result = C.collect_one(ctx, source, "other", 24)
    assert result["status"] == "partial" and result["jobs"]
    assert result["errors"] == ["history: accounting unavailable"]
    assert all(item[0].slurm is not ctx.slurm for item in captured)
    assert all(item[0].slurm.b.host == "login.example" for item in captured)
    assert ctx.slurm.user == "alex"


def test_cluster_reused_sacct_ids_keep_distinct_attempts(ctx, monkeypatch):
    from raven import operation_support as operations
    line = "7|run|COMPLETED|00:01:00|1|00:00:30|1G|10M|2026-10-01T01:00:00|2026-10-01T01:01:00|main|1|0:0|cpu=1|n1|2026-10-01T00:00:00|/tmp|00:10:00|alex|lab|"
    lines = line + "\n" + line.replace("2026-10-01", "2026-10-02")
    monkeypatch.setattr(operations, "command", lambda context, argv, **kwargs: "" if argv[0] == "squeue" else lines)
    source = C.configuration({"schema": "raven.cluster-workspace/v1", "clusters": [{"name": "a"}]})[0]
    result = C.collect_one(ctx, source, "alex", 24)
    assert len(result["jobs"]) == 2
    assert result["jobs"][0]["identity"] != result["jobs"][1]["identity"]


def test_cluster_garbage_output_is_not_empty_healthy_queue(ctx, monkeypatch):
    from raven import operation_support as operations
    monkeypatch.setattr(operations, "command", lambda *args, **kwargs: "unexpected wire format")
    source = C.configuration({"schema": "raven.cluster-workspace/v1", "clusters": [{"name": "a"}]})[0]
    assert C.collect_one(ctx, source, "alex", 24)["status"] == "unavailable"


def test_cluster_owner_precedence(ctx, monkeypatch):
    sources = C.configuration({"schema": "raven.cluster-workspace/v1", "clusters": [{"name": "a", "user": "siteuser"}]})
    users = []
    def collect_one(context, source, user, hours):
        users.append(user)
        return dict(source=source, jobs=[], errors=[], status="ok")
    monkeypatch.setattr(C, "collect_one", collect_one)
    C.collect(ctx, sources)
    C.collect(ctx, sources, user="override")
    assert users == ["siteuser", "override"]


def test_cluster_report_budget_and_transport_options_not_exported(ctx, monkeypatch):
    sources = C.configuration({"schema": "raven.cluster-workspace/v1", "clusters": [{"name": "a"}]}, {"ssh_opts": ["-J", "bastion"]})
    def collect_one(context, source, user, hours):
        jobs = [dict(id=str(n), job_id=str(n), name="same", state="RUNNING", submit="a", identity=[source["cluster_key"], str(n), "a"]) for n in range(5001)]
        return dict(source=source, jobs=jobs, errors=[], status="ok")
    monkeypatch.setattr(C, "collect_one", collect_one)
    result = C.collect(ctx, sources)
    assert len(result["jobs"]) == 5000
    assert result["omitted_jobs"] == 1
    assert "ssh_opts" not in result["sources"][0]["source"]
    assert "jobs" not in result["sources"][0]
    assert result["sources"][0]["job_count"] == 5001


def test_real_bounded_command_process(ctx):
    local = replace(ctx, slurm=Slurm(Backend(), "alex"))
    assert command(local, [sys.executable, "-c", "print('ok')"]).strip() == "ok"
    with pytest.raises(Exception, match="limit"):
        command(local, [sys.executable, "-c", "print('x'*5000)"], limit=1024)
    with pytest.raises(Exception, match="timed out"):
        command(local, [sys.executable, "-c", "import time; time.sleep(2)"], timeout=.05)


def test_replay_and_cancel_no_live_reads(ctx, monkeypatch):
    source = C.configuration({"schema": "raven.cluster-workspace/v1", "clusters": [{"name": "a"}]})
    monkeypatch.setattr(C, "collect_one", lambda *args: pytest.fail("unexpected live read"))
    with pytest.raises(ValueError, match="recorded"): C.collect(replace(ctx, replay=True), source)
    ctx.cancel.set()
    with pytest.raises(ValueError, match="cancelled"): S.run("energy", {"path": "none"}, ctx)


def test_operation_file_end_to_end_and_no_script_execution(ctx, tmp_path):
    path = tmp_path / "energy.json"; path.write_text(json.dumps(campaign()))
    report = S.run("energy", {"path": str(path)}, ctx)
    assert report["data"]["joules_per_work"] == 100
    assert not ctx.slurm.b.calls
    path.write_text('{"schema":"raven.energy/v1","schema":"other"}')
    with pytest.raises(ValueError, match="duplicate"): S.run("energy", {"path": str(path)}, ctx)


def test_profiler_attempt_binds_current_job(ctx, tmp_path):
    value = profiler()
    path = tmp_path / "profiler.json"; path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="attempt"): S.run("bottlenecks", {"path": str(path)}, ctx)
    value["attempt"] = ctx.jobs[0]["submit"] + "/" + ctx.jobs[0]["start"]
    path.write_text(json.dumps(value))
    assert S.run("bottlenecks", {"path": str(path)}, ctx)["data"]["selected_attempt_verified"]
    restarted = replace(ctx, jobs=({**ctx.jobs[0], "start": "2026-10-01T03:00:00+00:00"},))
    with pytest.raises(ValueError, match="attempt"):
        S.run("bottlenecks", {"path": str(path)}, restarted)


@pytest.mark.parametrize("name,parser", [("profiler", B.analyze), ("clusters", C.configuration), ("incidents", I.normalize), ("energy", E.analyze)])
def test_documented_scale_examples_parse(name, parser):
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    value = json.loads((root / "examples" / f"scale-{name}.json").read_text())
    assert parser(value)


def test_file_incidents_end_to_end_without_scheduler_query(ctx, tmp_path):
    value = events()
    path = tmp_path / "incidents.json"; path.write_text(json.dumps(value))
    report = S.run("incidents", {"path": str(path), "start": "1970-01-01T00:02:30Z", "end": "1970-01-01T00:05:00Z"}, ctx)
    assert report["data"]["events"][0]["overlap_s"] == 50
    assert not ctx.slurm.b.calls


def test_operations_cannot_apply_changes(ctx):
    for key in ("energy", "clusters", "bottlenecks", "incidents"):
        with pytest.raises(ValueError, match="read-only"): S.apply(key, {}, ctx)
