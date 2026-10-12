"""Colour capabilities and stable rendering must work without a real terminal."""
import io
from types import SimpleNamespace

import pytest

from raven import palette as P, screen


@pytest.fixture(autouse=True)
def terminal(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("COLORTERM", raising=False)


def test_ansi_depths_preserve_custom_foreground_background_and_flags(monkeypatch):
    style = "bold+fg:#12abef+bg:#172033"
    assert P.ansi_codes(style, 24) == "1;38;2;18;171;239;48;2;23;32;51"
    assert P.ansi_codes(style, 256).startswith("1;38;5;")
    basic = P.ansi_codes(style, 8).split(";")
    assert basic[0] == "1" and 30 <= int(basic[1]) <= 37 and 40 <= int(basic[2]) <= 47
    assert P.ansi_codes(style, 0) == ""
    assert P.color_depth() == 256
    monkeypatch.setenv("COLORTERM", "truecolor")
    assert P.color_depth() == 24
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.delenv("COLORTERM")
    assert P.color_depth() == 8


def test_no_color_and_dumb_terminal_disable_even_explicit_ansi(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert P.color_depth() == 0 and P.ansi_codes("bold+red", 24) == ""
    monkeypatch.setenv("NO_COLOR", "")
    assert P.ansi_codes("bold", 24) == "1"
    monkeypatch.setenv("TERM", "dumb")
    assert P.ansi_codes("bold+cyan", 256) == ""


def test_semantic_basic_colors_and_accessibility_themes():
    assert P.ansi_codes("success", 8) == "32"
    assert P.ansi_codes("danger", 8) == "31"
    assert P.ansi_codes("warning", 8) == "33"
    assert P.ansi_codes("green", 8, "cb") == "34"
    assert P.ansi_codes("red", 8, "cb") == "33"
    assert P.ansi_codes("red+under", 24, "mono") == "1;4"
    assert P.ansi_codes("sel", 24, "reader") == "1;7"
    assert P.ansi_codes("accent", 24, "high").startswith("1;")
    assert P.resolve("fg:#fb7185", "cb").foreground != P.rgb("#fb7185")


def test_gradient_clamps_and_rejects_invalid_terminal_color_text():
    assert P.gradient("#000000", "#ffffff", .5) == "#808080"
    assert P.gradient("#000000", "#ffffff", -2) == "#000000"
    assert P.gradient("#000000", "#ffffff", 2) == "#ffffff"
    with pytest.raises(ValueError):
        P.rgb("#12zzef")
    assert P.ansi_codes("fg:#12zzef+bg:bad+unknown", 24) == ""


class FakeCurses:
    class error(Exception):
        pass

    A_BOLD, A_DIM, A_REVERSE, A_UNDERLINE = 1, 2, 4, 8
    COLOR_BLACK, COLOR_WHITE = 0, 7

    def __init__(self, colors=256, pairs=32, fail_pair=None, default=True):
        self.COLORS, self.COLOR_PAIRS = colors, pairs
        self.fail_pair, self.default = fail_pair, default
        self.initialized = []

    def has_colors(self):
        return bool(self.COLORS)

    def start_color(self):
        pass

    def use_default_colors(self):
        if not self.default:
            raise self.error()

    def init_pair(self, number, foreground, background):
        assert 0 < number < self.COLOR_PAIRS
        assert -1 <= foreground < self.COLORS and -1 <= background < self.COLORS
        if number == self.fail_pair:
            raise self.error()
        assert all(number != initialized[0] for initialized in self.initialized)
        self.initialized.append((number, foreground, background))

    def color_pair(self, number):
        return number << 8


@pytest.mark.parametrize("colors,pairs", [(256, 16), (8, 8), (8, 2), (0, 0), (4, 8)])
def test_curses_pairs_remain_stable_and_bounded_during_gradient_frames(colors, pairs):
    curses = FakeCurses(colors, pairs)
    painter = screen.CursesPalette(curses)
    selected = painter.attr("sel")
    initial = tuple(curses.initialized)
    for i in range(100):
        painter.attr(f"fg:{P.gradient('#67e8f9', '#c4b5fd', i / 99)}+bg:#172033")
    assert tuple(curses.initialized[:len(initial)]) == initial
    assert len(curses.initialized) <= max(0, pairs - 1)
    count = len(curses.initialized)
    for i in range(100):
        painter.attr(f"fg:{P.gradient('#67e8f9', '#c4b5fd', i / 99)}+bg:#172033")
    assert len(curses.initialized) == count
    assert painter.attr("sel") == selected
    assert painter.attr("red", "mono") == curses.A_BOLD
    assert painter.attr("sel", "reader") == curses.A_BOLD | curses.A_REVERSE
    if colors == 8 and pairs == 2:
        assert curses.initialized[0][2] == curses.COLOR_BLACK
        assert selected & curses.A_REVERSE


def test_curses_backgrounds_themes_init_failure_and_disabled_color(monkeypatch):
    curses = FakeCurses(default=False, fail_pair=10)
    painter = screen.CursesPalette(curses)
    assert painter.attr("cyan", "high") & curses.A_BOLD
    painter.attr("fg:#ffffff+bg:#172033")
    painter.attr("fg:#111111+bg:#fbbf24")
    assert painter.limit == 9
    assert len(curses.initialized) == 9
    assert all(background >= 0 for _, _, background in curses.initialized)
    monkeypatch.setenv("NO_COLOR", "1")
    disabled = screen.CursesPalette(FakeCurses())
    assert not disabled.enabled and disabled.attr("red") == 1


def test_curses_direct_color_uses_packed_rgb_and_handles_pair_saturation():
    curses = FakeCurses(colors=1 << 24, pairs=10, default=False)
    painter = screen.CursesPalette(curses)
    foreground = 0x12ABEF
    background = 0x172033
    painter.attr("fg:#12abef+bg:#172033")
    assert curses.initialized[-1][1:] == (foreground, background)
    assert painter._index(None) == 0xE2E8F0
    assert painter._rgb(foreground, "white") == (18, 171, 239)
    # Saturation approximates initialized RGB pairs without treating huge packed
    # values as positions in the indexed palette.
    painter.attr("fg:#abcdef+bg:#fbbf24")
    assert len(curses.initialized) == 9
    assert painter.attr("fg:#abcdef+bg:#fbbf24")


def test_direct_color_disabled_if_curses_build_rejects_extended_indices():
    class LegacyCurses(FakeCurses):
        def init_pair(self, number, foreground, background):
            if foreground > 32767 or background > 32767:
                raise OverflowError("signed short integer is greater than maximum")
            super().init_pair(number, foreground, background)

    curses = LegacyCurses(colors=1 << 24)
    painter = screen.CursesPalette(curses)
    assert not painter.enabled and not curses.initialized
    assert painter.attr("red") == curses.A_BOLD


def test_once_and_watch_keep_theme_and_narrow_width(monkeypatch):
    import shutil

    app = SimpleNamespace(theme="mono", width=120, tick=lambda: None, bell=False)
    store = SimpleNamespace(snapshot=lambda: {"events": []})
    widths = []

    def compose(snap, app, width, height, actions):
        widths.append((width, height))
        return [[("status", "red")]], []

    views = SimpleNamespace(compose=compose)
    assert screen.once_text(app, views, store, None, 40, True) == "\033[1mstatus\033[0m"
    stdout = io.StringIO()
    monkeypatch.setattr(screen.sys, "stdout", stdout)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback: SimpleNamespace(columns=40, lines=12))
    monkeypatch.setattr(screen.time, "sleep", lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt))
    screen.run_watch(app, views, None, store, None, {}, 1, True)
    assert widths[-1] == (40, None) and app.width == 40
    assert stdout.getvalue() == "\033[1mstatus\033[0m\n"


def test_animated_watch_updates_only_changed_lines_and_restores_cursor(monkeypatch):
    import shutil

    class Terminal(io.StringIO):
        def isatty(self):
            return True

    output = Terminal()
    app = SimpleNamespace(theme="default", tick=lambda: None, bell=False)
    store = SimpleNamespace(snapshot=lambda: {"events": []})
    views = SimpleNamespace(compose=lambda *args: ([[("constant", "")]], []))
    calls = []

    def sleep(seconds):
        calls.append(seconds)
        if len(calls) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(screen.sys, "stdout", output)
    monkeypatch.setattr(screen.time, "sleep", sleep)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback: SimpleNamespace(columns=30, lines=10))
    screen.run_watch(app, views, None, store, None, {}, 1, False)
    assert output.getvalue().count("constant") == 1
    assert output.getvalue().startswith("\033[?25l\033[2J")
    assert output.getvalue().endswith("\033[?25h\n")


def test_watch_resizes_with_a_full_repaint_and_ctrl_c_footer(monkeypatch):
    import shutil

    class Terminal(io.StringIO):
        def isatty(self):
            return True

    output = Terminal()
    app = SimpleNamespace(theme="default", tick=lambda: None, bell=False)
    store = SimpleNamespace(snapshot=lambda: {"events": []})
    sizes = iter([SimpleNamespace(columns=30, lines=10), SimpleNamespace(columns=20, lines=6)])

    def compose(snap, app, width, height, actions):
        return [[(f"row {i}", "")] for i in range(height - 1)] + [[("q quit", "")]], []

    calls = []

    def sleep(seconds):
        calls.append(seconds)
        if len(calls) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(screen.sys, "stdout", output)
    monkeypatch.setattr(screen.time, "sleep", sleep)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback: next(sizes))
    screen.run_watch(app, SimpleNamespace(compose=compose), None, store, None, {}, 1, False)
    assert output.getvalue().count("\033[2J") == 2
    assert output.getvalue().count("row 0") == 2
    assert output.getvalue().count("Ctrl-C exit") == 2
    assert "q quit" not in output.getvalue()
    assert "\033[7;1H\033[J" not in output.getvalue()
