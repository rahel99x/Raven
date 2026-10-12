"""Slurm task averages become observed CPU totals without hiding application steps."""
import pytest

from raven.model import Job
from raven.slurm import FakeBackend, Slurm, parse_sstat


def row(step, cpu, rss="1K", tasks="1"):
    return f"{step}|{cpu}|{rss}|0|system|{rss}|{tasks}|{cpu}|0|system"


def test_numbered_application_step_contributes_when_batch_cpu_is_zero():
    text = "\n".join((row("1170.batch", "00:00:00", "3976K"),
                       row("1170.0", "00:00:18", "1670232K")))
    assert parse_sstat(text) == (18., 1670232 * 1024)
    assert parse_sstat("\n".join(reversed(text.splitlines()))) == parse_sstat(text)


def test_task_averages_are_summed_once_per_application_step():
    text = "\n".join((row("1170.batch", "00:00:02", "2K"),
                       row("1170.0", "00:00:18", "3K", "4"),
                       row("1170.1", "00:00:03.5", "4K", "2"),
                       row("1170.0", "00:00:17", "5K", "4"),
                       row("1170.extern", "00:50:00", "6K", "32")))
    assert parse_sstat(text) == (81., 6 * 1024)


@pytest.mark.parametrize("cpu", ["", "Unknown", "N/A", "nan", "inf", "-1"])
def test_missing_or_invalid_cpu_keeps_independent_memory_measurement(cpu):
    assert parse_sstat(row("1170.0", cpu, "1.5G")) == (None, 1.5 * 1024 ** 3)


@pytest.mark.parametrize("tasks", ["", "Unknown", "0", "-1", "2.5", "nan", "inf"])
def test_unknown_task_count_does_not_invent_a_cpu_total(tasks):
    assert parse_sstat(row("1170.0", "00:00:18", "2M", tasks)) == (None, 2 * 1024 ** 2)


def test_partial_step_coverage_keeps_aggregate_cpu_unknown_and_memory_measured():
    text = "\n".join((row("1170.batch", "Unknown", "2K"),
                       row("1170.0", "00:00:18", "3K", "2")))
    assert parse_sstat(text) == (None, 3 * 1024)


def test_duplicate_measured_step_resolves_missing_cpu_without_counting_twice():
    text = "\n".join((row("1170.0", "Unknown"), row("1170.0", "00:00:18", "2K", "2")))
    assert parse_sstat(text) == (36., 2 * 1024)


def test_legacy_three_column_recordings_keep_single_task_cpu_units():
    assert parse_sstat("1170.batch|00:00:02|3976K\n1170.0|00:00:18|1670232K") == (20., 1670232 * 1024)
    assert parse_sstat("") == (None, None)
    assert parse_sstat("1170.extern|00:00:18|3976K") == (None, 3976 * 1024)


def test_live_efficiency_and_rate_use_application_cpu():
    job = Job(id="1170", name="app", partition="local", state="RUNNING", elapsed="00:01:00", cpus=2)
    text = "\n".join((row("1170.batch", "00:00:00"), row("1170.0", "00:00:18")))
    live, previous, steps = Slurm._live_result(job, (100., 8.), 110., text)
    assert live.cpu_time == 18.
    assert live.avg == .15
    assert live.rate == .5
    assert previous == (110., 18.)
    assert [step.id for step in steps] == ["1170.batch", "1170.0"]


def test_multinode_demo_reports_per_task_averages_and_original_cpu_fraction(monkeypatch):
    monkeypatch.setattr("raven.slurm.time.time", lambda: 60.)
    backend = FakeBackend("researcher", t0=0.)
    backend.spec = [{**backend.spec[0], "id": "1170", "start": 0., "dur": 1000., "cpus": 8,
                     "nodes": 2, "node": "n[01-02]"}]
    slurm = Slurm(backend, "researcher")
    job = slurm.jobs()[0]
    live, _, steps = slurm.live(job, None, 60.)
    assert live.cpu_time == 144.
    assert live.avg == .3
    application = next(step for step in steps if step.name == "0")
    assert application.cpu_time == 32.5 and application.ntasks == 4
