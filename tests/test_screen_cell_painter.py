"""Overlay feedback restores source glyphs with only changed terminal cells."""
from types import SimpleNamespace

from raven import layout as L, screen


def recorder():
    painted, erased = [], []

    def paint(y, x, row, width, height):
        painted.append((y, x, tuple(row)))

    return screen._DifferentialPainter(SimpleNamespace(erase=lambda: erased.append(True)), paint), painted, erased


def test_moving_vertical_crosshair_updates_two_cells_per_row_without_repainting_chart():
    painter, painted, erased = recorder()
    # Each source cell has a separate gradient tone, as real chart rasters do.
    base = [[("⠉", "fg:#%06x" % column) for column in range(200)] for _ in range(60)]
    first = [(y, 74, [("┊", "selector")]) for y in range(60)]
    second = [(y, 75, [("┊", "selector")]) for y in range(60)]
    assert painter.draw(base, first, 200, 60) == tuple(range(60))
    painted.clear()
    assert painter.draw(base, second, 200, 60) == tuple(range(60))
    assert len(painted) == 60
    assert sum(L.vlen(L.row_text(row)) for _, _, row in painted) == 120
    assert all(x == 74 and L.row_text(row) == "⠉┊" for _, x, row in painted)
    assert len(erased) == 1
    painted.clear()
    assert painter.draw(base, [], 200, 60) == tuple(range(60))
    assert sum(L.vlen(L.row_text(row)) for _, _, row in painted) == 60
    assert all(x == 75 and L.row_text(row) == "⠉" for _, x, row in painted)


def test_partially_overlaid_wide_glyph_is_restored_atomically_with_combining_marks():
    painter, painted, _ = recorder()
    base = [[("AB界e\u0301XYZ", "cyan")]]
    painter.draw(base, [(0, 3, [("+", "selector")])], 12, 1)
    assert painter.pixels[0][2][:2] == (" ", "cyan")
    assert painter.pixels[0][3][:2] == ("+", "selector")
    painted.clear()
    assert painter.draw(base, [], 12, 1) == (0,)
    assert painted == [(0, 2, (("界", "cyan"),))]
    assert painter.pixels[0][2] == ("界", "cyan", 2)
    assert painter.pixels[0][3] == ("", "cyan", 0)
    assert painter.pixels[0][4] == ("e\u0301", "cyan", 1)


def test_later_overlay_masks_lower_changes_and_removal_restores_correct_layer():
    painter, painted, _ = recorder()
    base = [[("abcdefghij", "base")]]
    menu = (0, 2, [("MENU", "menu")])
    painter.draw(base, [(0, 3, [("+", "selector")]), menu], 10, 1)
    painted.clear()
    assert painter.draw(base, [(0, 4, [("+", "selector")]), menu], 10, 1) == ()
    assert not painted
    assert painter.draw(base, [(0, 4, [("+", "selector")])], 10, 1) == (0,)
    assert painted == [(0, 2, (("cd", "base"), ("+", "selector"), ("f", "base")))]
    painted.clear()
    assert painter.draw(base, [], 10, 1) == (0,)
    assert painted == [(0, 4, (("e", "base"),))]


def test_style_only_selection_change_repaints_only_selected_text():
    painter, painted, _ = recorder()
    painter.draw([[('ordinary row', 'text')]], [], 20, 1)
    painted.clear()
    painter.draw([[('ordinary ', 'text'), ('row', 'text+selected')]], [], 20, 1)
    assert painted == [(0, 9, (("row", "text+selected"),))]


def test_pointer_motion_reuses_base_raster_and_source_publication_invalidates_it(monkeypatch):
    painter, painted, _ = recorder()
    base = [[("⠉" * 100, "curve")]]
    painter.draw(base, [(0, 50, [("┊", "selector")])], 100, 1)
    measured = []
    original = L.vlen

    def width(text):
        measured.append(text)
        return original(text)

    monkeypatch.setattr(L, "vlen", width)
    painter.draw(base, [(0, 51, [("┊", "selector")])], 100, 1)
    assert "⠉" not in measured
    measured.clear()
    published = [[("⠊" * 100, "curve")]]
    painter.draw(published, [(0, 51, [("┊", "selector")])], 100, 1)
    assert measured.count("⠊") == 100


def test_invalidation_forces_repaint_when_theme_resolves_identical_style_names_differently():
    painter, painted, erased = recorder()
    base = [[("unchanged style names", "text")]]
    painter.draw(base, [], 24, 1)
    painted.clear()
    assert painter.draw(base, [], 24, 1) == ()
    painter.invalidate()
    assert painter.draw(base, [], 24, 1) == (0,)
    assert len(erased) == 2
    assert sum(L.vlen(L.row_text(row)) for _, _, row in painted) == 24
