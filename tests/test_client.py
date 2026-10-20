"""Application outcomes and on-disk compatibility of the vendorable micro API."""
import asyncio
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from raven import client
from raven.metrics import MetricReader, _record


@pytest.fixture(autouse=True)
def fresh_client(monkeypatch):
    client._shutdown()
    client._state = None
    client._options = {}
    client._fork_child = False
    for name in ("RAVEN_RUN_DIR", "RAVEN_METRICS_FILE", "RANK", "SLURM_PROCID", "OMPI_COMM_WORLD_RANK", "PMI_RANK"):
        monkeypatch.delenv(name, raising=False)
    yield
    client._shutdown()
    client._state = None
    client._options = {}


def configured(tmp_path):
    path = tmp_path / "metrics.jsonl"
    client.configure(path=path, flush_interval=60)
    return path


def read(path):
    assert client._get_client().flush()
    records = [json.loads(line) for line in path.read_text().splitlines()]
    return records, json.loads(path.with_suffix(".meta.json").read_text())


def test_noop_does_not_touch_files_or_start_threads(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("inactive client attempted I/O or started a thread")
    monkeypatch.setattr(threading.Thread, "start", forbidden)
    monkeypatch.setattr(os, "open", forbidden)
    value = object()
    assert client.metric("anything", value) is value
    assert client.progress(value, object()) is value
    @client.measure("x")
    def function():
        return value
    with client.phase("x"):
        assert function() is value
    with client.watch(function, "value"):
        assert function() is value
    assert client._state is None


def test_lazy_package_exports_only_client():
    script = """
import sys, raven
assert 'raven.client' not in sys.modules
assert raven.Kind.RESIDUAL == 'residual'
assert callable(raven.measure)
assert {n for n in sys.modules if n.startswith('raven.')} == {'raven.client'}
assert 'curses' not in sys.modules and 'raven.slurm' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", script], check=True)


@given(st.one_of(st.none(), st.booleans(), st.integers(), st.text(), st.dictionaries(
    st.sampled_from(["t", "metrics", "step", "phase", "progress", "extra"]),
    st.one_of(st.none(), st.booleans(), st.integers(), st.floats(), st.text(),
              st.dictionaries(st.text(max_size=100), st.one_of(st.floats(), st.integers(), st.text()), max_size=3)),
    max_size=6)))
@settings(max_examples=300, deadline=None)
def test_record_parity_arbitrary_objects(value):
    outcomes = []
    for validate in (_record, client._record):
        try:
            outcomes.append(("ok", validate(value)))
        except ValueError as exc:
            outcomes.append(("error", str(exc)))
    assert outcomes[0] == outcomes[1]


@given(name=st.text(max_size=100), value=st.one_of(st.floats(), st.integers()),
       step=st.one_of(st.integers(), st.booleans()), phase=st.text(max_size=164))
@settings(max_examples=300, deadline=None)
def test_record_parity_fields(name, value, step, phase):
    record = {"t": 123.5, "metrics": {name: value}, "step": step, "phase": phase}
    try:
        expected = _record(record)
    except ValueError:
        with pytest.raises(ValueError):
            client._record(record)
    else:
        assert client._record(record) == expected


def test_all_entry_points_are_reader_compatible(tmp_path):
    path = configured(tmp_path)
    @client.measure("loss", kind=client.Kind.OBJECTIVE)
    def loss():
        return .25
    @client.measure("unused", kind="error")
    def errors():
        return {"l1": .1, "l2": .2}
    with client.phase("solve"):
        assert loss() == .25
        assert errors() == {"l1": .1, "l2": .2}
        assert client.metric("energy", 9, kind="conserved", step=4, unit="J") == 9
        assert client.progress(4, 10, unit="iterations") == 4
    records, meta = read(path)
    for record in records:
        assert client._record(record) == _record(record)
    snapshot = MetricReader().read(path)
    assert snapshot["status"] == "ok"
    assert snapshot["latest"]["loss"] == .25
    assert snapshot["latest"]["l1"] == .1
    assert snapshot["progress"]["completed"] == 4
    assert meta["kinds"]["energy"] == "conserved"
    assert meta["units"]["energy"] == "J"
    assert meta["baselines"]["energy"] == 9
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.with_suffix(".meta.json").stat().st_mode & 0o777 == 0o600


def test_local_environment_and_missing_directory(tmp_path, monkeypatch):
    path = tmp_path / "local.jsonl"
    monkeypatch.setenv("RAVEN_METRICS_FILE", str(path))
    client.metric("x", 1)
    assert read(path)[0][0]["metrics"] == {"x": 1}
    assert client.configure(path=tmp_path / "missing" / "metrics.jsonl")
    assert client.metric("x", 2) == 2
    assert not client._get_client().flush()
    assert not (tmp_path / "missing").exists()


def test_coalesced_counts_and_first_observation(tmp_path):
    path = configured(tmp_path)
    for value in range(100000):
        client.metric("loss", value)
    records, meta = read(path)
    assert len(records) <= 20
    assert records[-1]["metrics"]["loss"] == 99999
    assert meta["coalesced"]["loss"] + len(records) == 100000
    assert meta["counts"]["accepted"] == 100000
    assert meta["baselines"]["loss"] == 0


def test_threads_do_not_interleave_and_account_for_every_input(tmp_path):
    path = configured(tmp_path)
    threads = [threading.Thread(target=lambda: [client.metric("loss", n) for n in range(10000)]) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    records, meta = read(path)
    assert meta["counts"]["accepted"] == 80000
    assert meta["coalesced"]["loss"] + len(records) == 80000
    assert not MetricReader().read(path)["errors"]


def test_bounds_include_names_steps_diagnostics_and_queue(tmp_path):
    configured(tmp_path)
    for n in range(1000):
        client.metric("name" + str(n), n, step="auto")
    state = client._get_client()
    assert len(state.kinds) == 64
    assert len(state.steps) == 64
    assert len(state.latest) <= 64
    assert len(state.queue) <= 10000
    assert len(state.diagnosed) <= 32
    assert state.counts["dropped"] == 936


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "12", True, [1, 2], object()])
def test_unsupported_values_preserve_input_and_diagnose_once(tmp_path, bad, capsys):
    configured(tmp_path)
    for _ in range(3):
        assert client.metric("bad", bad) is bad
    state = client._get_client()
    assert state.flush()
    assert state.counts["unsupported"] == 3
    assert capsys.readouterr().err.count("Metric was not recorded") == 1
    assert not Path(state.path).exists()


def test_tensorlike_conversion_is_sampled_before_sync(tmp_path):
    path = configured(tmp_path)
    class Scalar:
        syncs = 0
        def detach(self):
            return self
        def item(self):
            self.syncs += 1
            return 7
    scalar = Scalar()
    @client.measure("value", every=10)
    def function():
        return scalar
    for _ in range(100):
        assert function() is scalar
    assert scalar.syncs == 10
    records, _ = read(path)
    assert records[-1]["step"] == 90


def test_measure_steps_attributes_keywords_mapping_and_exceptions(tmp_path):
    path = configured(tmp_path)
    class Worker:
        epoch = 7
        @client.measure("loss", step="epoch")
        def train(self):
            return 1
    assert Worker().train() == 1
    @client.measure("score", step="batch.step")
    def score(batch):
        return 2
    assert score(types.SimpleNamespace(step=5)) == 2
    @client.measure("broken")
    def broken():
        raise RuntimeError("application failure")
    with pytest.raises(RuntimeError, match="application failure"):
        broken()
    records, _ = read(path)
    steps = {next(iter(r["metrics"])): r.get("step") for r in records}
    assert steps == {"loss": 7, "score": 5}


def test_phases_nested_async_isolation_and_exception_propagation(tmp_path):
    path = configured(tmp_path)
    async def job(name):
        async with client.phase(name):
            await asyncio.sleep(0)
            client.metric(name, 1)
    async def jobs():
        await asyncio.gather(job("one"), job("two"))
    asyncio.run(jobs())
    with client.phase("outer"):
        with client.phase("inner"):
            client.metric("inside", 1)
        client.metric("outside", 1)
    with pytest.raises(RuntimeError), client.phase("failed"):
        raise RuntimeError("original")
    records, _ = read(path)
    phases = {name: r.get("phase") for r in records for name in r["metrics"]}
    assert phases["one"] == "one" and phases["two"] == "two"
    assert phases["inside"] == "inner" and phases["outside"] == "outer"
    assert phases["phase.inner.seconds"] == "inner"
    assert phases["phase.outer.seconds"] == "outer"
    assert "phase.failed.seconds" in phases
    assert client._phase.get() == ""


def test_async_measure_returns_original_value(tmp_path):
    path = configured(tmp_path)
    @client.measure("x")
    async def compute():
        await asyncio.sleep(0)
        return 2
    assert asyncio.run(compute()) == 2
    assert read(path)[0][0]["metrics"] == {"x": 2}


def test_watch_no_stale_local_and_profile_ownership(tmp_path):
    path = configured(tmp_path)
    def target(include):
        if include:
            loss = 3  # noqa: F841 - explicitly observed through return-frame locals
        return include
    events = []
    def previous(frame, event, argument):
        if frame.f_code is target.__code__:
            events.append(event)
    saved = sys.getprofile()
    try:
        sys.setprofile(previous)
        with client.watch(target, "loss"):
            assert target(True) is True
            assert target(False) is False
        assert sys.getprofile() is previous
    finally:
        sys.setprofile(saved)
    records, _ = read(path)
    assert len(records) == 1 and records[0]["metrics"] == {"loss": 3}
    assert events == ["call", "return", "call", "return"]


def test_watch_does_not_remove_replacement_profiler(tmp_path):
    configured(tmp_path)
    def function():
        return 1
    saved = sys.getprofile()
    replacement = lambda *args: None
    try:
        watched = client.watch(function, "x")
        sys.setprofile(replacement)
        watched.close()
        assert sys.getprofile() is replacement
    finally:
        sys.setprofile(saved)


def test_symlinks_hardlinks_and_devices_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("original")
    for name, mode in (("link", "symlink"), ("hard", "hardlink"), ("fifo", "fifo")):
        path = tmp_path / name
        if mode == "symlink":
            path.symlink_to(outside)
        elif mode == "hardlink":
            os.link(outside, path)
        else:
            os.mkfifo(path)
        assert client.configure(path=path)
        assert client.metric("x", 2) == 2
        assert not client._get_client().flush()
        # A failed stopped writer permits an explicit fresh configure.
        client._state = None
    assert outside.read_text() == "original"


def test_user_selected_parent_alias_is_resolved_and_leaf_stays_owned(tmp_path):
    directory = tmp_path / "real"
    directory.mkdir()
    link = tmp_path / "link"
    link.symlink_to(directory, target_is_directory=True)
    client.configure(path=link / "metrics.jsonl")
    client.metric("x", 1)
    assert client._get_client().flush()
    assert MetricReader().read(directory / "metrics.jsonl")["latest"] == {"x": 1}


def test_unterminated_previous_line_is_separated(tmp_path):
    path = configured(tmp_path)
    path.write_text('{"t":1,"metrics":{"old":2}}')
    client.metric("new", 3)
    records, _ = read(path)
    assert len(records) == 2
    assert MetricReader().read(path)["latest"] == {"old": 2, "new": 3}


def test_slow_filesystem_never_blocks_reported_function(tmp_path, monkeypatch):
    configured(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = client._append
    def slow(*args, **kwargs):
        entered.set()
        release.wait(5)
        original(*args, **kwargs)
    monkeypatch.setattr(client, "_append", slow)
    client.metric("x", 1)
    state = client._get_client()
    state.wake.set()
    assert entered.wait(2)
    started = time.monotonic()
    assert client.metric("x", 2) == 2
    assert not state.flush(timeout=.01)
    assert time.monotonic() - started < .5
    release.set()
    assert state.flush()


def test_reduction_uses_only_explicit_loaded_collective(tmp_path, monkeypatch):
    path = configured(tmp_path)
    calls = []
    class Communicator:
        def allreduce(self, value, op):
            calls.append(("ready", value, op))
            return value
        def reduce(self, value, op, root):
            calls.append((value, op, root))
            return value * 2
        def Get_rank(self):
            return 0
        def Get_size(self):
            return 2
    mpi = types.SimpleNamespace(SUM="sum", MIN="min", MAX="max", COMM_WORLD=Communicator(),
                                Is_initialized=lambda: True, Is_finalized=lambda: False)
    monkeypatch.setitem(sys.modules, "mpi4py.MPI", mpi)
    client.metric("local", 3)
    assert calls == []
    client.metric("reduced", 3, reduce="sum")
    assert calls == [("ready", 1, "min"), (3, "sum", 0)]
    client.metric("mean", 4, reduce="mean")
    records, _ = read(path)
    values = {name: value for r in records for name, value in r["metrics"].items()}
    assert values == {"local": 3, "reduced": 6, "mean": 4}


def test_uninitialized_reduction_falls_back_truthfully(tmp_path, monkeypatch):
    path = configured(tmp_path)
    monkeypatch.delitem(sys.modules, "mpi4py.MPI", raising=False)
    monkeypatch.delitem(sys.modules, "torch.distributed", raising=False)
    client.metric("energy", 3, reduce="sum")
    records, meta = read(path)
    assert records[0]["metrics"] == {"energy": 3}
    assert "rank 0 only: no initialized reduction communicator" in meta["rank_policy"]["coverage"]


def test_reduction_failure_preserves_return(tmp_path, monkeypatch):
    configured(tmp_path)
    monkeypatch.setattr(client, "_reduce", lambda *args: (_ for _ in ()).throw(RuntimeError("collective failed")))
    assert client.metric("value", 9, reduce="sum") == 9
    assert client._get_client().flush()
    assert client._get_client().counts["unsupported"] == 1


def test_vendored_single_file_and_normal_atexit(tmp_path):
    shutil.copyfile(client.__file__, tmp_path / "raven.py")
    path = tmp_path / "metrics.jsonl"
    script = tmp_path / "program.py"
    script.write_text("import raven\nassert raven.__raven_vendored__\nraven.configure(path=" + repr(str(path)) + ")\n@raven.measure('loss')\ndef f(): return 4\nassert f()==4\n")
    subprocess.run([sys.executable, "-I", "-c", "import runpy,sys;sys.path.insert(0,sys.argv[1]);runpy.run_path(sys.argv[2])", str(tmp_path), str(script)], check=True)
    assert MetricReader().read(path)["latest"] == {"loss": 4}


@pytest.mark.parametrize("rank_env", [{"RANK": "1"}, {"SLURM_PROCID": "3"}, {"OMPI_COMM_WORLD_RANK": "2"},
                                      {"RANK": "1", "SLURM_PROCID": "0"}, {"RANK": "bad"}])
def test_noncoordinator_suppressed(tmp_path, monkeypatch, rank_env):
    for name, value in rank_env.items():
        monkeypatch.setenv(name, value)
    path = configured(tmp_path)
    client.metric("x", 1)
    state = client._get_client()
    assert state.flush()
    assert not path.exists()
    assert state.thread is None
    assert state.counts["suppressed"] == 1


@pytest.mark.parametrize("name,rank", [("RANK", 1), ("SLURM_PROCID", 3), ("OMPI_COMM_WORLD_RANK", 2)])
def test_all_ranks_use_indexed_separate_files(tmp_path, monkeypatch, name, rank):
    monkeypatch.setenv(name, str(rank))
    client.configure(path=tmp_path / "metrics.jsonl", all_ranks=True)
    client.metric("x", rank)
    assert client._get_client().flush()
    path = tmp_path / f"metrics.rank-{rank}.jsonl"
    assert MetricReader().read(path)["latest"] == {"x": rank}
    index = json.loads((tmp_path / "metrics.index.json").read_text())
    assert index["streams"] == [{"path": path.name, "meta": f"metrics.rank-{rank}.meta.json", "rank": rank}]


def test_subprocess_writers_merge_metadata_counts(tmp_path):
    path = tmp_path / "metrics.jsonl"
    script = "from raven import client as r; r.configure(path=" + repr(str(path)) + "); [r.metric('loss',i) for i in range(100)]"
    processes = [subprocess.Popen([sys.executable, "-c", script]) for _ in range(4)]
    assert all(p.wait(timeout=15) == 0 for p in processes)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    meta = json.loads(path.with_suffix(".meta.json").read_text())
    assert meta["counts"]["accepted"] == 400
    assert meta["counts"]["written"] == len(records)
    assert meta["coalesced"]["loss"] + len(records) == 400


def test_fork_child_cannot_reflush_parent_queue(tmp_path):
    path = configured(tmp_path)
    client.metric("parent", 1)
    pid = os.fork()
    if pid == 0:
        try:
            client.metric("child", 2)
            client._shutdown()
        finally:
            os._exit(0)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    records, _ = read(path)
    assert len(records) == 1 and records[0]["metrics"] == {"parent": 1}


def test_multiprocessing_and_pool_children_count_without_writing(tmp_path):
    script = tmp_path / "helpers.py"
    script.write_text("""
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor
from raven import client as r
import sys

def worker(path):
    r.configure(path=path)
    r.metric('child',1)
    state=r._get_client()
    assert state.thread is None and state.counts['suppressed']==1
    return True

if __name__ == '__main__':
    context=mp.get_context('spawn')
    child=context.Process(target=worker,args=(sys.argv[1],))
    child.start(); child.join(10); assert child.exitcode==0
    with ProcessPoolExecutor(1,mp_context=context) as pool:
        assert pool.submit(worker,sys.argv[1]).result(10)
""")
    env = dict(os.environ, PYTHONPATH=str(Path(client.__file__).parents[1]))
    path = tmp_path / "metrics.jsonl"
    subprocess.run([sys.executable, str(script), str(path)], env=env, check=True, timeout=30)
    assert not path.exists()


def test_abrupt_exit_retains_only_successfully_flushed_window(tmp_path):
    path = tmp_path / "metrics.jsonl"
    script = f"""
import os
from raven import client as r
r.configure(path={str(path)!r},flush_interval=60)
for i in range(30): r.metric('x',i)
assert r._get_client().flush()
for i in range(30,60): r.metric('x',i)
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", script], check=True)
    assert MetricReader().read(path)["latest"] == {"x": 29}


def test_watch_nested_removal_and_completed_async_returns(tmp_path):
    path = configured(tmp_path)
    async def compute():
        loss = 1
        await asyncio.sleep(0)
        loss = 2  # noqa: F841 - observed return-frame local
        return 7
    original = sys.getprofile()
    first = client.watch(compute, "loss")
    second = client.watch(compute, "loss")
    first.close()
    try:
        assert asyncio.run(compute()) == 7
    finally:
        second.close()
    assert sys.getprofile() is original
    records, meta = read(path)
    assert len(records) == 1
    assert records[0]["metrics"] == {"loss": 2}
    assert meta["counts"]["accepted"] == 1


def test_watch_skips_locals_from_failed_call(tmp_path):
    path = configured(tmp_path)
    def broken():
        loss = 2  # noqa: F841 - deliberately present on a failed return
        raise RuntimeError("application")
    with client.watch(broken, "loss"), pytest.raises(RuntimeError, match="application"):
        broken()
    assert not path.exists()


def test_writer_stays_anchored_when_run_directory_is_replaced(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    client.configure(path=run / "metrics.jsonl")
    client.metric("first", 1)
    assert client._get_client().flush()
    original = tmp_path / "original"
    run.rename(original)
    run.mkdir()
    client.metric("second", 2)
    assert client._get_client().flush()
    assert not list(run.iterdir())
    assert MetricReader().read(original / "metrics.jsonl")["latest"] == {"first": 1, "second": 2}


def test_cross_name_pressure_cannot_bypass_twenty_hz_ceiling(tmp_path):
    path = configured(tmp_path)
    start = time.monotonic()
    while time.monotonic() - start < .3:
        for index in range(64):
            client.metric("x" + str(index), index)
    records, _ = read(path)
    counts = {}
    for record in records:
        for name in record["metrics"]:
            counts[name] = counts.get(name, 0) + 1
    # There can be an initial point plus 20 per elapsed second.
    assert max(counts.values()) <= math.ceil((time.monotonic() - start) * 20) + 1


def test_client_diagnostic_uses_existing_run_warning_path(tmp_path, monkeypatch):
    (tmp_path / ".raven").mkdir()
    (tmp_path / "run.json").write_text('{"metadata":{}}')
    monkeypatch.setenv("RAVEN_RUN_DIR", str(tmp_path))
    client.metric("bad", "not a scalar")
    assert client._get_client().flush()
    manifest = json.loads((tmp_path / "run.json").read_text())
    assert len(manifest["metadata"]["automation"]["warnings"]) == 1
    assert "Metric was not recorded" in manifest["metadata"]["automation"]["warnings"][0]


def test_stdlib_import_boundary_is_static_and_vendored_import_isolated(tmp_path):
    import ast
    source = Path(client.__file__).read_text()
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            imported.add(node.module.split(".")[0])
    assert imported <= sys.stdlib_module_names


def test_phase_same_instance_reentry_and_long_name(tmp_path):
    path = configured(tmp_path)
    shared = client.phase("scope")
    with shared:
        with shared:
            client.metric("inner", 1)
        client.metric("outer", 1)
    assert client._phase.get() == ""
    with client.phase("a=" * 80):
        client.metric("long", 1)
    records, _ = read(path)
    durations = {name for r in records for name in r["metrics"] if name.startswith("phase.")}
    assert len(durations) == 2
    assert all(len(name) <= 96 and "=" not in name for name in durations)


def test_inactive_decorator_late_configuration_and_disable(tmp_path):
    @client.measure("loss")
    def compute():
        return 4
    assert compute() == 4
    assert client._state is None
    path = configured(tmp_path)
    assert compute() == 4
    assert read(path)[0][0]["metrics"] == {"loss": 4}
    assert client.configure()
    assert compute() == 4
    assert client._state is None


def test_failed_writer_can_recover_by_explicit_reconfigure(tmp_path):
    client.configure(path=tmp_path / "missing" / "metrics.jsonl")
    client.metric("failed", 1)
    assert not client._get_client().flush()
    path = tmp_path / "recovered.jsonl"
    assert client.configure(path=path)
    client.metric("recovered", 2)
    assert read(path)[0][0]["metrics"] == {"recovered": 2}


def test_sampling_never_replays_an_older_coalesced_value(tmp_path, monkeypatch):
    path = configured(tmp_path)
    now = [1.0]
    monkeypatch.setattr(client.time, "monotonic", lambda: now[0])
    client._report("loss", 1, sampled=True)
    now[0] += .01
    client._report("loss", 2, sampled=True)
    now[0] += .05
    client._report("loss", 3, sampled=True)
    state = client._get_client()
    assert not state.latest
    assert state.flush()
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["metrics"]["loss"] for row in records] == [1, 3]
    assert state.coalesced["loss"] == 1


def test_progress_internal_queue_key_does_not_reserve_a_metric_name(tmp_path):
    path = configured(tmp_path)
    client.metric("@progress", 4, kind="gauge")
    client.progress(1, 2)
    records, meta = read(path)
    assert any(record["metrics"] == {"@progress": 4} for record in records)
    assert any(record.get("progress", {}).get("completed") == 1 for record in records)
    assert meta["kinds"]["@progress"] == "gauge"


def test_shared_stream_name_budget_is_checked_before_new_appends(tmp_path):
    path = configured(tmp_path)
    for i in range(64):
        client.metric("original" + str(i), i)
    assert client._get_client().flush()
    before = path.read_bytes()
    assert client.configure(path=path)
    client.metric("extra", 1)
    assert client._get_client().flush()
    assert path.read_bytes() == before
    assert len(json.loads(path.with_suffix(".meta.json").read_text())["kinds"]) == 64
    client.metric("original0", 123)
    assert client._get_client().flush()
    assert MetricReader().read(path)["latest"]["original0"] == 123


@pytest.mark.parametrize("actual,environment", [(1, {"SLURM_PROCID": "0"}), (0, {"RANK": "1"})])
def test_initialized_mpi_rank_overrides_inherited_environment(tmp_path, monkeypatch, capsys, actual, environment):
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    mpi = types.SimpleNamespace(COMM_WORLD=types.SimpleNamespace(Get_rank=lambda: actual),
                                Is_initialized=lambda: True, Is_finalized=lambda: False)
    monkeypatch.setitem(sys.modules, "mpi4py.MPI", mpi)
    path = configured(tmp_path)
    client.metric("rank", actual)
    state = client._get_client()
    assert state.flush()
    assert state.rank == actual and state.rank_source == "mpi4py.MPI"
    assert capsys.readouterr().err.count("overrides conflicting") == 1
    assert path.exists() is (actual == 0)
    if actual:
        assert not list(tmp_path.iterdir())


def test_launcher_rank_precedes_outer_slurm_before_mpi_import(tmp_path, monkeypatch):
    monkeypatch.setenv("SLURM_PROCID", "0")
    monkeypatch.setenv("OMPI_COMM_WORLD_RANK", "1")
    monkeypatch.delitem(sys.modules, "mpi4py.MPI", raising=False)
    monkeypatch.delitem(sys.modules, "torch.distributed", raising=False)
    path = configured(tmp_path)
    client.metric("x", 1)
    state = client._get_client()
    assert state.rank == 1 and state.rank_source == "OMPI_COMM_WORLD_RANK"
    assert not path.exists()
    assert state.thread is None


def test_initialized_torch_rank_is_authoritative_without_imports(tmp_path, monkeypatch):
    monkeypatch.setenv("RANK", "0")
    monkeypatch.setitem(sys.modules, "torch.distributed", types.SimpleNamespace(is_initialized=lambda: True, get_rank=lambda: 2))
    path = configured(tmp_path)
    client.metric("x", 1)
    state = client._get_client()
    assert state.rank == 2 and state.rank_source == "torch.distributed"
    assert state.flush()
    assert not path.exists()


@pytest.mark.parametrize("framework", ["ray", "dask"])
def test_loaded_framework_helper_context_is_counting_only(tmp_path, monkeypatch, framework):
    if framework == "ray":
        monkeypatch.setitem(sys.modules, "ray._private.worker", types.SimpleNamespace(
            global_worker=types.SimpleNamespace(mode=1), WORKER_MODE=1))
    else:
        monkeypatch.setitem(sys.modules, "distributed.worker", types.SimpleNamespace(get_worker=lambda: object()))
    path = configured(tmp_path)
    client.metric("worker", 1)
    state = client._get_client()
    assert state.helper and state.counts["suppressed"] == 1
    assert state.thread is None and not path.exists()


def test_invalid_watch_local_name_never_affects_application(tmp_path):
    configured(tmp_path)
    def target():
        return 4
    previous = sys.getprofile()
    with client.watch(target, []):
        assert target() == 4
    assert sys.getprofile() is previous
    assert client._get_client().flush()


def test_invalid_measure_step_has_actionable_diagnostic(tmp_path, capsys):
    configured(tmp_path)
    @client.measure("x", step="missing")
    def target(value):
        return value
    assert target(3) == 3
    assert client._get_client().flush()
    assert "Measurement was not recorded" in capsys.readouterr().err


def test_spawned_distributed_rank_is_not_a_dataloader_helper(tmp_path, monkeypatch):
    monkeypatch.setattr(client, "_helper", lambda: True)
    monkeypatch.setitem(sys.modules, "torch.distributed", types.SimpleNamespace(is_initialized=lambda: True, get_rank=lambda: 0))
    path = configured(tmp_path)
    client.metric("worker", 3)
    state = client._get_client()
    assert not state.helper
    assert read(path)[0][0]["metrics"] == {"worker": 3}


def test_forked_child_cannot_reuse_inherited_initialized_communicator(tmp_path, monkeypatch):
    monkeypatch.setattr(client, "_fork_child", True)
    monkeypatch.setitem(sys.modules, "torch.distributed", types.SimpleNamespace(is_initialized=lambda: True, get_rank=lambda: 0))
    path = configured(tmp_path)
    client.metric("worker", 3, reduce="sum")
    state = client._get_client()
    assert state.helper and state.thread is None and not path.exists()


def test_time_sampling_cannot_selectively_skip_a_collective():
    with pytest.raises(ValueError, match="cannot be combined with a collective"):
        client.measure("loss", seconds=1, reduce="mean")


def test_failed_writer_still_participates_in_explicit_collectives(tmp_path, monkeypatch):
    client.configure(path=tmp_path / "missing" / "metrics.jsonl")
    client.metric("x", 1)
    state = client._get_client()
    assert not state.flush() and state.failed
    reductions = []
    def reduce_after_failure(current, value, reduction, *, valid, device=None):
        assert valid
        reductions.append((current, value, reduction))
        return value
    monkeypatch.setattr(client, "_reduce", reduce_after_failure)
    assert client.metric("x", 2, reduce="sum") == 2
    assert reductions == [(state, 2, "sum")]
    assert state.counts["suppressed"] == 1


@pytest.mark.parametrize("change", [{"value": float("nan")}, {"name": ""}, {"kind": "unknown"},
                                    {"unit": "\n"}, {"step": -1}, {"reduce": "unknown"}])
def test_invalid_local_field_still_enters_collective_readiness(tmp_path, monkeypatch, change):
    configured(tmp_path)
    calls = []
    class Communicator:
        def Get_rank(self):
            return 0
        def allreduce(self, value, op):
            calls.append((value, op))
            return value
        def reduce(self, *args, **kwargs):
            pytest.fail("invalid readiness must skip the value collective on every rank")
    monkeypatch.setitem(sys.modules, "mpi4py.MPI", types.SimpleNamespace(
        COMM_WORLD=Communicator(), MIN="min", Is_initialized=lambda: True, Is_finalized=lambda: False))
    arguments = {"name": "energy", "value": 1, "kind": "conserved", "unit": "J", "step": 1, "reduce": "sum", **change}
    original = arguments["value"]
    assert client.metric(**arguments) is original
    assert calls == [(0, "min")]
    state = client._get_client()
    assert state.flush() and state.counts["unsupported"] == 1
    assert not Path(state.path).exists()


def test_peer_invalidity_skips_value_collective_and_next_call_recovers(tmp_path, monkeypatch):
    path = configured(tmp_path)
    outcomes = iter([0, 1])
    calls = []
    class Communicator:
        def Get_rank(self):
            return 0
        def allreduce(self, value, op):
            calls.append(("ready", value))
            return next(outcomes)
        def reduce(self, value, op, root):
            calls.append(("value", value))
            return value * 2
    monkeypatch.setitem(sys.modules, "mpi4py.MPI", types.SimpleNamespace(
        COMM_WORLD=Communicator(), MIN="min", SUM="sum", Is_initialized=lambda: True, Is_finalized=lambda: False))
    assert client.metric("energy", 1, reduce="sum") == 1
    assert client.metric("energy", 2, reduce="sum") == 2
    assert calls == [("ready", 1), ("ready", 1), ("value", 2)]
    assert read(path)[0][0]["metrics"] == {"energy": 4}


def test_decorator_missing_step_attribute_enters_readiness(tmp_path, monkeypatch):
    configured(tmp_path)
    flags = []
    class Communicator:
        def Get_rank(self):
            return 0
        def allreduce(self, value, op):
            flags.append(value)
            return value
    monkeypatch.setitem(sys.modules, "mpi4py.MPI", types.SimpleNamespace(
        COMM_WORLD=Communicator(), MIN="min", Is_initialized=lambda: True, Is_finalized=lambda: False))
    @client.measure("x", step="missing", reduce="sum")
    def target(value):
        return value
    assert target(3) == 3
    assert flags == [0]
    assert client._get_client().flush()


def test_raw_fork_identity_never_queries_inherited_frameworks(tmp_path, monkeypatch):
    monkeypatch.setattr(client, "_fork_child", True)
    def forbidden():
        pytest.fail("fork child queried an inherited distributed runtime")
    monkeypatch.setitem(sys.modules, "torch.distributed", types.SimpleNamespace(is_initialized=forbidden))
    monkeypatch.setitem(sys.modules, "mpi4py.MPI", types.SimpleNamespace(Is_initialized=forbidden))
    path = configured(tmp_path)
    assert client.metric("x", 1, reduce="sum") == 1
    state = client._get_client()
    assert state.helper and state.thread is None and not path.exists()
