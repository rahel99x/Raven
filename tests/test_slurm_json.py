"""Real recorded Slurm JSON equivalence and fail-closed capability discovery."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from raven import slurm_json as J
from raven.model import nbytes, secs
from raven.slurm import CommandError, ParseDiagnostics, Slurm, parse_jobs, parse_sacct

CORPUS = Path(__file__).parent / "fixtures" / "slurm" / "recorded" / "26.05.5"


def raw(name):
    return (CORPUS / (name + ".stdout.txt")).read_text()


def instant(value):
    if value in ("", "Unknown", "N/A"):
        return None
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("name", ["pending", "running", "array-pending", "array-running"])
def test_recorded_real_queue_shared_facts_match_legacy(name):
    value = J.document(raw(name + "-squeue-json"))
    actual = J.queue(value, ParseDiagnostics("jobs"), observed=value["last_update"])
    expected = parse_jobs(raw(name + "-squeue"))
    assert {job.id for job in actual} == {job.id for job in expected}
    for job in expected:
        other = next(item for item in actual if item.id == job.id)
        for field in ("name", "partition", "state", "nodes", "cpus", "gpus", "nodelist", "account", "qos", "command", "workdir", "priority"):
            assert getattr(other, field) == getattr(job, field), field
        assert secs(other.limit) == secs(job.limit)
        assert nbytes(other.mem_req) == nbytes(job.mem_req)
        assert instant(other.submit) == instant(job.submit)
        assert instant(other.start) == instant(job.start)


def test_recorded_accounting_shared_facts_match_legacy():
    value = J.document(raw("accounting-json"))
    report = ParseDiagnostics("finished")
    actual = J.accounting(value, report)
    legacy_report = ParseDiagnostics("finished")
    expected = parse_sacct(raw("accounting"), legacy_report)
    # The real capture contains two generations of requeued job 4. Neither
    # reader may collapse those into one apparently verified attempt.
    assert report.partial and legacy_report.partial
    assert report.rejected == legacy_report.rejected == 1
    assert {job.id for job in actual} == {job.id for job in expected}
    for job in expected:
        other = next(item for item in actual if item.id == job.id)
        for field in ("name", "state", "cpus", "nodes", "gpus", "req_mem", "rss", "workdir", "account", "user"):
            assert getattr(other, field) == getattr(job, field), (job.id, field)
        assert other.cpu_time == pytest.approx(job.cpu_time, abs=.001)
        assert instant(other.submit) == instant(job.submit)
        assert instant(other.start) == instant(job.start)
        assert instant(other.end) == instant(job.end)


def test_real_detail_keeps_spaces_and_exact_metadata():
    value = J.document(raw("running-scontrol-json"))
    job = value["jobs"][0]
    detail = J.detail(value, str(job["job_id"]), ParseDiagnostics("details"))
    assert detail["WorkDir"] == "/home/raven/work with spaces"
    assert detail["JobName"] == "Raven spaces = sample"
    assert detail["StdOut"].endswith("/1.log")
    assert detail["UserId"] == "raven(1100)"
    assert detail["Restarts"] == "0"


class Backend:
    probe_json = True
    def __init__(self, reply=None, *, help_text="--json optional structured output"):
        self.reply = raw("pending-squeue-json") if reply is None else reply
        self.help_text = help_text
        self.calls = []
    def run(self, command, timeout):
        self.calls.append(command)
        if command[-1] == "--help":
            return self.help_text, 0
        if "--json" in command:
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply, 0
        return raw("pending-squeue"), 0


def test_capability_probe_is_cached_and_explicitly_invalidated():
    backend = Backend()
    slurm = Slurm(backend, "raven")
    assert slurm.jobs()[0].name == "Raven spaces = sample"
    slurm.jobs()
    assert sum(command[-1] == "--help" for command in backend.calls) == 1
    slurm.invalidate_capabilities("squeue")
    slurm.jobs()
    assert sum(command[-1] == "--help" for command in backend.calls) == 2
    assert slurm.reader_status["jobs"]["parser"] == "data_parser/v0.0.45"


@pytest.mark.parametrize("reply", ["not json", '{"jobs":[],"jobs":[]}', '{"jobs":[],"x":NaN}',
                                  CommandError("controller temporarily unavailable")])
def test_transient_or_malformed_json_never_replays_as_legacy_success(reply):
    backend = Backend(reply)
    with pytest.raises(CommandError):
        Slurm(backend, "raven").jobs()
    assert len(backend.calls) == 2 and "--json" in backend.calls[-1]


def test_unknown_schema_is_established_visible_fallback_not_guessed_mapping():
    value = json.loads(raw("pending-squeue-json"))
    value["meta"]["plugin"]["data_parser"] = "data_parser/v9.9.9"
    backend = Backend(json.dumps(value))
    slurm = Slurm(backend, "raven")
    assert slurm.jobs()[0].id == "1"
    assert slurm.reader_status["jobs"]["format"] == "legacy fallback"
    slurm.jobs()
    assert len(backend.calls) == 4 and "--json" not in backend.calls[-1]


def test_unsupported_help_uses_one_cached_probe_and_legacy():
    backend = Backend(help_text="no structured flag available")
    slurm = Slurm(backend, "raven")
    assert slurm.jobs()[0].id == "1"
    slurm.jobs()
    assert len(backend.calls) == 3 and all("--json" not in call for call in backend.calls)
    assert slurm.reader_status["jobs"]["format"] == "legacy fallback"


def test_unknown_descriptive_extensions_preserve_known_facts():
    value = J.document(raw("pending-squeue-json"))
    expected = J.queue(value, ParseDiagnostics("jobs"), observed=value["last_update"])
    value["jobs"][0]["new_description"] = {"vendor": "additive", "label": "extra"}
    assert J.queue(value, ParseDiagnostics("jobs"), observed=value["last_update"]) == expected


@pytest.mark.parametrize("extension", [{"bad": "\u0001"}, {"bad": 1e101}, {"": 1}, {"bad\nkey": 1}, {"bad": "x" * 65537}])
def test_json_boundaries_reject_invalid_unknown_extensions(extension):
    value = json.loads(raw("pending-squeue-json"))
    value["descriptive_extension"] = extension
    with pytest.raises(ValueError):
        J.document(json.dumps(value))


def test_json_boundaries_reject_deep_unknown_extensions():
    value = json.loads(raw("pending-squeue-json"))
    nested = value
    for _ in range(25):
        nested["extension"] = {}
        nested = nested["extension"]
    with pytest.raises(ValueError, match="nesting"):
        J.document(json.dumps(value))


def test_unavailable_required_counts_do_not_turn_into_zero_measurements():
    value = J.document(raw("pending-squeue-json"))
    value["jobs"][0]["cpus"]["set"] = False
    report = ParseDiagnostics("jobs")
    assert J.queue(value, report) == [] and report.rejected == 1


def test_json_literal_delimiters_remain_exact_unlike_ambiguous_legacy_fields():
    value = J.document(raw("pending-squeue-json"))
    value["jobs"][0]["name"] = "pipe|name"
    value["jobs"][0]["command"] = "python train.py | tee log"
    job, = J.queue(value, ParseDiagnostics("jobs"))
    assert job.name == "pipe|name" and job.command == "python train.py | tee log"


def test_suspension_runtime_uses_slurm_pre_suspend_semantics():
    job = json.loads(raw("running-squeue-json"))["jobs"][0]
    job.update(start_time=100, suspend_time=150, pre_sus_time=20, job_state=["SUSPENDED"])
    assert J.runtime(job, 200) == 20
    job["job_state"] = ["RUNNING"]
    assert J.runtime(job, 200) == 70


def test_recording_replays_only_each_commands_recorded_capability(tmp_path):
    from raven.record import RecordingBackend, ReplayBackend
    path = tmp_path / "session.jsonl"
    recorder = RecordingBackend(Backend(), str(path), {"user": "raven"})
    expected = Slurm(recorder, "raven").jobs()
    recorder.close()
    replay = ReplayBackend(str(path), paused=True)
    assert replay.can_probe_json("squeue")
    assert not replay.can_probe_json("scontrol")
    # The capability probe precedes the actual queue response. Replaying at
    # startup must not expose that future answer.
    with pytest.raises(CommandError, match="no observation|not in the recording"):
        replay.run(next(key for key in replay.by_key if key[0] == "squeue" and "--json" in key))
    replay.clock.seek(replay.t1)
    reader = Slurm(replay, "raven")
    assert reader.jobs() == expected
    assert reader.reader_status["jobs"]["format"] == "json"


def test_recording_preserves_explicit_legacy_backend_contract(tmp_path):
    from raven.record import RecordingBackend
    backend = Backend()
    backend.probe_json = False
    recorder = RecordingBackend(backend, str(tmp_path / "legacy.jsonl"))
    try:
        Slurm(recorder, "raven").jobs()
        assert not any("--help" in command for command in backend.calls)
    finally:
        recorder.close()


def test_recorded_2302_plural_metadata_allows_explicit_schema_fallback():
    captured = (CORPUS.parent / "23.02.8" / "capability-running-squeue-json.stdout.txt").read_text()
    with pytest.raises(J.UnsupportedShape, match="v0.0.39"):
        J.document(captured)
    backend = Backend(captured)
    slurm = Slurm(backend, "raven")
    assert slurm.jobs()[0].id == "1"
    assert slurm.reader_status["jobs"]["format"] == "legacy fallback"
    assert "v0.0.39" in slurm.reader_status["jobs"]["reason"]
    assert len(backend.calls) == 3
    value = json.loads(captured)
    value["meta"]["plugin"] = {"data_parser": "data_parser/v0.0.45"}
    rejected = Backend(json.dumps(value))
    with pytest.raises(CommandError, match="ambiguous"):
        Slurm(rejected, "raven").jobs()
    assert len(rejected.calls) == 2 and "--json" in rejected.calls[-1]
