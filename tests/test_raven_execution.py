import copy
import hashlib
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven import operations as O, raven_execution as R


LIGHT = {"managed": True, "mode": "light", "site": "desktop"}


@pytest.fixture(autouse=True)
def restore_process_runtime():
    previous = R.process_router()
    yield
    R.reset_process(previous)


def comparison():
    return {"schema": "raven.statistics/v1", "design": "independent", "unit": "independent runs",
            "control": [{"id": "a", "value": 1}, {"id": "b", "value": 2}, {"id": "c", "value": 3}],
            "treatment": [{"id": "d", "value": 4}, {"id": "e", "value": 5}, {"id": "f", "value": 6}]}


class ReadyWorker:
    ready = True
    identity = {"target": "desktop", "generation": 1}

    def __init__(self):
        self.tasks, self.calls, self.acked = {}, [], []

    def submit(self, operation, inputs, context, budget=None):
        self.calls.append((operation, inputs, context, budget))
        result = R.execute_operation(operation, inputs, context, budget)
        self.tasks["task1"] = {"status": "committed", "result": result}
        return "task1"

    def poll(self):
        return []

    def ack(self, task):
        self.acked.append(task)

    def cancel(self, task):
        self.tasks[task]["status"] = "cancelled"


def test_statistics_worker_returns_real_scientific_result_and_identity():
    inputs = {"document": comparison()}
    context = {"cluster": "test", "job_id": "12", "attempt": "2026-01-01"}
    prior = copy.deepcopy(inputs)
    result = R.execute_operation("statistics", inputs, context)
    assert result["result"]["difference"] == 3
    assert result["schema"] == R.RESULT_SCHEMA
    assert result["context_sha256"] == hashlib.sha256(R._json(context, 65536, "context")).hexdigest()
    assert inputs == prior


def test_numerical_worker_preserves_scientific_failure():
    result = R.execute_operation("numerical-stability", {"document": {
        "schema": "raven.numerical-stability/v1", "metrics": [{"name": "loss", "reference": 1,
        "absolute": .01, "relative": 0}], "runs": [{"id": "r1", "configuration": "small", "metrics": {"loss": 2}}]}})
    assert result["result"]["state"] == "failed"


def test_log_search_preserves_line_numbers_and_complete_input_scope():
    result = R.execute_operation("log-search", {"text": "one\nfailure\nthree\nfailure two\n", "needle": "failure"})
    assert [row["line"] for row in result["result"]["matches"]] == [2, 4]
    assert "no whole-file claim" in result["result"]["coverage"]


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(unknown="payload"),
    lambda value: value.update(document=float("nan")),
    lambda value: value.update(document="x"*(R.MAX_INPUT+1)),
])
def test_task_rejects_unknown_nonfinite_or_oversized_inputs(mutation):
    inputs = {"document": comparison()}
    mutation(inputs)
    with pytest.raises(ValueError):
        R.execute_operation("statistics", inputs)


@pytest.mark.parametrize("budget", [{"cpus": True}, {"wall_seconds": 0}, {"memory_mib": 257},
                                     {"cpu_seconds": float("nan")}, {"shell": "bash"}])
def test_task_rejects_excessive_or_invalid_resource_requests(budget):
    with pytest.raises(ValueError):
        R.execute_operation("statistics", {"document": comparison()}, budget=budget)


def test_cancellation_prevents_algorithm_and_result_publication(monkeypatch):
    from raven import science_stats
    monkeypatch.setattr(science_stats, "compare", lambda *_: pytest.fail("cancelled task executed"))
    with pytest.raises(R.AdmissionError, match="cancelled"):
        R.execute_operation("statistics", {"document": comparison()}, cancelled=lambda: True)


def test_output_budget_rejects_large_result():
    with pytest.raises(ValueError, match="result"):
        R.execute_operation("log-search", {"text": "yes "*200, "needle": "yes"}, budget={"output_bytes": 100})


@pytest.mark.parametrize("feature", sorted(R.HEAVY_OPERATIONS-R.LIGHT_OPERATIONS))
def test_every_managed_heavy_inspection_defers_before_file_or_algorithm(monkeypatch, feature):
    ctx = O.Context(cfg={"raven": LIGHT})
    monkeypatch.setattr(O, "read_json", lambda *_a, **_k: pytest.fail("Light read Heavy input"))
    result = R.operation_route(feature, {}, ctx)
    assert result["status"] == "deferred"
    assert result["data"]["executed"] is False


def test_managed_operations_entrypoint_does_not_fall_back(tmp_path, monkeypatch):
    path = tmp_path/"must-not-open.json"
    from raven import ops_science
    monkeypatch.setattr(ops_science, "run", lambda *_: pytest.fail("legacy fallback"))
    result = O.run("statistics", {"manifest": str(path)}, O.Context(cfg={"raven": LIGHT}))
    assert result["status"] == "deferred"


def test_process_policy_survives_empty_programmatic_context(monkeypatch):
    R.configure_process(LIGHT)
    from raven import ops_science
    monkeypatch.setattr(ops_science, "run", lambda *_: pytest.fail("missing config bypass"))
    assert O.run("statistics", {"manifest": "/missing"}, O.Context())["status"] == "deferred"
    R.reset_process()
    assert not R.router_for(O.Context()).managed


def test_ready_router_executes_named_worker_and_acknowledges_once(tmp_path):
    path = tmp_path/"samples.json"
    path.write_text(json.dumps(comparison()))
    worker = ReadyWorker()
    router = R.ExecutionRouter(LIGHT, lambda: worker)
    result = O.run("statistics", {"manifest": str(path)}, O.Context(cfg={"raven": LIGHT}, execution=router))
    assert result["data"]["difference"] == 3
    assert len(worker.calls) == 1
    assert worker.acked == ["task1"]


def test_worker_disconnection_never_invokes_local_handler():
    worker = ReadyWorker()
    def disconnected():
        worker.ready = False
    worker.poll = disconnected
    router = R.ExecutionRouter(LIGHT, lambda: worker)
    with pytest.raises(R.AdmissionError, match="placement changed"):
        router.dispatch("statistics", {"document": comparison()})


def test_wrong_context_receipt_is_rejected_before_ack():
    worker = ReadyWorker()
    worker.poll = lambda: worker.tasks["task1"]["result"].update(context_sha256="0"*64)
    with pytest.raises(R.AdmissionError, match="another execution"):
        R.ExecutionRouter(LIGHT, lambda: worker).dispatch("statistics", {"document": comparison()})
    assert worker.acked == []


def test_managed_heavy_mutation_cannot_reuse_legacy_apply(monkeypatch):
    monkeypatch.setattr(O, "_registry", lambda: pytest.fail("apply imported local handlers"))
    with pytest.raises(R.AdmissionError):
        O.apply("regression-sweep", {}, O.Context(cfg={"raven": LIGHT}))


def test_unknown_background_callable_refused_but_known_bounded_reader_allowed():
    router = R.ExecutionRouter(LIGHT)
    assert not R.background_allowed(router, lambda: None)
    from raven.activity_ui import initialize
    assert R.background_allowed(router, initialize)


def test_light_planning_command_defers_before_source_open(monkeypatch):
    from raven import planning_commands
    monkeypatch.setattr(planning_commands, "load_json", lambda *_a, **_k: pytest.fail("source opened"))
    result, _ = planning_commands.result("scaling", ["analyze", "/not-read"], execution=R.ExecutionRouter(LIGHT))
    assert result["status"] == "deferred"
    assert not planning_commands.success(result)


def test_managed_research_background_planning_is_deferred():
    from raven.research import ResearchHub
    hub = ResearchHub({"raven": LIGHT})
    try:
        value = hub._planning({"settings": {"planning_file": "/not-read"}, "snap": {}, "view": "scaling", "job": None})
        assert value["status"] == "deferred"
    finally:
        hub.close()


def test_ledger_covers_all_operations_and_all_original_proposals():
    from raven.capabilities import PROPOSALS
    ledger = R.capability_ledger()
    assert {row["operation"] for row in ledger["operations"]} == {spec["key"] for spec in O.catalog()}
    assert {row["id"] for row in ledger["proposals"]} == {proposal.id for proposal in PROPOSALS}
    assert all(row["version"] == 1 and row["budget"] and row["cancellation"] for row in ledger["operations"])


def test_terminal_generated_helpers_do_not_change_ordinary_user_input():
    app = SimpleNamespace(cfg={"raven": LIGHT})
    assert R.helper_admission(app, "anything", {"helper_kind": "user-paste"}) is None
    assert R.helper_admission(app, "generated analysis", {"helper_kind": "generated"})["status"] == "deferred"
    assert R.helper_admission(app, "bounded hook", {"helper_kind": "shell-hook", "session_kind": "local"}) is None
    assert R.helper_admission(app, "bounded hook", {"helper_kind": "shell-hook", "session_kind": "ssh"})["status"] == "deferred"


def test_legacy_help_and_small_reporting_remain_available():
    router = R.ExecutionRouter(LIGHT)
    assert R.cli_admission(["trace", "run", "--help"], router) is None
    assert R.cli_admission(["report", "metric"], router) is None
    assert R.cli_admission(["trace", "run", "script.py"], router)["status"] == "deferred"


def test_direct_managed_adapter_cannot_bypass_registry():
    from raven import ops_science
    result = ops_science.run("statistics", {"manifest": "/never-open"}, O.Context(cfg={"raven": LIGHT}))
    assert result["status"] == "deferred"


def test_unknown_managed_plugin_background_is_not_scheduled():
    from raven.research import ResearchHub
    hub = ResearchHub({"raven": LIGHT})
    try:
        assert not hub.start_task(lambda: pytest.fail("unknown callable executed"), lambda value: None)
        assert hub.pending is None
        assert hub.execution.last_deferred["status"] == "deferred"
    finally:
        hub.close()


def test_existing_artifact_adapter_runs_with_real_energy_evidence():
    source = Path(__file__).resolve().parents[1]/"examples/scale-energy.json"
    result = R.execute_operation("energy", {"params": {"path": str(source)}})
    assert result["result"]["feature"] == "energy"
    assert result["result"]["data"]


def test_worker_file_reads_charge_aggregate_source_budget(tmp_path):
    from raven import science_files
    a, b = tmp_path/"a", tmp_path/"b"
    a.write_bytes(b"a"*8)
    b.write_bytes(b"b"*8)
    token = R._SOURCE_BUDGET.set({"remaining": 12, "files": 4})
    try:
        science_files.snapshot(a)
        with pytest.raises(ValueError, match="at most 4"):
            science_files.snapshot(b)
        assert R._SOURCE_BUDGET.get()["remaining"] == 4
    finally:
        R._SOURCE_BUDGET.reset(token)


def test_light_large_search_defers_before_file_bytes(tmp_path, monkeypatch):
    from raven import log_scan
    from raven.remote import LocalFiles
    source = tmp_path/"large.log"
    with source.open("wb") as stream:
        stream.truncate((2 << 20)+1)
    R.configure_process(LIGHT)
    monkeypatch.setattr(log_scan, "_read", lambda *_a, **_k: pytest.fail("large local scan"))
    with pytest.raises(R.AdmissionError, match="not ready"):
        log_scan.search_source(LocalFiles(), {"path": str(source)}, "failure")


def test_light_small_search_remains_available(tmp_path):
    from raven import log_scan
    from raven.remote import LocalFiles
    source = tmp_path/"small.log"
    source.write_text("first\nfailure\nlast\n")
    R.configure_process(LIGHT)
    result = log_scan.search_source(LocalFiles(), {"path": str(source)}, "failure")
    assert result["complete"]
    assert result["matches"][0]["line"] == 2


def test_real_worker_router_executes_with_smaller_lifetime(tmp_path):
    from raven.raven_worker_client import WorkerClient
    config = {"site_kind": "desktop", "desktop_explicit": True, "context": {},
              "resources": {"memory_mib": 512, "cpus": 1, "max_tasks": 1, "wall_seconds": 10}}
    path = tmp_path/"measurements.json"
    path.write_text(json.dumps(comparison()))
    with WorkerClient(config) as worker:
        deadline = time.monotonic()+5
        while not worker.ready and time.monotonic() < deadline:
            worker.poll()
            time.sleep(.01)
        assert worker.ready, worker.error
        router = R.ExecutionRouter(LIGHT, lambda: worker)
        result = O.run("statistics", {"manifest": str(path)}, O.Context(cfg={"raven": LIGHT}, execution=router))
        assert result["data"]["difference"] == 3
        assert result["execution_receipt"]["budget"]["wall_seconds"] <= 10


def test_real_worker_simulation_review_and_artifact_publish(tmp_path):
    from raven.raven_worker_client import WorkerClient
    config = {"site_kind": "desktop", "desktop_explicit": True, "context": {},
              "resources": {"memory_mib": 512, "cpus": 1, "max_tasks": 1, "wall_seconds": 10}}
    source = Path(__file__).resolve().parents[1]/"examples/runtime/execution-model.json"
    destination = tmp_path/"prediction"
    with WorkerClient(config) as worker:
        deadline = time.monotonic()+5
        while not worker.ready and time.monotonic() < deadline:
            worker.poll()
            time.sleep(.01)
        assert worker.ready, worker.error
        router = R.ExecutionRouter(LIGHT, lambda: worker)
        ctx = O.Context(cfg={"raven": LIGHT}, execution=router, scope={"cluster": "fixture", "user": "owner"},
                        state_dir=str(tmp_path/"state"))
        prepared = O.run("execution-simulation", {"manifest": str(source), "destination": str(destination)}, ctx)
        assert not destination.exists()
        assert prepared["plan"]["raven_worker"]
        result = O.apply("execution-simulation", prepared["plan"], ctx)
        assert result["feature"] == "execution-simulation"
        assert (destination/"prediction.json").is_file()
        assert (destination/".raven-receipt.json").is_file()
        with pytest.raises(ValueError, match="already attempted"):
            O.apply("execution-simulation", prepared["plan"], ctx)


def test_background_binding_retains_exact_router_and_resets_after_task():
    process = R.configure_process(dict(LIGHT, site="other"))
    router = R.ExecutionRouter(LIGHT, lambda: ReadyWorker())
    observations = []
    thread = threading.Thread(target=R.bounded_background(router, lambda: observations.append(R.router_for())))
    thread.start()
    thread.join(timeout=2)
    assert observations == [router]
    assert R.router_for() is process
    assert R._TASK_ROUTER.get() is None


@pytest.mark.parametrize("argv", [
    ["trace", "run", "--", "program", "--help"],
    ["trace", "run", "program", "-h"],
    ["report", "exec", "--", "program", "--help"],
    ["report", "submit", "--", "--help"],
    ["report", "aggregate"], ["report", "passport"],
])
def test_child_help_does_not_bypass_managed_driver_gate(argv):
    assert R.cli_admission(argv, R.ExecutionRouter(LIGHT))["status"] == "deferred"


@pytest.mark.parametrize("feature,params", [
    ("clusters", {}), ("supervisor", {"action": "start"}),
    ("site-telemetry", {"action": "query"}),
])
def test_ungoverned_collectors_defer_before_source_or_driver(feature, params):
    result = R.operation_route(feature, params, O.Context(execution=R.ExecutionRouter(LIGHT)))
    assert result["status"] == "deferred"
    assert "polling entitlement" in result["summary"]


def test_programmatic_report_drivers_cannot_bypass_managed_policy(tmp_path):
    from raven import research_reporting
    R.configure_process(LIGHT)
    with pytest.raises(R.AdmissionError):
        research_reporting.submit(tmp_path, experiment="x", script="never-read")
    with pytest.raises(R.AdmissionError):
        research_reporting.aggregate(tmp_path)
    assert not list(tmp_path.iterdir())


def test_offline_research_accepts_explicit_binding_without_process_global(tmp_path):
    from raven.research_commands import offline_result
    result, code = offline_result("validate", [str(tmp_path/"absent"), str(tmp_path)], execution=R.ExecutionRouter(LIGHT))
    assert result["status"] == "deferred" and code == 2


def test_artifact_validation_worker_returns_real_validation_and_bounded_coverage(tmp_path):
    data = tmp_path/"data.txt"
    data.write_text("actual result\n")
    manifest = tmp_path/"contract.json"
    manifest.write_text(json.dumps({"version": 1, "outputs": [{"path": "data.txt", "format": "text"}]}))
    result = R.execute_operation("research-command", {"command": "validate", "args": [str(manifest), str(tmp_path)]})
    assert result["result"]["output"]["valid"]
    assert result["result"]["exit_code"] == 0
    assert result["result"]["output"]["budget"]["bytes_read"] == data.stat().st_size


def test_reduced_record_budget_rejects_before_computation():
    with pytest.raises(ValueError, match="record budget"):
        R.execute_operation("statistics", {"document": comparison()}, budget={"max_records": 4})


def log_report(path):
    from raven.log_copy import _snapshot
    from raven.remote import LocalFiles
    snapshot = _snapshot(LocalFiles(), str(path))
    return {"status": "ready", "jobs": [{"job_id": "17"}], "entries": [{"job_id": "17", "id": "stdout", "path": str(path),
        "snapshot": snapshot, "associations": [{"job_id": "17", "path": str(path), "snapshot": dict(snapshot)}]}]}


def test_light_small_selected_log_copy_is_available(tmp_path):
    from raven.log_bundle import export_logs
    source = tmp_path/"job.log"
    source.write_bytes(b"complete selected log\n")
    R.configure_process(LIGHT)
    result = export_logs(log_report(source), str(tmp_path), root=str(tmp_path))
    assert result["status"] == "ready"
    assert (Path(result["export_path"])/result["files"][0]["relative_path"]).read_bytes() == source.read_bytes()


def test_real_worker_log_bundle_preserves_identity_and_delivers_clipboard_at_interface(tmp_path, monkeypatch):
    from raven import log_bundle
    from raven.raven_worker_client import WorkerClient
    source = tmp_path/"large.log"
    source.write_bytes(b"whole selected log\n"*120000)
    copied = []
    def copy_file(path, **kwargs):
        copied.append((Path(path).read_bytes(), kwargs))
        return {"methods": ["fixture clipboard"], "warnings": []}
    monkeypatch.setattr(log_bundle.clipboard_io, "copy_file", copy_file)
    config = {"site_kind": "desktop", "desktop_explicit": True, "context": {},
              "resources": {"memory_mib": 512, "cpus": 1, "max_tasks": 1, "wall_seconds": 10, "input_bytes": 8 << 20}}
    with WorkerClient(config) as worker:
        deadline = time.monotonic()+5
        while not worker.ready and time.monotonic() < deadline:
            worker.poll()
            time.sleep(.01)
        assert worker.ready, worker.error
        router = R.ExecutionRouter(LIGHT, lambda: worker)
        result = R.bounded_background(router, lambda: log_bundle.export_logs(log_report(source), clipboard=True,
            state_dir=str(tmp_path/"state"), copy_destination="yank", editor_context={"target": "fixture"}))()
    assert result["status"] == "ready", result
    assert result["bytes"] == source.stat().st_size
    assert len(copied) == 1 and source.read_bytes() in copied[0][0]
    assert copied[0][1]["destination"] == "yank"
    assert "fixture clipboard" in result["message"] and "unavailable" not in result["message"]


def managed_site():
    from raven.raven_config import DEFAULTS
    return dict(copy.deepcopy(DEFAULTS), managed=True)


def test_job_local_bridge_uses_actual_membership_and_fresh_governed_records(monkeypatch):
    from raven import raven_resources
    router = R.ExecutionRouter(managed_site())
    calls = []
    def command(argv, *_args):
        calls.append(argv)
        if argv[2] == "job":
            return "JobId=42 JobState=RUNNING UserId=owner(1000) NodeList=compute[1-2] SubmitTime=2026-01-01T00:00:00 StartTime=2026-01-01T00:00:01"
        return "StepId=42.batch State=RUNNING Nodes=compute[1-2]"
    router.source_backend = SimpleNamespace(ledger=object(), command=command)
    probe = raven_resources.placement_probe
    monkeypatch.setattr(raven_resources, "placement_probe", lambda config, **kwargs: probe(config,
        environ={"SLURM_JOB_ID": "42"}, host="compute1", uid=1000, pid=44,
        cgroups="0::/slurm/job_42/step_batch/task_0\n", **kwargs))
    assert R.cli_admission(["report", "exec", "--", "payload"], router) is None
    assert len(calls) == 2 and all(argv[0] == "scontrol" for argv in calls)
    assert router.job_local_receipt["identity"]["heavy_admission"] is False
    assert R.cli_admission(["trace", "run", "--", "script.py", "--help"], router) is None
    assert len(calls) == 2  # bounded immediate double-entry reuse, same PID/host/owner
    router.job_local_receipt["expires"] = 0
    assert R.cli_admission(["report", "aggregate"], router) is None
    assert len(calls) == 4


def test_job_local_bridge_rejects_environment_only_even_with_matching_scheduler(monkeypatch):
    from raven import raven_resources
    router = R.ExecutionRouter(managed_site())
    def command(argv, *_args):
        return ("JobId=42 JobState=RUNNING UserId=owner(1000) NodeList=compute1" if argv[2] == "job"
                else "StepId=42.0 State=RUNNING Nodes=compute1")
    router.source_backend = SimpleNamespace(ledger=object(), command=command)
    probe = raven_resources.placement_probe
    monkeypatch.setattr(raven_resources, "placement_probe", lambda config, **kwargs: probe(config,
        environ={"SLURM_JOB_ID": "42", "SLURM_STEP_ID": "0"}, host="compute1", uid=1000, pid=44, cgroups="", **kwargs))
    result = R.cli_admission(["report", "exec", "--", "payload"], router)
    assert result["status"] == "deferred"
    assert "unverified" in result["summary"]
    assert result["data"]["prepared_invocation"] == ["report", "exec", "--", "payload"]


def test_report_submit_preserves_environment_and_uses_same_governed_owner(tmp_path):
    from contextlib import contextmanager
    from raven import research_reporting
    router = R.configure_process(managed_site())
    debits, executed = [], []
    class Ledger:
        @contextmanager
        def admit(self, descriptor, argv, **kwargs):
            debits.append((descriptor.family, list(argv), kwargs))
            yield
    router.source_backend = SimpleNamespace(ledger=Ledger())
    (tmp_path/"batch.sh").write_text("#!/bin/bash\ntrue\n")
    def run(argv, **kwargs):
        assert len(debits) == 1
        executed.append((argv, kwargs["env"]))
        kwargs["stdout"].write(b"42\n")
        return SimpleNamespace(returncode=0)
    assert research_reporting.submit(tmp_path, experiment="trial", script="batch.sh", parameters={"seed": 7}, runner=run) == "42"
    assert debits[0][0] == "submit" and debits[0][2]["validation"]
    assert len(executed) == 1 and json.loads(executed[0][1]["RAVEN_PARAMETERS"]) == {"seed": 7}


def test_trace_sdk_output_does_not_need_allocation():
    router = R.ExecutionRouter(LIGHT)
    assert R.cli_admission(["trace", "mpi", "--source"], router) is None
    assert R.cli_admission(["trace", "native", "--sdk-header"], router) is None
    assert not R.needs_job_local(["trace", "native", "--sdk-header"])


def test_managed_scientific_inspection_preserves_failed_acceptance_and_display_rows(tmp_path):
    manifest = Path(__file__).resolve().parents[1]/"examples/operations-science/acceptance.json"
    results = tmp_path/"results.json"
    results.write_text(json.dumps({"schema": "raven.results/v1", "runs": [{"id": "sum-three", "metrics": {"sum": 9}}]}))
    worker = ReadyWorker()
    router = R.ExecutionRouter(LIGHT, lambda: worker)
    report = O.run("acceptance", {"manifest": str(manifest), "results": str(results)}, O.Context(execution=router))
    assert report["status"] == "failed"
    assert report["data"]["passed"] == 0 and report["data"]["total"] == 1
    assert report["rows"][0].startswith("FAIL sum-three")
    assert report["execution_receipt"]["operation"] == "scientific-inspection"


def test_managed_trace_recorder_rejects_unverified_programmatic_entry(tmp_path, monkeypatch, capsys):
    from raven import trace_recorder
    R.configure_process(LIGHT)
    marker = tmp_path/"executed"
    script = tmp_path/"payload.py"
    script.write_text("from pathlib import Path\nPath("+repr(str(marker))+").touch()\n")
    assert trace_recorder.main(["run", "--", str(script), "--help"]) == 3
    assert not marker.exists()
    assert json.loads(capsys.readouterr().out)["status"] == "deferred"


def test_public_managed_job_local_rejection_closes_owned_source_and_never_launches(tmp_path, monkeypatch, capsys):
    from raven import cli, raven_launch, raven_resources
    from raven.raven_config import DEFAULTS
    marker = tmp_path/"never-executed"
    closed = []
    backend = SimpleNamespace(ledger=object(), close=lambda: closed.append(True))
    manager = SimpleNamespace(collection_backend=None)
    monkeypatch.setattr(raven_launch, "configure_placement", lambda _policy, owner: setattr(owner, "collection_backend", backend))
    monkeypatch.setattr(raven_resources, "_proc_cgroups", lambda: "")
    monkeypatch.setenv("SLURM_JOB_ID", "42")
    monkeypatch.delenv("SLURM_STEP_ID", raising=False)
    previous = R.process_router()
    status = cli.main(["report", "exec", "--root", str(tmp_path), "--", "touch", str(marker)],
                      raven=copy.deepcopy(DEFAULTS), raven_allocation=manager)
    assert status == 3 and not marker.exists() and closed == [True]
    assert R.process_router() is previous
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "deferred"
    assert report["data"]["prepared_invocation"][-1] == str(marker)


def test_public_managed_report_submit_routes_once_without_heavy(tmp_path, monkeypatch, capsys):
    from contextlib import contextmanager
    from raven import cli, research_reporting
    from raven.raven_config import DEFAULTS
    calls = []
    class Ledger:
        @contextmanager
        def admit(self, descriptor, argv, **kwargs):
            calls.append((descriptor.family, list(argv)))
            yield
    manager = SimpleNamespace(collection_backend=SimpleNamespace(ledger=Ledger()))
    (tmp_path/"batch.sh").write_text("#!/bin/bash\ntrue\n")
    original = research_reporting.submit
    def runner(argv, **kwargs):
        assert len(calls) == 1 and calls[0][0] == "submit"
        kwargs["stdout"].write(b"52\n")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(research_reporting, "submit", lambda *args, **kwargs: original(*args, **kwargs, runner=runner))
    status = cli.main(["report", "submit", "--root", str(tmp_path), "--experiment", "trial", "--script", "batch.sh"],
                      raven=copy.deepcopy(DEFAULTS), raven_allocation=manager)
    assert status == 0 and capsys.readouterr().out.strip() == "52"
    assert len(calls) == 1 and (tmp_path/"runs/job-52-a1/run.json").exists()


def test_direct_worker_adapter_cannot_run_on_managed_interface():
    R.configure_process(LIGHT)
    with pytest.raises(R.AdmissionError, match="admitted worker process"):
        R.execute_operation("statistics", {"document": comparison()})
