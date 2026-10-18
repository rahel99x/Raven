import json
from pathlib import Path

import pytest

from raven import distribution_profile as D


@pytest.fixture
def profile_path(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "__file__", str(tmp_path / "distribution_profile.py"))
    D.profile.cache_clear()
    yield tmp_path / "_distribution_profile.json"
    D.profile.cache_clear()


def test_complete_distribution_does_not_disable_any_builtin(profile_path):
    assert D.profile()["profile"] == "complete"
    assert not D.absent("raven.uninstalled_fake_lab")
    assert not D.missing_operation("statistics")


def test_only_explicit_absent_extension_is_optional(profile_path):
    profile_path.write_text(json.dumps({"profile": "core-experiment", "api": D.API,
        "optional_modules": ["raven.uninstalled_fake_lab"], "operations": {"test-lab": "raven.uninstalled_fake_lab"}}))
    assert D.absent("raven.uninstalled_fake_lab")
    assert D.missing_operation("test-lab")
    assert not D.absent("raven.slurm")
    assert "matching" in D.unavailable("test-lab")


@pytest.mark.parametrize("value", [{}, {"profile": "core-experiment", "api": 2, "optional_modules": [], "operations": {}},
    {"profile": "core-experiment", "api": 1, "optional_modules": ["os"], "operations": {}},
    {"profile": "core-experiment", "api": 1, "optional_modules": [], "operations": []}])
def test_incompatible_profile_fails_instead_of_hiding_broken_install(value, profile_path):
    profile_path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="Unsupported"):
        D.profile()


def test_missing_extension_keeps_saved_pin_identity(profile_path):
    from raven import ops_ui
    profile_path.write_text(json.dumps({"profile": "core-experiment", "api": D.API,
        "optional_modules": ["raven.uninstalled_fake_lab"], "operations": {"test-lab": "raven.uninstalled_fake_lab"}}))
    assert ops_ui._valid_pins(["test-lab", "unknown-operation"]) == ["test-lab"]
