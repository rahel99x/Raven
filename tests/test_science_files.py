"""Native file identities remain exact across the bounded worker protocol."""
from types import SimpleNamespace

import pytest

from tower import raven_protocol as protocol, science_files as science


UNSIGNED_INODE = (1 << 64) - 1


def test_signature_preserves_normal_values_and_signed_boundaries():
    values = (3, 41, 1024, -(1 << 63), (1 << 63) - 1)
    native = SimpleNamespace(**dict(zip(
        ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"), values)))
    assert science.signature(native) == values
    assert all(isinstance(part, int) for part in science.signature(native))


@pytest.fixture
def unsigned_source(tmp_path, monkeypatch):
    path = tmp_path / "source.json"
    path.write_text('{"value": 1}\n')
    native = path.stat()
    current = {"inode": UNSIGNED_INODE}
    original_stat, original_fstat = science.os.stat, science.os.fstat

    class NativeIdentity:
        def __init__(self, value):
            self.value = value
            self.st_ino = current["inode"]

        def __getattr__(self, name):
            return getattr(self.value, name)

    def captured(value):
        if (value.st_dev, value.st_ino) == (native.st_dev, native.st_ino):
            return NativeIdentity(value)
        return value

    monkeypatch.setattr(science.os, "stat", lambda *args, **kwargs: captured(original_stat(*args, **kwargs)))
    monkeypatch.setattr(science.os, "fstat", lambda *args, **kwargs: captured(original_fstat(*args, **kwargs)))
    return path, current


def test_max_unsigned_inode_snapshot_roundtrips_through_worker_protocol(unsigned_source):
    path, _current = unsigned_source
    record, _ = science.snapshot(path)
    assert record["signature"][1] == str(UNSIGNED_INODE)
    decoder = protocol.Decoder()
    decoder.feed(protocol.encode({"source": record}))
    restored = decoder.take()[0]["source"]
    assert restored == record
    assert science.verify(restored) == record
    with pytest.raises(protocol.ProtocolError, match="signed 64-bit"):
        protocol.encode({"inode": UNSIGNED_INODE})


@pytest.mark.parametrize("action", ["verify", "publish"])
def test_changed_unsigned_inode_refuses_reviewed_source(unsigned_source, tmp_path, action):
    path, current = unsigned_source
    record, _ = science.snapshot(path)
    current["inode"] = UNSIGNED_INODE - 1
    destination = tmp_path / "published"
    with pytest.raises(ValueError, match="Reviewed source changed"):
        if action == "verify":
            science.verify(record)
        else:
            science.publish(destination, copies=[(record, "model.json")])
    assert not destination.exists()
