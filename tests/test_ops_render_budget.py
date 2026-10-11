"""Repeated operations paints reuse immutable definitions and review bounds."""
from types import SimpleNamespace

import pytest

from tower import layout as L, operations as O, ops_ui


@pytest.mark.parametrize("ascii_mode", [False, True])
@pytest.mark.parametrize("width", [24, 70, 150])
def test_contextual_and_catalog_paints_do_not_copy_registry(monkeypatch, ascii_mode, width):
    glyphs = L.Glyphs(ascii_mode)
    app = SimpleNamespace()
    ops_ui.initialize(app)
    specs = ops_ui._presentation_specs()
    def no_registry_copy(*args, **kwargs):
        raise AssertionError("A paint copied operation adapter definitions")
    monkeypatch.setattr(O, "catalog", no_registry_copy)
    monkeypatch.setattr(O, "specification", no_registry_copy)
    for _ in range(3):
        rows, hits = ops_ui.contextual_controls(glyphs, width, tuple(specs))
        assert rows and hits
        for _, _, hit in hits:
            assert 0 <= hit["left"] < hit["right"] <= width
            assert hit["action"][1].startswith("ops ")
        rows, hits = ops_ui.catalog_rows(glyphs, width)
        assert len(hits) == len(specs)


def test_cached_metadata_cannot_mutate_action_definitions():
    specs = ops_ui._presentation_specs()
    key = next(iter(specs))
    with pytest.raises(TypeError):
        specs[key]["title"] = "changed"
    with pytest.raises(TypeError):
        specs[key]["fields"][0]["key"] = "changed"
    prepared = O.specification(key)
    prepared["title"] = "isolated mutable copy"
    assert specs[key]["title"] != prepared["title"]


def test_review_wrap_bound_cached_and_invalidates_on_width_and_plan(monkeypatch):
    state = {"result": {"plan": {"digest": "first"}}}
    reads = []
    def lines(value):
        reads.append(value["result"]["plan"]["digest"])
        return ["x" * 1000] * 1000
    monkeypatch.setattr(ops_ui, "_review_lines", lines)
    assert ops_ui._review_is_truncated(state, 10)
    assert ops_ui._review_is_truncated(state, 10)
    assert len(reads) == 1
    assert not ops_ui._review_is_truncated(state, 1000)
    assert len(reads) == 2
    state["result"]["plan"] = {"digest": "second"}
    assert not ops_ui._review_is_truncated(state, 1000)
    assert reads == ["first", "first", "second"]


@pytest.mark.parametrize("ascii_mode", [False, True])
@pytest.mark.parametrize("width", [70, 150])
def test_standalone_analytics_retains_tools_inline_does_not_build_discarded_controls(monkeypatch, ascii_mode, width):
    from tower.config import Config
    from tower.controller import App
    from tower.model import Job, Store
    from tower.views import Views
    from tower import job_panels

    cfg = Config({"log_lines": 0, "animations": False, "startup_animation": False})
    store = Store(persist=False)
    store.jobs = [Job("81", "running", "main", "RUNNING", cpus=4)]
    app = App(store, None, None, cfg, "test", interactive=False)
    views = Views(L.Glyphs(ascii_mode), cfg)
    app.views_ref = views
    app.enter_tab("analytics")
    app.analytics_view, app.analytics_job = "job", "81"
    try:
        _, hits = views.analytics_tab(store.snapshot(), app, width, 60)
        assert {data["action"][1] for _, kind, data in hits if kind == "control" and data.get("group") == "operation-tools"} == {
            "ops bottlenecks", "ops statistics", "ops energy", "ops regression-investigation", "ops numerical-stability", "ops execution-simulation"}
        def discarded_controls(*args, **kwargs):
            raise AssertionError("Inline Analytics rebuilt discarded operation buttons")
        monkeypatch.setattr(ops_ui, "contextual_controls", discarded_controls)
        app.enter_tab("jobs")
        app.selected_id = "81"
        job_panels.run_command(app, ["jobpanel", "analytics", "job"])
        rows, _ = views.compose(store.snapshot(), app, width, 60)
        assert len(rows) == 60
    finally:
        if app.research:
            app.research.close()
