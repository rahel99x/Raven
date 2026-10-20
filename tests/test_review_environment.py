"""Hermetic CI roots and explicit capability evidence for the review harness."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import pwd

import pytest

from raven import doctor
from raven.config import Config
from raven.user_identity import current_user


FIXTURES = {
    "forecast/model.json": "c1c4fd9435a8a431290033264209dfd441cb8dd1c6fbf667718034ebdae30831",
    "forecast/result.json": "3dbb04e8fc6d326e936b2aec13bd776606a8c7ca79608917f633a320c0710aa9",
    "queue/model.json": "6e06b63591c2e57984f56b8226e8f73495faaf4c83acc800cbe7f4404bf9a11e",
    "queue/result.json": "056bef3d6a8a1125143c878e5d95822097643f801282c0fdd4c0131bb8c5b2ff",
}


def test_each_test_owns_its_home_and_xdg_roots(isolated_user_files, tmp_path):
    assert Path.home() == isolated_user_files
    assert isolated_user_files != tmp_path
    assert not list(tmp_path.iterdir())
    for variable in (
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
    ):
        assert Path(os.environ[variable]).is_relative_to(isolated_user_files)
    assert "RAVEN_CONFIG" not in os.environ
    assert "RAVEN_STATE_DIR" not in os.environ


@pytest.mark.parametrize("spoof", [None, "untrusted-shell-name"])
def test_login_identity_does_not_depend_on_USER(monkeypatch, spoof):
    if spoof is None:
        monkeypatch.delenv("USER", raising=False)
    else:
        monkeypatch.setenv("USER", spoof)
    assert current_user() == pwd.getpwuid(os.getuid()).pw_name


def test_missing_ssh_capability_is_an_explicit_fixture(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        doctor,
        "SshBackend",
        lambda *a, **k: pytest.fail("Missing SSH must not initiate a connection"),
    )
    result = doctor.diagnose(Config({}), host="fixture.example.invalid")
    (check,) = [check for check in result["checks"] if check["name"] == "SSH client"]
    assert check["status"] == "error" and not result["ready"]
    # This deliberately records a mocked missing capability, not the host's
    # installed tools or a successful remote scheduler connection.


def test_temporal_holdout_fixtures_preserve_original_bytes():
    root = Path(__file__).parent / "fixtures/review-temporal-holdout"
    assert sorted(
        str(path.relative_to(root)) for path in root.rglob("*.json")
    ) == sorted(FIXTURES)
    for name, expected in FIXTURES.items():
        raw = (root / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == expected
        json.loads(raw)
