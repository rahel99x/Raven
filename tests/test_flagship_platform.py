"""Evidence fidelity, native dialects, reviewed effects and runtime isolation."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError

import pytest

from raven import evidence as E, evidence_formats as F, site_policy as P
from raven import adapter_protocol as A, ops_platform as OPS
from raven import operations as O
from raven.collection_broker import CollectionBroker
from raven.shared_collection import SharedQueryCache
from raven.platform_session import wrap_backend, GovernedBackend, read_operation

IDENTITY = {"cluster": "alpha", "job_id": "123_4", "attempt": "2026-01-01T00:00:00/2026-01-01T00:00:02"}
SCOPE = {"connection": "alpha/user", "generation": "connection-1"}


def receipt():
    return E.receipt(IDENTITY, {"id": "test", "format": "test-json", "version": "1", "sha256": "a" * 64},
                     [{"id": "rss", "kind": "metric", "data": {"name": "memory", "value": 123, "unit": "B"}}])


def policy(interval=.01):
    return {"schema": P.SCHEMA, "id": "test-site", "revision": "1", "sources": ["test", "slurm-cli"],
            "operations": ["read", "queue", "accounting", "nodes", "statistics", "configuration", "job-detail"],
            "limits": {"min_interval": interval, "max_duration": 5, "max_bytes": 100000, "max_records": 1000, "max_inflight": 4},
            "allocation_required": False, "allow_intrusive": False}


def caps(interval=.01):
    return {"schema": P.CAPABILITIES, "source": "test", "version": "1", "operations": ["read"], "min_interval": interval, "status": "available"}


def request():
    return {"source": "test", "operation": "read", "interval": .05, "duration": 2,
            "max_bytes": 10000, "max_records": 100, "allocation": None, "intrusive": False}


def query(**parameters):
    return {"request": request(), "parameters": parameters}


def write_json(path, value):
    path.write_text(json.dumps(value))
    return str(path)


def context(**kwargs):
    return O.Context(scope={"cluster": "alpha", "user": "researcher"}, **kwargs)


def adapter(tmp_path, body, **limits):
    script = tmp_path / "adapter.py"
    script.write_text("import sys,json,time,os\nrequest=json.loads(sys.stdin.readline())\n" + body + "\n")
    executable = str(Path(sys.executable).resolve())
    return {"schema": A.SCHEMA, "id": "fixture", "version": "1", "protocol": A.PROTOCOL,
            "argv": [executable, str(script)], "environment": {}, "methods": ["describe", "validate", "run"],
            "assets": [{"path": p, "sha256": hashlib.sha256(Path(p).read_bytes()).hexdigest()} for p in (executable, str(script))],
            "limits": {"timeout": 2, "frame_bytes": 16384, "total_bytes": 65536, "stderr_bytes": 4096, **limits}}


EMIT = "print(json.dumps({'schema':request['schema'],'id':request['id'],'type':'result','result':{'ok':True}}),flush=True)"


def test_evidence_receipt_integrity_detached_roundtrip():
    value = receipt()
    assert E.verify_receipt(E.loads(E.canonical(value))) == value
    detached = E.validate(value)
    detached["records"][0]["data"]["value"] = 999
    assert value["records"][0]["data"]["value"] == 123
    with pytest.raises(ValueError, match="digest"):
        E.verify_receipt(detached)


@pytest.mark.parametrize("value", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b'\xff', b'[]' * 100, b'{"x":1e999}', b'{"x":"\\u001b"}'])
def test_evidence_rejects_invalid_json(value):
    with pytest.raises(ValueError):
        E.loads(value)


@pytest.mark.parametrize("mutate", [
    lambda v: v.update(schema="raven.evidence/v999"),
    lambda v: v["identity"].update(pid=10),
    lambda v: v["identity"].update(job_id="1_[0-9]"),
    lambda v: v["records"].append(copy.deepcopy(v["records"][0])),
    lambda v: v["records"][0]["data"].update(value=True),
    lambda v: v["records"][0]["data"].update(value=float("nan")),
    lambda v: v["records"][0]["data"].update(unit="guess"),
    lambda v: v["clock"].update(uncertainty=-1),
    lambda v: v["clock"].update(unit="ticks"),
    lambda v: v["source"].update(sha256="not-a-digest"),
    lambda v: v["coverage"].update(complete=True),
    lambda v: v["coverage"].update(complete=1),
    lambda v: v["coverage"].update(omitted=-1),
    lambda v: v["coverage"].update(gaps=[{"start": 4, "end": 3, "reason": "missing"}]),
    lambda v: v["loss"].update(records=True),
    lambda v: v.update(transforms=[{"name": "bad"}]),
    lambda v: v.update(extra=True),
])
def test_evidence_rejects_false_or_ambiguous_claims(mutate):
    value = receipt()
    value.pop("digest")
    mutate(value)
    with pytest.raises(ValueError):
        E.validate(value)


def test_complete_scope_requires_known_zero_loss_and_no_gaps():
    value = receipt()
    value.pop("digest")
    value["coverage"]["complete"] = True
    value["loss"] = {"records": 0, "reason": "Producer counter checked"}
    assert E.validate(value)["coverage"]["complete"]
    value["coverage"]["omitted"] = 1
    with pytest.raises(ValueError):
        E.validate(value)


def test_evidence_depth_and_size_bound():
    value = {}
    for _ in range(26):
        value = {"nested": value}
    with pytest.raises(ValueError, match="nesting"):
        E.canonical(value)
    with pytest.raises(ValueError, match="bound"):
        E.canonical("x" * 65537)


def test_perfetto_native_roundtrip_and_open_intervals():
    value = {"displayTimeUnit": "ns", "traceEvents": [
        {"ph": "B", "ts": 1, "name": "step", "pid": 1, "tid": 2},
        {"ph": "C", "ts": 2, "name": "power", "pid": 1, "args": {"watts": 3}},
        {"ph": "E", "ts": 5, "pid": 1, "tid": 2},
        {"ph": "s", "ts": 3, "pid": 1, "tid": 2, "id2": {"local": "11"}, "cat": "MPI"},
        {"ph": "f", "ts": 4, "pid": 2, "tid": 2, "id2": {"local": "11"}, "cat": "MPI"},
        {"ph": "B", "ts": 6, "name": "open", "pid": 1, "tid": 2},
        {"ph": "M", "name": "thread_name", "pid": 1, "tid": 2, "args": {"name": "main"}}]}
    result = F.import_document(value, "perfetto-json", IDENTITY)
    assert result["clock"]["unit"] == "us"  # displayTimeUnit does not redefine timestamps.
    assert result["coverage"]["complete"] is None
    assert [r["data"]["end"] for r in result["records"] if r["kind"] == "span"] == [5, None]
    assert len([r for r in result["records"] if r["kind"] == "flow"]) == 2
    assert F.export_document(result, "perfetto-json") == value


@pytest.mark.parametrize("event", [{"ph": "X", "ts": 1, "dur": -1, "pid": 1, "tid": 2}, {"ph": "s", "ts": 1, "pid": 1, "tid": 2}, {"ph": "X", "ts": 1, "dur": 1}, {"ph": "C", "ts": 1, "args": {"metric": True}}, {"ph": "B", "ts": "1", "pid": 1, "tid": 2}])
def test_perfetto_rejects_bad_event(event):
    with pytest.raises(ValueError):
        F.import_document({"traceEvents": [event]}, "perfetto-json", IDENTITY)


@pytest.mark.parametrize("native", [[{"path": "main/step", "sum#time.duration": 3.1}], {"records": [{"path": "main", "mpi.rank": 0, "sum#time.duration": 1}], "globals": {"job": "123_4"}, "attributes": {"sum#time.duration": {"unit": "sec"}}}])
def test_caliper_records_object_roundtrip(native):
    result = F.import_document(native, "caliper-json-records", IDENTITY)
    assert F.export_document(result, "caliper-json-records") == native
    assert all(item["data"]["unit"] == "unknown" for item in result["records"] if item["kind"] == "metric")


def test_rocrate_roundtrip_duplicate_and_generic_export():
    crate = F.export_document(receipt(), "workflow-run-ro-crate")
    result = F.import_document(crate, "workflow-run-ro-crate", IDENTITY)
    assert result["coverage"]["mode"] == "provenance"
    assert F.export_document(result, "workflow-run-ro-crate") == crate
    crate["@graph"].append(crate["@graph"][0])
    with pytest.raises(ValueError, match="Duplicate"):
        F.import_document(crate, "workflow-run-ro-crate", IDENTITY)


@pytest.mark.parametrize("dialect,value", [("unknown", {}), ("caliper-json-records", {"data": [], "columns": []}), ("workflow-run-ro-crate", {"@context": "https://example.invalid", "@graph": []}), ("perfetto-json", [])])
def test_explicit_unsupported_formats(dialect, value):
    with pytest.raises(ValueError):
        F.import_document(value, dialect, IDENTITY)


def test_evidence_identity_must_match():
    other = {**IDENTITY, "attempt": "later"}
    with pytest.raises(ValueError, match="identity"):
        F.import_document(receipt(), "raven-evidence", other)


@pytest.mark.parametrize("change", [{"intrusive": True}, {"source": "other"}, {"operation": "write"}, {"interval": .001}, {"duration": 10}, {"max_bytes": 9999999}, {"max_records": 999999}])
def test_policy_admission_denies_out_of_contract(change):
    value = request()
    value.update(change)
    decision = P.admit(policy(), value, caps())
    assert decision["allowed"] is False and decision["reasons"]


def test_policy_explicit_allocation_capability_status_and_digest():
    value = policy()
    value["allocation_required"] = True
    assert not P.admit(value, request(), caps())["allowed"]
    req = request()
    req["allocation"] = IDENTITY
    assert P.admit(value, req, caps())["allowed"]
    source = caps()
    source["status"] = "unknown"
    assert not P.admit(value, req, source)["allowed"]
    assert P.admit(policy(), request(), caps())["request_digest"] == E.digest(request())


def test_broker_coalesces_isolates_results_and_cancel_one_subscriber():
    entered, release = threading.Event(), threading.Event()
    calls = []
    def load(cancel):
        calls.append(1)
        entered.set()
        assert release.wait(1)
        assert not cancel.is_set()
        return {"records": [{"x": 1}]}
    with CollectionBroker() as broker:
        first = broker.acquire(SCOPE, query(), load, policy(), caps())
        assert entered.wait(1)
        second = broker.acquire(SCOPE, query(), load, policy(), caps())
        first.cancel()
        release.set()
        result = second.result(1)
        result["records"][0]["x"] = 9
        assert second.result(1)["records"][0]["x"] == 1
        second.close()
        with broker.acquire(SCOPE, query(), load, policy(), caps()) as cached:
            assert cached.result(1)["records"][0]["x"] == 1
        assert len(calls) == 1


def test_broker_all_subscribers_cancel_source_invalidation_and_bounds():
    entered, cancelled = threading.Event(), threading.Event()
    def load(stop):
        entered.set()
        assert stop.wait(1)
        cancelled.set()
        return {}
    with CollectionBroker(capacity=1) as broker:
        lease = broker.acquire(SCOPE, query(), load, policy(), caps())
        # Cancellation before dispatch may correctly prevent the loader from
        # running at all; this assertion exercises cooperative cancellation.
        assert entered.wait(1)
        other = {**SCOPE, "generation": "other"}
        with pytest.raises(ValueError, match="capacity"):
            broker.acquire(other, query(), load, policy(), caps())
        lease.cancel()
        assert cancelled.wait(1)
    with CollectionBroker() as broker:
        lease = broker.acquire(SCOPE, query(), lambda _: {}, policy(), caps())
        lease.result(1)
        broker.invalidate(SCOPE)
        with pytest.raises(ValueError, match="changed"):
            lease.result(1)
        lease.close()


def test_broker_cancel_before_dispatch_does_not_call_source_and_frees_capacity():
    calls = []
    def load(stop):
        calls.append(1)
        return {}
    # Caller-owned reads remain queued until result(), making pre-dispatch
    # cancellation deterministic without depending on worker scheduling.
    with CollectionBroker(capacity=1, caller_owned=True) as broker:
        lease = broker.acquire(SCOPE, query(), load, policy(), caps())
        lease.cancel()
        assert broker.stats()["active"] == 0
        assert broker.stats()["leases"] == 0
        with pytest.raises(ValueError, match="closed"):
            lease.result(1)
        other = {**SCOPE, "generation": "other"}
        with broker.acquire(other, query(), lambda _: {"ok": True}, policy(), caps()) as replacement:
            assert replacement.result(1) == {"ok": True}
        assert calls == []


def test_broker_nested_single_worker_does_not_deadlock():
    from raven.worker_scheduler import WorkerScheduler
    scheduler = WorkerScheduler(workers=2, mode="single")
    lane = scheduler.lane("research")
    def parent():
        with CollectionBroker() as broker:
            with broker.acquire(SCOPE, query(), lambda _: {"ok": 1}, policy(), caps()) as lease:
                return lease.result(1)
    try:
        assert lane.submit(parent).result(timeout=2) == {"ok": 1}
        assert scheduler.status()["limit"] == 1
    finally:
        scheduler.shutdown(wait=True)


def test_broker_oversize_source_output_and_cancel_before_admit():
    with CollectionBroker() as broker:
        q = query()
        q["request"]["max_bytes"] = 10
        with broker.acquire(SCOPE, q, lambda _: {"larger": "data"}, policy(), caps()) as lease:
            with pytest.raises(ValueError, match="byte"):
                lease.result(1)
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(ValueError, match="cancelled"):
            broker.acquire(SCOPE, query(), lambda _: {}, policy(), caps(), cancel)


def test_adapter_real_subprocess_success_and_environment_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_TEST_SECRET", "must-not-leak")
    manifest = adapter(tmp_path, "assert 'RAVEN_TEST_SECRET' not in os.environ\n" + EMIT)
    assert A.AdapterClient(manifest).request("describe") == {"ok": True}
    Path(manifest["argv"][1]).write_text("different code")
    with pytest.raises(ValueError, match="hash changed"):
        A.AdapterClient(manifest).request("describe")


@pytest.mark.parametrize("body,match", [
    ("print('{\"x\":1,\"x\":2}',flush=True)", "Duplicate"),
    ("print(json.dumps({'schema':request['schema'],'id':'wrong','type':'result','result':{}}),flush=True)", "identity"),
    ("print('x'*20000,flush=True)", "frame"),
    ("print(json.dumps({'schema':request['schema'],'id':request['id'],'type':'progress','progress':2}),flush=True)", "progress"),
    ("print(json.dumps({'schema':request['schema'],'id':request['id'],'type':'result','result':[]}))", "invalid"),
    ("sys.stderr.write('x'*5000);sys.stderr.flush()", "stderr"),
    (EMIT + "\n" + EMIT, "Duplicate"),
    (EMIT + "\nsys.exit(2)", "unsuccessfully"),
    ("sys.stdout.write('{');sys.stdout.flush()", "complete"),
])
def test_adapter_malformed_bounded_protocol(tmp_path, body, match):
    manifest = adapter(tmp_path, body)
    with pytest.raises(ValueError, match=match):
        A.AdapterClient(manifest).request("run")


def test_adapter_timeout_and_cooperative_cancel(tmp_path):
    manifest = adapter(tmp_path, "time.sleep(10)", timeout=.1)
    start = time.monotonic()
    with pytest.raises(ValueError, match="timed out"):
        A.AdapterClient(manifest).request("run")
    assert time.monotonic() - start < 2
    manifest = adapter(tmp_path, "message=json.loads(sys.stdin.readline())\nassert message['method']=='cancel'\n" + EMIT)
    stop = threading.Event()
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(A.AdapterClient(manifest).request, "run", cancel=stop)
        time.sleep(.05)
        stop.set()
        with pytest.raises(ValueError, match="cancelled"):
            future.result(timeout=2)


def test_adapter_manifest_never_executes_on_inspection_and_plan_revalidates(tmp_path):
    marker = tmp_path / "executed"
    manifest = adapter(tmp_path, f"open({str(marker)!r},'w').write('yes')\n" + EMIT)
    path = write_json(tmp_path / "manifest.json", manifest)
    result = OPS.run("adapter", {"manifest": path, "action": "inspect"}, context())
    assert not marker.exists() and "plan" not in result
    result = OPS.run("adapter", {"manifest": path, "action": "run"}, context())
    assert not marker.exists()
    applied = OPS.apply("adapter", result["plan"], context())
    assert marker.exists() and applied["data"] == {"ok": True}
    Path(path).write_text(json.dumps({**manifest, "version": "2"}))
    with pytest.raises(ValueError, match="source changed"):
        OPS.apply("adapter", result["plan"], context())


def test_portable_review_export_and_changed_source(tmp_path):
    source = write_json(tmp_path / "evidence.json", receipt())
    params = {"path": source, "format": "raven-evidence", **IDENTITY, "destination": str(tmp_path / "export"), "export_format": "workflow-run-ro-crate"}
    result = OPS.run("research-portable", params, context())
    assert not Path(params["destination"]).exists()
    OPS.apply("research-portable", result["plan"], context())
    assert (tmp_path / "export" / "raven-evidence.json").is_file()
    assert (tmp_path / "export" / "ro-crate-metadata.json").is_file()
    with pytest.raises(ValueError, match="new directory"):
        OPS.apply("research-portable", result["plan"], context())
    params["destination"] = str(tmp_path / "export2")
    result = OPS.run("research-portable", params, context())
    Path(source).write_text(json.dumps(receipt(), indent=2))
    with pytest.raises(ValueError, match="source changed"):
        OPS.apply("research-portable", result["plan"], context())


def test_portable_selected_attempt_mismatch_remote_write_refused(tmp_path):
    path = write_json(tmp_path / "source.json", receipt())
    params = {"path": path, "format": "raven-evidence", **IDENTITY}
    with pytest.raises(ValueError, match="attempt"):
        OPS.run("research-portable", params, context(jobs=({"id": "123_4", "submit": "other", "start": "later"},)))
    class Remote:
        remote = True
    # A remote profile can inspect through its actual remote file API, but can
    # never publish a local substitute; test the apply guard directly.
    result = OPS.run("research-portable", {**params, "destination": str(tmp_path / "new")}, context())
    with pytest.raises(ValueError, match="target host"):
        OPS.apply("research-portable", result["plan"], context(files=Remote()))


def test_qualification_is_real_hashed_fixture_not_live_claim(tmp_path):
    raw = E.canonical(receipt())
    path = tmp_path / "fixture.json"
    path.write_bytes(raw)
    check = {"id": "format", "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "format": "raven-evidence", "identity": IDENTITY, "expect": {"min_records": 1, "max_records": 1, "mode": "mixed"}}
    manifest = write_json(tmp_path / "qualification.json", {"schema": "raven.qualification-manifest/v1", "id": "example", "checks": [check]})
    result = OPS.run("qualification", {"manifest": manifest}, context())
    assert result["data"]["passed"] and not result["data"]["live_slurm_tested"]
    path.write_text("{}")
    result = OPS.run("qualification", {"manifest": manifest}, context())
    assert not result["data"]["passed"] and "hash differs" in result["data"]["checks"][0]["reason"]


def test_support_explicit_sources_redaction_and_source_revalidation(tmp_path):
    report = O.report("fixture", "token=secret-password", data={"api_token": "hidden", "environment": {"SECRET": "bad"}, "public": "fine"})
    report_path = write_json(tmp_path / "report.json", report)
    manifest = write_json(tmp_path / "support.json", {"schema": "raven.support-bundle/v1", "reports": [report_path]})
    params = {"manifest": manifest, "destination": str(tmp_path / "bundle")}
    result = OPS.run("support-bundle", params, context())
    encoded = json.dumps(result["data"])
    assert "hidden" not in encoded and "secret-password" not in encoded and "fine" in encoded
    OPS.apply("support-bundle", result["plan"], context())
    assert (tmp_path / "bundle" / "support.json").is_file()


def test_shared_cache_same_scope_queries_coalesce_and_other_scope_isolated(tmp_path):
    first = SharedQueryCache(tmp_path / "cache")
    second = SharedQueryCache(tmp_path / "cache")
    calls = []
    def loader(stop):
        calls.append(1)
        time.sleep(.05)
        return {"records": [1]}
    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(first.query, SCOPE, query(), loader, policy(), caps(), read_only=True)
        b = pool.submit(second.query, SCOPE, query(), loader, policy(), caps(), read_only=True)
        values = [a.result(2), b.result(2)]
    assert len(calls) == 1 and sum(v["cached"] for v in values) == 1
    third = first.query({**SCOPE, "generation": "other"}, query(), loader, policy(), caps(), read_only=True)
    assert not third["cached"] and len(calls) == 2
    with pytest.raises(ValueError, match="read-only"):
        first.query(SCOPE, query(), loader, policy(), caps())


def test_shared_cache_cancellation_invalidation_and_ownership(tmp_path):
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    unsafe.chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        SharedQueryCache(unsafe)
    link = tmp_path / "link"
    link.symlink_to(unsafe)
    with pytest.raises(ValueError, match="symlink"):
        SharedQueryCache(link)
    cache = SharedQueryCache(tmp_path / "safe")
    stop = threading.Event()
    stop.set()
    with pytest.raises(ValueError, match="cancelled"):
        cache.query(SCOPE, query(), lambda _: {}, policy(), caps(), read_only=True, cancel=stop)
    def invalidate(_):
        cache.invalidate(SCOPE)
        return {}
    with pytest.raises(ValueError, match="invalidated"):
        cache.query(SCOPE, query(), invalidate, policy(), caps(), read_only=True)


def test_session_hook_default_unchanged_and_real_read_cache_action_invalidation(tmp_path):
    class Backend:
        def __init__(self): self.calls = []
        def run(self, argv, timeout):
            self.calls.append(list(argv))
            return "123|RUNNING\n", .01
        def call(self, argv, timeout):
            self.calls.append(list(argv))
            return True, "ok"
    backend = Backend()
    assert wrap_backend(backend, {}, {"cluster": "alpha"}, str(tmp_path)) is backend
    value = policy(.5)
    path = write_json(tmp_path / "policy.json", value)
    wrapped = wrap_backend(backend, {"site_policy": path}, {"cluster": "alpha", "user": "user"}, str(tmp_path / "state"))
    assert isinstance(wrapped, GovernedBackend)
    assert wrapped.run(["squeue", "--noheader"])[0] == "123|RUNNING\n"
    assert wrapped.run(["squeue", "--noheader"])[0] == "123|RUNNING\n"
    assert len(backend.calls) == 1
    wrapped.call(["scontrol", "hold", "123"])
    wrapped.run(["squeue", "--noheader"])
    assert len(backend.calls) == 3
    assert read_operation(["scontrol", "update", "JobId=123"]) is None
    assert read_operation(["sbatch", "x.sh"]) is None


def _shared_process(directory, gate, counter, results):
    cache = SharedQueryCache(directory)
    q = query()
    q['request']['interval'] = .3
    def load(stop):
        with counter.get_lock():
            counter.value += 1
        time.sleep(.1)
        return {'records': ['measured']}
    gate.wait(3)
    try:
        results.put(cache.query(SCOPE, q, load, policy(), caps(), read_only=True))
    except BaseException as exc:
        results.put({'error': str(exc)})


def _crash_shared_owner(directory, entered):
    cache = SharedQueryCache(directory)
    def load(_):
        entered.set()
        os._exit(17)
    cache.query(SCOPE, query(), load, policy(), caps(), read_only=True)


def test_cross_process_one_collection_and_crashed_owner_recovery(tmp_path):
    import multiprocessing
    mp = multiprocessing.get_context('fork')
    directory = str(tmp_path / 'multi')
    SharedQueryCache(directory)
    gate, counter, results = mp.Event(), mp.Value('i', 0), mp.Queue()
    children = [mp.Process(target=_shared_process, args=(directory, gate, counter, results)) for _ in range(2)]
    for child in children: child.start()
    gate.set()
    outputs = [results.get(timeout=5) for _ in children]
    for child in children:
        child.join(5)
        assert child.exitcode == 0
    assert counter.value == 1 and sum(output['cached'] for output in outputs) == 1
    entered = mp.Event()
    directory = str(tmp_path / 'crash')
    crashed = mp.Process(target=_crash_shared_owner, args=(directory, entered))
    crashed.start()
    assert entered.wait(5)
    crashed.join(5)
    assert crashed.exitcode == 17
    cache = SharedQueryCache(directory)
    recovered = cache.query(SCOPE, query(), lambda _: {'recovered': True}, policy(), caps(), read_only=True)
    assert recovered['value'] == {'recovered': True}


def test_shared_waiter_cancel_does_not_interrupt_owner(tmp_path):
    cache = SharedQueryCache(tmp_path / 'cancel')
    entered, release, stop = threading.Event(), threading.Event(), threading.Event()
    def load(cancel):
        entered.set()
        assert release.wait(2)
        assert not cancel.is_set()
        return {'ok': True}
    with ThreadPoolExecutor(2) as pool:
        owner = pool.submit(cache.query, SCOPE, query(), load, policy(), caps(), read_only=True)
        assert entered.wait(1)
        waiter = pool.submit(cache.query, SCOPE, query(), load, policy(), caps(), read_only=True, cancel=stop)
        stop.set()
        with pytest.raises(ValueError, match='cancelled'):
            waiter.result(1)
        release.set()
        assert owner.result(1)['value'] == {'ok': True}


def test_shared_cache_byte_budget_and_policy_epoch(tmp_path):
    cache = SharedQueryCache(tmp_path / 'bounds', max_bytes=20000)
    first = cache.query(SCOPE, query(), lambda _: {'old': True}, policy(), caps(), read_only=True)
    revised = policy()
    revised['revision'] = '2'
    second = cache.query(SCOPE, query(), lambda _: {'new': True}, revised, caps(), read_only=True)
    assert not first['cached'] and not second['cached'] and second['value'] == {'new': True}
    q = query()
    q['request']['max_bytes'] = 30000
    with pytest.raises(ValueError, match='byte capacity'):
        cache.query(SCOPE, q, lambda _: {}, policy(), caps(), read_only=True)


def test_governed_backend_preserves_remote_forecast_scope(tmp_path):
    from raven.remote import SshBackend
    from raven.cli import forecast_scope
    from raven.config import Config
    backend = SshBackend('cluster.example', user='person', opts=['-p', '2222'])
    cfg = Config()
    original = forecast_scope(backend, cfg, 'person')
    wrapper = GovernedBackend(backend, policy(.5), SharedQueryCache(tmp_path / 'remote'), original)
    assert forecast_scope(wrapper, cfg, 'person') == original
    assert wrapper.host == 'cluster.example'
    with pytest.raises(ValueError, match='target host'):
        O.local_only(O.Context(slurm=type('Slurm', (), {'b': wrapper})()))


@pytest.mark.parametrize('field', ['schema', 'identity', 'source', 'clock', 'coverage', 'loss', 'records', 'transforms'])
@pytest.mark.parametrize('bad', [None, False, [], {}, 1, ''])
def test_evidence_malformed_field_types_are_controlled(field, bad):
    value = receipt()
    value.pop('digest')
    value[field] = bad
    # Empty records/transforms legitimately describe empty observations.
    if field in ('records', 'transforms') and bad == []:
        E.validate(value)
    else:
        with pytest.raises(ValueError):
            E.validate(value)


@pytest.mark.parametrize('field,bad', [('limits', []), ('sources', []), ('operations', []), ('allocation_required', 1), ('allow_intrusive', None)])
def test_policy_malformed_fields_controlled(field, bad):
    value = policy()
    value[field] = bad
    with pytest.raises(ValueError):
        P.validate(value)


def test_large_integer_rejected_without_overflow():
    with pytest.raises(ValueError):
        E.canonical({'number': 10 ** 1000})


def test_broker_submit_failure_rolls_back_admission_and_throttle():
    from raven.worker_scheduler import WorkerScheduler, WorkerQueueFull
    scheduler = WorkerScheduler(workers=1, mode='single')
    lane = scheduler.lane('source')
    real_submit = lane.submit
    lane.submit = lambda *args, **kwargs: (_ for _ in ()).throw(WorkerQueueFull('fixture full'))
    try:
        with CollectionBroker(lane=lane) as broker:
            with pytest.raises(WorkerQueueFull):
                broker.acquire(SCOPE, query(), lambda _: {}, policy(), caps())
            assert broker.stats()['entries'] == 0
            lane.submit = real_submit
            with broker.acquire(SCOPE, query(), lambda _: {'retry': True}, policy(), caps()) as lease:
                assert lease.result(1) == {'retry': True}
    finally:
        lane.shutdown(wait=True)
        scheduler.shutdown(wait=True)


def test_otlp_import_preserves_nanosecond_precision_parentage_and_native_roundtrip():
    native = {'resourceSpans': [{'resource': {'attributes': [{'key': 'service.name', 'value': {'stringValue': 'solver'}}]}, 'scopeSpans': [{'scope': {'name': 'sdk'}, 'spans': [{
        'traceId': '1' * 32, 'spanId': '2' * 16, 'parentSpanId': '3' * 16, 'name': 'solve',
        'startTimeUnixNano': '1760000000000000001', 'endTimeUnixNano': '1760000000000000011', 'droppedEventsCount': 2}]}]}]}
    receipt = F.import_document(native, 'otlp-json-traces', IDENTITY)
    span = next(item for item in receipt['records'] if item['kind'] == 'span')
    assert span['data']['end'] - span['data']['start'] == 10
    assert span['data']['attributes']['parent_span_id'] == '3' * 16
    assert receipt['metadata']['reported_dropped_span_fields'] == 2
    assert receipt['loss']['records'] is None
    assert F.export_document(receipt, 'otlp-json-traces') == native
    native['resourceSpans'][0]['scopeSpans'][0]['spans'] *= 2
    with pytest.raises(ValueError, match='Duplicate'):
        F.import_document(native, 'otlp-json-traces', IDENTITY)


def test_broker_cached_result_outlives_collection_deadline():
    q = query()
    q['request']['duration'] = .05
    q['request']['interval'] = .5
    with CollectionBroker() as broker:
        with broker.acquire(SCOPE, q, lambda _: {'cached': True}, policy(), caps()) as lease:
            assert lease.result(1) == {'cached': True}
        time.sleep(.06)
        with broker.acquire(SCOPE, q, lambda _: {'unexpected': True}, policy(), caps()) as lease:
            assert lease.result(0) == {'cached': True}


def test_native_reexport_refuses_missing_original_records_even_if_receipt_rehashed():
    native = {'traceEvents': [{'ph': 'X', 'name': 'solve', 'pid': 1, 'tid': 1, 'ts': 2, 'dur': 3}]}
    value = F.import_document(native, 'perfetto-json', IDENTITY)
    value.pop('digest')
    value['records'] = [item for item in value['records'] if item['kind'] != 'annotation']
    value['digest'] = E.digest(value)
    with pytest.raises(ValueError, match='native source differs'):
        F.export_document(value, 'perfetto-json')


def test_support_snapshot_includes_ui_persistence_status_without_reading_store(tmp_path):
    path = write_json(tmp_path / 'evidence.json', receipt())
    manifest = write_json(tmp_path / 'support.json', {'schema': 'raven.support-bundle/v1', 'reports': [path]})
    ctx = context(ui_state_status={'state': 'corrupt', 'message': 'recovered snapshot'})
    result = OPS.run('support-bundle', {'manifest': manifest}, ctx)
    assert result['data']['ui_state_status'] == ctx.ui_state_status


def test_packaged_platform_examples_qualification_and_sdk(tmp_path):
    result = OPS.run('qualification', {'manifest': 'examples/platform/qualification.json'}, O.Context())
    assert result['data']['passed']
    import subprocess
    value = E.loads(subprocess.check_output([sys.executable, 'examples/platform/make_adapter_manifest.py'], timeout=5))
    result = A.AdapterClient(value).request('run', {'text': 'abc'})
    assert result['sha256'] == hashlib.sha256(b'abc').hexdigest()


@pytest.mark.parametrize('field,bad', [('parentSpanId', []), ('parentSpanId', None), ('startTimeUnixNano', 1), ('endTimeUnixNano', '-1'), ('traceId', '0'*32), ('spanId', 'bad'), ('droppedEventsCount', True)])
def test_otlp_malformed_span_fields_are_controlled(field, bad):
    span = {'traceId': '1'*32, 'spanId': '2'*16, 'name': 'work', 'startTimeUnixNano': '1', 'endTimeUnixNano': '2'}
    span[field] = bad
    with pytest.raises(ValueError):
        F.import_document({'resourceSpans': [{'scopeSpans': [{'spans': [span]}]}]}, 'otlp-json-traces', IDENTITY)


@pytest.mark.parametrize('token', [{}, {'local': 1, 'global': 1}, {'local': []}, [], True, -1])
def test_perfetto_malformed_flow_ids_are_controlled(token):
    event = {'ph': 's', 'ts': 1, 'pid': 1, 'tid': 1, 'id2': token}
    with pytest.raises(ValueError):
        F.import_document({'traceEvents': [event]}, 'perfetto-json', IDENTITY)


def test_shared_cache_refuses_known_network_storage(tmp_path, monkeypatch):
    from raven import shared_collection
    monkeypatch.setattr(shared_collection, '_network_mount', lambda path: True)
    with pytest.raises(ValueError, match='host-local'):
        shared_collection.SharedQueryCache(tmp_path / 'network')


def test_broker_cancelled_running_generation_keeps_reservation_until_drained():
    entered, drain = threading.Event(), threading.Event()
    def loader(stop):
        entered.set()
        assert drain.wait(2)
        return {}
    with CollectionBroker() as broker:
        first = broker.acquire(SCOPE, query(), loader, policy(), caps())
        assert entered.wait(1)
        first.cancel()
        time.sleep(.02)
        with pytest.raises(ValueError, match='draining'):
            broker.acquire(SCOPE, query(), loader, policy(), caps())
        assert broker.stats()['active'] == 1
        drain.set()
