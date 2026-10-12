"""Narrow progress glyphs animate only published, visible cached cells."""
from types import SimpleNamespace

import pytest

from raven import job_progress as P, layout as L
from raven.model import Job


def fixture():
    app = SimpleNamespace(tab="jobs", mode="main", theme="default", animations_enabled=True,
                          job_progress_animation={"7": "clock", "8": "hourglass"})
    rows = [[("    PROG    JOBID", "cyan+bold")],
            [(" 前 ", "rev"), ("◷ █▋░░  7", "bg:track+cyan+rev")],
            [("    ", ""), ("⧗ wait  8", "bg:surface-raised+under+dim")]]
    hits = [(0, "sort_header", ("jobs", "progress", 4, 10)),
            (1, "job", "7"), (2, "job", "8")]
    P.publish_animation(app, rows, hits)
    return app, rows, hits


@pytest.mark.parametrize("basis", ["reported", "time"])
@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("fraction", [0, .001, .125, .42, .999, 1, 1.2, 900])
def test_progress_source_has_one_cell_gap_at_every_fraction(basis, ascii_, fraction):
    text = P.Observation(fraction, basis).format(ascii_)
    assert L.vlen(text) == len(text) == 6
    assert text[1] == " " and L.vlen(text[0]) == 1
    assert L.vlen(text[2:]) == 4
    if ascii_:
        assert text.isascii() and text[0] == ("t" if basis == "time" else "p")
    else:
        assert set(text[2:]) <= set("░▏▎▍▌▋▊▉█")


@pytest.mark.parametrize("fraction", [True, float("nan"), float("inf"), "42", None])
def test_invalid_embedded_observations_cannot_overflow_progress(fraction):
    assert P.Observation(fraction, "time").format() == "   -- "


@pytest.mark.parametrize("frames", [P.CLOCK_FRAMES, P.HOURGLASS_FRAMES])
def test_frames_use_single_text_cells_without_emoji_variation_selectors(frames):
    assert all(L.vlen(glyph) == 1 for glyph in frames)
    assert "\ufe0f" not in frames and "\u200d" not in frames


def test_clock_and_hourglass_cadence_preserves_content_styles_and_pristine_rows(monkeypatch):
    app, rows, _ = fixture()
    original = tuple(tuple(row) for row in rows)

    def forbidden(*args, **kwargs):
        pytest.fail("Cosmetic animation performed data acquisition or initialization")

    monkeypatch.setattr(P, "initialize", forbidden)
    for now, clock, hourglass in [(0, "◴", "⧗"), (.399, "◴", "⧗"), (.4, "◷", "⧗"),
                                  (.799, "◷", "⧗"), (.8, "◶", "⧖"), (1.21, "◵", "⧖"),
                                  (1.6, "◴", "⧗")]:
        result = P.animate_rows(app, rows, now=now)
        assert P._glyph_at(result[1], 4) == clock
        assert P._glyph_at(result[2], 4) == hourglass
        assert result[1][1][0][1:] == rows[1][1][0][1:]
        assert result[1][1][1] == "bg:track+cyan+rev"
        assert result[2][1][1] == "bg:surface-raised+under+dim"
        assert all(L.vlen(L.row_text(a)) == L.vlen(L.row_text(b)) for a, b in zip(rows, result))
        assert tuple(tuple(row) for row in rows) == original


@pytest.mark.parametrize("change", [{"animations_enabled": False}, {"mode": "help"},
                                    {"tab": "history"}, {"theme": "reader"}])
def test_stale_or_disabled_animation_returns_published_rows_unchanged(change):
    app, rows, _ = fixture()
    for key, value in change.items():
        setattr(app, key, value)
    assert P.animate_rows(app, rows, now=0) is rows


@pytest.mark.parametrize("now", [True, -1, float("nan"), float("inf"), "1"])
def test_invalid_time_cannot_change_cached_document(now):
    app, rows, _ = fixture()
    assert P.animate_rows(app, rows, now=now) is rows


@pytest.mark.parametrize("ascii_", [False, True])
def test_publication_clears_stale_slots_when_progress_column_is_hidden(ascii_):
    app, rows, hits = fixture()
    assert len(app.job_progress_state["animation_slots"]) == 2
    P.publish_animation(app, rows, hits[1:], ascii_=ascii_)
    assert app.job_progress_state["animation_slots"] == ()
    assert P.animate_rows(app, rows, now=0) is rows


def test_ascii_publication_does_not_animate_or_change_semantic_letters():
    app, rows, hits = fixture()
    rows[1][1] = ("t =>--  7", rows[1][1][1])
    rows[2][1] = ("w wait  8", rows[2][1][1])
    P.publish_animation(app, rows, hits, ascii_=True)
    assert app.job_progress_state["animation_slots"] == ()
    assert P.animate_rows(app, rows, now=.8) is rows


def test_only_visible_running_time_and_pending_rows_publish_slots():
    app, rows, hits = fixture()
    app.job_progress_animation["7"] = ""
    hits += [(900, "job", "8"), (True, "job", "8"), (1, "recent", "7"), (2, "job", "8")]
    P.publish_animation(app, rows, hits)
    assert app.job_progress_state["animation_slots"] == ((2, 4, "hourglass"),)
    result = P.animate_rows(app, rows, now=.8)
    assert result[1] is rows[1] and P._glyph_at(result[2], 4) == "⧖"


@pytest.mark.parametrize("header", [("jobs", "progress", 4, 9), ("jobs", "progress", -1, 5),
                                    ("recent", "progress", 4, 10), ("jobs", "id", 4, 10)])
def test_clipped_or_wrong_table_columns_never_publish_an_animation(header):
    app, rows, hits = fixture()
    hits[0] = (0, "sort_header", header)
    P.publish_animation(app, rows, hits)
    assert not app.job_progress_state["animation_slots"]


def test_animation_slots_are_bounded_and_duplicate_rows_are_not_retained():
    app = SimpleNamespace(tab="jobs", mode="main", theme="default", animations_enabled=True,
                          job_progress_animation={str(i): "clock" for i in range(600)})
    rows = [[("◷ ████", "cyan")]] * 600
    hits = [(0, "sort_header", ("jobs", "progress", 0, 6))]
    hits += [(y, "job", str(y)) for y in range(600)]
    P.publish_animation(app, rows, hits)
    assert len(app.job_progress_state["animation_slots"]) == P.MAX_ANIMATIONS


def test_list_form_header_payload_uses_the_same_published_bounds():
    app, rows, hits = fixture()
    hits[0] = (0, "sort_header", ["jobs", "progress", 4, 10])
    P.publish_animation(app, rows, hits)
    assert app.job_progress_state["animation_slots"] == ((1, 4, "clock"), (2, 4, "hourglass"))


def test_pending_overrides_prior_application_progress_after_requeue():
    job = Job("7", "retry", "cpu", "PENDING")
    result = P.observation(job, {"7": P.Source(.75)})
    assert result.basis == "pending" and result.fraction is None
    assert result.format() == "⧗ wait" and result.format(True) == "w wait"


def test_stale_cells_and_wide_glyph_boundaries_cannot_be_overwritten():
    app, rows, _ = fixture()
    rows[1] = [(" 前 ⏳ text", "cyan")]
    rows[2] = [("    replaced", "dim")]
    assert P.animate_rows(app, rows, now=.8) is rows
    assert P._replace_glyph([("前 clock", "cyan")], 1, "◴") == [("前 clock", "cyan")]


def test_malformed_hit_publication_is_ignored_and_previous_slots_are_cleared():
    app, rows, _ = fixture()
    P.publish_animation(app, rows, [None, "hit", (1,), (0, "sort_header", ("jobs", "progress", True, 7))])
    assert not app.job_progress_state["animation_slots"]


@pytest.mark.parametrize("ascii_,animations,theme", [(False, True, "default"),
                                                     (False, False, "default"),
                                                     (True, True, "default"),
                                                     (False, True, "reader")])
def test_cached_curses_feedback_animates_without_io_or_document_rebuild(monkeypatch, ascii_, animations, theme):
    from raven import screen
    from raven.config import Config
    from raven.controller import App
    from raven.model import Store
    from raven.views import Views

    cfg = Config({"animations": animations, "startup_animation": False, "log_lines": 0, "theme": theme})
    store = Store(persist=False)
    store.jobs = [Job("7", "render", "cpu", "RUNNING", elapsed="00:25:00", limit="01:00:00"),
                  Job("8", "wait", "cpu", "PENDING")]
    app = App(store, None, None, cfg, "test", interactive=True)
    views = Views(L.Glyphs(ascii_), cfg)
    app.views_ref = views
    cache = screen._FrameCache()
    cache.rebuild(app, views, store, None, 160, 40)
    pristine = tuple(tuple(row) for row in cache.rows)
    graph = app.interaction_state["graph"]
    deadlines = (cache.next_maintenance, cache.next_animation, cache.next_live)

    def forbidden(*args, **kwargs):
        pytest.fail("Progress animation rebuilt a document or read files")

    monkeypatch.setattr(app, "tick", forbidden)
    monkeypatch.setattr(store, "snapshot", forbidden)
    monkeypatch.setattr(views, "compose", forbidden)
    monkeypatch.setattr(views.files, "stat", forbidden)
    monkeypatch.setattr(views.files, "tail", forbidden)
    active = not ascii_ and animations and theme != "reader"
    assert len(app.job_progress_state["animation_slots"]) == (2 if active else 0)
    monkeypatch.setattr(P.time, "monotonic", lambda: .8)
    rows, _, _ = cache.feedback(app, views)
    if active:
        for y, x, kind in app.job_progress_state["animation_slots"]:
            assert P._glyph_at(rows[y], x) == ("◶" if kind == "clock" else "⧖")
    else:
        assert all(L.row_text(row) == L.row_text(cached) for row, cached in zip(rows, cache.rows))
    assert tuple(tuple(row) for row in cache.rows) == pristine
    assert app.interaction_state["graph"] is graph
    assert (cache.next_maintenance, cache.next_animation, cache.next_live) == deadlines
    if app.research:
        app.research.close()
