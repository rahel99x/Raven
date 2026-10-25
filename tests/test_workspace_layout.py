"""Theme contrast and real bounded panel interactions across terminal sizes."""
import json
from types import SimpleNamespace

import pytest

from raven import layout as L, palette as P, workspace_layout as W


def app(**settings):
    instance = SimpleNamespace(cfg={"workspace": settings}, tab="jobs", mode="main", selected_id="job-2",
                               messages=[], saves=0)
    instance.say = lambda text: instance.messages.append(text)
    instance.fail = lambda text: instance.messages.append("ERROR: " + text)
    def persist():
        instance.saves += 1
    instance.save = persist
    return instance


def body(ascii_=False, details=28):
    g = L.Glyphs(ascii_)
    rows = [L.rule(g, 150, "jobs"), [(" JOBID  NAME", "heading+bold")],
            [(" job-1  preparing", "")], [(" job-2  running", "rev")],
            L.rule(g, 150, "selected")]
    rows.extend([(f" detail {n:02}: original metadata", "cyan" if n == 0 else "")] for n in range(details))
    rows.extend([L.rule(g, 150, "recent"), [(" JOBID  NAME", "bold")], [(" job-old complete", "green")]])
    return rows, [(2, "job", "job-1"), (3, "job", "job-2"), (len(rows) - 1, "recent", "job-old")]


@pytest.mark.parametrize("density", W.DENSITIES)
@pytest.mark.parametrize("focus", W.PANELS)
def test_geometry_fits_tiny_narrow_and_wide_screens(density, focus):
    instance = app(density=density)
    state = W.initialize(instance)
    state.focus = focus
    for width in (0, 1, 3, 40, 48, 80, 109, 110, 160):
        for height in (0, 1, 3, 7, 8, 15, 40):
            for ratio in (20, 45, 80):
                state.ratio = ratio
                rects = W.geometry(instance, width, height)
                for rect in rects.values():
                    assert 0 <= rect.x < width and 0 <= rect.y < height
                    assert 0 < rect.width <= width - rect.x
                    assert 0 < rect.height <= height - rect.y
                values = list(rects.values())
                if len(values) == 2:
                    a, b = values
                    assert a.x + a.width <= b.x or a.y + a.height <= b.y


@pytest.mark.parametrize("width,height,ascii_", [(1, 1, True), (40, 8, True), (80, 20, False), (160, 26, False)])
def test_panel_transform_preserves_exact_hit_identity_and_width(width, height, ascii_):
    instance = app(density="comfortable")
    rows, hits = W.transform_body(instance, *body(ascii_), width, height, ascii_=ascii_)
    assert len(rows) == height
    assert all(L.vlen(L.row_text(row)) <= width for row in rows)
    assert all(0 <= y < height for y, _, _ in hits)
    assert all(key in L.row_text(rows[y]) for y, _, key in hits)
    if ascii_:
        assert L.to_text(rows, width).isascii()


def test_details_scroll_does_not_move_the_jobs_cursor_and_reaches_the_last_row():
    instance = app(density="comfortable")
    instance.cursor = {"jobs": 1}
    rows, hits = W.transform_body(instance, *body(), 160, 14)
    W.run_command(instance, ["focus", "details"])
    assert W.handle_key(instance, "end")
    rows, hits = W.transform_body(instance, *body(), 160, 14)
    assert "detail 27" in L.to_text(rows, 160)
    assert instance.cursor == {"jobs": 1}
    assert next(key for _, kind, key in hits if kind == "job") == "job-1"
    assert W.handle_key(instance, "home")
    rows, _ = W.transform_body(instance, *body(), 160, 14)
    assert "detail 00" in L.to_text(rows, 160)


def test_maximize_focus_and_restore_make_hidden_details_accessible():
    instance = app(density="comfortable")
    W.transform_body(instance, *body(), 40, 10)
    assert W.handle_key(instance, "ctrl-w")
    assert W.initialize(instance).focus == "details"
    assert W.handle_key(instance, "z")
    rows, hits = W.transform_body(instance, *body(), 40, 10)
    assert "detail 00" in L.to_text(rows, 40)
    assert not hits
    assert W.handle_key(instance, "esc")
    assert not W.initialize(instance).maximized


def test_selected_job_is_revealed_and_manual_details_scroll_is_retained():
    instance = app(density="comfortable")
    rows, hits = body(details=55)
    W.transform_body(instance, rows, hits, 80, 14)
    W.run_command(instance, ["focus", "details"])
    W.handle_key(instance, "end")
    W.transform_body(instance, rows, hits, 80, 14)
    key = "jobs:details"
    before = instance.layout_state.scroll[key]
    W.transform_body(instance, rows, hits, 80, 14)
    assert instance.layout_state.scroll[key] == before > 0
    instance.selected_id = "job-1"
    W.transform_body(instance, rows, hits, 80, 14)
    assert instance.layout_state.scroll[key] == 0


def test_named_layouts_roundtrip_and_reject_invalid_names_and_oversized_storage():
    instance = app(density="comfortable", split=45)
    W.run_command(instance, ["focus", "details"])
    W.run_command(instance, ["maximize", "on"])
    W.run_command(instance, ["layout", "save", "research-wide"])
    W.run_command(instance, ["density", "compact"])
    W.run_command(instance, ["layout", "load", "research-wide"])
    restored = app()
    W.restore(restored, json.loads(json.dumps(W.save(instance))))
    assert W.save(restored) == W.save(instance)
    assert restored.layout_state.density == "comfortable"
    assert restored.layout_state.focus == "details" and restored.layout_state.maximized
    W.run_command(instance, ["layout", "save", "../../private"])
    assert "ERROR:" in instance.messages[-1]
    assert "../../private" not in instance.layout_state.named
    for n in range(W.MAX_LAYOUTS):
        W.run_command(instance, ["layout", "save", "layout-" + str(n)])
    assert len(instance.layout_state.named) == W.MAX_LAYOUTS
    assert "At most" in instance.messages[-1]


@pytest.mark.parametrize("bad", [None, [], "junk", {"version": 99}, {"density": "oops", "focus": "footer", "split": True, "named": []}])
def test_corrupt_saved_state_does_not_change_valid_defaults(bad):
    instance = app(density="comfortable", split=45)
    W.restore(instance, bad)
    state = W.initialize(instance)
    assert (state.density, state.focus, state.ratio) == ("comfortable", "main", 45)


def test_layout_list_scrolls_to_all_saved_presets_on_short_terminals():
    instance = app()
    for n in range(W.MAX_LAYOUTS):
        W.run_command(instance, ["layout", "save", f"preset-{n:02}"])
    W.run_command(instance, ["layout", "list"])
    views = SimpleNamespace(g=L.Glyphs(True))
    initial = W.overlay(views, {}, instance, 80, 12)
    assert "preset-15" not in "".join(L.row_text(row) for _, _, row in initial)
    assert W.handle_key(instance, "end")
    final = W.overlay(views, {}, instance, 80, 12)
    assert "preset-15" in "".join(L.row_text(row) for _, _, row in final)
    assert all(0 <= y < 12 and x >= 0 and x + L.vlen(L.row_text(row)) <= 80 for y, x, row in final)


def test_compact_renderer_preserves_native_scroll_and_runs_once():
    instance = app(density="compact", split=45)
    calls = []
    def render(width, height):
        calls.append((width, height))
        return body()
    expected = body()
    assert W.render_body(SimpleNamespace(g=L.Glyphs(False)), {}, instance, 120, 20, None, render) == expected
    assert calls == [(120, 20)]


def test_adaptive_renderer_refits_columns_to_actual_width_and_bounds_work():
    instance = app(density="comfortable", split=45)
    calls = []
    def render(width, height):
        calls.append((width, height))
        rows, hits = body(details=45)
        return [L.clip_row(row, width) for row in rows], hits
    rows, hits = W.render_body(SimpleNamespace(g=L.Glyphs(False)), {}, instance, 160, 25, None, render)
    assert len(calls) <= 3 and all(height == W.MAX_SOURCE_ROWS for _, height in calls)
    assert calls[1][0] < 160 and calls[2][0] < 160
    assert all(key in L.row_text(rows[y]) for y, _, key in hits)
    assert len(rows) == 25


def test_log_reader_keeps_its_native_line_selection_and_follow_scroll():
    instance = app(density="comfortable")
    instance.tab = "log"
    calls = []
    def render(width, height):
        calls.append((width, height))
        return [[(" original log", "")]], [(0, "log_line", "3")]
    assert W.render_body(SimpleNamespace(g=L.Glyphs(True)), {}, instance, 80, 20, None, render)[1] == [(0, "log_line", "3")]
    assert calls == [(80, 20)]
    assert not W.handle_key(instance, "z")


def test_primary_summary_and_column_names_stay_visible_at_the_bottom_of_a_table():
    instance = app(density="comfortable")
    instance.selected_id = "job-49"
    rows = [[(" 50 users, 50 running", "bold")], L.rule(L.Glyphs(False), 120, "job table"),
            [(" JOBID  NAME  STATE", "heading")]]
    rows.extend([(f" job-{n}  experiment-{n}", "rev" if n == 49 else "")] for n in range(50))
    hits = [(n + 3, "job", f"job-{n}") for n in range(50)]
    rendered, remapped = W.transform_body(instance, rows, hits, 120, 15)
    text = L.to_text(rendered, 120)
    assert "50 users" in text and "JOBID  NAME  STATE" in text and "experiment-49" in text
    assert any(key == "job-49" for _, _, key in remapped)


def test_source_cursor_selection_is_revealed_without_a_job_id():
    instance = app(density="comfortable")
    instance.tab, instance.selected_id = "sources", "old-job-id"
    rows = [[(" SOURCE  STATUS", "heading")]]
    rows.extend([(f" source-{n}  ready", "rev" if n == 40 else "")] for n in range(50))
    hits = [(n + 1, "source", f"source-{n}") for n in range(50)]
    rendered, remapped = W.transform_body(instance, rows, hits, 80, 15)
    assert "source-40" in L.to_text(rendered, 80)
    assert any(key == "source-40" for _, _, key in remapped)


def test_plain_main_panel_can_scroll_and_long_metadata_remains_reachable():
    instance = app(density="comfortable")
    instance.tab = "analytics"
    words = "resource observation " * 8 + "unclipped-evidence-end"
    rows = [[(" observed metrics", "heading")], [(words, "magenta")]]
    W.transform_body(instance, rows, [], 40, 8)
    assert W.handle_key(instance, "end")
    final, _ = W.transform_body(instance, rows, [], 40, 8)
    assert "unclipped-evidence-end" in L.to_text(final, 40)
    assert all(L.vlen(L.row_text(row)) <= 40 for row in final)


def test_narrow_refit_preserves_complete_header_totals_and_advice():
    instance = app(density="comfortable", split=45)
    title = "node map: measured allocation - 10/20 gpus in use - verified observation"
    advice = " rb3-identity is running; these jobs need CPU and memory advice across a longer observation window"
    def render(width, height):
        g = L.Glyphs(False)
        return ([L.rule(g, width, "jobs"), [(" job-2 running", "rev")], L.rule(g, width, "selected"),
                 L.rule(g, width, title), [(L.cut(advice, max(1, width - 12)), "magenta")]], [(1, "job", "job-2")])
    rendered, _ = W.render_body(SimpleNamespace(g=L.Glyphs(False)), {}, instance, 160, 18, None, render)
    text = L.to_text(rendered, 160)
    assert "10/20 gpus in use" in text and "rb3-identity" in text and "observation window" in text


def _luminance(value):
    components = [v / 255 for v in P.rgb(value)]
    components = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in components]
    return sum(a * b for a, b in zip(components, (.2126, .7152, .0722)))


@pytest.mark.parametrize("theme", ["default", "dark", "light"])
@pytest.mark.parametrize("foreground", ["white", "text-secondary", "muted", "green", "red", "blue", "cyan", "magenta"])
def test_theme_text_colors_are_readable_on_their_canvas(theme, foreground):
    tokens = P.theme_tokens(theme)
    a, b = sorted((_luminance(tokens[foreground]), _luminance(tokens["canvas"])))
    assert (b + .05) / (a + .05) >= 4.5


def test_semantic_surfaces_selection_and_chart_series_work_in_all_themes(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    for theme in P.THEME_NAMES:
        selected = P.resolve("sel", theme)
        assert "bold" in selected.flags
        assert selected.background is not None or "rev" in selected.flags
        assert len(P.chart_colors(theme)) == 6 and len(set(P.chart_colors(theme))) == 6
    assert P.resolve("text+bg:surface", "light").foreground == P.rgb(P.ROSE_PINE_DAWN_PALETTE["white"])
    assert P.resolve("text+bg:surface", "light").background == P.rgb(P.ROSE_PINE_DAWN_PALETTE["surface"])
    assert P.resolve("text+bg:surface", "terminal").background is None
    assert P.resolve("text+bg:surface", "terminal").foreground is None
    assert P.resolve("muted", "terminal").flags == ("dim",)
    assert P.resolve("fg:heading", "light").foreground == P.resolve("accent", "light").foreground
    changed = P.theme_tokens("light")
    changed["white"] = "#000000"
    assert P.LIGHT_PALETTE["white"] != "#000000"


def test_row_padding_and_scroll_slices_preserve_wide_text_and_explicit_colors():
    row = [("日本", "fg:#fb923c"), ("abc", "bold")]
    padded = L.fill_row(row, 10)
    assert L.vlen(L.row_text(padded)) == 10
    assert "fg:#fb923c" in padded[0][1]
    rows, hits, top = L.scroll_window([row] * 10, [(i, "job", str(i)) for i in range(10)], 5, 3, 99)
    assert top == 7 and hits == [(0, "job", "7"), (1, "job", "8"), (2, "job", "9")]
    assert all(L.vlen(L.row_text(item)) <= 5 for item in rows)
