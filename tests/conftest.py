"""Each test owns its user files, including inherited CLI subprocess state.

Keep login identity and timezone independent: tests may unset/spoof USER or
select TZ explicitly. Isolation supplies filesystem roots, not fake capability
or scheduler evidence.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_user_files(tmp_path_factory, monkeypatch):
    # Keep setup/review tests' own tmp_path completely untouched.
    home = tmp_path_factory.mktemp("raven-user")
    monkeypatch.setenv("HOME", str(home))
    for variable, folder in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_DATA_HOME", "data"),
    ):
        monkeypatch.setenv(variable, str(home / folder))
    for variable in (
        "RAVEN_CONFIG",
        "RAVEN_PROFILE",
        "RAVEN_STATE_DIR",
        "RAVEN_EXPORT_DIR",
        "TOWER_CONFIG",
        "TOWER_STATE_DIR",
        "BASH_ENV",
        "ENV",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("PYTHONNOUSERSITE", "1")
    return home
