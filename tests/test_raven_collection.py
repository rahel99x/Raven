"""Synthetic scheduler admission: no live Slurm commands or allocations."""
from __future__ import annotations

from dataclasses import replace
import threading
from types import SimpleNamespace

import pytest

from raven.config import Config
from raven.model import Job, Store
from raven.platform_session import wrap_backend
from raven.raven_collection import (DEFAULTS, Deferred, Descriptor, Ledger, classify,
                                    admit_allocation_launch, admit_terminal_launch,
                                    expand_hostlist, make_placement_verifier, settings, wrap_files)
from raven.shared_collection import SharedQueryCache
from raven.slurm import CommandError, Slurm


class Timer:
    value = 1000.

    def __call__(self):
        return self.value


class Recorder:
    def __init__(self):
        self.commands = []
        self.response = "1|RUNNING\n"
        self.error = None

    def run(self, argv, timeout=8):
        self.commands.append(tuple(argv))
        if self.error:
            raise CommandError(self.error)
        return self.response, .001

    def call(self, argv, timeout=8):
        self.commands.append(tuple(argv))
        return self.error is None, self.error or "ok"


def backend(tmp_path, **settings):
    raw = Recorder()
    cfg = {"raven": {"collection": settings}, "collection_cache_dir": str(tmp_path / "cache")}
    return raw, wrap_backend(raw, cfg, {"cluster": "test", "user": "researcher"}, None)


def ledger(tmp_path, timer=None, **config):
    cache = SharedQueryCache(tmp_path / "ledger")
    return Ledger(cache, {"cluster": "test", "user": "researcher"}, config, now=timer or Timer())


def test_legacy_cli_sixty_second_interval_stays_sixty_seconds():
    from raven import cli
    session = cli.build(cli.parse(["--fake", "--no-state", "--no-plugins", "--interval", "60"]), Config())
    try:
        assert session.sampler.effective_interval("jobs") == 60
        from raven.refresh_rate import candidate_cadence
        assert candidate_cadence(session.app, "jobs", 1) == 60
    finally:
        session.close()


@pytest.mark.parametrize("argv,endpoint", [
    (["squeue", "-u", "me"], "controller"), (["sacct", "-u", "me"], "accounting"),
    (["sstat", "-j", "1,2"], "statistics"), (["sinfo", "-N"], "controller"),
    (["sshare", "-n"], "controller"), (["sreport", "cluster"], "accounting"),
    (["sacctmgr", "-n", "show", "assoc"], "accounting"),
    (["scontrol", "show", "job", "7"], "controller"),
    (["scontrol", "show", "hostnames", "node[1-2]"], "local"),
    (["sbatch", "--test-only", "job.sh"], "probes"), (["srun", "--jobid=7", "true"], "steps"),
    (["salloc", "-n", "1"], "controller"), (["scancel", "7"], "controller"),
])
def test_all_native_command_families_have_explicit_endpoint_descriptors(argv, endpoint):
    assert classify(argv).endpoint == endpoint


def test_managed_unknown_reads_and_unsolicited_steps_fail_before_spawn(tmp_path):
    raw, governed = backend(tmp_path)
    for argv in (["unregistered-collector"], ["sh", "-c", "squeue; sstat -a"],
                 ["srun", "--jobid=1", "nvidia-smi"], ["sbatch", "--test-only", "x.sh"]):
        with pytest.raises(CommandError):
            governed.run(argv)
    assert not raw.commands


def test_allocation_launch_debits_controller_and_step_without_holding_worker_slot(tmp_path):
    raw, governed = backend(tmp_path)
    plan = {"site_kind": "slurm", "ownership": "dedicated-allocation",
            "prefix": ["salloc", "--nodes=1", "srun", "--ntasks=1"]}
    receipt = admit_allocation_launch(governed, plan)
    report = governed.ledger.report()
    assert [item["endpoint"] for item in receipt["requests"]] == ["controller", "steps"]
    assert report["rates_last_minute"]["controller"] == 1
    assert report["rates_last_minute"]["steps"] == 1
    assert report["rates_last_minute"]["process"] == 2
    assert report["inflight"] == 0
    assert raw.commands == []
    with pytest.raises(Deferred, match="step launch cooldown"):
        admit_allocation_launch(governed, {"site_kind": "slurm", "ownership": "owned-step-only", "prefix": ["srun", "--jobid=7"]})
    assert governed.ledger.report()["inflight"] == 0


def test_allocation_launch_fails_closed_and_desktop_has_no_scheduler_debit(tmp_path):
    _, governed = backend(tmp_path, slurm_enabled=False)
    with pytest.raises(Deferred, match="disabled"):
        admit_allocation_launch(governed, {"site_kind": "slurm", "ownership": "owned-step-only", "prefix": ["srun", "--jobid=7"]})
    with pytest.raises(ValueError, match="prefix"):
        admit_allocation_launch(governed, {"site_kind": "desktop", "ownership": "local-worker", "prefix": ["srun"]})
    assert governed.ledger.report()["rates_last_minute"] == {}
    admit_allocation_launch(governed, {"site_kind": "desktop", "ownership": "local-worker", "prefix": []})
    assert governed.ledger.report()["rates_last_minute"] == {"local": 1, "process": 1}


def test_reviewed_terminal_launch_uses_persisted_step_allowance(tmp_path):
    raw, governed = backend(tmp_path)
    with pytest.raises(CommandError, match="explicit reviewed action"):
        governed.run(["sattach", "123.0"])
    with pytest.raises(ValueError, match="exact reviewed"):
        admit_terminal_launch(governed, ["sh", "-c", "srun --pty bash"])
    assert not governed.ledger.report()["rates_last_minute"]
    receipt = admit_terminal_launch(governed, ["sattach", "123.0"])
    assert receipt["endpoint"] == "steps"
    assert governed.ledger.report()["inflight"] == 0
    with pytest.raises(Deferred, match="step launch cooldown"):
        admit_terminal_launch(governed, ["srun", "--jobid=123", "--pty", "bash"])
    assert not raw.commands


@pytest.mark.parametrize("validation", [False, True])
def test_step_cooldown_is_ten_seconds_across_commands_wrappers_and_profiles(tmp_path, validation):
    timer = Timer()
    first = ledger(tmp_path, timer)
    argv = ["srun", "--jobid=7", "--pty", "bash"]
    with first.admit(classify(argv), argv, validation=validation):
        pass
    second = Ledger(first.cache, {"cluster": "test", "user": "researcher", "profile": "Heavy", "mode": "heavy"}, first.config, now=timer)
    other = ["sattach", "8.0"]
    timer.value = 1009.5
    with pytest.raises(Deferred, match="step launch cooldown.*0.50 seconds") as denied:
        with second.admit(classify(other), other, validation=validation):
            pytest.fail("A new command or profile must not bypass the step cooldown")
    assert denied.value.retry_after == .5
    assert first.report()["rates_last_minute"]["steps"] == 1
    timer.value = 1010.
    with second.admit(classify(other), other, validation=validation):
        pass
    timer.value = 1020.
    with second.admit(classify(other), other, validation=validation):
        pass
    assert first.report()["rates_last_minute"]["steps"] == 3


def test_failed_step_attempt_keeps_the_cooldown_and_rolling_debit(tmp_path):
    timer = Timer()
    owner = ledger(tmp_path, timer)
    argv = ["srun", "--jobid=7"]
    with pytest.raises(OSError, match="worker unavailable"):
        with owner.admit(classify(argv), argv, validation=True):
            raise OSError("worker unavailable")
    timer.value = 1009.999
    other = ["srun", "--jobid=8"]
    with pytest.raises(Deferred, match="step launch cooldown"):
        with owner.admit(classify(other), other, validation=True):
            pytest.fail("Failed attempts must retain admission debits")
    timer.value = 1010.
    with owner.admit(classify(other), other, validation=True):
        pass
    assert owner.report()["rates_last_minute"]["steps"] == 2


def test_step_minute_quota_remains_independent_from_spacing(tmp_path):
    timer = Timer()
    owner = ledger(tmp_path, timer, step_launch_interval_seconds=.1)
    for index in range(6):
        timer.value = 1000. + index
        argv = ["srun", "--jobid=" + str(index)]
        with owner.admit(classify(argv), argv, validation=True):
            pass
    timer.value = 1015.
    argv = ["srun", "--jobid=7"]
    with pytest.raises(Deferred, match="rolling steps budget"):
        with owner.admit(classify(argv), argv, validation=True):
            pytest.fail("Spacing cannot bypass the rolling six-step allowance")
    timer.value = 1060.
    with owner.admit(classify(argv), argv, validation=True):
        pass
    assert owner.report()["rates_last_minute"]["steps"] == 6


def test_explicit_stricter_step_quota_is_preserved(tmp_path):
    timer = Timer()
    owner = ledger(tmp_path, timer, step_launches_per_minute=1)
    argv = ["srun", "--jobid=7"]
    with owner.admit(classify(argv), argv, validation=True):
        pass
    timer.value = 1010.
    with pytest.raises(Deferred, match="rolling steps budget"):
        with owner.admit(classify(argv), argv, validation=True):
            pytest.fail("A strict user or site quota still applies after the cooldown")
    timer.value = 1060.
    with owner.admit(classify(argv), argv, validation=True):
        pass


def test_step_cooldown_longer_than_minute_survives_request_pruning(tmp_path):
    timer = Timer()
    owner = ledger(tmp_path, timer, step_launch_interval_seconds=120.)
    argv = ["srun", "--jobid=7"]
    with owner.admit(classify(argv), argv, validation=True):
        pass
    timer.value = 1061.
    # An unrelated endpoint can prune the old rolling request records.
    queue = ["squeue"]
    with owner.admit(classify(queue), queue):
        pass
    assert "steps" not in owner.report()["rates_last_minute"]
    second = Ledger(owner.cache, {"cluster": "test", "user": "researcher", "profile": "other"}, owner.config, now=timer)
    with pytest.raises(Deferred, match="step launch cooldown") as denied:
        with second.admit(classify(argv), argv, validation=True):
            pytest.fail("Pruning must not lower a longer configured or mandatory cooldown")
    assert denied.value.retry_after == 59.
    timer.value = 1120.
    with second.admit(classify(argv), argv, validation=True):
        pass


def test_step_cooldown_upgrade_preserves_legacy_debits_before_pruning(tmp_path):
    from raven import evidence as E
    timer = Timer()
    timer.value = 1061.
    cache = SharedQueryCache(tmp_path / "legacy-ledger")
    scope = {"cluster": "test", "user": "researcher"}
    identity = E.digest(scope)
    with cache._connection() as db:
        db.executescript("""
            CREATE TABLE raven_requests (scope TEXT, endpoint TEXT, started REAL);
            CREATE TABLE raven_endpoints (scope TEXT, endpoint TEXT, failures INTEGER, until REAL, PRIMARY KEY(scope,endpoint));
        """)
        db.execute("INSERT INTO raven_requests VALUES (?,?,?)", (identity, "steps", 1000.))
        db.execute("INSERT INTO raven_endpoints VALUES (?,?,?,?)", (identity, "steps", 2, 0.))
    owner = Ledger(cache, scope, {"step_launch_interval_seconds": 120.}, now=timer)
    with cache._connection() as db:
        assert db.execute("SELECT failures,last_launch FROM raven_endpoints WHERE scope=? AND endpoint='steps'", (identity,)).fetchone() == (2, 1000.)
        assert "cost" in {row[1] for row in db.execute("PRAGMA table_info(raven_requests)")}
    argv = ["srun", "--jobid=7"]
    with pytest.raises(Deferred, match="step launch cooldown"):
        with owner.admit(classify(argv), argv, validation=True):
            pytest.fail("An upgrade must not erase a recent launch under a longer site floor")
    timer.value = 1120.
    with owner.admit(classify(argv), argv, validation=True):
        pass


def test_simultaneous_step_owners_share_one_atomic_cooldown(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    timer = Timer()
    first = ledger(tmp_path, timer)
    second = Ledger(first.cache, {"cluster": "test", "user": "researcher", "profile": "other"}, first.config, now=timer)
    ready = threading.Barrier(2)

    def launch(owner, job):
        argv = ["srun", "--jobid=" + job]
        ready.wait()
        try:
            with owner.admit(classify(argv), argv, validation=True):
                return "admitted"
        except Deferred as exc:
            assert "step launch cooldown" in str(exc)
            return "deferred"

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = [pool.submit(launch, first, "7"), pool.submit(launch, second, "8")]
        assert sorted(item.result() for item in attempts) == ["admitted", "deferred"]
    assert first.report()["rates_last_minute"]["steps"] == 1
    assert first.report()["inflight"] == 0


def test_simultaneous_ledger_upgrade_adds_step_timestamp_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    cache = SharedQueryCache(tmp_path / "legacy-ledger")
    with cache._connection() as db:
        db.executescript("""
            CREATE TABLE raven_requests (scope TEXT, endpoint TEXT, started REAL);
            CREATE TABLE raven_endpoints (scope TEXT, endpoint TEXT, failures INTEGER, until REAL, PRIMARY KEY(scope,endpoint));
        """)
    ready = threading.Barrier(2)

    def upgrade(profile):
        ready.wait()
        return Ledger(cache, {"cluster": "test", "user": "researcher", "profile": profile}, {}, now=Timer())

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = [pool.submit(upgrade, name) for name in ("one", "two")]
        owners = [item.result() for item in attempts]
    assert owners[0].scope == owners[1].scope
    with cache._connection() as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(raven_endpoints)")]
        assert columns.count("last_launch") == 1


def test_step_cooldown_does_not_add_a_probe_interval(tmp_path):
    owner = ledger(tmp_path)
    step = ["srun", "--jobid=7"]
    with owner.admit(classify(step), step, validation=True):
        pass
    for name in ("one.sh", "two.sh"):
        argv = ["sbatch", "--test-only", name]
        with owner.admit(classify(argv), argv, validation=True):
            pass
    assert owner.report()["rates_last_minute"]["probes"] == 2


@pytest.mark.parametrize("value", [0, .05, float("nan"), float("inf"), True, "10"])
def test_invalid_step_cooldown_is_rejected(value):
    with pytest.raises(ValueError, match="step_launch_interval_seconds"):
        settings({"step_launch_interval_seconds": value})


def test_fractional_step_cooldown_is_supported():
    assert settings({"step_launch_interval_seconds": .5})["step_launch_interval_seconds"] == .5


def test_exact_submission_wrapper_classifies_but_shell_injection_does_not():
    import shlex
    command = "cd " + shlex.quote("/a project") + " && " + shlex.join(["sbatch", "a job.sh"])
    assert classify(["sh", "-c", command]).family == "submit"
    assert classify(["sh", "-c", command + "; squeue"]) is None
    assert classify(["sacctmgr", "modify", "user"]) is None


def test_cached_equivalent_reads_consume_one_actual_command_allowance(tmp_path):
    raw, governed = backend(tmp_path)
    assert governed.run(["squeue", "-u", "me"])[0] == "1|RUNNING\n"
    assert governed.run(["squeue", "-u", "me"])[0] == "1|RUNNING\n"
    assert len(raw.commands) == 1
    assert governed.ledger.report()["rates_last_minute"]["controller"] == 1
    governed.invalidate()
    with pytest.raises(CommandError, match="source floor"):
        governed.run(["squeue", "-u", "me"])
    assert len(raw.commands) == 1


def test_simulations_have_independent_ephemeral_caches_and_do_not_fill_live_owner(tmp_path, monkeypatch):
    from raven.shared_collection import ProcessQueryCache
    from raven.slurm import FakeBackend
    raw, real = backend(tmp_path)
    real.run(["squeue"])
    def no_disk_cache(*args, **kwargs):
        pytest.fail("Synthetic observations must not create filesystem caches")
    monkeypatch.setattr("raven.platform_session.SharedQueryCache", no_disk_cache)
    cfg = {"raven": {"collection": {}}, "collection_cache_dir": str(tmp_path / "cache")}
    simulations = [wrap_backend(FakeBackend("test"), cfg, {"user": "test", "backend": "demo"}, None) for _ in range(3)]
    paths = [item.cache.path for item in simulations]
    try:
        assert all(isinstance(item.cache, ProcessQueryCache) for item in simulations)
        assert len(set(paths)) == 3 and all(path != real.cache.path for path in paths)
        assert all(Slurm(item, "test").jobs() for item in simulations)
        assert real.ledger.report()["rates_last_minute"]["controller"] == 1
    finally:
        for item in simulations:
            item.close()
    assert all(item.cache._database is None for item in simulations)
    assert all(item.acquisition.stats()["closed"] for item in simulations)
    assert real.cache.path.exists()


@pytest.mark.parametrize("fresh", [False, True])
def test_simulation_shutdown_rejects_late_writes_and_new_reads(tmp_path, fresh):
    from concurrent.futures import ThreadPoolExecutor
    from raven.slurm import FakeBackend
    entered, release = threading.Event(), threading.Event()

    class Blocking(FakeBackend):
        def run(self, argv, timeout=8):
            entered.set()
            assert release.wait(5)
            return "1|RUNNING\n", .001

    cfg = {"raven": {"collection": {}}, "collection_cache_dir": str(tmp_path / "unused")}
    governed = wrap_backend(Blocking("test"), cfg, {"user": "test", "backend": "demo"}, None)
    read = lambda: governed._query(["squeue"], 8, 65536, None, fresh=fresh)
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(read)
        try:
            assert entered.wait(5)
            governed.close()
            assert governed.cache._database is None
            assert governed.acquisition.stats()["closed"]
            with pytest.raises(CommandError, match="closed"):
                read()
        finally:
            release.set()
        with pytest.raises(CommandError, match="closed|cancel"):
            pending.result(5)
    assert governed.acquisition.stats()["active"] == 0
    assert not list(tmp_path.iterdir())


def test_completed_cache_values_evict_under_pressure_without_resetting_source_quotas(tmp_path):
    raw, governed = backend(tmp_path)
    governed.cache.capacity = 1
    first, second = ["squeue", "-j", "1"], ["squeue", "-j", "2"]
    governed.run(first)
    governed.run(second)
    with governed.cache._connection() as db:
        assert db.execute("SELECT count(*) FROM cache").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM raven_sources").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM throttle").fetchone()[0] == 1
    assert governed.ledger.report()["rates_last_minute"]["controller"] == 2
    with pytest.raises(Deferred, match="source floor"):
        with governed.ledger.admit(classify(first), first):
            pytest.fail("Value eviction must not reset admission")


@pytest.mark.parametrize("target", ["12480001", "12477369"])
@pytest.mark.parametrize("availability", ["available", "unavailable", "empty", "stale", "invalid", "zero"])
def test_advised_cli_resubmit_demands_exact_running_job_and_never_silently_loses_advice(
        tmp_path, monkeypatch, capsys, target, availability):
    from raven import cli
    from raven.raven_config import resolve, to_raven_config
    from raven.slurm import FakeBackend

    value = resolve(site="slurm", system_policy_paths=[], env={
        "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    })
    calls, original = [], FakeBackend.run

    def record(self, argv, timeout=8):
        calls.append(list(argv))
        if argv[0] == "sstat" and availability == "unavailable":
            raise CommandError("statistics unavailable for this job")
        if argv[0] == "sstat" and availability == "empty":
            return "", .001
        return original(self, argv, timeout)

    monkeypatch.setattr(FakeBackend, "run", record)
    original_result = Slurm._live_result

    def live_result(*args):
        result = original_result(*args)
        if availability == "stale":
            result[0].t -= 3600
        elif availability == "zero":
            result[0].cpu_time = result[0].rss = result[0].avg = result[0].rate = 0.
        return result

    monkeypatch.setattr(Slurm, "_live_result", staticmethod(live_result))
    if availability == "invalid":
        original_settle = cli.settle

        def invalid_after_settle(session, *args, **kwargs):
            original_settle(session, *args, **kwargs)
            # Exercise the final submission boundary against corrupt evidence,
            # after the normal acquisition and selection render have completed.
            live = session.store.live[target]
            live.cpu_time = live.rss = float("nan")
            live.avg = live.rate = float("inf")

        monkeypatch.setattr(cli, "settle", invalid_after_settle)
    args = cli.parse(["--fake", "--no-state", "--no-plugins", "run", "resubmit", target, "--advised", "--yes"])
    session = cli.build(args, Config(to_raven_config(value)))
    try:
        code = cli.scripted(args, session)
        output = capsys.readouterr().out
        statistics = [argv for argv in calls if argv[0] == "sstat"]
        assert statistics and all(argv[argv.index("-j") + 1] == target for argv in statistics)
        if availability in ("available", "zero"):
            assert code == 0 and f"resubmitted {target}" in output
            assert "--mem=" in output if availability == "available" else "--cpus-per-task=1" in output
            assert session.store.snapshot()["live"].get(target) is not None
        else:
            assert code == 1 and f"current live advice is unavailable for {target}" in output
            assert "no submission was sent" in output
            assert not any("sbatch" in " ".join(argv) for argv in calls)
    finally:
        session.close()


def test_fresh_validation_observes_post_review_data_and_reserved_quota(tmp_path):
    raw, governed = backend(tmp_path, action_validation_requests_per_minute=2)
    governed.run(["squeue", "-u", "me"])
    raw.response = "2|PENDING\n"
    assert governed.command(["squeue", "-u", "me"], 2, 4096, None) == raw.response
    raw.response = "3|FAILED\n"
    assert governed.command(["squeue", "-u", "me"], 2, 4096, None) == raw.response
    with pytest.raises(CommandError, match="validation budget"):
        governed.command(["squeue", "-u", "me"], 2, 4096, None)
    assert len(raw.commands) == 3


def test_rolling_quota_survives_new_wrapper_profiles_and_refresh(tmp_path):
    timer = Timer()
    first = ledger(tmp_path, timer, controller_requests_per_minute=3, controller_burst=20)
    d = classify(["squeue"])
    for index in range(3):
        with first.admit(d, ["squeue", "-j", str(index)]):
            pass
    second = Ledger(first.cache, {"cluster": "test", "user": "researcher", "profile": "Heavy"}, first.config, now=timer)
    with pytest.raises(Deferred, match="rolling controller"):
        with second.admit(d, ["squeue", "-j", "new"]):
            pass
    timer.value += 60.001
    with second.admit(d, ["squeue", "-j", "new"]):
        pass
    assert second.report()["rates_last_minute"]["controller"] == 1


def test_failed_attempts_consume_allowance_and_open_endpoint_circuit(tmp_path):
    timer = Timer()
    owner = ledger(tmp_path, timer, controller_burst=20)
    d = classify(["squeue"])
    for index in range(3):
        with pytest.raises(OSError):
            with owner.admit(d, ["squeue", "-j", str(index)]):
                raise OSError("controller offline")
    assert owner.report()["rates_last_minute"]["controller"] == 3
    with pytest.raises(Deferred, match="endpoint circuit"):
        with owner.admit(d, ["squeue", "-j", "different"]):
            pass
    # A bounded fresh validation can still check current state for cancellation.
    with owner.admit(d, ["squeue", "-j", "review"], validation=True):
        pass


def test_cancelled_producer_retains_permit_until_backend_really_drains(tmp_path):
    owner = ledger(tmp_path)
    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def producer():
        try:
            with owner.admit(classify(["squeue"]), ["squeue"]):
                entered.set()
                assert release.wait(3)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=producer)
    worker.start()
    try:
        assert entered.wait(2)
        cancelled.set()
        with pytest.raises(Deferred, match="lane"):
            with owner.admit(classify(["sinfo"]), ["sinfo"]):
                pass
        with owner.admit(classify(["scontrol", "show", "job", "1"]), ["scontrol", "show", "job", "1"], validation=True):
            assert owner.report()["inflight"] == 2
            with pytest.raises(Deferred, match="lane"):
                with owner.admit(classify(["scontrol", "show", "job", "2"]), ["scontrol", "show", "job", "2"], validation=True):
                    pass
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and not errors and owner.report()["inflight"] == 0


@pytest.mark.parametrize("clients", [1, 10, 50, 200])
def test_synthetic_sessions_per_user_startup_and_refresh_flood_has_bounded_commands(tmp_path, clients):
    timer = Timer()
    owner = ledger(tmp_path, timer)
    accepted = 0
    command_log = []
    d = classify(["squeue"])
    # All sessions for this user/host/connection share one rolling ledger.
    for tick in range(120):
        timer.value = 1000. + tick / 2
        for client in range(clients):
            try:
                with owner.admit(d, ["squeue", "-j", str(client)]):
                    accepted += 1
                    command_log.append((timer.value, client))
            except Deferred:
                pass
    assert accepted <= DEFAULTS["controller_requests_per_minute"]
    assert len(command_log) == owner.report()["rates_last_minute"].get("controller", 0)


def test_host_local_cache_rejects_network_mount(tmp_path, monkeypatch):
    monkeypatch.setattr("raven.shared_collection._network_mount", lambda path: True)
    with pytest.raises(ValueError, match="host-local"):
        backend(tmp_path)


def test_file_metadata_and_byte_budgets_remain_separate_from_controller(tmp_path):
    from raven.remote import LocalFiles
    raw, governed = backend(tmp_path, log_file_bytes_per_minute=8, filesystem_metadata_per_minute=2)
    files = wrap_files(LocalFiles(), governed)
    path = tmp_path / "data.log"
    path.write_bytes(b"0123456789")
    assert files.read(str(path), 0, 8) == b"01234567"
    with pytest.raises(OSError, match="log-bytes budget"):
        files.read(str(path), 8, 1)
    assert files.stat(path)[0] == 10
    assert files.stat(path)[0] == 10
    with pytest.raises(OSError, match="filesystem budget"):
        files.stat(path)
    assert not raw.commands
    assert "process" not in governed.ledger.report()["rates_last_minute"]


@pytest.mark.parametrize("text,expected", [("n[001-003,007]", ["n001", "n002", "n003", "n007"]),
    ("a[1-2]b[3-4],last", ["a1b3", "a1b4", "a2b3", "a2b4", "last"]), ("node", ["node"])])
def test_bounded_hostlist_expansion_never_starts_a_scheduler_process(text, expected):
    assert expand_hostlist(text) == expected


@pytest.mark.parametrize("text", ["n[1-999999]", "n[4-2]", "n[$(id)]", "n[1-99]x[1-99]", "", "n[1-2"])
def test_invalid_or_explosive_hostlists_refused(text):
    with pytest.raises(ValueError):
        expand_hostlist(text)


def test_placement_verifier_uses_two_fresh_controller_reads_and_no_hostnames_command(tmp_path):
    raw, governed = backend(tmp_path)
    def run(argv, timeout=8):
        raw.commands.append(tuple(argv))
        return ("JobId=7 NodeList=n[1-2]" if argv[2] == "job" else "StepId=7.0 NodeList=n2 Nodes=1"), .001
    raw.run = run
    value = make_placement_verifier(governed)({"jobid": "7", "stepid": "0", "probe_id": "nonce", "cgroup_proof": True})
    assert value["job_nodes"] == ["n1", "n2"] and value["step_nodes"] == ["n2"]
    assert value["probe_id"] == "nonce" and len(raw.commands) == 2
    assert governed.ledger.report()["rates_last_minute"]["validation"] == 2


@pytest.mark.parametrize("nodelist,count,nodes", [
    ("system", "1", ["system"]),
    ("n[01-02]", "2", ["n01", "n02"]),
])
def test_placement_verifier_prefers_step_hostlist_over_slurm_node_count(tmp_path, nodelist, count, nodes):
    raw, governed = backend(tmp_path)
    def run(argv, timeout=8):
        raw.commands.append(tuple(argv))
        return ("JobId=7 NodeList=" + nodelist if argv[2] == "job" else
                "StepId=7.0 State=RUNNING NodeList=" + nodelist + " Nodes=" + count + " CPUs=2 Tasks=1"), .001
    raw.run = run
    value = make_placement_verifier(governed)({"job_id": "7", "step_id": "0", "probe_id": "launch", "cgroup_proof": True})
    assert value["job_nodes"] == value["step_nodes"] == nodes
    assert value["probe_id"] == "launch"
    assert raw.commands == [("scontrol", "show", "job", "7", "-o"),
                            ("scontrol", "show", "step", "7.0", "-o")]
    assert governed.ledger.report()["rates_last_minute"]["validation"] == 2


def test_placement_verifier_retains_legacy_adapter_hostlist_fallback(tmp_path):
    raw, governed = backend(tmp_path)
    def run(argv, timeout=8):
        raw.commands.append(tuple(argv))
        return ("JobId=7 NodeList=system" if argv[2] == "job" else "StepId=7.0 Nodes=system"), .001
    raw.run = run
    value = make_placement_verifier(governed)({"job_id": "7", "step_id": "0", "probe_id": "launch", "cgroup_proof": True})
    assert value["job_nodes"] == value["step_nodes"] == ["system"]


@pytest.mark.parametrize("nodelist", ["", "(null)", "n[1-2"])
def test_invalid_step_hostlist_is_not_masked_by_legacy_adapter_field(tmp_path, nodelist):
    raw, governed = backend(tmp_path)
    def run(argv, timeout=8):
        raw.commands.append(tuple(argv))
        return ("JobId=7 NodeList=system" if argv[2] == "job" else
                "StepId=7.0 NodeList=" + nodelist + " Nodes=system"), .001
    raw.run = run
    with pytest.raises(ValueError):
        make_placement_verifier(governed)({"job_id": "7", "step_id": "0", "probe_id": "launch", "cgroup_proof": True})
    assert len(raw.commands) == 2


def test_remote_effective_uid_read_is_exact_and_debits_no_scheduler_rpc(tmp_path):
    raw, governed = backend(tmp_path, slurm_enabled=False)
    raw.response = "2345\n"
    assert governed.command(["id", "-u"], 2, 64, None) == "2345\n"
    assert governed.ledger.report()["rates_last_minute"] == {"local": 1, "process": 1}
    for argv in (["id"], ["id", "-u", "someone"], ["id", "--user"], ["sh", "-c", "id -u"]):
        assert classify(argv) is None


@pytest.mark.parametrize("step", ["batch", "extern"])
def test_only_instrumentation_factory_accepts_special_job_local_steps(tmp_path, step):
    raw, governed = backend(tmp_path)
    def run(argv, timeout=8):
        raw.commands.append(tuple(argv))
        return ("JobId=7 NodeList=n1" if argv[2] == "job" else "StepId=7." + step + " NodeList=n1 Nodes=1"), .001
    raw.run = run
    candidate = {"jobid": "7", "stepid": step, "purpose": "instrumentation", "cgroup_proof": True}
    with pytest.raises(ValueError, match="unambiguous"):
        make_placement_verifier(governed)(candidate)
    assert not raw.commands
    value = make_placement_verifier(governed, purpose="instrumentation")(candidate)
    assert value["job_nodes"] == value["step_nodes"] == ["n1"]
    assert raw.commands[-1][3] == "7." + step
    assert len(raw.commands) == 2


def test_batched_sstat_preserves_exact_array_members_and_missing_coverage():
    raw = Recorder()
    raw.response = "7_1.batch|00:01:00|1G\n7_10.batch|00:02:00|2G\n999.batch|01:00:00|9G\n"
    slurm = Slurm(raw, "test")
    jobs = [Job(jid, "job", "cpu", "RUNNING", cpus=2, elapsed="00:10:00") for jid in ("7_1", "7_10", "8")]
    result = slurm.live_batch(jobs, {}, 1000.)
    assert len(raw.commands) == 1 and raw.commands[0][2] == "7_1,7_10,8"
    assert result["7_1"][0].cpu_time == 60 and result["7_10"][0].cpu_time == 120
    assert result["8"][0].cpu_time is None


def test_managed_node_sampling_batches_and_retains_only_current_exact_hosts():
    from raven.sampler import Sampler
    raw = Recorder()
    def run(argv, timeout=8):
        raw.commands.append(tuple(argv))
        return "\n".join("NodeName=" + name + " CPUTot=8" for name in [*argv[3].split(","), "unrequested"]), .001
    raw.run = run
    store = Store(persist=False)
    store.jobs = [Job("7", "job", "cpu", "RUNNING", hosts=["n" + str(i) for i in range(35)])]
    sampler = Sampler(Slurm(raw, "test"), store, {}, [], collection={})
    try:
        for _ in range(3):
            sampler.src_nodes()
        assert len(raw.commands) == 3
        assert all(len(command[3].split(",")) == 16 for command in raw.commands)
        assert set(store.nodes) == set(store.jobs[0].hosts)
        store.jobs = []
        sampler.src_nodes()
        assert store.nodes == {} and len(raw.commands) == 3
    finally:
        sampler.shutdown()


def test_managed_sampler_floors_refresh_demand_and_unsolicited_probes(tmp_path):
    from raven.sampler import Sampler
    raw = Recorder()
    store = Store(persist=False)
    sampler = Sampler(Slurm(raw, "test"), store, {"jobs": 2, "live": 5}, [], collection={"jitter_fraction": 0})
    try:
        assert sampler.effective_interval("jobs") == 60
        sampler.set_polling_multiplier(50)
        assert sampler.effective_interval("jobs") == 60
        sampler._collection_not_before["jobs"] = 10000.
        sampler.last_run["jobs"] = 5000.
        sampler.health("jobs").backoff = 120
        sampler.refresh_all()
        assert sampler.last_run["jobs"] == 5000 and sampler.health("jobs").backoff == 120
        assert not sampler.due("jobs", 9999.)
        sampler.src_gpu(); sampler.src_weather(); sampler.src_live()
        assert not raw.commands
        sampler.set_collection_demand("analytics", "7", active=True)
        assert sampler._collection_needed("live")
        assert sampler.effective_interval("live") == 60
        sampler._collection_activity -= 301
        assert sampler.effective_interval("live") == 300
        sampler.set_collection_demand("jobs", "7")
        assert not sampler._collection_needed("live")
    finally:
        sampler.shutdown()


def test_governed_file_adapter_preserves_confined_local_and_real_remote_project_readers(tmp_path):
    from test_project_remote import IsolatedSSH
    from raven import projects, project_remote
    from raven.remote import LocalFiles, RemoteFiles
    raw, governed = backend(tmp_path)
    root = tmp_path / "project"
    root.mkdir()
    local = wrap_files(LocalFiles(), governed)
    assert projects.discover_project(str(root), files=local)["status"] == "ready"
    remote = wrap_files(RemoteFiles(IsolatedSSH()), governed)
    assert project_remote.supported(remote)
    result = projects.discover_project(str(root), files=remote)
    assert result["status"] == "ready" and result["root"] == str(root)
    assert project_remote.identity(remote) == project_remote.identity(remote.inner)
    assert 0 < governed.ledger.report()["rates_last_minute"]["file-bytes"] < 2 << 20


def test_governed_remote_catalog_keeps_manifest_symlink_rejection(tmp_path):
    from raven import log_catalog
    from raven.remote import RemoteFiles
    _, governed = backend(tmp_path)
    class LinkStat:
        def run(self, argv, timeout):
            return "a1ff 1 2 10 100 100", .01
    files = wrap_files(RemoteFiles(LinkStat()), governed)
    with pytest.raises(OSError, match="symlink"):
        log_catalog._remote_signature(files, "/remote/manifest.json")


def test_explicit_slow_metric_source_is_preserved_without_an_override():
    from raven.metric_sampling import interval
    assert interval(60, 1, 1, source="live") == 60
    assert interval(60, 50, 1, source="live") == 6
    assert interval(60, 1, 100, source="live") == .5


def test_unchanged_collection_demand_does_not_wake_background_scheduler():
    from raven.sampler import Sampler
    sampler = Sampler(Slurm(Recorder(), "test"), Store(persist=False), {"jobs": 60}, [], collection={})
    try:
        sampler.set_collection_demand("analytics", "7")
        sampler.kick.clear()
        sampler.set_collection_demand("analytics", "7")
        assert not sampler.kick.is_set()
        sampler.set_collection_demand("analytics", "7", active=True)
        assert sampler.kick.is_set()
    finally:
        sampler.shutdown()


@pytest.mark.parametrize("mode", ["off", "existing-telemetry"])
def test_managed_gpu_setting_controls_existing_trace_reads(mode):
    from raven.sampler import Sampler
    reads = []
    files = SimpleNamespace(exists=lambda path: reads.append(path) or False)
    raw, store = Recorder(), Store(persist=False)
    store.jobs = [Job("7", "gpu-job", "local", "RUNNING", gpus=1, workdir="/work")]
    sampler = Sampler(Slurm(raw, "test"), store, {}, [], files=files,
                      collection={"gpu": mode})
    try:
        sampler.select_trace("7")
        sampler.set_collection_demand("analytics", "7")
        sampler.src_trace()
        assert reads == ([] if mode == "off" else ["/work/logs/gpu-util-7.csv"])
        assert not raw.commands
    finally:
        sampler.shutdown()


def test_managed_initial_and_periodic_jitter_never_schedule_below_source_floor(monkeypatch):
    from raven.sampler import Sampler
    timer = Timer()
    monkeypatch.setattr("raven.sampler.time.time", timer)
    monkeypatch.setattr("random.SystemRandom.uniform", lambda self, first, last: last)
    raw = Recorder()
    raw.response = ""  # A successful empty queue, not a malformed two-column record.
    sampler = Sampler(Slurm(raw, "test"), Store(persist=False), {"jobs": 2}, [], collection={})
    try:
        assert not sampler.due("jobs", 1000.)
        assert sampler.due("jobs", 1012.)
        timer.value = 1012.
        sampler.run_source("jobs")
        assert sampler._collection_not_before["jobs"] == 1084.
        sampler.refresh_all()
        assert not sampler.due("jobs", 1083.)
    finally:
        sampler.shutdown()


def test_explicit_snapshot_primes_only_first_visible_attempts(monkeypatch):
    from raven.sampler import Sampler
    timer = Timer()
    monkeypatch.setattr("raven.sampler.time.time", timer)
    monkeypatch.setattr("random.SystemRandom.uniform", lambda self, first, last: last)
    raw = Recorder()
    raw.response = ""  # A successful empty queue, not a malformed two-column record.
    sampler = Sampler(Slurm(raw, "test"), Store(persist=False), {"jobs": 60}, [], collection={})
    try:
        sampler.prime_collection_snapshot()
        assert sampler.due("jobs", 1000.)
        assert sampler._collection_not_before["nodes"] == 1060.
        assert not sampler._collection_needed("details")
        sampler.run_source("jobs")
        deadline = sampler._collection_not_before["jobs"]
        sampler.prime_collection_snapshot()
        assert sampler._collection_not_before["jobs"] == deadline == 1072.
        assert not sampler.due("jobs", 1000.)
        sampler.want_detail = "7"
        sampler.prime_collection_snapshot()
        assert sampler._collection_not_before["details"] == 0.
    finally:
        sampler.shutdown()


def test_new_visible_demand_primes_only_unsampled_sources(monkeypatch):
    from raven.sampler import Sampler
    timer = Timer()
    monkeypatch.setattr("raven.sampler.time.time", timer)
    monkeypatch.setattr("random.SystemRandom.uniform", lambda self, first, last: last)
    raw = Recorder()
    raw.response = ""  # A successful empty queue, not a malformed two-column record.
    sampler = Sampler(Slurm(raw, "test"), Store(persist=False), {"jobs": 60}, [], collection={})
    try:
        sampler.prime_collection_snapshot()
        sampler.run_source("jobs")
        queue_deadline = sampler._collection_not_before["jobs"]
        assert not sampler.due("nodes", timer.value)
        assert sampler._collection_not_before["partitions"] == 1060.

        sampler.set_collection_demand("nodes")
        assert sampler.due("nodes", timer.value)
        assert sampler.due("partitions", timer.value)
        assert sampler._collection_not_before["jobs"] == queue_deadline == 1072.
        assert not sampler.due("share", timer.value)

        sampler.kick.clear()
        sampler.set_collection_demand("nodes")
        assert not sampler.kick.is_set()
    finally:
        sampler.shutdown()


@pytest.mark.parametrize("error", [CommandError("scheduler unavailable"), Deferred("command lane occupied")])
def test_visible_demand_and_refresh_preserve_first_attempt_recovery(monkeypatch, error):
    from raven.sampler import Sampler
    timer = Timer()
    monkeypatch.setattr("raven.sampler.time.time", timer)
    monkeypatch.setattr("random.SystemRandom.uniform", lambda self, first, last: last)
    class FailingRecorder(Recorder):
        def run(self, argv, timeout=8):
            self.commands.append(tuple(argv))
            raise error
    raw = FailingRecorder()
    sampler = Sampler(Slurm(raw, "test"), Store(persist=False), {"jobs": 60}, [], collection={})
    try:
        sampler.prime_collection_snapshot()
        sampler.run_source("jobs")
        deadline = sampler._collection_not_before["jobs"]
        backoff = sampler.health("jobs").backoff
        assert deadline > timer.value
        for tab in ("nodes", "jobs", "jobs"):
            sampler.set_collection_demand(tab, active=True)
            sampler.refresh_all()
            assert sampler._collection_not_before["jobs"] == deadline
            assert sampler.health("jobs").backoff == backoff
            assert not sampler.due("jobs", timer.value)
        assert len(raw.commands) == 1
    finally:
        sampler.shutdown()


def test_burst_retry_hint_waits_for_its_window_without_resetting_quotas(tmp_path):
    timer = Timer()
    owner = ledger(tmp_path, timer)
    for argv in (["squeue"], ["scontrol", "show", "job", "7"]):
        with owner.admit(classify(argv), argv):
            pass
    argv = ["sinfo"]
    with pytest.raises(Deferred) as denied:
        with owner.admit(classify(argv), argv):
            pytest.fail("Burst must remain bounded")
    assert denied.value.retry_after == 1.01
    assert owner.report()["rates_last_minute"]["controller"] == 2
    timer.value += denied.value.retry_after
    with owner.admit(classify(argv), argv):
        pass
    assert owner.report()["rates_last_minute"]["controller"] == 3


def test_many_explicit_live_subscriptions_use_bounded_round_robin_batches():
    from raven.sampler import Sampler
    raw, store = Recorder(), Store(persist=False)
    store.apply_jobs([Job(str(jid), "job", "cpu", "RUNNING", submit="s", start="t") for jid in range(35)])
    sampler = Sampler(Slurm(raw, "test"), store, {"live": 60}, [], collection={"live_batch_jobs": 16})
    try:
        sampler.set_collection_demand("analytics", "0")
        sampler.set_metric_sampling({("resource-series", str(jid), "cpu:rate", "%", "s|t", None, None, None): 100 for jid in range(35)})
        for _ in range(3):
            sampler.src_live()
        queries = [command for command in raw.commands if command[0] == "sstat"]
        assert len(queries) == 3
        assert all(len(command[2].split(",")) <= 16 for command in queries)
        assert {jid for command in queries for jid in command[2].split(",")} == {str(jid) for jid in range(35)}
    finally:
        sampler.shutdown()


def test_temporary_admission_deferral_does_not_become_source_failure():
    from raven.sampler import Sampler
    sampler = Sampler(Slurm(Recorder(), "test"), Store(persist=False), {"jobs": 60}, [], collection={})
    try:
        def occupied():
            raise Deferred("Raven collection deferred: command lane is occupied")
        sampler.sources["jobs"] = occupied
        for _ in range(4):
            sampler.run_source("jobs")
        assert sampler.health("jobs").errors == 0
        assert sampler.health("jobs").backoff == 0
        assert sampler._collection_failures["jobs"] == 0
        assert "jobs" in sampler._collection_deferred
    finally:
        sampler.shutdown()
