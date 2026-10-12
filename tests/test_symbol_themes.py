"""Cached glyph styles inherit the active palette without losing semantics."""
from types import SimpleNamespace

import pytest

from raven import job_progress, layout as L, metric_live, palette as P, pane_drag, toolbar
from raven.config import Config
from raven.controller import App
from raven.model import Job, Store
from raven.views import Views


THEMES = P.THEME_NAMES + ("Darcula", "Monokai", "Gruvbox", "Gruvbox Dark", "gruvbox_dark")
LEGACY = {"#67e8f9": "cyan", "#22d3ee": "cyan", "#a78bfa": "magenta", "#ec4899": "magenta",
          "#34d399": "green", "#38bdf8": "blue", "#3b82f6": "blue", "#fb923c": "orange",
          "#475569": "faint", "#92400e": "notice", "#fff7ed": "notice-text"}


@pytest.fixture(autouse=True)
def colored_terminal(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)


def _component_styles():
    """Collect actual renderer metadata once, before any runtime theme switch."""
    cfg = Config({"theme": "default", "startup_animation": False, "log_lines": 0})
    store = Store(persist=False)
    store.jobs = [Job("7", "clock", "cpu", "RUNNING", elapsed="00:25:00", limit="01:00:00")]
    app = App(store, None, None, cfg, "symbols", interactive=True)
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    result = []
    row, _ = metric_live._row(views.g, {"enabled": True, "delta": .1}, 30)
    result.append(("live-slider", next(style for text, style in row if text == "◆"), "accent"))
    result.append(("live-toggle", next(style for text, style in row if "●" in text), "accent"))
    for basis, fraction, role in (("reported", .42, "accent"), ("time", .2, "green"),
                                  ("time", .75, "yellow"), ("time", 1.2, "red"), ("pending", None, "text")):
        observation = job_progress.Observation(fraction, basis)
        result.append(("progress-" + basis + "-" + str(fraction), observation.style, role))
    pane_drag.begin_frame(app, 40, 12)
    assert pane_drag.register(app, "test-divider", "vertical", 15, 0, 1, 11, 0, 39, 50,
                              lambda value: None, full_vertical=True)
    canvas = [[(" " * 40, "")]] * 11
    pane_drag.paint(canvas, app, "test-divider")
    result.append(("divider-diamond", next(style for text, style in canvas[5] if text == "◆"), "accent"))
    result.append(("divider-line", next(style for text, style in canvas[0] if text == "│"), "border"))
    bar = toolbar.render_bar(views, app, 160)
    for text, style in bar:
        if text == "[-] " or text == "[+]":
            result.append(("toolbar-step-" + text, style, "accent"))
        elif "[x]" in text:
            result.append(("toolbar-quit", style, "danger"))
    app.toolbar_state.update(menu=3, cursor=0)
    menu = toolbar.overlay(views, {}, app, 160, 40)
    marker = next(style for _, _, row in menu for text, style in row if text.startswith("▸ "))
    result.append(("menu-selection", marker, "accent"))
    app.toolbar_state.update(menu=None)
    app.sel_anchor = app.sel_end = 0
    rows, _ = views.compose(store.snapshot(), app, 160, 40)
    result.append(("selected-line-marker", next(style for text, style in rows[0] if text == "◆"), "fg:orange"))
    return app, result


@pytest.mark.parametrize("theme", THEMES)
def test_every_cursor_token_form_inherits_the_current_accent(theme):
    tokens = P.theme_tokens(theme)
    assert tokens["cursor"] == tokens["cyan"]
    for style in ("cursor", "cursor+bold", "fg:cursor", "fg:cursor+under"):
        P.resolve(style, "default")  # Warm a previous-theme cache entry first.
        resolved = P.resolve(style, theme)
        assert resolved.foreground == P.resolve("accent", theme).foreground
    assert P.resolve("bg:cursor", theme).background == P.resolve("fg:accent", theme).foreground
    assert P.resolve("gradient:cursor:accent:0.5", theme).foreground == P.resolve("fg:accent", theme).foreground


@pytest.mark.parametrize("theme", THEMES)
def test_actual_cached_control_and_status_glyphs_follow_runtime_theme_switches(theme):
    app, styles = _component_styles()
    assert len(styles) >= 12
    for _, style, _ in styles:
        P.resolve(P.cell_style(style, app.theme), app.theme)
    app.run_command("theme " + theme)
    assert app.command_ok and app.theme == P.canonical_theme(theme)
    for name, style, role in styles:
        resolved = P.resolve(P.cell_style(style, app.theme), app.theme)
        expected = P.resolve(P.cell_style(role, app.theme), app.theme)
        assert resolved.foreground == expected.foreground, (theme, name, style, role)
        if app.theme in ("mono", "reader"):
            assert resolved.foreground is None and resolved.background is None
    if app.research:
        app.research.close()


@pytest.mark.parametrize("theme", ("light", "darcula", "modnokai", "gruvbox-dark", "high", "cb"))
def test_fixed_application_glyph_colors_and_gradient_endpoints_use_semantic_theme_colors(theme):
    for color, role in LEGACY.items():
        assert P.resolve("fg:" + color, theme).foreground == P.resolve("fg:" + role, theme).foreground
        assert P.resolve("bg:" + color, theme).background == P.resolve("bg:" + role, theme).background
        assert P.resolve("gradient:" + color + ":" + color + ":0.5", theme).foreground == P.resolve("fg:" + role, theme).foreground


@pytest.mark.parametrize("theme", ("default", "dark", "light", "terminal", "darcula", "modnokai", "gruvbox-dark"))
def test_custom_colors_and_exact_overlay_backgrounds_keep_their_values(theme):
    assert P.resolve("fg:#12abef+bg:#132537", theme).foreground == (18, 171, 239)
    assert P.resolve("fg:#12abef+bg:#132537", theme).background == (19, 37, 55)
    assert P.resolve("accent+bg-raw:#67e8f9", theme).background == P.rgb("#67e8f9")
    assert P.resolve("accent+bg-raw:#8ec07c", theme).background == P.rgb("#8ec07c")
    assert P.resolve("accent+bg-raw:#12abef", theme).foreground == P.resolve("accent", theme).foreground


def test_color_blind_exact_overlay_background_is_not_transformed_twice():
    assert P.resolve("accent+bg-raw:#6ee7b7", "cb").background == P.rgb("#6ee7b7")
    assert P.resolve("accent+bg:#6ee7b7", "cb").background != P.rgb("#6ee7b7")


@pytest.mark.parametrize("theme", ("mono", "reader"))
def test_plain_accessibility_modes_keep_symbols_and_semantic_emphasis(theme):
    assert P.resolve("cursor+bold+bg-raw:#123456", theme) == P.Style(("bold",))
    for role in ("danger", "warning", "error", "warn"):
        resolved = P.resolve(role, theme)
        assert resolved.foreground is None and resolved.background is None and "bold" in resolved.flags
    assert P.resolve("sel", theme) == P.Style(("bold", "rev"))


@pytest.mark.parametrize("environment,value", [("NO_COLOR", "1"), ("TERM", "dumb")])
def test_disabled_color_does_not_emit_symbol_color_sequences(monkeypatch, environment, value):
    monkeypatch.setenv(environment, value)
    assert P.ansi_codes("cursor+bold+bg:surface", 24, "darcula") == ""
    assert L.to_text([[("◆▸⧗", "cursor+bold")]], 10, True, 24, "gruvbox-dark") == "◆▸⧗"
