"""Planning input must be bounded, finite, stable, and safe on special files."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

import pytest

from raven.planning_io import load_json


def file(tmp_path, data):
    path = tmp_path / "plan.json"
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return path


@pytest.mark.parametrize("value", [{}, [], {"unicode": "训练", "flags": [True, False, None], "x": 1.25}, [0, -1, 1e99]])
def test_regular_utf8_objects_arrays_roundtrip(tmp_path, value):
    assert load_json(file(tmp_path, json.dumps(value, ensure_ascii=False))) == value


@pytest.mark.parametrize("data", ["null", "1", "true", '"hello"', "", "{", "[1,]", "{} extra", b"\xff{}", b"\xef\xbb\xbf{}"])
def test_invalid_syntax_encoding_and_scalar_roots_are_rejected(tmp_path, data):
    with pytest.raises(ValueError):
        load_json(file(tmp_path, data))


@pytest.mark.parametrize("data", ['{"a":1,"a":2}', '{"a":{"x":0,"x":1}}', '{"a":1,"\\u0061":2}'])
def test_duplicate_keys_are_not_silently_overwritten(tmp_path, data):
    with pytest.raises(ValueError, match="duplicate JSON key"):
        load_json(file(tmp_path, data))


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e400", "-1e400"])
def test_nonfinite_constants_and_exponents_are_rejected(tmp_path, value):
    with pytest.raises(ValueError):
        load_json(file(tmp_path, '{"value":' + value + '}'))


def test_integer_bit_budget_including_negative_values(tmp_path):
    allowed = (1 << 256) - 1
    assert load_json(file(tmp_path, json.dumps({"x": allowed}))) == {"x": allowed}
    assert load_json(file(tmp_path, json.dumps({"x": -allowed}))) == {"x": -allowed}
    for value in (1 << 256, -(1 << 256)):
        with pytest.raises(ValueError):
            load_json(file(tmp_path, json.dumps({"x": value})))


def test_large_integer_is_rejected_before_unguarded_decimal_conversion(tmp_path):
    # This catches Python 3.10's unbounded decimal conversion as well as 3.11's
    # interpreter-specific fallback. The loader must own a stable size limit.
    with pytest.raises(ValueError, match="integer"):
        load_json(file(tmp_path, '{"x":' + "9" * 8000 + '}'))


@pytest.mark.parametrize("bad", [True, 0, -1, 16 * 1024**2 + 1, "10", 1.5, None])
def test_invalid_read_budget_rejected_before_open(tmp_path, bad):
    with pytest.raises(ValueError):
        load_json(tmp_path / "absent", max_bytes=bad)


@pytest.mark.parametrize("bad", [True, 0, -1, 65, "10", 1.5, None])
def test_invalid_depth_budget_rejected_before_open(tmp_path, bad):
    with pytest.raises(ValueError):
        load_json(tmp_path / "absent", max_depth=bad)


def test_exact_byte_budget_and_one_byte_over(tmp_path):
    path = file(tmp_path, "{}")
    assert load_json(path, max_bytes=2) == {}
    with pytest.raises(ValueError, match="read limit"):
        load_json(path, max_bytes=1)


def test_unicode_byte_count_not_character_count(tmp_path):
    content = '{"x":"训练"}'
    path = file(tmp_path, content)
    with pytest.raises(ValueError, match="read limit"):
        load_json(path, max_bytes=len(content))
    assert load_json(path, max_bytes=len(content.encode("utf-8"))) == {"x": "训练"}


def test_depth_budget_is_enforced_on_already_parsed_values(tmp_path):
    assert load_json(file(tmp_path, "[" * 32 + "0" + "]" * 32), max_depth=32)
    with pytest.raises(ValueError, match="depth limit"):
        load_json(file(tmp_path, "[" * 33 + "0" + "]" * 33), max_depth=32)


def test_parser_recursion_error_is_normalized(tmp_path):
    with pytest.raises(ValueError):
        load_json(file(tmp_path, "[" * 2000 + "0" + "]" * 2000))


def test_item_budget_is_independent_of_small_syntax_byte_size(tmp_path):
    with pytest.raises(ValueError, match="item or depth limit"):
        load_json(file(tmp_path, "[" + ",".join("0" for _ in range(100000)) + "]"))


def test_final_symlink_is_not_followed(tmp_path):
    original = file(tmp_path, "{}")
    link = tmp_path / "linked.json"
    link.symlink_to(original)
    with pytest.raises((ValueError, OSError)):
        load_json(link)


def test_directory_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="regular file"):
        load_json(tmp_path)


def test_fifo_without_writer_is_rejected_without_waiting(tmp_path):
    path = tmp_path / "pipe"
    os.mkfifo(path)
    start = time.monotonic()
    with pytest.raises(ValueError, match="regular file"):
        load_json(path)
    assert time.monotonic() - start < 1


def test_character_device_is_rejected():
    if not Path("/dev/null").exists():
        pytest.skip("POSIX character device is unavailable")
    with pytest.raises(ValueError, match="regular file"):
        load_json("/dev/null")


def test_missing_file_reports_os_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_json(tmp_path / "missing.json")


def test_file_growth_during_read_is_rejected(tmp_path, monkeypatch):
    path = file(tmp_path, "{}")
    real_read = os.read
    changed = False
    def read(fd, size):
        nonlocal changed
        data = real_read(fd, size)
        if not changed:
            changed = True
            with path.open("ab") as stream:
                stream.write(b" ")
        return data
    monkeypatch.setattr(os, "read", read)
    with pytest.raises(ValueError, match="changed during inspection"):
        load_json(path)


def test_same_size_mutation_during_read_is_rejected(tmp_path, monkeypatch):
    path = file(tmp_path, '{"x":1}')
    real_read = os.read
    changed = False
    def read(fd, size):
        nonlocal changed
        data = real_read(fd, size)
        if not changed:
            changed = True
            path.write_text('{"x":2}')
            before = path.stat()
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000))
        return data
    monkeypatch.setattr(os, "read", read)
    with pytest.raises(ValueError, match="changed during inspection"):
        load_json(path)


def test_named_path_replacement_during_read_is_rejected(tmp_path, monkeypatch):
    path = file(tmp_path, '{"x":1}')
    replacement = tmp_path / "replacement"
    replacement.write_text('{"x":2}')
    real_read = os.read
    changed = False
    def read(fd, size):
        nonlocal changed
        data = real_read(fd, size)
        if not changed:
            changed = True
            replacement.replace(path)
        return data
    monkeypatch.setattr(os, "read", read)
    with pytest.raises(ValueError, match="changed during inspection"):
        load_json(path)


@pytest.mark.parametrize("content", ["{}", "not JSON", "[NaN]"])
def test_fd_is_closed_after_success_or_parse_failure(tmp_path, monkeypatch, content):
    path = file(tmp_path, content)
    closed = []
    real_close = os.close
    def close(fd):
        closed.append(fd)
        return real_close(fd)
    monkeypatch.setattr(os, "close", close)
    try:
        load_json(path)
    except ValueError:
        pass
    assert len(closed) == 1


def test_parent_symlink_policy_is_explicitly_final_component_only(tmp_path):
    # The caller explicitly selects a local file; this loader does not claim
    # root confinement or forbid aliases for parent directories.
    directory = tmp_path / "real"
    directory.mkdir()
    (directory / "plan.json").write_text("{}")
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    assert load_json(alias / "plan.json") == {}
