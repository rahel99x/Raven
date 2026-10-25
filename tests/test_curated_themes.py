"""Curated palettes, truthful terminal inheritance, and compatible saved choices."""
from __future__ import annotations

import pytest

from raven import layout as L, palette as P, screen
from raven.config import Config, DEFAULTS
from raven.configuration import policy
from raven.controller import App, THEMES
from raven.model import Store


@pytest.fixture(autouse=True)
def terminal(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)


def _contrast(a, b):
    def luminance(value):
        channels = [v / 3294.6 if v <= 10 else ((v / 255 + .055) / 1.055) ** 2.4
                    for v in P.rgb(value)]
        return sum(v * k for v, k in zip(channels, (.2126, .7152, .0722)))
    low, high = sorted((luminance(a), luminance(b)))
    return (high + .05) / (low + .05)


def test_public_choices_default_and_compatibility_are_distinct():
    assert P.THEME_NAMES == ("gruvbox-dark", "modnokai", "darcula", "catppuccin-mocha",
                             "nord", "tokyo-night", "rose-pine-dawn", "system")
    assert tuple(THEMES) == P.THEME_NAMES
    assert set(P.THEME_LABELS) == set(P.THEME_NAMES)
    assert not set(P.LEGACY_THEME_NAMES) & set(P.THEME_NAMES)
    assert set(P.LEGACY_THEME_NAMES) <= set(P.ACCEPTED_THEME_NAMES)
    assert DEFAULTS["theme"] == "gruvbox-dark"
    assert policy.DEFAULTS["ui"]["theme"] == "gruvbox-dark"


@pytest.mark.parametrize("theme", P.THEME_NAMES[:-1])
def test_each_curated_palette_has_complete_readable_semantic_surfaces(theme):
    tokens = P.theme_tokens(theme)
    assert set(tokens) == set(P.PALETTE)
    assert len(set(P.chart_colors(theme))) == 6
    for value in tokens.values():
        assert P.rgb(value)
    for ink in ("white", "text-secondary", "muted"):
        for paper in ("canvas", "surface", "surface-raised"):
            assert _contrast(tokens[ink], tokens[paper]) >= 3, (theme, ink, paper)
    for paper in ("canvas", "surface", "surface-raised"):
        assert _contrast(tokens["white"], tokens[paper]) >= 4.5, (theme, paper)
    for ink in ("white", "text-secondary", "muted", "green", "red", "blue", "cyan", "magenta"):
        assert _contrast(tokens[ink], tokens["canvas"]) >= 4.5, (theme, ink)
    tokens["canvas"] = "#000001"
    assert P.theme_tokens(theme)["canvas"] != tokens["canvas"]


@pytest.mark.parametrize("theme", P.THEME_NAMES[:-1])
@pytest.mark.parametrize("role,index", (("green", 2), ("red", 1), ("yellow", 3),
                                       ("blue", 4), ("magenta", 5), ("cyan", 6)))
def test_basic_terminal_fallback_keeps_status_hues(theme, role, index):
    assert P.ansi_codes(role, 8, theme) == str(30 + index)


@pytest.mark.parametrize("old,new", (("default", "gruvbox-dark"), ("dark", "gruvbox-dark"),
                                     ("light", "rose-pine-dawn"), ("terminal", "system"),
                                     ("Monokai", "modnokai"), ("Gruvbox", "gruvbox-dark")))
def test_retired_saved_and_configured_theme_names_migrate_without_losing_job_context(old, new):
    app = App(Store(persist=False), None, None, Config({"theme": old}), "test")
    assert app.theme == new
    app.selected_id = "17"
    app.restore({"theme": old, "tab": "history"})
    assert app.theme == new and app.tab == "history" and app.selected_id == "17"
    app.run_command("theme " + old)
    assert app.command_ok and app.theme == new


@pytest.mark.parametrize("mode", P.LEGACY_THEME_NAMES)
def test_accessibility_commands_remain_explicitly_available(mode):
    app = App(Store(persist=False), None, None, Config({"theme": mode}), "test")
    app.run_command("theme " + mode)
    assert app.command_ok and app.theme == mode
    app.handle("T")
    assert app.theme == "gruvbox-dark"


@pytest.mark.parametrize("depth", (8, 256, 24))
def test_system_uses_native_slots_at_every_ansi_depth_and_inherits_defaults(depth):
    assert P.ansi_codes("cyan+bg:surface", depth, "system") == "36"
    assert P.ansi_codes("red", depth, "system") == "1;31"
    assert P.ansi_codes("fg:accent+bg:selection", depth, "system") == "36;44"
    assert P.ansi_codes("text+bg:canvas", depth, "system") == ""
    assert P.ansi_codes("muted+bg:surface-raised", depth, "system") == "2"
    assert P.ansi_codes("sel", depth, "system") == "1;7"
    assert P.resolve("cyan", "system").foreground is None
    assert P.resolve("cyan", "system").foreground_index == 6
    assert P.resolve("text+bg:canvas", "system") == P.Style()
    assert L.to_text([[("Short", "")], []], 40, True, depth, "system", canvas=True) == "Short\n"


def test_system_preserves_exact_custom_and_child_terminal_colours():
    assert P.ansi_codes("fg:#12abef+bg:#132537", 24, "system") == "38;2;18;171;239;48;2;19;37;55"
    assert P.ansi_codes("terminal+fg:#67e8f9+bg:#172033", 24, "system") == "38;2;103;232;249;48;2;23;32;51"
    assert P.ansi_codes("fg:#67e8f9", 24, "system") == "36"
    assert P.ansi_codes("bg:#0b1020", 24, "system") == ""
    assert P.ansi_codes("bg-raw:#67e8f9", 24, "system") == "48;2;103;232;249"


@pytest.mark.parametrize("style", ("gradient:cyan:red:nan", "gradient:cyan:red:inf",
                                    "gradient:cyan:red:1:extra", "bg:\x1b[31m", "fg:#12zzef"))
def test_system_rejects_invalid_or_injectable_styles(style):
    assert P.resolve(style, "system") == P.Style()
    assert P.ansi_codes(style, 24, "system") == ""


class NativeCurses:
    class error(Exception):
        pass

    A_BOLD, A_DIM, A_REVERSE, A_UNDERLINE = 1, 2, 4, 8
    COLOR_BLACK, COLOR_WHITE = 0, 7

    def __init__(self, colors=256, defaults=True):
        self.COLORS = colors
        self.COLOR_PAIRS = 64
        self.defaults = defaults
        self.pairs = {}

    def has_colors(self):
        return True

    def start_color(self):
        pass

    def use_default_colors(self):
        if not self.defaults:
            raise self.error()

    def init_pair(self, pair, foreground, background):
        self.pairs[pair] = (foreground, background)

    def color_pair(self, pair):
        return pair << 8


@pytest.mark.parametrize("colors", (8, 256))
def test_system_curses_uses_user_palette_indices_without_redefining_colours(colors):
    curses = NativeCurses(colors)
    painter = screen.CursesPalette(curses, theme="system")
    cyan = painter.attr("cyan+bg:surface", "system")
    assert curses.pairs[cyan >> 8] == (6, -1)
    red = painter.attr("red", "system")
    assert curses.pairs[red >> 8] == (1, -1) and red & curses.A_BOLD
    assert painter.attr("sel", "system") & curses.A_REVERSE
    assert painter.attr("text+bg:canvas", "system") == 0
    assert all(bg == -1 for fg, bg in curses.pairs.values())


def test_system_direct_curses_defaults_are_truthful_and_custom_rgb_remains_exact():
    curses = NativeCurses(1 << 24)
    painter = screen.CursesPalette(curses, theme="system")
    accent = painter.attr("cyan", "system")
    assert curses.pairs[accent >> 8] == (-1, -1)
    assert painter.attr("red", "system") & curses.A_BOLD
    explicit = painter.attr("fg:#12abef+bg:#132537", "system")
    assert curses.pairs[explicit >> 8] == (0x12ABEF, 0x132537)


@pytest.mark.parametrize("environment,value", (("NO_COLOR", "1"), ("TERM", "dumb")))
def test_system_and_curated_themes_honor_disabled_colour(monkeypatch, environment, value):
    monkeypatch.setenv(environment, value)
    for theme in P.THEME_NAMES:
        assert P.ansi_codes("red+bg:surface", 24, theme) == ""
        assert L.to_text([[("status", "red")]], 20, True, 24, theme, canvas=True) == "status"


@pytest.mark.parametrize("mode", P.LEGACY_THEME_NAMES)
def test_settings_preview_and_cancel_preserve_legacy_accessibility_modes(mode):
    from raven import navigation_tools as N
    app = App(Store(persist=False), None, None, Config({"theme": mode}), "test")
    app.run_command("settings")
    assert app.mode == "settings_editor"
    state = N.initialize(app)
    state["cursor"] = N._setting_keys(app).index("theme")
    N.handle_key(app, "right")
    assert app.theme == "gruvbox-dark"
    N.handle_key(app, "esc")
    assert app.theme == mode and app.mode == "main"


def test_exact_theme_command_persists_canonical_choice_for_next_launch(tmp_path):
    state_dir = str(tmp_path / "state")
    store = Store(state_dir=state_dir, persist=True)
    app = App(store, None, None, Config({}), "test")
    app.run_command("theme Nord")
    assert app.command_ok and store.load_ui()["theme"] == "nord"
    restarted = App(Store(state_dir=state_dir, persist=True), None, None, Config({}), "test")
    assert restarted.theme == "nord"


def test_settings_preview_does_not_save_until_apply(tmp_path):
    from raven import navigation_tools as N
    store = Store(state_dir=str(tmp_path / "state"), persist=True)
    app = App(store, None, None, Config({}), "test")
    app.run_command("theme darcula")
    app.run_command("settings")
    state = N.initialize(app)
    state["cursor"] = N._setting_keys(app).index("theme")
    N.handle_key(app, "right")
    assert app.theme == "catppuccin-mocha"
    assert store.load_ui()["theme"] == "darcula"
    N.handle_key(app, "esc")
    assert app.theme == "darcula" and store.load_ui()["theme"] == "darcula"
