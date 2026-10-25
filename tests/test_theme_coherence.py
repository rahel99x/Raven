"""Theme changes cover physical cells, cached charts and complete overlays."""
from types import SimpleNamespace

import pytest

from raven import layout as L, palette as P, screen, toolbar
from raven.config import Config
from raven.controller import App, THEMES
from raven.model import Store
from raven.navigation_tools import SETTING_CHOICES


NAMED = ("darcula", "modnokai", "gruvbox-dark")


@pytest.fixture(autouse=True)
def color_terminal(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)


def _luminance(value):
    linear = [x / 3294.6 if x <= 10 else ((x / 255 + .055) / 1.055) ** 2.4
              for x in value]
    return .2126 * linear[0] + .7152 * linear[1] + .0722 * linear[2]


@pytest.mark.parametrize("theme", ("default", "dark", "light") + NAMED)
def test_palettes_define_all_semantics_and_readable_body_and_menu_ink(theme):
    tokens = P.theme_tokens(theme)
    assert set(tokens) == set(P.PALETTE)
    assert all(P.rgb(value) for value in tokens.values())
    for ink in ("white", "text-secondary", "muted"):
        for paper in ("canvas", "surface", "surface-raised"):
            a, b = sorted((_luminance(P.rgb(tokens[ink])), _luminance(P.rgb(tokens[paper]))))
            assert (b + .05) / (a + .05) >= 3, (theme, ink, paper)
    original = tokens["canvas"]
    tokens["canvas"] = "#123456"
    assert P.theme_tokens(theme)["canvas"] == original


@pytest.mark.parametrize("source,expected", [("Darcula", "darcula"), ("monokai", "modnokai"),
                                                ("Modnokai", "modnokai"), ("gruvbox", "gruvbox-dark"),
                                                ("Gruvbox Dark", "gruvbox-dark"), ("gruvbox_dark", "gruvbox-dark")])
def test_theme_aliases_are_shared_by_initial_config_and_commands(source, expected):
    assert P.canonical_theme(source) == expected
    app = App(Store(persist=False), None, None, Config({"theme": source}), "test")
    assert app.theme == expected
    app.set_theme("default")
    app.run_command("theme " + source)
    assert app.theme == expected
    assert app.command_ok
    assert expected in THEMES and expected in SETTING_CHOICES["theme"]


@pytest.mark.parametrize("theme", NAMED + ("light",))
def test_cached_gradient_cells_and_legacy_colours_resolve_in_the_current_theme(theme):
    row = L.gradient_bar(L.Glyphs(False), .7, 15)
    tokens = P.theme_tokens(theme)
    assert P.resolve(row[0][1], theme).foreground == P.rgb(tokens["cyan"])
    other = "nord" if P.canonical_theme(theme) == "darcula" else "darcula"
    assert P.resolve(row[0][1], other).foreground != P.resolve(row[0][1], theme).foreground
    assert P.resolve(row[-1][1], theme).foreground == P.rgb(tokens["track"])
    assert P.resolve("fg:#fb923c+bold", theme).foreground == P.rgb(tokens["orange"])
    assert P.resolve("bg:#92400e+fg:#fff7ed", theme).background == P.rgb(tokens["notice"])
    assert P.resolve("fg:#12abef+bg:#132537", theme).foreground == (18, 171, 239)
    assert P.resolve("fg:#12abef+bg:#132537", theme).background == (19, 37, 55)
    expected = tuple(round((a + b) / 2) for a, b in
                     zip(P.rgb(tokens["cyan"]), P.rgb(tokens["magenta"])))
    assert P.resolve(P.gradient_style("cyan", "magenta", .5), theme).foreground == expected


@pytest.mark.parametrize("bad", ["gradient:cyan:red:nan", "gradient:cyan:red:inf", "gradient:bad:red:.5",
                                  "gradient:cyan:red:.5:extra", "gradient:\x1b:red:.5"])
def test_malformed_gradient_styles_cannot_inject_controls_or_nonfinite_colours(bad):
    assert P.resolve(bad) == P.Style()
    assert P.ansi_codes(bad, 24) == ""


@pytest.mark.parametrize("bad", ["#-f00aa", "# f00aa", "#+f00aa", "#ff 0aa", "#ff\t0aa", None, 123])
def test_rgb_requires_six_actual_hex_digits_without_signs_or_space(bad):
    with pytest.raises(ValueError):
        P.rgb(bad)
    if isinstance(bad, str):
        assert P.resolve("fg:" + bad, "darcula").foreground is None
        assert P.ansi_codes("bg:" + bad, 24, "darcula") == ""


@pytest.mark.parametrize("theme", NAMED + ("light", "default"))
@pytest.mark.parametrize("menu", (0, 1, 2, 3))
def test_every_menu_cell_including_selected_padding_and_borders_has_a_background(theme, menu):
    app = App(Store(persist=False), None, None, Config({"theme": theme}), "test")
    views = SimpleNamespace(g=L.Glyphs(False))
    toolbar.render_bar(views, app, 140)
    app.toolbar_state.update(menu=menu, cursor=0)
    frame = toolbar.overlay(views, {}, app, 140, 40)
    top, left, bottom, right = app.toolbar_state["menu_rect"]
    assert len(frame) == bottom - top
    for _, x, row in frame:
        assert x == left and L.vlen(L.row_text(row)) == right - left
        for text, style in row:
            if text:
                assert P.resolve(style, theme).background is not None
    # Help's first entry is enabled and selected, so its trailing blanks must
    # carry the same raised background as its label, not the underlying page.
    if menu == 3:
        item = next(row for y, _, row in frame if y == top + 1)
        interior = item[1:-1]
        assert L.row_text(interior).endswith(" ")
        assert all(P.resolve(style, theme).background == P.rgb(P.theme_tokens(theme)["surface-raised"])
                   for text, style in interior if text)


@pytest.mark.parametrize("theme", NAMED + ("light",))
def test_generic_overlay_is_opaque_to_its_exact_box_edges(theme):
    lines = [[("Tiny", "yellow+bold")], [("", "")], [("Selection", "sel")]]
    frame = L.box(L.Glyphs(False), lines, 60, 15, "Inspector")
    widths = {L.vlen(L.row_text(row)) for _, _, row in frame}
    assert len(widths) == 1
    surface = P.rgb(P.theme_tokens(theme)["surface"])
    for _, _, row in frame:
        for text, style in row:
            if text and "sel" not in style.split("+"):
                assert P.resolve(style, theme).background == surface


@pytest.mark.parametrize("theme", NAMED + ("light", "default"))
def test_ansi_canvas_covers_blank_rows_preserves_explicit_surfaces_and_changes_live(theme):
    rows = [[("Info", "cyan+bg:surface-raised")], [], [("Plain", "")]]
    output = L.to_text(rows, 12, color=True, color_depth=24, theme=theme, canvas=True)
    assert output.count("\n") == 2
    tokens = P.theme_tokens(theme)
    canvas = ";".join(str(part) for part in P.rgb(tokens["canvas"]))
    raised = ";".join(str(part) for part in P.rgb(tokens["surface-raised"]))
    assert "48;2;" + canvas in output.splitlines()[1]
    assert "48;2;" + raised in output.splitlines()[0]
    assert "48;2;" + canvas in output.splitlines()[2]
    assert L.to_text(rows, 12, color=False, theme=theme, canvas=True) == "Info\n\nPlain"


@pytest.mark.parametrize("theme", ("terminal", "mono", "reader"))
def test_inherited_terminal_canvas_and_no_colour_modes_do_not_get_forced_paper(theme, monkeypatch):
    assert P.resolve(P.cell_style("", theme), theme).background is None
    output = L.to_text([[('Short', '')], []], 50, color=True, color_depth=24, theme=theme, canvas=True)
    assert "48;" not in output and output == "Short\n"
    monkeypatch.setenv("NO_COLOR", "1")
    assert L.to_text([[('Short', 'red')], []], 50, color=True, theme="darcula", canvas=True) == "Short\n"


@pytest.mark.parametrize("theme", ("terminal", "mono", "reader"))
def test_inherited_canvas_menu_selection_uses_reverse_through_its_trailing_padding(theme):
    app = App(Store(persist=False), None, None, Config({"theme": theme}), "test")
    views = SimpleNamespace(g=L.Glyphs(theme == "reader"))
    toolbar.render_bar(views, app, 120)
    app.toolbar_state.update(menu=3, cursor=0)
    frame = toolbar.overlay(views, {}, app, 120, 40)
    interior = frame[1][2][1:-1]
    assert L.row_text(interior).endswith(" ")
    assert all("rev" in P.resolve(style, theme).flags for text, style in interior if text)


class ThemeCurses:
    class error(Exception):
        pass

    A_BOLD, A_DIM, A_REVERSE, A_UNDERLINE = 1, 2, 4, 8
    COLOR_BLACK, COLOR_WHITE = 0, 7

    def __init__(self, colors=256, pairs=16):
        self.COLORS, self.COLOR_PAIRS = colors, pairs
        self.initialized = {}

    def has_colors(self):
        return True

    def start_color(self):
        pass

    def use_default_colors(self):
        pass

    def init_pair(self, pair, foreground, background):
        assert 0 < pair < self.COLOR_PAIRS
        self.initialized[pair] = (foreground, background)

    def color_pair(self, pair):
        return pair << 8


@pytest.mark.parametrize("colors,pairs", [(256, 16), (8, 8), (8, 2)])
def test_theme_switch_reseeds_the_same_terminal_after_saturation_and_clears_all_cells(colors, pairs):
    curses = ThemeCurses(colors, pairs)
    canvas = [[None] * 25 for _ in range(8)]
    erased = []
    window = SimpleNamespace(erase=lambda: erased.append(True))
    theme, palette = "default", screen.CursesPalette(curses)

    def paint(y, x, row, width, height):
        for text, style in row:
            attr = palette.attr(P.cell_style(style, theme), theme)
            for char in text:
                if x < width:
                    canvas[y][x] = (char, attr)
                    x += 1

    painter = screen._DifferentialPainter(window, paint)
    rows = [[("Info", "cyan+bg:surface-raised")], [("Body", "")]]
    painter.draw(rows, [], 25, 8)
    for i in range(100):
        palette.attr(P.gradient_style("cyan", "magenta", i / 99), theme)
    for theme in NAMED + ("light", "default"):
        palette = screen.CursesPalette(curses, theme=theme)
        painter.invalidate()
        assert painter.draw(rows, [], 25, 8) == tuple(range(8))
        base_attr = palette.attr(P.cell_style("", theme), theme)
        expected_bg = P.color_index(P.rgb(P.theme_tokens(theme)["canvas"]), colors, background=True)
        assert curses.initialized[base_attr >> 8][1] == expected_bg
        assert all(canvas[7][x] == (" ", base_attr) for x in range(25))
        for i in range(100):
            palette.attr(P.gradient_style("cyan", "magenta", i / 99), theme)
        assert len(palette.pairs) <= pairs - 1
    assert len(erased) == 6


@pytest.mark.parametrize("theme", NAMED + ("default", "light"))
def test_eight_colour_selection_stays_visible_when_its_surface_matches_canvas(theme):
    curses = ThemeCurses(8, 16)
    palette = screen.CursesPalette(curses, theme=theme)
    style = P.cell_style("sel", theme)
    selection = P.resolve(style, theme)
    base = P.resolve(P.cell_style("", theme), theme)
    if P.color_index(selection.background, 8, background=True) == P.color_index(base.background, 8, background=True):
        assert palette.attr(style, theme) & curses.A_REVERSE
        assert "7" in P.ansi_codes(style, 8, theme).split(";")
    assert "7" not in P.ansi_codes(style, 24, theme).split(";")[:2]
    assert "7" not in P.ansi_codes(style, 256, theme).split(";")[:2]


@pytest.mark.parametrize("theme", NAMED + ("default", "light"))
def test_small_pair_table_keeps_opaque_menu_and_info_surfaces_after_gradient_saturation(theme):
    curses = ThemeCurses(256, 16)
    palette = screen.CursesPalette(curses, theme=theme)
    for i in range(100):
        palette.attr(P.cell_style(P.gradient_style("cyan", "magenta", i / 99), theme), theme)
    for style, paper in (("text+bg:surface", "surface"),
                         ("text+bg:surface-raised", "surface-raised"),
                         ("accent+bg:surface-raised", "surface-raised"),
                         ("muted+bg:surface", "surface"),
                         ("border+bg:surface", "surface"),
                         ("cursor", "canvas")):
        attr = palette.attr(P.cell_style(style, theme), theme)
        foreground, background = curses.initialized[attr >> 8]
        assert background == P.color_index(P.rgb(P.theme_tokens(theme)[paper]))
        if style == "cursor":
            assert foreground == P.color_index(P.rgb(P.theme_tokens(theme)["cursor"]))
