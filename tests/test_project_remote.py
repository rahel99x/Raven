"""Actual isolated stdlib SSH-helper execution and exact Research bindings."""
import json
import hashlib
import subprocess
import threading

import pytest

from raven import array_manifest_ui, project_remote, project_ui, projects
from raven.config import Config
from raven.controller import App
from raven.metrics import MetricReader, write_metric
from raven.model import Finished, Job, Store
from raven.provenance import capture, save
from raven.remote import LocalFiles, RemoteFiles, SshBackend
from raven.research import RESEARCH_VIEWS, ResearchHub
from raven.submission import prepare


class IsolatedSSH:
    """Run the exact transmitted command with no installed Raven on its path."""
    def __init__(self):
        self.commands = []
        self.threads = []

    def run(self, command, timeout):
        self.commands.append(command)
        self.threads.append(threading.get_ident())
        assert command[:4] == ["python3", "-I", "-S", "-c"]
        assert len(SshBackend("example.invalid").argv(command)[-1].encode()) < 131072
        result = subprocess.run(command, cwd="/tmp", text=True, capture_output=True, timeout=timeout)
        if result.returncode:
            raise OSError(result.stderr)
        return result.stdout, 0


def test_bundled_model_publications_work_without_installed_raven(tmp_path):
    """Model imports and lazy history dependencies must travel with the helper."""
    bootstrap = project_remote._BOOTSTRAP.partition("from raven import projects")[0]
    script = bootstrap + '''
from raven.model import Store
store = Store(persist=False)
store.record("71", {"k": "live", "t": 1.0, "cpu": .5})
first = store.series_publication("71", memory_only=True)
same = first is store.series_publication("71", memory_only=True)
store.series["71"][0]["cpu"] = .75
second = store.series_publication("71", memory_only=True)
print(json.dumps({"reused": same, "original": first[0]["cpu"], "corrected": second[0]["cpu"]}))
'''
    result = subprocess.run(["python3", "-I", "-S", "-c", script, "{}", *project_remote._payload()],
                            cwd=tmp_path, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"reused": True, "original": .5, "corrected": .75}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.fixture
def reported_project(tmp_path):
    root = tmp_path / "a project ' with space"
    run = root / "runs" / "trial-1"
    run.mkdir(parents=True)
    script = root / "batch.sh"
    script.write_text("#!/bin/bash\n#SBATCH --time=00:10:00\necho experiment\n")
    inventory = {"schema": "raven.run/v1", "run_id": run.name, "experiment_id": "trial", "attempt": 1,
                 "state": "FAILED", "job_id": "901_2", "parameters": {"seed": 1},
                 "metadata": {"raven_launch": {"cluster": "test-cluster", "dependency": "afterok:900"}},
                 "paths": {"metrics": "metrics.jsonl", "stdout": "stdout.log", "stderr": "stderr.log",
                           "planning": "reports/planning.json", "workflow": "reports/workflow.json",
                           "submit": "reports/submit.json", "array_manifest": "reports/array.json",
                           "passports": "passports", "contract": ".raven/contracts/outputs.v1.json"}}
    write_json(run / "run.json", inventory)
    write_metric(run / "metrics.jsonl", {"loss": .2}, step=1, t=100)
    (run / "stdout.log").write_text("Started experiment\n")
    (run / "stderr.log").write_text("RuntimeError: CUDA out of memory\n")
    write_json(run / "result.json", {"observed": 2})
    write_json(run / ".raven/contracts/outputs.v1.json", {"version": 1, "outputs": [
        {"path": "result.json", "format": "json", "required_keys": ["observed"]}]})
    # A project-wide contract deliberately does not apply to this attempt.
    write_json(root / ".raven/contracts/outputs.v1.json", {"version": 1, "outputs": [{"path": "not-this-run"}]})
    write_json(run / "reports/planning.json", {"version": 1, "kind": "raven.planning", "history": [],
        "job_id": "901_2", "jobs": [{"id": "901_2", "name": "trial", "state": "FAILED",
                                     "partition": "cpu", "cpus": 1, "nodes": 1, "gpus": 0}]})
    write_json(run / "reports/workflow.json", {"version": 1, "kind": "raven.workflow", "name": "trial",
        "nodes": [{"id": "preprocess", "script": "batch.sh"},
                  {"id": "train", "script": "batch.sh", "depends_on": ["preprocess"]}]})
    write_json(run / "reports/submit.json", prepare(str(script), workdir=str(root)))
    write_json(run / "reports/array.json", {"schema": "raven.array-manifest/v1", "cluster": "test-cluster",
        "array_id": "901", "entries": [{"index": 2, "id": "seed-1", "label": "Seed 1", "parameters": {"seed": 1}}]})
    passport = capture(str(root), script=str(script), job_id="901_2")
    save(passport, str(run / "passports"))
    return root, run


@pytest.fixture(params=["local", "ssh"])
def backend(request):
    return RemoteFiles(IsolatedSSH()) if request.param == "ssh" else LocalFiles()


def make_app(root, files):
    app = App(Store(persist=False), None, None, Config({}), "tester")
    app.files = app.logs.files = files
    app.research = ResearchHub(app.cfg, files=files)
    app.store.jobs = [Job("901_3", "trial", "cpu", "RUNNING", cluster="test-cluster")]
    app.store.finished = [Finished("901_2", "trial", "FAILED", exit="1:0", cluster="test-cluster")]
    app.store.details["901_2"] = {"WorkDir": str(root)}
    app.tab, app.selected_id = "jobs", "901_2"
    return app


def test_every_research_view_reads_same_exact_attempt_on_local_and_isolated_ssh(reported_project, backend):
    root, run = reported_project
    app = make_app(root, backend)
    try:
        binding = project_ui.settle(app)
        assert binding["job_id"] == "901_2"
        assert binding["contract"] == str(run / ".raven/contracts/outputs.v1.json")
        assert binding["launch"]["dependency"] == "afterok:900"
        assert (app.tab, app.selected_id) == ("jobs", "901_2")
        manifest = array_manifest_ui.initialize(app)["manifest"]
        assert manifest.lookup("901_2", "test-cluster").id == "seed-1"
        results = {}
        for view, _ in RESEARCH_VIEWS:
            app.research_view = view
            result = app.research.request(app.research.context(app.store.snapshot(), app), wait=True, force=True)
            assert result["status"] not in {"error", "loading"}, (view, result)
            if result["status"] == "empty":
                assert view in {"tradeoffs", "scaling"}  # no invented candidates/measurements
            results[view] = result
        assert results["experiment"]["latest"]["loss"] == .2
        assert results["artifacts"]["valid"] is True
        assert results["submit"]["display_only"] is True
        assert results["passport"]["passport"]["job_id"] == "901_2"
        assert "workflow.json" in results["workflow"]["source"]
        if backend.remote:
            assert backend.ssh.commands
            assert all(ident != threading.get_ident() for ident in backend.ssh.threads)
    finally:
        app.research.close()


def test_remote_metric_stream_is_incremental_and_partial_utf8_survives(reported_project):
    root, run = reported_project
    files = RemoteFiles(IsolatedSSH())
    binding = projects.select_run(str(root), run.name, files=files)["binding"]
    reader = MetricReader(files)
    path = str(run / "metrics.jsonl")
    assert reader.read_confined(path, binding)["records"] == 1
    with open(path, "ab") as stream:
        stream.write('{"t":101,"metrics":{"loss":0.1},"phase":"'.encode() + b"\xe8")
    assert reader.read_confined(path, binding)["records"] == 1
    with open(path, "ab") as stream:
        stream.write(b'\xae\xad"}\n')
    result = reader.read_confined(path, binding)
    assert result["records"] == 2 and result["latest"]["loss"] == .1
    assert result["phase"] == "训"
    assert reader.read_confined(path, binding)["records"] == 2
    from raven.project_wire import classify
    actions = [classify(command)["action"] for command in files.ssh.commands]
    # One discovery plus four incremental reads, with at most one cache-miss
    # probe and a single bounded bundle upload on a previously unused host.
    assert actions.count("read") in {5, 6}
    assert actions.count("upload") in {0, len(project_remote._payload())}
    assert all(classify(command)["action"] == "read" for command in files.ssh.commands[-4:])


@pytest.mark.parametrize("kind", ["job", "attempt", "parent-symlink", "report-symlink", "declaration"])
def test_bound_remote_identity_and_confinement_fail_closed(reported_project, kind):
    root, run = reported_project
    files = RemoteFiles(IsolatedSSH())
    binding = projects.select_run(str(root), run.name, files=files)["binding"]
    document = json.loads((run / "run.json").read_text())
    if kind in ("job", "attempt", "declaration"):
        if kind == "job":
            document["job_id"] = "902_2"
        elif kind == "attempt":
            document["attempt"] = 2
        else:
            document["paths"]["planning"] = "reports/different.json"
        write_json(run / "run.json", document)
    elif kind == "parent-symlink":
        root.rename(root.with_name("renamed"))
        root.symlink_to(root.with_name("renamed"), target_is_directory=True)
    else:
        source = run / "reports/planning.json"
        moved = source.with_name("different.json")
        source.rename(moved)
        source.symlink_to(moved)
    with pytest.raises((OSError, ValueError)):
        projects.read_bound_json(binding, binding["planning_file"], key="planning", files=files)


def test_remote_binding_never_falls_back_locally_or_to_another_connection(reported_project):
    root, run = reported_project
    files = RemoteFiles(IsolatedSSH())
    binding = projects.select_run(str(root), run.name, files=files)["binding"]
    with pytest.raises(ValueError, match="original SSH"):
        projects.read_bound_json(binding, binding["planning_file"], key="planning")
    other = RemoteFiles(IsolatedSSH())
    with pytest.raises(ValueError, match="another connection"):
        projects.read_bound_json(binding, binding["planning_file"], key="planning", files=other)
    assert not other.ssh.commands


def test_late_metric_file_and_map_revision_refresh_without_navigation(reported_project, backend):
    root, run = reported_project
    (run / "metrics.jsonl").unlink()
    app = make_app(root, backend)
    try:
        project_ui.settle(app)
        app.research_view = "experiment"
        context = app.research.context(app.store.snapshot(), app)
        missing = app.research.request(context, wait=True, force=True)
        assert missing["status"] in {"missing", "error"} and not missing.get("latest")
        write_metric(run / "metrics.jsonl", {"loss": .4}, t=102)
        before_generation = app.research.generation
        document = json.loads((run / "reports/array.json").read_text())
        document["entries"][0]["label"] = "Renamed seed"
        write_json(run / "reports/array.json", document)
        project_ui.settle(app)
        assert app.research.generation == before_generation
        assert array_manifest_ui.initialize(app)["manifest"].entries[0].label == "Renamed seed"
        context = app.research.context(app.store.snapshot(), app)
        assert app.research.request(context, wait=True, force=True)["latest"]["loss"] == .4
        assert (app.tab, app.selected_id) == ("jobs", "901_2")
    finally:
        app.research.close()


def test_array_map_wrong_parent_or_cluster_never_attaches(reported_project, backend):
    root, run = reported_project
    document = json.loads((run / "reports/array.json").read_text())
    for key, value in (("array_id", "900"), ("cluster", "another")):
        changed = dict(document, **{key: value})
        write_json(run / "reports/array.json", changed)
        selected = projects.select_run(str(root), run.name, files=backend)
        assert "array_manifest_record" not in selected
        assert any("array_manifest:" in item for item in selected["warnings"])


def test_cluster_identity_and_duplicate_attempts_do_not_guess(reported_project, backend):
    root, run = reported_project
    assert projects.discover_job([str(root)], "901_2", files=backend, cluster="other")["status"] == "missing"
    document = json.loads((run / "run.json").read_text())
    document.update(run_id="trial-2", attempt=2)
    write_json(root / "runs/trial-2/run.json", document)
    assert projects.discover_job([str(root)], "901_2", files=backend, cluster="test-cluster")["status"] == "ambiguous"


def test_remote_helper_errors_are_bounded_and_do_not_create_local_sources(tmp_path):
    existing = set(tmp_path.iterdir())
    class Failure:
        def run(self, command, timeout):
            raise OSError("python3 missing on selected host")
    with pytest.raises(OSError, match="python3 missing"):
        projects.discover_project(str(tmp_path), files=RemoteFiles(Failure()))
    assert set(tmp_path.iterdir()) == existing


def test_report_source_refresh_preserves_other_cached_views_and_discards_stale_reads():
    hub = ResearchHub({})
    try:
        hub.cache[(0, "experiment", "7")] = (1, {"latest": {"loss": .2}})
        hub.cache[(0, "workflow", None)] = (1, {"status": "old"})
        hub.rebind_sources(planning_files={"workflow": "/new.json"})
        assert hub.cache[(1, "experiment", "7")][1]["latest"]["loss"] == .2
        assert (1, "workflow", None) not in hub.cache
        assert hub.generation == 1
    finally:
        hub.close()


def test_exact_index_binds_after_directory_discovery_limit(reported_project, backend, monkeypatch):
    root, run = reported_project
    write_json(root / ".raven/jobs/901_2.json", {"schema": "raven.job-index/v1", "job_id": "901_2",
        "cluster": "test-cluster", "run_ids": [run.name]})
    # These malformed siblings would make an ordinary scan incomplete. The
    # producer's explicit exact-job index never scans or trusts those siblings.
    for index in range(270):
        write_json(root / f"runs/other-{index}/run.json", {"unrelated": True})
    result = projects.discover_job([str(root)], "901_2", files=backend)
    assert result["status"] == "ready"
    assert result["projects"][0]["indexed"] is True
    assert result["projects"][0]["entries_seen"] == 1
    assert result["selected"]["binding"]["run_id"] == run.name


def test_index_collision_and_missing_attempt_fail_closed(reported_project, backend):
    root, run = reported_project
    index = root / ".raven/jobs/901_2.json"
    write_json(index, {"schema": "raven.job-index-ambiguous/v1", "job_id": "901_2", "clusters": ["test-cluster", "other"]})
    assert projects.discover_job([str(root)], "901_2", files=backend)["status"] == "incomplete"
    scoped = root / (".raven/jobs/" + hashlib.sha256(b"test-cluster").hexdigest()[:12] + "-901_2.json")
    write_json(scoped, {"schema": "raven.job-index/v1", "job_id": "901_2", "cluster": "test-cluster", "run_ids": [run.name]})
    assert projects.discover_job([str(root)], "901_2", files=backend, cluster="test-cluster")["status"] == "ready"
    write_json(scoped, {"schema": "raven.job-index/v1", "job_id": "901_2", "cluster": "test-cluster", "run_ids": ["missing"]})
    assert projects.discover_job([str(root)], "901_2", files=backend, cluster="test-cluster")["status"] != "ready"


def test_requeues_use_scheduler_restart_evidence_and_never_guess_latest(reported_project, backend):
    root, run = reported_project
    first = json.loads((run / "run.json").read_text())
    first["metadata"]["raven_launch"]["restart_count"] = 0
    write_json(run / "run.json", first)
    second = json.loads(json.dumps(first))
    second.update(run_id="trial-requeued", attempt=2)
    second["metadata"]["raven_launch"]["restart_count"] = 1
    write_json(root / "runs/trial-requeued/run.json", second)
    write_json(root / ".raven/jobs/901_2.json", {"schema": "raven.job-index/v1", "job_id": "901_2",
        "cluster": "test-cluster", "run_ids": [run.name, "trial-requeued"]})
    assert projects.discover_job([str(root)], "901_2", files=backend)["status"] == "ambiguous"
    selected = projects.discover_job([str(root)], "901_2", files=backend, restart_count=1)
    assert selected["status"] == "ready" and selected["selected"]["binding"]["attempt"] == 2


def test_shared_project_planning_refreshes_but_explicit_run_report_wins(reported_project, backend):
    root, run = reported_project
    write_json(root / ".raven/project.json", {"schema": "raven.project/v1", "paths": {"planning": "reports/planning.json"}})
    write_json(root / "reports/planning.json", {"version": 1, "kind": "raven.planning", "history": [], "shared": 1})
    explicit = projects.select_run(str(root), run.name, files=backend)["binding"]
    assert explicit["planning_scope"] == "run"
    (run / "reports/planning.json").unlink()
    shared = projects.select_run(str(root), run.name, files=backend)["binding"]
    assert shared["planning_scope"] == "project"
    assert projects.read_bound_json(shared, shared["planning_file"], key="planning", files=backend)["shared"] == 1
    write_json(root / ".raven/project.json", {"schema": "raven.project/v1", "paths": {"planning": "reports/other.json"}})
    with pytest.raises(ValueError, match="declaration changed"):
        projects.read_bound_json(shared, shared["planning_file"], key="planning", files=backend)


def test_bound_reader_pins_inventory_and_reports_to_same_run_directory(reported_project, monkeypatch):
    root, run = reported_project
    binding = projects.select_run(str(root), run.name)["binding"]
    original = projects._inventory
    def replace_after_inventory(root_fd, directory, *args, **kwargs):
        value = original(root_fd, directory, *args, **kwargs)
        run.rename(root / "runs/old")
        replacement = dict(value["inventory"], job_id="somebody-else")
        write_json(run / "run.json", replacement)
        write_json(run / "reports/planning.json", {"private": True})
        return value
    monkeypatch.setattr(projects, "_inventory", replace_after_inventory)
    result = projects.read_bound_json(binding, binding["planning_file"], key="planning")
    assert "private" not in result and result["job_id"] == "901_2"


def test_bound_reader_refuses_cluster_changed_after_selection(reported_project, backend):
    root, run = reported_project
    binding = projects.select_run(str(root), run.name, files=backend)["binding"]
    value = json.loads((run / "run.json").read_text())
    value["metadata"]["raven_launch"]["cluster"] = "other"
    write_json(run / "run.json", value)
    with pytest.raises(ValueError, match="launch identity changed"):
        projects.read_bound_json(binding, binding["planning_file"], key="planning", files=backend)


def test_same_adapter_target_mutation_invalidates_remote_binding(reported_project):
    root, run = reported_project
    ssh = IsolatedSSH()
    ssh.target = "first.example"
    files = RemoteFiles(ssh)
    binding = projects.select_run(str(root), run.name, files=files)["binding"]
    before = len(ssh.commands)
    ssh.target = "other.example"
    with pytest.raises(ValueError, match="another connection"):
        projects.read_bound_json(binding, binding["planning_file"], files=files)
    assert len(ssh.commands) == before


def test_auto_array_map_clear_is_respected_and_manual_map_is_not_overwritten(reported_project):
    root, run = reported_project
    app = make_app(root, LocalFiles())
    try:
        project_ui.settle(app)
        state = array_manifest_ui.initialize(app)
        assert state["origin"] == "automatic"
        array_manifest_ui.run_command(app, ["arraymap", "clear"])
        project_ui.settle(app)
        assert state["manifest"] is None
        app.interactive = False
        array_manifest_ui.run_command(app, ["arraymap", "load", str(run / "reports/array.json")])
        manual = state["manifest"]
        assert manual is not None and state["origin"] == "manual"
        value = json.loads((run / "reports/array.json").read_text())
        value["entries"][0]["label"] = "Producer update"
        write_json(run / "reports/array.json", value)
        project_ui.settle(app)
        assert state["manifest"] is manual
        project_ui.clear_binding(app)
        assert state["manifest"] is manual
    finally:
        app.research.close()


def test_bash_producer_reports_auto_link_metrics_shared_history_and_failed_attempt(tmp_path, backend):
    from raven import research_reporting as reporting
    root = tmp_path / "producer project"
    root.mkdir()
    (root / "batch.sh").write_text("#!/bin/bash\n#SBATCH --job-name=different-scheduler-name\necho measured\n")
    run = reporting.start(str(root), script="batch.sh", experiment="scientific-cohort", parameters={"seed": 42},
                          resources={"partition": "cpu", "cpus": 1, "nodes": 1, "gpus": 0},
                          environ={"SLURM_JOB_ID": "901", "SLURM_ARRAY_JOB_ID": "901", "SLURM_ARRAY_TASK_ID": "2",
                                   "SLURM_CLUSTER_NAME": "test-cluster", "SLURM_RESTART_COUNT": "0"})
    write_metric(run / "metrics.jsonl", {"observed": 3}, t=100)
    reporting.finish(str(run), exit_code=1, runtime_seconds=2)
    reporting.aggregate(str(root))
    app = make_app(root, backend)
    try:
        binding = project_ui.settle(app)
        assert binding["job_id"] == "901_2" and binding["state"] == "FAILED"
        assert binding["planning_scope"] == "project"
        app.research_view = "experiment"
        result = app.research.request(app.research.context(app.store.snapshot(), app), wait=True, force=True)
        assert result["latest"]["observed"] == 3
        app.research_view = "predict"
        context = app.research.context(app.store.snapshot(), app)
        result = app.research.request(context, wait=True, force=True)
        assert result["status"] not in {"error", "loading"}
        assert binding["scientific_query"]["name"] == "scientific-cohort"
        assert binding["scientific_query"]["parameters"] == {"seed": 42}
        history = projects.read_bound_json(binding, binding["planning_file"], key="planning", files=backend)["history"]
        assert len(history) == 1 and history[0]["state"] == "FAILED"
    finally:
        app.research.close()


def test_ssh_artifact_browser_uses_same_confined_pager(reported_project):
    from raven.artifact_pages import read_page
    root, run = reported_project
    files = RemoteFiles(IsolatedSSH())
    selected = projects.select_run(str(root), run.name, files=files)
    tree = projects.artifact_tree(selected["binding"]["contract"], str(run), files=files)
    leaf = next(node for node in tree["nodes"] if not node["directory"])
    page = read_page(str(run), leaf["specification"], files=files)
    assert page["status"] == "ready" and page["json_value"] == {"observed": 2}
    replacement = run / "external.json"
    (run / "result.json").rename(replacement)
    (run / "result.json").symlink_to(replacement)
    with pytest.raises((OSError, ValueError)):
        read_page(str(run), leaf["specification"], files=files)


def test_endpoint_change_during_remote_read_never_stamps_result_with_new_host(reported_project):
    root, run = reported_project
    class MovingSSH(IsolatedSSH):
        target = "old.example"
        def run(self, command, timeout):
            value = super().run(command, timeout)
            self.target = "new.example"
            return value
    with pytest.raises(ValueError, match="connection changed"):
        projects.select_run(str(root), run.name, files=RemoteFiles(MovingSSH()))


def test_array_parent_picker_explicitly_labels_submission_receipt(reported_project):
    from raven import cli, layout
    root, run = reported_project
    value = json.loads((run / "run.json").read_text())
    value.update(job_id="901", state="SUBMITTED")
    value["metadata"]["raven_launch"].update(array_submission_receipt=True, array_id="901", array_index=None)
    write_json(run / "run.json", value)
    session = cli.build(cli.parse(["--fake", "--no-state", "--no-plugins"]), Config())
    try:
        selected = projects.select_run(str(root), run.name)
        project_ui._apply_run(session.app, selected, automatic=True)
        session.app.project_state.update(root=str(root), runs=[selected["run"]])
        session.app.mode = "project_runs"
        rendered = project_ui.overlay(session.views, session.store.snapshot(), session.app, 150, 30)
        text = "\n".join(layout.row_text(row) for _, _, row in rendered)
        assert "ARRAY RECEIPT" in text and "[SUBMITTED]" not in text
        assert "array launch receipt" in session.app.project_state["summary"]
    finally:
        session.close()


def test_unknown_scheduler_cluster_still_rejects_launch_swap_during_discovery(reported_project, monkeypatch):
    root, run = reported_project
    original = projects.select_run
    def changed(*args, **kwargs):
        value = json.loads((run / "run.json").read_text())
        value["metadata"]["raven_launch"]["cluster"] = "other"
        write_json(run / "run.json", value)
        return original(*args, **kwargs)
    monkeypatch.setattr(projects, "select_run", changed)
    result = projects.discover_job([str(root)], "901_2")
    assert result["status"] == "changed" and result["selected"] is None


def test_checkpoint_advice_uses_explicit_bound_bundle_on_local_and_isolated_ssh(reported_project, backend):
    from pathlib import Path
    root, run = reported_project
    source_path = run / "reports/planning.json"
    source = json.loads(source_path.read_text())
    source["checkpoint_advice"] = json.loads((Path(__file__).resolve().parents[1] / "examples/checkpoint-advice.json").read_text())
    write_json(source_path, source)
    app = make_app(root, backend)
    try:
        project_ui.settle(app)
        app.research_view = "checkpoint-advice"
        result = app.research.request(app.research.context(app.store.snapshot(), app), wait=True, force=True)
        assert result["schema"] == "raven.checkpoint-advice-result/v1"
        assert result["status"] == "advisory"
        assert result["scope"] == source["checkpoint_advice"]["scope"]
    finally:
        app.research.close()


@pytest.mark.parametrize("remote", [False, True])
def test_metric_metadata_uses_verified_run_and_one_remote_round_trip(reported_project, remote):
    root, run = reported_project
    files = RemoteFiles(IsolatedSSH()) if remote else LocalFiles()
    binding = projects.select_run(str(root), run.name, files=files)["binding"]
    meta = run / "metrics.meta.json"
    meta.write_text(json.dumps({"schema": "raven.metrics-meta/v1", "client": {"api_version": 1},
        "kinds": {"loss": "residual"}, "coalesced": {"loss": 12},
        "rank_policy": {"mode": "coordinator", "rank": 0}}))
    reader = MetricReader(files)
    result = reader.read_confined(binding["metrics_file"], binding)
    assert result["kinds"] == {"loss": "residual"} and result["coalesced"] == {"loss": 12}
    if remote:
        assert len(files.ssh.commands) == 2  # discovery plus a combined stream/sidecar read
    meta.unlink()
    outside = root / "outside.json"
    outside.write_text('{"schema":"untrusted"}')
    meta.symlink_to(outside)
    result = reader.read_confined(binding["metrics_file"], binding)
    assert result["records"] == 1 and result["meta_error"] and not result["kinds"]


def rank_index(run, ranks=(0, 3, 7)):
    entries = []
    for rank in ranks:
        name = f"metrics.rank-{rank}.jsonl"
        meta = f"metrics.rank-{rank}.meta.json"
        entries.append({"path": name, "meta": meta, "rank": rank})
        if rank != 7:
            write_metric(run / name, {"loss": rank + 1}, t=100)
            (run / meta).write_text(json.dumps({"schema": "raven.metrics-meta/v1", "client": {"api_version": 1},
                "kinds": {"loss": "residual"}, "coalesced": {}, "rank_policy": {"mode": "all_ranks", "rank": rank}}))
    (run / "metrics.index.json").write_text(json.dumps({"schema": "raven.metrics-index/v1", "streams": entries}))
    return entries


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("canonical", ["missing", "empty"])
def test_all_rank_selection_is_confined_disjoint_and_recovers_missing_sources(reported_project, remote, canonical):
    root, run = reported_project
    if canonical == "missing":
        (run / "metrics.jsonl").unlink()
    else:
        (run / "metrics.jsonl").write_text("")
    rank_index(run)
    files = RemoteFiles(IsolatedSSH()) if remote else LocalFiles()
    binding = projects.select_run(str(root), run.name, files=files)["binding"]
    reader = MetricReader(files)
    path = binding["metrics_file"]
    automatic = reader.read_confined(path, binding)
    assert automatic["latest"] == {"loss": 1}
    assert automatic["selected_stream"] == "metrics.rank-0.jsonl"
    assert automatic["metadata"]["rank_policy"]["rank"] == 0
    other = reader.read_confined(path, binding, rank_stream="metrics.rank-3.jsonl")
    assert other["latest"] == {"loss": 4} and other["records"] == 1
    assert other["requested_path"] == path
    missing = reader.read_confined(path, binding, rank_stream="metrics.rank-7.jsonl")
    assert missing["status"] == "missing" and missing["records"] == 0
    assert len(missing["rank_streams"]) == 3
    assert missing["selected_stream"] == "metrics.rank-7.jsonl"
    assert missing["path"].endswith("metrics.rank-7.jsonl")
    assert reader.read_confined(path, binding)["latest"] == {"loss": 1}
    with pytest.raises(ValueError, match="no longer registered"):
        reader.read_confined(path, binding, rank_stream="metrics.rank-900.jsonl")
    if remote:
        assert len(files.ssh.commands) == 6  # discovery + five single requests, no rank fanout


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("corruption", ["symlink", "traversal", "oversized", "mismatch", "duplicate", "stream-symlink"])
def test_rank_index_cannot_expand_a_bound_streams_authority(reported_project, remote, corruption):
    root, run = reported_project
    entries = rank_index(run)
    path = run / "metrics.index.json"
    if corruption == "symlink":
        outside = root / "outside-index.json"
        path.rename(outside)
        path.symlink_to(outside)
    elif corruption == "stream-symlink":
        stream = run / "metrics.rank-3.jsonl"
        stream.unlink()
        stream.symlink_to(run / "metrics.jsonl")
    else:
        if corruption == "traversal":
            entries[1]["path"] = "../metrics.rank-3.jsonl"
        elif corruption == "mismatch":
            entries[1]["rank"] = 4
        elif corruption == "duplicate":
            entries.append(entries[1])
        else:
            entries = [entries[1]] * 129
        path.write_text(json.dumps({"schema": "raven.metrics-index/v1", "streams": entries}))
    files = RemoteFiles(IsolatedSSH()) if remote else LocalFiles()
    binding = projects.select_run(str(root), run.name, files=files)["binding"]
    reader = MetricReader(files)
    with pytest.raises((OSError, ValueError)):
        reader.read_confined(binding["metrics_file"], binding, rank_stream="metrics.rank-3.jsonl")
    canonical = reader.read_confined(binding["metrics_file"], binding)
    assert canonical["latest"] == {"loss": .2}
    if corruption != "stream-symlink":
        assert canonical["index_error"]
