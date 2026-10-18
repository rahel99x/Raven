"""Recorded scheduler acquisition survives controller purging and a fresh UI."""
import json
from pathlib import Path

import pytest

from raven.model import Store
from raven.report import _ReportStore
from raven.sampler import Sampler
from raven.slurm import CommandError, SACCT_LOG_FIELDS, Slurm

CORPUS = Path(__file__).parent / "fixtures/slurm/recorded/26.05.5"


def capture(name):
    return (CORPUS / (name + ".stdout.txt")).read_text()


class RecordedBackend:
    probe_json = True

    def __init__(self, *, purged=False, restart=0, logs=True):
        self.purged, self.restart, self.calls, self.logs = purged, restart, [], logs

    def run(self, command, timeout):
        self.calls.append(list(command))
        if command[-1] == "--help":
            return "--json optional structured output", 0
        if command[:3] == ["scontrol", "show", "hostnames"]:
            assert command[3] == "raven-ci"
            return "raven-ci\n", 0
        assert "--json" in command
        if command[0] == "scontrol":
            if self.purged:
                raise CommandError("Invalid job id specified: completed job purged")
            return capture("running-scontrol-json"), 0
        if command[0] == "squeue":
            return capture("running-squeue-json"), 0
        assert command[0] == "sacct"
        value = json.loads(capture("accounting-json"))
        value["jobs"] = [job for job in value["jobs"] if job["job_id"] == 1]
        for job in value["jobs"]:
            if self.restart is None:
                job.pop("restart_cnt", None)
            else:
                job["restart_cnt"] = self.restart
            if not self.logs:
                job["stdout"] = job["stderr"] = ""
        return json.dumps(value), 0


def persisted_live_attempt(path, storage="jsonl"):
    store = Store(str(path), series_storage=storage)
    sampler = Sampler(Slurm(RecordedBackend(), "raven"), store, {}, [], workers=1)
    try:
        sampler.src_jobs()
        sampler.select("1")
        sampler.src_details()
        identity = store.capture_series_identity("1")
        assert identity.verified
        sample = {"t": 1792279061.125, "k": "live", "cpu": .375, "rss": 1234567}
        store.record("1", sample, identity=identity)
        return identity, sample, Path(store._series_path("1"))
    finally:
        sampler.shutdown()
        store.close()


@pytest.mark.parametrize("storage", ["jsonl", "blocks-v1"])
@pytest.mark.parametrize("logs", [True, False])
def test_purged_completed_attempt_reopens_native_history_and_report(tmp_path, storage, logs):
    identity, sample, path = persisted_live_attempt(tmp_path, storage)
    backend = RecordedBackend(purged=True, logs=logs)
    # A default-format fresh session must also recognize an existing compact copy.
    store = Store(str(tmp_path))
    sampler = Sampler(Slurm(backend, "raven"), store, {}, [], workers=1)
    try:
        sampler.src_finished()
        sampler.select("1")
        sampler.src_details()
        assert store.details["1"]["LogPathSource"] == "sacct"
        if not logs:
            assert "no stdout/stderr paths" in store.details["1"]["LogPathError"]
        assert store.capture_series_identity("1") == identity
        # The report restores from its private reader before the UI loads data.
        report = _ReportStore(store, store.snapshot(), sample["t"] + 1)
        assert report.series_of("1") == [sample]
        tasks = []
        store.configure_series_loader(tasks.append)
        assert store.series_view("1") == []
        tasks.pop()()
        assert store.series_view("1") == [sample]
        assert path.exists()
        exact = [call for call in backend.calls if call[0] == "sacct" and "-j" in call]
        assert len(exact) == 1
        assert all(flag in exact[0] for flag in ("-S", "-E", "-D", "-u", "-X"))
    finally:
        sampler.shutdown()
        store.close()


@pytest.mark.parametrize("restart", [-1, True, 1.5, 10000000000, "0"])
def test_invalid_accounting_restart_metadata_never_verifies_retained_data(tmp_path, restart):
    _, sample, path = persisted_live_attempt(tmp_path)
    store = Store(str(tmp_path))
    backend = RecordedBackend(purged=True, restart=restart)
    sampler = Sampler(Slurm(backend, "raven"), store, {}, [], workers=1)
    try:
        sampler.src_finished()
        sampler.select("1")
        with pytest.raises(CommandError, match="parse coverage"):
            sampler.src_details()
        assert not store.capture_series_identity("1").verified
        assert store.series_of("1") == []
        assert json.loads(path.read_text()) == sample
        assert all("--json" in call for call in backend.calls if call[0] == "sacct" and call[-1] != "--help")
    finally:
        sampler.shutdown()
        store.close()


def test_legacy_completed_paths_remain_available_without_inventing_restart(tmp_path):
    _, sample, path = persisted_live_attempt(tmp_path)

    class LegacyBackend:
        def run(self, command, timeout):
            if command[0] == "scontrol":
                raise CommandError("Invalid job id specified")
            assert command[0] == "sacct" and "--json" not in command
            if command[-1] == SACCT_LOG_FIELDS:
                return ("1|1|Raven spaces = sample|/home/raven/work with spaces|logs/1.out||raven|"
                        "2026-10-17T23:17:37|2026-10-17T23:17:40|raven-ci\n"), 0
            return capture("accounting"), 0

    store = Store(str(tmp_path))
    sampler = Sampler(Slurm(LegacyBackend(), "raven", json_mode="legacy"), store, {}, [], workers=1)
    try:
        sampler.src_finished()
        sampler.select("1")
        sampler.src_details()
        assert store.details["1"]["StdOut"] == "logs/1.out"
        assert "Restarts" not in store.details["1"]
        assert not store.capture_series_identity("1").verified
        assert store.series_of("1") == []
        assert "restart counter" in store.series_status("1")["message"]
        assert json.loads(path.read_text()) == sample
    finally:
        sampler.shutdown()
        store.close()


@pytest.mark.parametrize("restart", [None, 1])
def test_purged_missing_or_different_restart_never_borrows_archive(tmp_path, restart):
    identity, sample, path = persisted_live_attempt(tmp_path)
    store = Store(str(tmp_path))
    sampler = Sampler(Slurm(RecordedBackend(purged=True, restart=restart), "raven"), store, {}, [], workers=1)
    try:
        sampler.src_finished()
        sampler.select("1")
        sampler.src_details()
        current = store.capture_series_identity("1")
        assert current != identity and current.verified == (restart is not None)
        assert store.series_of("1") == []
        assert json.loads(path.read_text()) == sample
        if restart is None:
            assert store.series_status("1")["attribution"] == "session-only"
            assert "restart counter" in store.series_status("1")["message"]
    finally:
        sampler.shutdown()
        store.close()
