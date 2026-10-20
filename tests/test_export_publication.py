"""A named export becomes visible only after its complete inode is flushed."""

import os
from pathlib import Path
import stat

import pytest

from raven import export


def test_named_export_is_invisible_until_flushed_and_published(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_EXPORT_DIR", str(tmp_path))
    target = tmp_path / "results.csv"
    fsync = os.fsync
    observed = []

    def inspect(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            observed.append(target.exists())
            assert not target.exists()
        else:
            assert target.read_text() == "complete,record\n" * 1000
        return fsync(fd)

    monkeypatch.setattr(export.os, "fsync", inspect)
    result = export.write(None, target.name, "complete,record\n" * 1000)
    assert observed == [False]
    assert Path(result).read_text() == "complete,record\n" * 1000
    assert target.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".raven-export-*"))


@pytest.mark.parametrize("failure", ["flush", "publish"])
def test_failed_export_does_not_publish_or_leave_temporary_files(
    tmp_path, monkeypatch, failure
):
    monkeypatch.setenv("RAVEN_EXPORT_DIR", str(tmp_path))

    def fail(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(export.os, "fsync" if failure == "flush" else "link", fail)
    with pytest.raises(OSError, match="disk unavailable"):
        export.write(None, "results.csv", "complete,record\n")
    assert list(tmp_path.iterdir()) == []
