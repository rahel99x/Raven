"""Declarative artifact contracts: correctness, bounded work, and filesystem safety."""
import hashlib
import json
import os

import pytest

from raven import artifacts
from raven.artifacts import load_contract, validate_contract


def contract(*outputs):
    return {"version": 1, "outputs": list(outputs)}


def check(result, name, index=0):
    return next(item for item in result["outputs"][index]["checks"] if item["name"] == name)


def test_complete_contract_with_csv_json_text_and_hash(tmp_path):
    csv_data = b"epoch,loss\r\n1,0.5\r\n\r\n2,0.25\r\n"
    (tmp_path / "metrics.csv").write_bytes(csv_data)
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "result.json").write_text('{"value": 42, "converged": true}')
    (tmp_path / "notes.txt").write_text("A complete result.\n")
    result = validate_contract(contract(
        {"path": "metrics.csv", "min_bytes": 1, "max_bytes": 4096, "format": "csv",
         "columns": ["loss"], "rows": 2, "min_rows": 1, "max_rows": 2,
         "sha256": hashlib.sha256(csv_data).hexdigest().upper()},
        {"path": "nested/result.json", "format": "json", "required_keys": ["value", "converged"]},
        {"path": "notes.txt", "format": "text"},
        {"path": "optional", "required": False}), tmp_path)
    assert result["status"] == "valid" and result["valid"] is True
    assert result["budget"]["entries_checked"] == 4
    assert result["budget"]["bytes_read"] == sum((tmp_path / p).stat().st_size
                                                for p in ["metrics.csv", "nested/result.json", "notes.txt"])
    assert check(result, "rows")["actual"] == 2
    assert result["outputs"][3]["missing"] is True
    assert json.loads(json.dumps(result, allow_nan=False)) == result


@pytest.mark.parametrize("path", ["/etc/passwd", "../secret", "nested/../secret", "./result", "a//b",
                                  "a/", "a\\b", "*.csv", "result?.json", "a[0]", "a\x00b", "a\nb", ""])
def test_unsafe_paths_never_touch_filesystem(path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("filesystem access before schema validation")
    monkeypatch.setattr(artifacts.os, "open", forbidden)
    result = validate_contract(contract({"path": path}), "/unused")
    assert result["status"] == "error" and result["outputs"] == []


@pytest.mark.parametrize("bad", [
    {}, {"version": 1, "outputs": [], "extra": True}, {"version": True, "outputs": []},
    {"version": 2, "outputs": []}, {"version": 1, "outputs": {}},
    contract({"path": "x", "required": 1}), contract({"path": "x", "min_bytes": True}),
    contract({"path": "x", "min_bytes": -1}), contract({"path": "x", "max_bytes": 1.2}),
    contract({"path": "x", "max_bytes": 10 ** 10000}),
    contract({"path": "x", "min_bytes": 2, "max_bytes": 1}),
    contract({"path": "x", "format": "csv", "min_rows": 2, "max_rows": 1}),
    contract({"path": "x", "format": "csv", "rows": 1, "min_rows": 2}),
    contract({"path": "x", "format": "csv", "rows": 3, "max_rows": 2}),
    contract({"path": "x", "format": []}), contract({"path": "x", "format": "binary"}),
    contract({"path": "x", "columns": ["a"]}), contract({"path": "x", "rows": 1}),
    contract({"path": "x", "required_keys": ["value"]}),
    contract({"path": "x", "format": "csv", "columns": ["a", "a"]}),
    contract({"path": "x", "format": "csv", "columns": [1]}),
    contract({"path": "x", "format": "csv", "columns": ["bad\ncolumn"]}),
    contract({"path": "x", "sha256": "bad"}), contract({"path": "x", "command": "touch nope"}),
    contract({"path": "x"}, {"path": "x"}),
])
def test_malformed_contract_has_no_filesystem_actions(bad, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("filesystem access before schema validation")
    monkeypatch.setattr(artifacts.os, "open", forbidden)
    result = validate_contract(bad, "/unused")
    assert result["status"] == "error" and result["outputs"] == []
    assert result["errors"]


@pytest.mark.parametrize("data", [b'{"version":1,"outputs":[],"version":1}',
                                b'{"version":1,"outputs":NaN}', b'{"version":1,"outputs":Infinity}',
                                b'{"version":1,"outputs":1e999}', b'{', b'\xff'])
def test_load_rejects_duplicate_nonfinite_malformed_or_nonutf8_json(tmp_path, data):
    path = tmp_path / "contract.json"
    path.write_bytes(data)
    with pytest.raises(ValueError):
        load_contract(path)


def test_load_contract_size_and_file_type_limits(tmp_path):
    path = tmp_path / "large.json"
    path.write_bytes(b" " * (artifacts.CONTRACT_MAX_BYTES + 1))
    with pytest.raises(ValueError, match="exceeds"):
        load_contract(path)
    fifo = tmp_path / "contract-fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular"):
        load_contract(fifo)
    with pytest.raises(ValueError, match="regular"):
        load_contract(tmp_path)
    target = tmp_path / "real.json"
    target.write_text(json.dumps(contract()))
    link = tmp_path / "linked.json"
    link.symlink_to(target)
    with pytest.raises(OSError):
        load_contract(link)


def test_load_good_contract_and_deep_json_rejection(tmp_path):
    path = tmp_path / "contract.json"
    expected = contract({"path": "results.json", "format": "json"})
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(expected).encode())
    assert load_contract(path) == expected
    path.write_text("[" * 2000 + "0" + "]" * 2000)
    with pytest.raises(ValueError):
        load_contract(path)


@pytest.mark.parametrize("fmt,data", [
    ("json", b"NaN"), ("json", b"1e999"), ("json", b'{"x":1,"x":2}'), ("json", b"{"),
    ("json", b""), ("json", b"\xff"), ("csv", b""), ("csv", b"a,a\n1,2\n"),
    ("csv", b"a,b\n1\n"), ("csv", b'a\n"unterminated\n'), ("text", b"\xff"),
])
def test_malformed_artifact_content_is_invalid(tmp_path, fmt, data):
    (tmp_path / "output").write_bytes(data)
    result = validate_contract(contract({"path": "output", "format": fmt}), tmp_path)
    assert result["status"] == "invalid"
    assert check(result, "format")["status"] == "fail"


def test_csv_missing_columns_counts_and_json_missing_keys(tmp_path):
    (tmp_path / "a.csv").write_text("a,b\n1,2\n")
    (tmp_path / "b.json").write_text('[{"value":42}]')
    result = validate_contract(contract(
        {"path": "a.csv", "format": "csv", "columns": ["a", "c"], "rows": 2, "min_rows": 2, "max_rows": 2},
        {"path": "b.json", "format": "json", "required_keys": ["value"]}), tmp_path)
    assert result["status"] == "invalid"
    assert check(result, "columns")["missing"] == ["c"]
    assert check(result, "rows")["status"] == "fail"
    assert check(result, "min_rows")["status"] == "fail"
    assert check(result, "max_rows")["status"] == "pass"
    assert check(result, "required_keys", 1)["missing"] == ["value"]


def test_missing_optional_missing_required_and_empty_file(tmp_path):
    (tmp_path / "empty").touch()
    result = validate_contract(contract({"path": "missing"}, {"path": "optional", "required": False},
                                        {"path": "empty", "min_bytes": 1}), tmp_path)
    assert result["status"] == "invalid"
    assert [item["status"] for item in result["outputs"]] == ["invalid", "valid", "invalid"]
    assert check(result, "min_bytes", 2)["actual"] == 0


@pytest.mark.parametrize("kind", ["outside_symlink", "inside_symlink", "parent_symlink", "fifo", "directory"])
def test_unsafe_file_types_never_read(tmp_path, monkeypatch, kind):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"not permitted")
    path = "output"
    if kind == "outside_symlink":
        (root / path).symlink_to(outside)
    elif kind == "inside_symlink":
        (root / "real").write_bytes(b"also refused")
        (root / path).symlink_to(root / "real")
    elif kind == "parent_symlink":
        (root / "sub").symlink_to(tmp_path, target_is_directory=True)
        path = "sub/outside"
    elif kind == "fifo":
        os.mkfifo(root / path)
    else:
        (root / path).mkdir()
    def forbidden(*args, **kwargs):
        raise AssertionError("unsafe file type read")
    monkeypatch.setattr(artifacts.os, "read", forbidden)
    result = validate_contract(contract({"path": path, "format": "text"}), root)
    assert result["status"] == "invalid" and result["budget"]["bytes_read"] == 0


def test_shared_byte_budget_and_entry_budget(tmp_path):
    for name in ["a", "b", "c"]:
        (tmp_path / name).write_bytes(b"1234")
    specs = contract(*[{"path": name, "format": "text"} for name in ["a", "b", "c"]])
    result = validate_contract(specs, tmp_path, max_bytes=6, max_entries=2)
    assert result["status"] == "incomplete" and result["valid"] is False
    assert result["budget"]["bytes_read"] == 4
    assert result["budget"]["entries_checked"] == 2
    assert [item["status"] for item in result["outputs"]] == ["valid", "not_checked", "not_checked"]
    assert "budget" in check(result, "format", 1)["message"]


def test_huge_sparse_output_size_checks_do_not_read_content(tmp_path, monkeypatch):
    path = tmp_path / "huge"
    with path.open("wb") as handle:
        handle.truncate(1 << 35)
    def forbidden(*args, **kwargs):
        raise AssertionError("over-budget file read")
    monkeypatch.setattr(artifacts.os, "read", forbidden)
    result = validate_contract(contract({"path": "huge", "min_bytes": 1, "format": "json", "sha256": "0" * 64}),
                               tmp_path, max_bytes=10)
    assert result["status"] == "incomplete"
    assert check(result, "min_bytes")["status"] == "pass"
    assert check(result, "format")["status"] == "not_checked"
    assert check(result, "sha256")["status"] == "not_checked"
    assert result["outputs"][0]["size"] == 1 << 35


def test_size_only_checks_do_not_consume_read_budget(tmp_path, monkeypatch):
    (tmp_path / "a").write_bytes(b"1234")
    def forbidden(*args, **kwargs):
        raise AssertionError("unrequested content read")
    monkeypatch.setattr(artifacts.os, "read", forbidden)
    result = validate_contract(contract({"path": "a", "min_bytes": 4, "max_bytes": 4}), tmp_path, max_bytes=0)
    assert result["status"] == "valid" and result["budget"]["bytes_read"] == 0


def test_sha256_mismatch_and_streamed_large_hash(tmp_path, monkeypatch):
    data = b"x" * 200000
    (tmp_path / "a").write_bytes(data)
    sizes = []
    real_read = os.read
    def track(fd, count):
        sizes.append(count)
        return real_read(fd, count)
    monkeypatch.setattr(artifacts.os, "read", track)
    result = validate_contract(contract({"path": "a", "sha256": "0" * 64}), tmp_path)
    assert result["status"] == "invalid"
    assert check(result, "sha256")["actual"] == hashlib.sha256(data).hexdigest()
    assert max(sizes) <= 65536 and len(sizes) > 1


def test_long_csv_fields_are_not_false_failures(tmp_path):
    (tmp_path / "a").write_text("a\n" + "x" * 140000 + "\n")
    result = validate_contract(contract({"path": "a", "format": "csv", "columns": ["a"], "rows": 1}), tmp_path)
    assert result["status"] == "incomplete"
    assert check(result, "format")["status"] == "not_checked"


@pytest.mark.parametrize("race", ["same_size_write", "truncate", "replace"])
def test_content_changes_are_incomplete_not_valid_or_invalid(tmp_path, monkeypatch, race):
    path = tmp_path / "a"
    path.write_bytes(b"old-data")
    real_read = os.read
    mutated = False
    def mutate(fd, count):
        nonlocal mutated
        if not mutated:
            mutated = True
            if race == "same_size_write":
                path.write_bytes(b"new-data")
                info = path.stat()
                os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
            elif race == "truncate":
                path.write_bytes(b"new")
            else:
                replacement = tmp_path / "replacement"
                replacement.write_bytes(b"new-data")
                os.replace(replacement, path)
        return real_read(fd, count)
    monkeypatch.setattr(artifacts.os, "read", mutate)
    result = validate_contract(contract({"path": "a", "format": "text", "sha256": "0" * 64}), tmp_path)
    assert result["status"] == "incomplete"
    assert result["outputs"][0]["stable"] is False
    assert all(item["status"] == "not_checked" for item in result["outputs"][0]["checks"])


def test_file_replaced_with_fifo_between_stat_and_open_cannot_hang(tmp_path, monkeypatch):
    path = tmp_path / "a"
    path.write_bytes(b"before")
    real_open = os.open
    def replace(file, flags, *args, **kwargs):
        if file == "a" and "dir_fd" in kwargs:
            path.unlink()
            os.mkfifo(path)
        return real_open(file, flags, *args, **kwargs)
    monkeypatch.setattr(artifacts.os, "open", replace)
    result = validate_contract(contract({"path": "a", "format": "text"}), tmp_path)
    assert result["status"] == "invalid" and result["budget"]["bytes_read"] == 0


def test_parent_directory_replacement_cannot_validate_the_old_named_file(tmp_path, monkeypatch):
    parent = tmp_path / "nested"
    parent.mkdir()
    (parent / "a").write_text("original")
    real_read = os.read
    def replace_parent(fd, count):
        data = real_read(fd, count)
        parent.rename(tmp_path / "old-nested")
        parent.mkdir()
        (parent / "a").write_text("replacement")
        return data
    monkeypatch.setattr(artifacts.os, "read", replace_parent)
    result = validate_contract(contract({"path": "nested/a", "format": "text"}), tmp_path)
    assert result["status"] == "incomplete"
    assert result["outputs"][0]["stable"] is False


def test_read_failure_is_error_and_file_descriptors_are_closed(tmp_path, monkeypatch):
    (tmp_path / "a").write_bytes(b"before")
    opened, closed = [], []
    real_open, real_dup, real_close = os.open, os.dup, os.close
    def track_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd
    def track_dup(*args):
        fd = real_dup(*args)
        opened.append(fd)
        return fd
    def track_close(fd):
        closed.append(fd)
        return real_close(fd)
    def fail_read(*args):
        raise PermissionError("read denied")
    monkeypatch.setattr(artifacts.os, "open", track_open)
    monkeypatch.setattr(artifacts.os, "dup", track_dup)
    monkeypatch.setattr(artifacts.os, "close", track_close)
    monkeypatch.setattr(artifacts.os, "read", fail_read)
    result = validate_contract(contract({"path": "a", "format": "text"}), tmp_path)
    assert result["status"] == "error" and result["errors"]
    assert sorted(opened) == sorted(closed)


@pytest.mark.parametrize("kwargs", [{"max_bytes": -1}, {"max_bytes": True}, {"max_bytes": float("nan")},
                                    {"max_bytes": artifacts.MAX_READ_BYTES + 1}, {"max_entries": -1},
                                    {"max_bytes": 10 ** 10000},
                                    {"max_entries": True}, {"max_entries": artifacts.MAX_OUTPUTS + 1}])
def test_invalid_budget_arguments_are_safe_json_errors(kwargs, monkeypatch):
    def forbidden(*args, **kw):
        raise AssertionError("invalid budget triggered filesystem access")
    monkeypatch.setattr(artifacts.os, "open", forbidden)
    result = validate_contract(contract({"path": "a"}), "/unused", **kwargs)
    assert result["status"] == "error" and result["outputs"] == []
    json.dumps(result, allow_nan=False)


def test_zero_entry_budget_never_opens_root(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("zero entry budget accessed filesystem")
    monkeypatch.setattr(artifacts.os, "open", forbidden)
    result = validate_contract(contract({"path": "a"}), tmp_path / "missing", max_entries=0)
    assert result["status"] == "incomplete"


def test_remote_reader_is_truthful_and_never_reads_unsafe_content():
    class Reader:
        remote = True
        paths = []
        def stat(self, path):
            self.paths.append(path)
            return 123, (1, 2)
        def read(self, *args):
            raise AssertionError("reader lacking path/type proof must not read")
    reader = Reader()
    result = validate_contract(contract({"path": "nested/a.json", "format": "json", "min_bytes": 1}),
                               "/job/work", files=reader)
    assert reader.paths == ["/job/work/nested/a.json"]
    assert result["status"] == "incomplete"
    assert result["outputs"][0]["size"] == 123
    assert check(result, "format")["status"] == "not_checked"
    assert check(result, "min_bytes")["status"] == "not_checked"
    assert result["budget"]["bytes_read"] == 0


def test_remote_missing_and_stat_errors_are_distinguished():
    class Reader:
        remote = True
        def stat(self, path):
            if path.endswith("missing"):
                raise FileNotFoundError(path)
            raise OSError("remote unavailable")
    result = validate_contract(contract({"path": "missing"}, {"path": "optional-missing", "required": False},
                                        {"path": "unknown"}), "/job", files=Reader())
    assert [item["status"] for item in result["outputs"]] == ["invalid", "valid", "error"]
    assert result["status"] == "error" and result["errors"]


@pytest.mark.parametrize("root", ["relative", "/a/../b", ""])
def test_remote_root_requires_absolute_unambiguous_path(root):
    class Reader:
        remote = True
        def stat(self, path):
            raise AssertionError("invalid root triggered remote action")
    assert validate_contract(contract({"path": "a"}), root, files=Reader())["status"] == "error"


def test_empty_contract_performs_no_filesystem_access():
    result = validate_contract(contract(), "/path/does/not/exist")
    assert result["status"] == "valid" and result["summary"] == "No outputs declared"


def test_contract_file_mutation_during_load_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contract()))
    real_read = os.read
    mutated = False
    def mutate(fd, size):
        nonlocal mutated
        data = real_read(fd, size)
        if not mutated:
            mutated = True
            path.write_text(json.dumps(contract({"path": "x"})))
        return data
    monkeypatch.setattr(artifacts.os, "read", mutate)
    with pytest.raises(ValueError, match="changed"):
        load_contract(path)


def test_safe_log_tail_is_byte_exact_and_bounded(tmp_path):
    path = tmp_path / "job.log"
    data = b"prefix" * 20000 + b"\xff\r\nend"
    path.write_bytes(data)
    observed, size, stable = artifacts.read_local_tail(path, 131072)
    assert observed == data[-131072:] and size == len(data) and stable
    assert artifacts.read_local_tail(path, 0) == (b"", len(data), True)


def test_named_contract_replacement_during_load_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contract()))
    real_read = os.read
    changed = False
    def replace(fd, count):
        nonlocal changed
        data = real_read(fd, count)
        if not changed:
            changed = True
            replacement = tmp_path / "new.json"
            replacement.write_text(json.dumps(contract({"path": "different"})))
            os.replace(replacement, path)
        return data
    monkeypatch.setattr(artifacts.os, "read", replace)
    with pytest.raises(ValueError, match="changed"):
        load_contract(path)


@pytest.mark.parametrize("kind", ["fifo", "symlink", "directory"])
def test_safe_log_tail_refuses_unsafe_file_type_without_reads(tmp_path, monkeypatch, kind):
    path = tmp_path / "job.log"
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "symlink":
        target = tmp_path / "target.log"
        target.write_text("data")
        path.symlink_to(target)
    else:
        path.mkdir()
    def forbidden(*args):
        raise AssertionError("unsafe log read")
    monkeypatch.setattr(artifacts.os, "read", forbidden)
    with pytest.raises((ValueError, OSError)):
        artifacts.read_local_tail(path)


def test_safe_log_tail_marks_changed_file_unstable(tmp_path, monkeypatch):
    path = tmp_path / "job.log"
    path.write_text("original")
    real_read = os.read
    def mutate(fd, count):
        data = real_read(fd, count)
        path.write_text("new data that is much longer")
        return data
    monkeypatch.setattr(artifacts.os, "read", mutate)
    data, size, stable = artifacts.read_local_tail(path)
    assert data == b"original" and size == 8 and stable is False


def test_safe_log_tail_does_not_leak_descriptors_on_read_errors(tmp_path, monkeypatch):
    path = tmp_path / "job.log"
    path.write_text("data")
    real_close = os.close
    closed = []
    def close(fd):
        closed.append(fd)
        real_close(fd)
    def fail_read(*args):
        raise OSError("log failed")
    monkeypatch.setattr(artifacts.os, "read", fail_read)
    monkeypatch.setattr(artifacts.os, "close", close)
    with pytest.raises(OSError, match="log failed"):
        artifacts.read_local_tail(path)
    assert len(closed) == 1


@pytest.mark.parametrize("budget", [-1, True, 1.5, artifacts.DEFAULT_MAX_BYTES + 1])
def test_safe_log_tail_rejects_bad_budgets_without_opening(budget, monkeypatch):
    def forbidden(*args):
        raise AssertionError("invalid tail budget opened path")
    monkeypatch.setattr(artifacts.os, "open", forbidden)
    with pytest.raises(ValueError):
        artifacts.read_local_tail("unused", budget)
