"""Evidence fidelity, actual retained recordings, bounded exports and advice."""

import hashlib
import json
import threading
from copy import deepcopy
from pathlib import Path

import pytest

from raven.engineering import diagnosis as D
from raven.engineering import incidents as I
from raven.engineering import performance as P

IDENTITY = {"cluster": "site", "job_id": "42", "attempt": "attempt-1"}


def source(identifier="s", clock="c", **extra):
    return {
        "id": identifier,
        "identity": {
            **IDENTITY,
            "node": "node-1",
            "boot_id": "boot-1",
            "pid": 1,
            "process_start": "start",
            "stream": identifier,
        },
        "clock": {"id": clock, **extra},
        "status": "COMPLETE",
        "validated": True,
    }


def event(
    identifier="a", *, start=10, end=20, kind="python", source_id="s", clock="c", data=None, **extra
):
    return dict(
        id=identifier,
        name="solve",
        kind=kind,
        source_id=source_id,
        clock_id=clock,
        start_ns=start,
        end_ns=end,
        duration_ns=end - start,
        thread="t",
        depth=0,
        file="solve.py",
        line=4,
        first_seq=1,
        last_seq=2,
        data=data or {},
        **extra,
    )


def query(*rows, **extra):
    return dict(
        identity=deepcopy(IDENTITY),
        sources=[source()],
        intervals=list(rows),
        markers=[],
        caught_up=True,
        **extra,
    )


def trigger(**extra):
    return {
        "kind": "oom",
        "clock_id": "c",
        "time_ns": 20,
        "evidence": "Supplied scheduler state OUT_OF_MEMORY",
        **extra,
    }


def load(result):
    return json.loads((Path(result["path"]) / "incident.json").read_text())


def test_calls_do_not_become_cpu_or_wait_and_nested_calls_count_only_elapsed():
    value = P.analyze(query(event(start=0, end=100), event("b", start=20, end=30)))
    assert value["functions"]["groups"][0]["inclusive_ns"] == 110
    assert value["clock_envelopes"][0]["elapsed_ns"] == 100
    assert not value["execution"]["available"] and not value["waiting"]["available"]
    assert value["waiting"]["observations"] == []
    assert "unknown" in value["waiting"]["missing_reason"]
    assert not value["device_activity"]["available"]


def test_explicit_cpu_wait_and_device_observations_reuse_union_semantics():
    value = P.analyze(
        query(
            event(
                "cpu1",
                start=0,
                end=15,
                kind="scheduling",
                data={"state": "running", "logical_cpu": 0},
            ),
            event(
                "cpu2",
                start=10,
                end=20,
                kind="scheduling",
                data={"state": "running", "logical_cpu": 0},
            ),
            event("wait", start=20, end=40, kind="scheduling", data={"state": "blocked"}),
            event("gpu", start=5, end=35, kind="gpu_kernel", device={"vendor": "other", "id": "0"}),
        )
    )
    assert value["execution"]["observations"][0]["value"] == 20
    assert value["waiting"]["observations"][0]["value"] == 20
    assert value["device_activity"]["observations"][0]["value"] == 30
    assert (
        next(item for item in value["resources"]["metrics"] if item["name"] == "gpu_occupancy")[
            "value"
        ]
        is None
    )


def test_clock_envelopes_remain_separate_and_causal_calibration_keeps_uncertainty():
    first = event("a", data={"depends_on": []})
    second = event(
        "b", clock="other", source_id="other", start=25, end=40, data={"depends_on": ["a"]}
    )
    value = query(first, second)
    value["sources"].append(source("other", "other"))
    report = P.analyze(value, selected_id="b")
    assert len(report["clock_envelopes"]) == 2
    assert "causal_slice" in report
    assert "not a whole-run" in " ".join(report["limitations"])
    # The existing causal engine declines elapsed timing without a calibration.
    assert report["causal_slice"]["schema"] == "raven.trace-causal-slice/v1"


@pytest.mark.parametrize("where", ["query", "source", "event-source"])
def test_attempt_or_source_mismatch_cannot_become_an_investigation(where):
    value = query(event())
    if where == "query":
        value["identity"]["attempt"] = "reused-job-id"
    elif where == "source":
        value["sources"][0]["identity"]["attempt"] = "reused-job-id"
    else:
        value["intervals"][0]["source_id"] = "unregistered"
    with pytest.raises(ValueError, match="different recording attempt|matching source"):
        P.analyze(value, identity=IDENTITY)


def test_partial_read_error_and_cancellation_remain_visible():
    value = query(event())
    value.update(
        caught_up=False, discovery_errors=[{"read_error": "retry later: source not yet visible"}]
    )
    report = P.analyze(value)
    assert report["partial"] and report["coverage"]["read_errors"] == value["discovery_errors"]
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(InterruptedError):
        P.analyze(value, cancel=cancel)


def test_cpu_memory_counters_remain_available_without_gpu_recording():
    value = query(
        event(
            kind="metric",
            data={
                "values": {"rss_bytes": 4096, "cpu_percent": 37},
                "units": {"rss_bytes": "B", "cpu_percent": "percent"},
            },
        )
    )
    report = P.analyze(value)
    assert not report["device_activity"]["available"]
    assert len(report["counters"]["series"]) == 2
    assert {item["name"] for item in report["counters"]["series"]} == {"rss_bytes", "cpu_percent"}


def test_actual_cooperative_retained_recording_is_read_without_scheduler(tmp_path):
    with I.recorder(tmp_path, identity=IDENTITY, budget={"post_seconds": 0.1}) as recorder:
        with recorder.span("retained phase"):
            recorder.emit("phase", {"name": "pre"})
        assert recorder.trigger("explicit test incident")
        with recorder.span("post phase"):
            recorder.emit("phase", {"name": "post"})
    value = P.read_recordings([recorder.receipt["manifest"]], identity=IDENTITY)
    report = P.analyze(value)
    assert report["coverage"]["returned_events"] >= 2
    assert report["coverage"]["caught_up"]
    assert report["identity"] == IDENTITY
    assert not report["execution"]["available"]
    assert any(row["name"] == "retained phase" for row in report["functions"]["groups"])
    bounded = P.read_recordings(
        [recorder.receipt["manifest"]], identity=IDENTITY, options={"limit": 1}
    )
    assert len(bounded["intervals"]) + len(bounded["markers"]) <= 1
    assert bounded["truncated"]
    clock = value["intervals"][0]["clock_id"]
    filtered = P.read_recordings(
        [recorder.receipt["manifest"]], identity=IDENTITY, options={"clock_id": clock, "limit": 1}
    )
    assert {row["clock_id"] for row in filtered["intervals"] + filtered["markers"]} <= {clock}


def test_malformed_scheduling_semantics_have_an_explicit_diagnostic():
    with pytest.raises(ValueError, match="scheduling states"):
        P.analyze(query(event(kind="scheduling", data={"state": {"unknown": "extension"}})))


def test_time_filters_need_an_explicit_clock_instead_of_being_ignored(tmp_path):
    with pytest.raises(ValueError, match="Time filters require"):
        P.read_recordings([str(tmp_path / "unopened")], identity=IDENTITY, options={"start_ns": 0})
    with pytest.raises(ValueError, match="filters must be an object"):
        P.read_recordings([str(tmp_path / "unopened")], identity=IDENTITY, options=[])


def test_incident_retains_original_overlap_boundaries_identity_and_redacted_logs(tmp_path):
    log = tmp_path / "job.log"
    log.write_text("password=veryprivate\nGPU driver error observed\n")
    scheduler = {"identity": IDENTITY, "state": "OUT_OF_MEMORY"}
    value = query(event(start=0, end=100))
    result = I.capture(
        tmp_path / "incident",
        identity=IDENTITY,
        trigger=trigger(),
        query=value,
        scheduler=scheduler,
        logs=[{"path": str(log), "identity": IDENTITY}],
        samples=[{"identity": IDENTITY, "clock_id": "c", "time_ns": 20, "rss_bytes": 4096}],
        budget={"pre_seconds": 1e-8, "post_seconds": 1e-8},
    )
    saved = load(result)
    assert saved["trace"]["intervals"][0]["end_ns"] == 100
    assert saved["window"] == {"clock_id": "c", "start_ns": 10, "end_ns": 30}
    assert saved["scheduler"]["state"] == "OUT_OF_MEMORY"
    assert saved["samples"][0]["rss_bytes"] == 4096
    assert "veryprivate" not in saved["logs"][0]["text"]
    assert "[REDACTED]" in saved["logs"][0]["text"]
    assert saved["logs"][0]["original_sha256"] == hashlib.sha256(log.read_bytes()).hexdigest()
    assert "unverified" in saved["logs"][0]["scope"]
    assert result["stored_bytes"] == sum(
        path.stat().st_size for path in Path(result["path"]).iterdir()
    )
    assert Path(result["path"]).stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in Path(result["path"]).iterdir())
    assert I.load(result["path"], identity=IDENTITY)["document"] == saved
    with pytest.raises(ValueError, match="new directory"):
        I.capture(result["path"], identity=IDENTITY, trigger=trigger())


def test_incident_budget_other_clocks_errors_and_omissions_are_explicit(tmp_path):
    value = query(*(event(str(n), start=n, end=n + 1) for n in range(10)))
    value["sources"].append(source("other", "other"))
    value["intervals"].append(event("other", source_id="other", clock="other"))
    result = I.capture(
        tmp_path / "bounded",
        identity=IDENTITY,
        trigger=trigger(),
        query=value,
        logs=[{"path": str(tmp_path / "missing.log"), "identity": IDENTITY}],
        budget={"max_events": 2, "max_bytes": 64 << 10},
    )
    saved = load(result)
    assert saved["coverage"]["retained_events"] == 2
    assert saved["coverage"]["omitted_window_events"] == 8
    assert saved["coverage"]["other_clock_events"] == 1
    assert saved["coverage"]["read_errors"][0]["read_error"]
    assert saved["trace"]["truncated"]
    assert result["stored_bytes"] <= 64 << 10


@pytest.mark.parametrize("section", ["scheduler", "sample", "log"])
def test_incident_never_mixes_attempts_or_publishes_on_validation_failure(tmp_path, section):
    other = {**IDENTITY, "attempt": "another"}
    args = {}
    if section == "scheduler":
        args["scheduler"] = {"identity": other, "state": "FAILED"}
    elif section == "sample":
        args["samples"] = [{"identity": other, "clock_id": "c", "time_ns": 10}]
    else:
        args["logs"] = [{"identity": other, "path": str(tmp_path / "unused")}]
    with pytest.raises(ValueError, match="different attempt"):
        I.capture(tmp_path / "bad", identity=IDENTITY, trigger=trigger(), **args)
    assert not (tmp_path / "bad").exists()


def test_log_symlink_and_race_are_reported_or_rejected_without_corrupt_bundle(
    tmp_path, monkeypatch
):
    original = tmp_path / "source.log"
    original.write_text("original")
    link = tmp_path / "link.log"
    link.symlink_to(original)
    result = I.capture(
        tmp_path / "symlink",
        identity=IDENTITY,
        trigger=trigger(),
        logs=[{"identity": IDENTITY, "path": str(link)}],
    )
    assert load(result)["logs"] == []
    assert load(result)["coverage"]["read_errors"]
    real_verify = I.science_files.verify

    def replace_before_verify(record, cancel=None):
        original.write_text("changed!")
        return real_verify(record, cancel=cancel)

    monkeypatch.setattr(I.science_files, "verify", replace_before_verify)
    with pytest.raises(ValueError, match="Reviewed source changed"):
        I.capture(
            tmp_path / "raced",
            identity=IDENTITY,
            trigger=trigger(),
            logs=[{"identity": IDENTITY, "path": str(original)}],
        )
    assert not (tmp_path / "raced").exists()


def test_incident_cancellation_never_publishes(tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(InterruptedError):
        I.capture(tmp_path / "cancelled", identity=IDENTITY, trigger=trigger(), cancel=cancel)
    assert not (tmp_path / "cancelled").exists()


@pytest.mark.parametrize(
    "mutation", ["tamper", "incomplete", "public", "wrong-attempt", "extra", "symlink"]
)
def test_incident_reopen_revalidates_integrity_privacy_and_attempt(tmp_path, mutation):
    result = I.capture(
        tmp_path / "saved", identity=IDENTITY, trigger=trigger(), query=query(event())
    )
    folder = Path(result["path"])
    if mutation == "tamper":
        target = folder / "incident.json"
        target.write_bytes(target.read_bytes().replace(b'"oom"', b'"bad"'))
    elif mutation == "incomplete":
        (folder / ".raven-incomplete").write_text("unfinished")
    elif mutation == "public":
        (folder / "incident.json").chmod(0o644)
    elif mutation == "wrong-attempt":
        with pytest.raises(ValueError, match="different recording attempt"):
            I.load(folder, identity={**IDENTITY, "attempt": "another"})
        return
    elif mutation == "extra":
        (folder / "unexpected").write_text("do not trust")
    else:
        target = folder / "incident.json"
        moved = tmp_path / "moved.json"
        target.rename(moved)
        target.symlink_to(moved)
    with pytest.raises(ValueError):
        I.load(folder)


def test_advice_respects_additive_estimates_and_never_claims_a_bottleneck():
    report = P.analyze(query(event()))
    advice = D.propose(report, budget={"max_overhead_percent": 2.0, "max_bytes": 2 << 20})
    assert advice["estimated_cost"]["overhead_percent"] <= 2
    assert advice["estimated_cost"]["bytes"] <= 2 << 20
    assert [item["id"] for item in advice["proposed_measurements"]] == ["scheduler-states"]
    assert advice["unresolved_targets"] == ["device"]
    assert all(
        item["review_required"] and item["execution"] == "not-run"
        for item in advice["proposed_measurements"]
    )
    assert "experimental" in advice["status"]
    assert len(advice["facts"]) == 3 and advice["hypotheses"]
    assert report["evidence_digest"] == advice["evidence_digest"]


def test_zero_budget_still_advises_existing_coverage_inspection_and_no_collectors():
    report = P.analyze({**query(event()), "caught_up": False})
    advice = D.propose(report, budget={"max_overhead_percent": 0.0, "max_bytes": 0})
    assert [item["id"] for item in advice["proposed_measurements"]] == ["coverage"]
    assert advice["estimated_cost"] == {"overhead_percent": 0.0, "bytes": 0}
    assert set(advice["unresolved_targets"]) == {"cpu", "waiting", "device"}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(estimated_bytes=-1),
        lambda value: value.update(estimated_overhead_percent=float("nan")),
        lambda value: value.update(targets=[{}]),
        lambda value: value.update(targets=["unmeasured-idleness"]),
        lambda value: value.update(action="collect-now"),
    ],
)
def test_advice_rejects_invalid_or_executable_candidates(mutate):
    item = deepcopy(D.DEFAULT_CANDIDATES[0])
    mutate(item)
    with pytest.raises(ValueError):
        D.propose(P.analyze(query(event())), candidates=[item])
