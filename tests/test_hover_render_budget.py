"""Frame feedback has bounded work and keeps exact terminal-cell identities."""
from types import SimpleNamespace

import pytest

from raven import interaction as ui, layout as L


def application(width=180, height=80):
    app = SimpleNamespace(mode="main", tab="jobs", width=width, height=height,
                          selected_id="7", last_hits=[])
    from raven.workbench import bind_actions
    bind_actions(app)
    return app


def button(identity, y, left=2, right=12, action=None):
    return (y, "control", {"id": identity, "left": left, "right": right,
                           "action": action or ("command", "view job")})


def test_pointer_lookup_ignores_other_terminal_rows(monkeypatch):
    app = application()
    rows = [[(" " * 180, "")]] * 80
    hits = [button(f"{y}:{x}", y, x, x + 10) for y in range(80) for x in range(0, 160, 16)]
    graph = ui.publish(app, rows, hits, 180, 80)
    checks = []
    original = ui.Rect.contains

    def contains(rect, y, x):
        checks.append(rect)
        return original(rect, y, x)

    monkeypatch.setattr(ui.Rect, "contains", contains)
    assert graph.at(21, 20).id == "21:16"
    assert len(checks) <= 10
    assert all(rect.top == 21 for rect in checks)
    assert graph.get("79:144").id == "79:144"


def test_spanning_and_single_row_controls_keep_layer_area_and_stable_ties():
    app = application()
    row = ui.Control("row", "row", ui.Rect(2, 0, 3, 80), ("key", "enter"), button=False)
    first = ui.Control("first", "first", ui.Rect(2, 5, 3, 10), ("key", "enter"))
    second = ui.Control("second", "second", first.rect, ("key", "enter"))
    spanning = ui.Control("span", "span", ui.Rect(1, 0, 5, 80), ("key", "enter"), layer=4)
    graph = ui.Graph((row, first, second, spanning), (), 180, 80, 1)
    assert graph.at(2, 7) is spanning
    graph = ui.Graph((row, first, second), (), 180, 80, 2)
    assert graph.at(2, 7) is first
    assert graph.at(2, 20) is row
    assert graph.at(3, 7) is None
    with pytest.raises(TypeError):
        graph._identities["first"] = second


def test_unchanged_frame_reuses_graph_but_mutated_actions_and_job_identity_do_not():
    app = application()
    rows = [[(" Buttons ", "")]] * 80
    hit = button("one", 2, action=["command", "view job"])
    first = ui.publish(app, rows, [hit], 180, 80)
    assert ui.publish(app, rows, [hit], 180, 80) is first
    hit[2]["action"][1] = "view history"
    changed = ui.publish(app, rows, [hit], 180, 80)
    assert changed is not first
    assert first.get("one").action == ("command", "view job")
    assert changed.get("one").action == ("command", "view history")
    app.selected_id = "8"
    assert ui.publish(app, rows, [hit], 180, 80) is not changed


def test_overlay_mask_changes_cannot_reuse_a_visible_job_control():
    app = application()
    rows = [[(" job seven", "")]] * 80
    first = ui.publish(app, rows, [(2, "job", "7")], 180, 80)
    masked = ui.publish(app, rows, [(2, "job", "7")], 180, 80,
                        overlays=[(2, 0, [(" covered by dropdown ", "")])])
    assert first.get("job:7") and masked is not first
    assert masked.get("job:7") is None


def test_decoration_only_visits_actual_feedback_rows_and_keeps_pristine_data(monkeypatch):
    app = application(height=1000)
    rows = [[("ordinary content " * 10, "cyan+dim")]] * 1000
    ui.publish(app, rows, [button("hover", 12), button("focus", 780)], 180, 1000)
    ui.handle_mouse(app, 12, 4, "motion")
    state = ui.initialize(app)
    state.update(active=True, focused="focus")
    visited = []
    from raven.ui import control_feedback
    original = control_feedback._decorate_row

    def decorate(row, ranges):
        visited.append(row)
        return original(row, ranges)

    monkeypatch.setattr(control_feedback, "_decorate_row", decorate)
    painted = ui.decorate(app, rows)
    assert ui.feedback_rows(app) == frozenset((12, 780))
    assert len(visited) == 2
    assert painted[11] is rows[11] and painted[781] is rows[781]
    assert all(L.row_text(before) == L.row_text(after) for before, after in zip(rows, painted))
    assert rows[12] == [("ordinary content " * 10, "cyan+dim")]
    previous = ui.feedback_rows(app)
    ui.handle_mouse(app, 20, 4, "motion")
    assert previous | ui.feedback_rows(app) == frozenset((12, 780))


def test_overlay_canvas_resolves_graph_context_once(monkeypatch):
    app = application()
    app.mode = "analysis"
    app.analysis_state = {"control_hits": [button("section", 25, 11, 19)]}
    overlays = [(y, 10, [("| section |", "cyan+dim")]) for y in range(80)]
    ui.publish(app, [], [], 180, 80, overlays=overlays)
    ui.handle_mouse(app, 25, 12, "motion")
    calls = []
    from raven.ui import interaction_state
    original = interaction_state._context

    def context(app):
        calls.append(1)
        return original(app)

    monkeypatch.setattr(interaction_state, "_context", context)
    painted = ui.decorate_overlays(app, overlays)
    assert len(calls) == 1
    assert painted[24][2] is overlays[24][2]
    assert any("under" in style for _, style in painted[25][2])
    assert [L.row_text(row) for _, _, row in painted] == [L.row_text(row) for _, _, row in overlays]


@pytest.mark.parametrize("text", ["  simple label", "  界e\u0301label", "  e\u0301界😀", "\u0301leading mark"])
@pytest.mark.parametrize("left,right", [(0, 2), (1, 4), (2, 8), (4, 50)])
def test_fast_feedback_preserves_text_cell_width_and_combining_base(text, left, right):
    row = [(text[:3], "cyan+dim"), (text[3:], "yellow+bold")]
    painted = ui._decorate_row(row, [(left, right, ui.POINTER_STYLE)])
    assert L.row_text(painted) == text
    assert L.vlen(L.row_text(painted)) == L.vlen(text)
    for segment, style in painted:
        if segment.startswith("\u0301"):
            assert text.startswith("\u0301")


def columns():
    return [L.Column("id", "JOBID", 4, 8, ">"), L.Column("name", "NAME", 8, 32, flex=True),
            L.Column("where", "WHERE", 6, 12), L.Column("state", "STATE", 5, 12)]


@pytest.mark.parametrize("width,expected,kept", [
    (80, {"id": 8, "name": 32, "where": 12, "state": 9}, ["id", "name", "where", "state"]),
    (40, {"id": 8, "name": 19, "state": 9}, ["id", "name", "state"]),
    (25, {"id": 8, "name": 15}, ["id", "name"]),
    (15, {"id": 8, "name": 6}, ["id", "name"]),
    (5, {"id": 8, "name": 6}, ["id", "name"]),
])
def test_table_fit_keeps_existing_flex_drop_and_width_floor(width, expected, kept):
    rows = [{"id": "123456789", "name": "界" * 20, "where": "cluster-name", "state": "COMPLETED"}]
    actual, selected = L.fit_columns(columns(), rows, width, droppable=("where", "state"))
    assert actual == expected
    assert [column.key for column in selected] == kept
    rendered, _ = L.table(columns(), rows, width, droppable=("where", "state"))
    assert all(L.vlen(L.row_text(row)) <= width for row in rendered)


def test_table_fit_measures_each_value_once_even_when_dropping_columns(monkeypatch):
    rows = [{"id": str(i), "name": "界" * 20, "where": "cluster-name", "state": "COMPLETED"}
            for i in range(500)]
    calls = []
    original = L.vlen

    def width(text):
        calls.append(text)
        return original(text)

    monkeypatch.setattr(L, "vlen", width)
    actual, kept = L.fit_columns(columns(), rows, 12, droppable=("where", "state"))
    assert [column.key for column in kept] == ["id", "name"]
    assert len(calls) == 4 * (len(rows) + 1)
    assert actual["name"] == 6


def test_unicode_width_cache_is_bounded_and_long_log_lines_are_not_retained():
    L._short_unicode_width.cache_clear()
    assert L.vlen("e\u0301界😀") == 5
    assert L.vlen(" e\u0301界😀 ") == 7
    for index in range(2200):
        assert L.vlen("界" + str(index)) == 2 + len(str(index))
    assert L._short_unicode_width.cache_info().currsize == 2048
    before = L._short_unicode_width.cache_info()
    assert L.vlen("界" * 10000) == 20000
    assert L._short_unicode_width.cache_info() == before
    assert L.vlen("ASCII" * 10000) == 50000


def test_hover_lookup_skips_tall_controls_from_unrelated_rows(monkeypatch):
    controls = tuple(ui.Control(str(index), "rail", ui.Rect(index * 4, 5, index * 4 + 3, 8),
                                ("key", "enter")) for index in range(1000))
    graph = ui.Graph(controls, (), 80, 4000, 1)
    calls = []
    original = ui.Rect.contains

    def contains(rect, y, x):
        calls.append(rect)
        return original(rect, y, x)

    monkeypatch.setattr(ui.Rect, "contains", contains)
    assert graph.at(333, 6).id == "83"
    assert len(calls) <= 11
    # Sparse storage keeps a tall rail once, including on very tall terminals.
    assert len(graph._spanning) == len(controls)


def test_empty_direct_control_rectangles_do_not_create_recursive_span_indexes():
    controls = (ui.Control("empty", "empty", ui.Rect(5, 1, 5, 8), ("key", "enter")),
                ui.Control("inverted", "inverted", ui.Rect(7, 1, 2, 8), ("key", "enter")))
    graph = ui.Graph(controls, (), 80, 40, 1)
    assert graph.at(3, 4) is None
    assert graph.at(5, 4) is None


def test_sparse_interval_hit_lookup_keeps_exact_layer_area_and_paint_order():
    import random

    randomizer = random.Random(713)
    controls = []
    for index in range(160):
        top, left = randomizer.randrange(40), randomizer.randrange(70)
        rect = ui.Rect(top, left, top + randomizer.randrange(1, 12), left + randomizer.randrange(1, 12))
        controls.append(ui.Control(str(index), str(index), rect, ("key", "enter"),
                                   button=bool(index % 2), layer=index % 5))
    graph = ui.Graph(tuple(controls), (), 90, 60, 1)
    for y in range(60):
        for x in range(0, 90, 3):
            matching = [(control.layer, control.button,
                         -(control.rect.right - control.rect.left) * (control.rect.bottom - control.rect.top),
                         -index, control)
                        for index, control in enumerate(controls) if control.rect.contains(y, x)]
            expected = max(matching, key=lambda item: item[:4])[-1] if matching else None
            assert graph.at(y, x) is expected


def test_hover_viewport_signature_does_not_materialize_long_preference_collections():
    class BoundedSequence:
        def __init__(self, limit):
            self.limit = limit
            self.visited = 0

        def __iter__(self):
            for index in range(100000):
                self.visited += 1
                assert self.visited <= self.limit
                yield str(index)

    app = application()
    hidden, order, collapsed = BoundedSequence(64), BoundedSequence(64), BoundedSequence(256)
    app.table_state = {"hidden": {"jobs": hidden}, "order": {"jobs": order}, "collapsed": collapsed}
    ui._viewport(app)
    assert hidden.visited == 64 and order.visited == 64 and collapsed.visited == 256
