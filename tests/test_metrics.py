"""Adversarial coverage for bounded telemetry and cooperating JSONL reporters."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
import random
import subprocess
import sys

import pytest

from raven.metrics import MAX_ERRORS, MAX_LINE_BYTES, MAX_METRICS, MAX_POINTS, MAX_READ_BYTES, MAX_STREAMS, MetricReader, write_metric
from raven.remote import LocalFiles


def encoded(**overrides):
    rec = {"t": 100, "metrics": {"loss": 0.5}}
    rec.update(overrides)
    return (json.dumps(rec, ensure_ascii=False) + "\n").encode()


def append(path, data):
    with path.open("ab") as file:
        file.write(data)


class CountingFiles(LocalFiles):
    def __init__(self):
        self.reads = []
        self.stats = 0

    def stat(self, path):
        self.stats += 1
        return super().stat(path)

    def read(self, path, offset, length):
        self.reads.append((offset, length))
        return super().read(path, offset, length)


def test_metrics_are_incremental_and_snapshot_independent(tmp_path):
    path = tmp_path / "application.jsonl"
    path.write_bytes(encoded(step=12, phase="train", metrics={"loss": 0, "speed": -3.5}))
    files = CountingFiles()
    reader = MetricReader(files)
    result = reader.read(path)
    assert result["status"] == "ok"
    assert result["latest"] == {"loss": 0.0, "speed": -3.5}
    assert result["series"]["loss"] == [{"t": 100.0, "value": 0.0, "step": 12}]
    assert result["phase"] == "train" and result["progress"] == {}
    assert "missing" not in result["latest"]
    assert len(files.reads) == 1
    result["series"]["loss"][0]["value"] = 999
    result["latest"].clear()
    assert reader.read(path)["latest"]["loss"] == 0
    assert len(files.reads) == 1 and files.stats == 2
    offset = path.stat().st_size
    append(path, encoded(t=101, metrics={"loss": 0.25}))
    result = reader.read(path)
    assert files.reads[-1] == (offset, path.stat().st_size - offset)
    assert result["records"] == 2 and result["latest"]["loss"] == 0.25
    json.dumps(result, allow_nan=False)


def test_partial_lines_and_multibyte_boundary_are_preserved(tmp_path):
    path = tmp_path / "metrics"
    data = encoded(phase="entraînement", metrics={"résidu": 0})
    cut = data.index("î".encode()) + 1
    path.write_bytes(data[:cut])
    reader = MetricReader()
    result = reader.read(path)
    assert result["status"] == "partial" and result["records"] == 0 and not result["errors"]
    append(path, data[cut:-1])
    assert reader.read(path)["records"] == 0
    append(path, b"\n")
    result = reader.read(path)
    assert result["status"] == "ok" and result["latest"] == {"résidu": 0}
    assert result["phase"] == "entraînement"


@pytest.mark.parametrize("seed", range(5))
def test_random_append_boundaries_match_complete_input(tmp_path, seed):
    path = tmp_path / "stream"
    path.write_bytes(b"")
    reader = MetricReader()
    records = [encoded(t=i, step=i, metrics={"loss": i / 100, "résidu": 30 - i}, phase="époque") for i in range(30)]
    data = b"".join(records)
    rng = random.Random(seed)
    while data:
        length = rng.randrange(1, 90)
        append(path, data[:length])
        reader.read(path)
        data = data[length:]
    result = reader.read(path)
    assert result["records"] == 30 and len(result["series"]["résidu"]) == 30
    assert result["latest"] == {"loss": 0.29, "résidu": 1}
    assert result["errors"] == []


@pytest.mark.parametrize("bad", [
    b"not json\n", b"[]\n", b"null\n", b"{\"t\": 1, \"metrics\": {\"loss\": NaN}}\n",
    b"{\"t\": 1, \"metrics\": {\"loss\": Infinity}}\n", b"\xff\n",
    b'{"t": 1, "metrics": {"loss": 0, "loss": 5}}\n',
    b'{"t": 1, "t": 2, "metrics": {"loss": 0}}\n',
    encoded(t=True), encoded(t="1"), encoded(t=-1), encoded(t=float("inf")),
    encoded(metrics=[]), encoded(metrics={"loss": True}), encoded(metrics={"loss": None}),
    encoded(metrics={"loss": "1.0"}), encoded(metrics={"\x1b[31mloss": 2}), encoded(metrics={"": 1}),
    encoded(metrics={"x" * 97: 1}), encoded(phase="x\n"), encoded(phase=20),
    encoded(step=True), encoded(step=1.2), encoded(step=-1), encoded(step=1 << 63),
    encoded(progress=[]), encoded(progress={"completed": 1}),
    encoded(progress={"completed": 1, "total": 0}), encoded(progress={"completed": 2, "total": 1}),
    encoded(progress={"completed": False, "total": 1}),
    encoded(progress={"completed": 0, "total": 1, "unit": "steps\x1b"}),
])
def test_malformed_records_are_atomic_and_do_not_prevent_recovery(tmp_path, bad):
    path = tmp_path / "bad.jsonl"
    path.write_bytes(bad + encoded(t=2, metrics={"valid": 0}))
    result = MetricReader().read(path)
    assert result["records"] == 1 and result["latest"] == {"valid": 0}
    assert result["errors"] and result["status"] == "partial"
    json.dumps(result, allow_nan=False)


def test_deep_json_huge_integer_and_nonfinite_remain_errors(tmp_path):
    path = tmp_path / "bad"
    path.write_bytes(b"[" * 2000 + b"0" + b"]" * 2000 + b"\n"
                     + b'{"t": 1, "metrics": {"value": ' + b"9" * 1000 + b"}}\n"
                     + encoded(t=2))
    result = MetricReader().read(path)
    assert result["records"] == 1 and len(result["errors"]) == 2


def test_record_without_metric_values_can_report_explicit_progress(tmp_path):
    path = tmp_path / "progress"
    path.write_bytes(encoded(metrics={}, phase="solve", progress={"completed": 0, "total": 20, "unit": "iterations"}))
    result = MetricReader().read(path)
    assert result["latest"] == {} and result["records"] == 1
    assert result["progress"]["fraction"] == 0
    assert "eta_seconds" not in result["progress"]


def test_eta_requires_sufficient_consistent_reported_progress(tmp_path):
    path = tmp_path / "progress"
    path.write_bytes(b"")
    reader = MetricReader()
    for t, completed in [(0, 0), (10, 10)]:
        append(path, encoded(t=t, phase="train", progress={"completed": completed, "total": 100, "unit": "steps"}))
        assert "eta_seconds" not in reader.read(path)["progress"]
    append(path, encoded(t=20, phase="train", progress={"completed": 20, "total": 100, "unit": "steps"}))
    result = reader.read(path)
    assert result["progress"] == {"completed": 20.0, "total": 100.0, "unit": "steps", "fraction": 0.2,
                                  "eta_seconds": 80.0, "rate_per_second": 1.0,
                                  "eta_source": "reported_progress", "eta_samples": 3}
    append(path, encoded(t=30, phase="evaluate"))
    assert reader.read(path)["progress"] == {}


@pytest.mark.parametrize("changed", [
    {"t": 5, "completed": 30, "total": 100, "unit": "steps"},
    {"t": 30, "completed": 2, "total": 100, "unit": "steps"},
    {"t": 30, "completed": 30, "total": 200, "unit": "steps"},
    {"t": 30, "completed": 30, "total": 100, "unit": "epochs"},
])
def test_eta_resets_on_clock_progress_or_metadata_change(tmp_path, changed):
    path = tmp_path / "progress"
    path.write_bytes(b"".join(encoded(t=t, progress={"completed": t, "total": 100, "unit": "steps"}) for t in [0, 10, 20]))
    reader = MetricReader()
    assert "eta_seconds" in reader.read(path)["progress"]
    append(path, encoded(t=changed["t"], progress={k: v for k, v in changed.items() if k != "t"}))
    assert "eta_seconds" not in reader.read(path)["progress"]


def test_eta_handles_underflow_and_stalled_progress_without_guessing(tmp_path):
    path = tmp_path / "progress"
    path.write_bytes(b"".join(encoded(t=t, progress={"completed": completed, "total": 1e300})
                              for t, completed in [(0, 0), (1e200, 1e-300), (1e300, 2e-300)]))
    assert "eta_seconds" not in MetricReader().read(path)["progress"]
    path.write_bytes(b"".join(encoded(t=t, progress={"completed": 0, "total": 10}) for t in [0, 1, 2]))
    assert "eta_seconds" not in MetricReader().read(path)["progress"]


def test_rotation_and_truncation_reset_series_and_partial_tail(tmp_path):
    path = tmp_path / "stream"
    path.write_bytes(encoded(t=100, metrics={"old": 1}) + b'{"t":')
    reader = MetricReader()
    assert reader.read(path)["latest"] == {"old": 1}
    previous = tmp_path / "rotated"
    path.rename(previous)
    path.write_bytes(encoded(t=2, metrics={"new": 0}))
    result = reader.read(path)
    assert result["records"] == 1 and result["latest"] == {"new": 0} and not result["errors"]
    path.write_bytes(b"{}\n")
    result = reader.read(path)
    assert result["records"] == 0 and result["latest"] == {} and result["errors"]


def test_missing_empty_and_io_failure_are_distinct(tmp_path):
    path = tmp_path / "stream"
    reader = MetricReader()
    assert reader.read(path)["status"] == "missing"
    path.write_bytes(b"")
    assert reader.read(path)["status"] == "empty"
    assert reader.read(path)["latest"] == {}

    class BrokenFiles:
        def stat(self, path):
            return 20, (1, 1)

        def read(self, path, offset, length):
            raise PermissionError("denied")

    result = MetricReader(BrokenFiles()).read(path)
    assert result["status"] == "error" and result["records"] == 0


def test_short_read_advances_only_consumed_bytes(tmp_path):
    path = tmp_path / "stream"
    path.write_bytes(encoded(t=1) + encoded(t=2))

    class ShortFiles(CountingFiles):
        def read(self, path, offset, length):
            return super().read(path, offset, min(length, 7))

    files = ShortFiles()
    reader = MetricReader(files)
    for _ in range(30):
        result = reader.read(path)
    assert result["records"] == 2
    assert [offset for offset, _ in files.reads] == list(range(0, path.stat().st_size, 7))


def test_points_metrics_errors_and_stream_cache_are_bounded(tmp_path):
    path = tmp_path / "stream"
    records = [encoded(t=i, metrics={f"m{i}": i, "main": i}) for i in range(MAX_METRICS + 50)]
    path.write_bytes(b"".join(records) + b"bad\n" * 100)
    reader = MetricReader(max_points=5, max_streams=2)
    result = reader.read(path)
    assert len(result["series"]) == MAX_METRICS
    assert len(result["series"]["main"]) == 5 and result["latest"]["main"] == MAX_METRICS + 49
    assert len(result["errors"]) == MAX_ERRORS and result["truncated"]
    for number in range(5):
        file = tmp_path / str(number)
        file.write_bytes(encoded(t=number))
        reader.read(file)
    assert len(reader._streams) == 2 and list(reader._streams) == [str(tmp_path / "3"), str(tmp_path / "4")]


def test_byte_budget_tail_boundary_and_exact_budget_are_bounded(tmp_path):
    path = tmp_path / "stream"
    data = b"".join(encoded(t=i, metrics={"value": i}) for i in range(30))
    path.write_bytes(data)
    files = CountingFiles()
    reader = MetricReader(files, max_bytes=200)
    result = reader.read(path)
    assert result["truncated"] and all(length <= 200 for _, length in files.reads)
    assert result["records"] >= 1 and not result["errors"]
    # The preceding boundary byte consumes one byte of the first read's budget.
    result = reader.read(path)
    assert result["latest"] == {"value": 29}
    assert all(length <= 200 for _, length in files.reads)


def test_tail_window_at_an_exact_line_boundary_retains_the_first_record(tmp_path):
    path = tmp_path / "stream"
    first, second, third = encoded(t=1), encoded(t=2), encoded(t=3)
    path.write_bytes(first + second + third)
    reader = MetricReader(max_bytes=len(second + third))
    result = reader.read(path)
    assert result["series"]["loss"] == [{"t": 2.0, "value": 0.5}]
    assert reader.read(path)["records"] == 2


def test_oversized_partial_line_is_discarded_through_its_newline(tmp_path):
    path = tmp_path / "stream"
    path.write_bytes(b"x" * (MAX_LINE_BYTES + 1))
    reader = MetricReader()
    assert reader.read(path)["truncated"]
    assert len(reader._streams[str(path)].partial) == 0
    append(path, encoded(t=1))  # Still part of the oversized, unterminated record.
    assert reader.read(path)["records"] == 0
    append(path, encoded(t=2))
    result = reader.read(path)
    assert result["records"] == 1 and result["last_t"] == 2


def test_complete_oversized_line_and_excess_record_burst_do_not_allocate_unbounded_series(tmp_path):
    path = tmp_path / "stream"
    path.write_bytes(b"x" * (MAX_LINE_BYTES + 1) + b"\n" + encoded(t=1))
    result = MetricReader().read(path)
    assert result["records"] == 1 and result["errors"] and result["truncated"]
    path.write_bytes(b"{}\n" * 20000 + encoded(t=2))
    result = MetricReader().read(path)
    assert result["records"] == 1 and len(result["errors"]) == MAX_ERRORS and result["truncated"]


@pytest.mark.parametrize("option", ["max_bytes", "max_points", "max_streams"])
@pytest.mark.parametrize("value", [0, -1, 1.5, True, "10"])
def test_invalid_reader_limits_are_rejected(option, value):
    with pytest.raises(ValueError):
        MetricReader(**{option: value})


def test_writer_appends_without_overwriting_and_roundtrips(tmp_path):
    path = tmp_path / "metrics.jsonl"
    first = write_metric(path, {"loss": 0}, t=10, step=1, phase="train", completed=0, total=10, unit="steps")
    original = path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    assert first["metrics"] == {"loss": 0}
    write_metric(path, {"loss": 0.2}, t=20, step=2)
    assert path.read_bytes().startswith(original)
    result = MetricReader().read(path)
    assert result["records"] == 2 and result["latest"] == {"loss": 0.2}
    assert result["phase"] == "train" and result["progress"]["fraction"] == 0
    assert all(json.loads(line) for line in path.read_bytes().splitlines())


@pytest.mark.parametrize("kwargs", [
    {"metrics": {"value": float("nan")}}, {"metrics": {"value": float("inf")}},
    {"metrics": {"value": True}}, {"metrics": []}, {"step": 1.5}, {"step": -1},
    {"phase": "bad\x1b"}, {"phase": None}, {"completed": 1}, {"total": 1},
    {"completed": 1, "total": 0}, {"completed": 2, "total": 1}, {"unit": "steps"},
    {"unit": None}, {"unit": False}, {"unit": []},
    {"t": False}, {"t": float("nan")}, {"metrics": {f"m{i}": 1 for i in range(MAX_METRICS + 1)}},
])
def test_invalid_writer_input_never_creates_or_changes_file(tmp_path, kwargs):
    path = tmp_path / "metrics"
    params = {"metrics": {"loss": 1}, "t": 1, **kwargs}
    with pytest.raises(ValueError):
        write_metric(path, **params)
    assert not path.exists()
    path.write_bytes(b"preserved\n")
    with pytest.raises(ValueError):
        write_metric(path, **params)
    assert path.read_bytes() == b"preserved\n"


def test_writer_preserves_unterminated_existing_data_and_permissions(tmp_path):
    path = tmp_path / "stream"
    path.write_bytes(b"existing data")
    path.chmod(0o640)
    write_metric(path, {"loss": 1}, t=2)
    assert path.read_bytes().startswith(b"existing data\n{")
    assert path.stat().st_mode & 0o777 == 0o640
    assert MetricReader().read(path)["records"] == 1


def test_writer_refuses_symlink_fifo_directory_and_missing_parent(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"preserved")
    link = tmp_path / "link"
    link.symlink_to(target)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    for path in [link, fifo, tmp_path]:
        with pytest.raises(ValueError):
            write_metric(path, {"loss": 1})
    assert target.read_bytes() == b"preserved"
    with pytest.raises(FileNotFoundError):
        write_metric(tmp_path / "missing" / "metrics", {"loss": 1})


def test_concurrent_thread_writers_produce_complete_unique_records(tmp_path):
    path = tmp_path / "threaded"
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: write_metric(path, {"result": i}, step=i, t=i), range(150)))
    records = [json.loads(line) for line in path.read_bytes().splitlines()]
    assert len(records) == 150 and sorted(rec["step"] for rec in records) == list(range(150))
    assert MetricReader().read(path)["records"] == 150


def test_concurrent_process_writers_cooperate_without_interleaving(tmp_path):
    path = tmp_path / "processes"
    code = ("import sys; from raven.metrics import write_metric; "
            "[write_metric(sys.argv[1], {'result': i}, step=i, t=i) "
            "for i in range(int(sys.argv[2]), int(sys.argv[2])+25)]")
    processes = [subprocess.Popen([sys.executable, "-c", code, str(path), str(i * 25)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for i in range(4)]
    for process in processes:
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0 and not stdout, stderr.decode()
    records = [json.loads(line) for line in path.read_bytes().splitlines()]
    assert len(records) == 100 and sorted(rec["step"] for rec in records) == list(range(100))


def test_short_and_interrupted_writes_complete_under_lock(tmp_path, monkeypatch):
    path = tmp_path / "short"
    real_write = os.write
    calls = 0

    def short_write(fd, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise InterruptedError()
        return real_write(fd, data[:3])

    monkeypatch.setattr(os, "write", short_write)
    write_metric(path, {"loss": 1}, t=2)
    assert MetricReader().read(path)["records"] == 1 and calls > 2


@pytest.mark.parametrize("option,maximum", [("max_bytes", MAX_READ_BYTES), ("max_points", MAX_POINTS), ("max_streams", MAX_STREAMS)])
def test_reader_rejects_impractical_or_platform_overflowing_limits(option, maximum):
    for value in [maximum + 1, 10 ** 1000]:
        with pytest.raises(ValueError):
            MetricReader(**{option: value})
    assert getattr(MetricReader(**{option: maximum}), option) == maximum


def test_local_nonregular_sources_are_errors_not_empty_streams(tmp_path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    target = tmp_path / "target"
    target.write_bytes(encoded())
    link = tmp_path / "link"
    link.symlink_to(target)
    for source in [fifo, link, tmp_path, "/dev/null"]:
        result = MetricReader().read(source)
        assert result["status"] == "error" and result["records"] == 0
        assert result["errors"]


@pytest.mark.parametrize("replacement", ["fifo", "symlink", "device"])
def test_local_stat_to_open_replacements_cannot_block_or_be_read(tmp_path, replacement):
    # Run under a deadline: removing O_NONBLOCK would hang the FIFO case.
    path = tmp_path / "stream"
    path.write_bytes(encoded())
    target = tmp_path / "target"
    target.write_bytes(encoded(metrics={"unreviewed": 123}))
    code = """
import json, os, sys
from pathlib import Path
from raven.metrics import MetricReader
path, target, replacement = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
real_open = os.open
triggered = False
def changed_open(name, flags, *args, **kwargs):
    global triggered
    if not triggered and os.fspath(name) == str(path):
        triggered = True
        if replacement == 'device':
            return real_open('/dev/null', flags, *args, **kwargs)
        path.unlink()
        if replacement == 'fifo':
            os.mkfifo(path)
        else:
            path.symlink_to(target)
    return real_open(name, flags, *args, **kwargs)
os.open = changed_open
result = MetricReader().read(path)
assert triggered and result['status'] == 'error' and not result['latest'], result
"""
    process = subprocess.run([sys.executable, "-c", code, str(path), str(target), replacement],
                             capture_output=True, timeout=3)
    assert process.returncode == 0, process.stderr.decode()


def test_inode_is_pinned_between_stat_and_incremental_read(tmp_path, monkeypatch):
    path = tmp_path / "stream"
    path.write_bytes(encoded(metrics={"original": 123}))
    moved = tmp_path / "old-stream"
    real_pread = os.pread
    triggered = False

    def rotated_pread(fd, length, offset):
        nonlocal triggered
        if not triggered:
            triggered = True
            path.rename(moved)
            os.mkfifo(path)
        return real_pread(fd, length, offset)

    monkeypatch.setattr(os, "pread", rotated_pread)
    reader = MetricReader()
    result = reader.read(path)
    assert result["latest"] == {"original": 123} and result["status"] == "ok"
    assert reader.read(path)["status"] == "error"


@pytest.mark.parametrize("contents", [b"", b"bad\n", encoded()])
def test_local_reader_always_closes_pinned_file_descriptors(tmp_path, monkeypatch, contents):
    path = tmp_path / "stream"
    path.write_bytes(contents)
    real_open, real_close = os.open, os.close
    opened, closed = [], []

    def tracking_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def tracking_close(fd):
        closed.append(fd)
        return real_close(fd)

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "close", tracking_close)
    reader = MetricReader()
    reader.read(path)
    reader.read(path)
    assert opened == closed and len(opened) == 2
