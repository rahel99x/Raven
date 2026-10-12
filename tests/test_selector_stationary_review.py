"""A stationary terminal cell must not jitter into a neighboring Braille slot."""
from types import SimpleNamespace

from raven import chart_interaction as C, selector_glyphs as G


def test_identical_visual_endpoints_remain_exact_at_every_animation_fraction():
    center = (5.5, 30.5)
    for index in range(1, 800):
        position = G.interpolate(center, center, index / 10000)
        assert position == center
        cell = G.locate(*position)
        assert (cell.row, cell.column, cell.y_slot, cell.x_slot) == (5, 30, 2, 1)


def test_idle_selector_has_stable_subcell_strokes_and_no_animation_or_source_work(monkeypatch):
    now = {"value": 0.0}
    monkeypatch.setattr(C.time, "monotonic", lambda: now["value"])

    def forbidden(*args, **kwargs):
        raise AssertionError("Idle selector read a source or rasterized a graph")

    app = SimpleNamespace(mode="main", tab="analytics", width=120, height=40,
        selected_id="7", analytics_job="7", analytics_view="job",
        toolbar_state={}, project_state={}, job_panel_state={},
        animations_enabled=True, cfg={"animations": True}, theme="darcula",
        store=SimpleNamespace(snapshot=forbidden),
        research=SimpleNamespace(generation=1, current=forbidden, request=forbidden))
    C.begin_frame(app, app.width, app.height)
    C.record(app, ("resource-series", "7", "cpu", "%", "first"), {
        "plot_rect": (1, 10, 20, 110), "x_bounds": (0., 100.), "y_bounds": (0., 200.)})
    C.publish(app, app.width, app.height)
    assert C.hover(app, 5, 30)
    monkeypatch.setattr(C.charts, "braille_chart", forbidden)
    monkeypatch.setattr(C.charts, "vbar_chart", forbidden)
    with monkeypatch.context() as no_source:
        no_source.setattr("builtins.open", forbidden)
        for elapsed in (.0001, .0002, .0007, .0017, .0399, .08):
            now["value"] = elapsed
            strokes = C.feedback(app)
            assert {char for _, _, row in strokes for char, _ in row} == {"⠤", "⢸", "⢼"}
            assert C.initialize(app)["pointer"] == (5, 30)
            assert C.next_deadline(app) == float("inf")
            assert not C.active(app) and not C.initialize(app)["zoom"]
