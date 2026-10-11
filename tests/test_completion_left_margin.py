"""Completion feedback cannot paint Details, grips or table content."""
from copy import deepcopy

import pytest

from tower import layout as L
from tower.config import Config
from tower.controller import App
from tower.model import Finished, Job, Store
from tower.screen import _DifferentialPainter
from tower.transitions import paint_completion_trails
from tower.views import Views
from tower.workspace_layout import Rect


def cells(row, width):
    return _DifferentialPainter._raster([(0, row)], width)


def move(*, source=3, progress=1.0):
    return [{"job": "77", "source": source, "progress": progress}]


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("comfortable", [False, True])
def test_trail_changes_only_free_left_margin_and_preserves_unicode_details(ascii_, comfortable):
    rows = [[("界界", "red"), (" >* " if comfortable else ">*  ", "text"),
             (" main job ".ljust(14), "green"), ("│Details λ🚀e\u0301", "cyan")]
            for _ in range(12)]
    original = deepcopy(rows)
    width = L.vlen(L.row_text(rows[0]))
    main = Rect(4, 2, 18, 8)
    result = paint_completion_trails(rows, [(7, "recent", "77")], move(), main=main,
                                    width=width, body_top=2, comfortable=comfortable, ascii_=ascii_)
    assert rows == original
    target = 4 if comfortable else 6
    changed = []
    for y, (before, after) in enumerate(zip(rows, result)):
        a, b = cells(before, width), cells(after, width)
        for x, (old, new) in enumerate(zip(a, b)):
            if old != new:
                changed.append((y, x))
                assert x == target and y in (5, 6, 7)
                assert new[0] in (("v", ":") if ascii_ else ("◆", "│"))
                assert new[1] in ("cyan+bold", "dim")
        assert a[main.x + main.width:] == b[main.x + main.width:]
    assert changed == [(5, target), (6, target), (7, target)]


def test_column_headers_queue_grips_and_occupied_cells_remain_untouched():
    rows = [[("   job", "text")] for _ in range(12)]
    main = Rect(0, 2, 10, 10)
    hits = [(7, "recent", "77"), (6, "sort_header", ("recent", "id", 4, 10)),
            (5, "control", {"id": "queue-grip:batch", "left": 2, "right": 3})]
    result = paint_completion_trails(rows, hits, move(), main=main, width=10, body_top=2)
    assert result[5:7] == rows[5:7]
    assert L.row_text(result[7]) == "  ◆job"
    rows[7] = [(" >*job", "text")]
    assert paint_completion_trails(rows, hits, move(), main=main, width=10, body_top=2) is rows


@pytest.mark.parametrize("occupied", [" 界job", "  界job", "  e\u0301job", "  >job", "x"])
def test_trails_skip_wide_combining_or_short_content(occupied):
    rows = [[(occupied, "text")]] * 10
    assert paint_completion_trails(rows, [(7, "recent", "77")], move(), main=Rect(0, 2, 9, 8),
                                   width=9, body_top=2) is rows


@pytest.mark.parametrize("width", range(7))
@pytest.mark.parametrize("height", range(6))
def test_tiny_main_panes_clip_without_expanding_rows(width, height):
    rows = [[(" " * width, "text")] for _ in range(height)]
    result = paint_completion_trails(rows, [(height - 1, "recent", "77")], move(source=-99),
                                    main=Rect(0, 0, width, height), width=width, body_top=0)
    assert len(result) == height
    assert all(L.vlen(L.row_text(row)) <= width for row in result)


def test_hidden_recents_and_hidden_main_have_no_trail():
    rows = [[("   job", "text")]] * 10
    for main, hits in ((None, [(7, "recent", "77")]), (Rect(0, 2, 9, 8), []),
                       (Rect(0, 2, 9, 5), [(9, "recent", "77")])):
        assert paint_completion_trails(rows, hits, move(), main=main, width=9, body_top=2) is rows


@pytest.mark.parametrize("width,height", [(90, 35), (130, 35), (180, 55), (8, 10)])
@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("density", ["compact", "comfortable"])
def test_composed_departure_stays_inside_main_margin(monkeypatch, width, height, ascii_, density):
    import tower.transitions as transitions

    now = [100.0]
    monkeypatch.setattr(transitions.time, "monotonic", lambda: now[0])
    cfg = Config({"animations": True, "log_lines": 0})
    store = Store(persist=False)
    watched = Job("77", "watched experiment", "main", "RUNNING", cpus=2)
    remaining = Job("900", "remaining experiment", "main", "RUNNING", cpus=4)
    store.apply_jobs([watched, remaining])
    store.apply_finished([Finished("66", "old experiment", "COMPLETED")])
    app = App(store, None, None, cfg, "reader", ascii_=ascii_, interactive=True)
    views = Views(L.Glyphs(ascii_), cfg)
    app.views_ref = views
    app.layout_state.density = density
    observations = []

    def checked(rows, hits, moves, **options):
        saved = deepcopy(rows)
        result = paint_completion_trails(rows, hits, moves, **options)
        assert saved == rows
        main = options["main"]
        if main is not None:
            target = main.x if density == "comfortable" and main.width >= 8 and main.height >= 7 else main.x + 2
            for y, (before, after) in enumerate(zip(rows, result)):
                a, b = cells(before, width), cells(after, width)
                for x, (old, new) in enumerate(zip(a, b)):
                    if old != new:
                        observations.append((y, x))
                        assert x == target
                        assert main.y < y < main.y + main.height
        return result

    monkeypatch.setattr(transitions, "paint_completion_trails", checked)
    try:
        views.compose(store.snapshot(), app, width, height, feedback=False)
        store.apply_jobs([remaining])
        views.compose(store.snapshot(), app, width, height, feedback=False)
        now[0] += 1.4
        rows, _ = views.compose(store.snapshot(), app, width, height, feedback=False)
        assert len(rows) == height
        assert all(L.vlen(L.row_text(row)) <= width for row in rows)
        if width >= 130:
            assert observations
    finally:
        if app.research:
            app.research.close()
