"""Canonical package moves retain released feature keys and stored preferences."""

from types import SimpleNamespace

import pytest

from raven import workbench


def test_feature_names_and_preferences_do_not_follow_python_module_paths(monkeypatch):
    restored = []
    modules = tuple(
        SimpleNamespace(
            __name__=module_name,
            save=lambda app, value=index: {"setting": value},
            restore=lambda app, value, name=key: restored.append((name, value)),
        )
        for index, (key, module_name) in enumerate(
            (
                ("terminal_ui", "raven.terminal.interface"),
                ("job_selection", "raven.jobs.selection"),
            )
        )
    )
    monkeypatch.setattr(workbench, "modules", lambda: modules)
    monkeypatch.setattr(workbench, "FEATURES", ("terminal_ui", "job_selection"))
    workbench.module_map.cache_clear()
    workbench.owners.cache_clear()
    try:
        assert tuple(workbench.module_map()) == ("terminal_ui", "job_selection")
        assert workbench.owners()["terminal_ui"].name == "terminal_ui"
        assert workbench.owners()["job_selection"].name == "job_selection"
        saved = workbench.save(object())
        assert saved == {"terminal_ui": {"setting": 0}, "job_selection": {"setting": 1}}
        workbench.restore(object(), saved)
        assert restored == list(saved.items())
    finally:
        workbench.module_map.cache_clear()
        workbench.owners.cache_clear()


@pytest.mark.parametrize("keys", [("duplicate", "duplicate"), ("only-one",)])
def test_incomplete_or_duplicate_feature_composition_fails_at_binding(
    monkeypatch, keys
):
    monkeypatch.setattr(workbench, "modules", lambda: (object(), object()))
    monkeypatch.setattr(workbench, "FEATURES", keys)
    workbench.module_map.cache_clear()
    try:
        with pytest.raises(
            RuntimeError, match="one module for each stable feature key"
        ):
            workbench.module_map()
    finally:
        workbench.module_map.cache_clear()
