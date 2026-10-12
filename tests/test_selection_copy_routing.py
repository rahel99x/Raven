"""Copy intent survives refreshes and never silently widens to the screen."""
import pytest

from raven import clipboard, layout, text_selection as text
from raven.config import Config
from raven.controller import App
from raven.model import Store
from raven.views import Views

from test_text_selection import paint


@pytest.fixture
def dashboard(monkeypatch):
    config = Config({"animations": False, "startup_animation": False,
                     "gpu_sampling": False, "clipboard": {"tools": False}})
    app = App(Store(persist=False), None, None, config, "tester", interactive=False)
    app.views_ref = Views(layout.Glyphs(False), config)
    app.tab, app.analytics_view = "analytics", "history"
    app.width, app.height = 80, 20
    copies, failures = [], []
    monkeypatch.setattr(clipboard, "copy", lambda value, *a, **kw: copies.append(value) or "copied")
    monkeypatch.setattr(app, "fail", failures.append)
    monkeypatch.setattr(app, "save", lambda: None)
    yield app, copies, failures
    if app.research:
        app.research.close()


@pytest.mark.parametrize("key", ["y", ":copy"])
@pytest.mark.parametrize("repaint", [False, True])
def test_changed_rendered_source_never_copies_other_lines(dashboard, key, repaint):
    app, copies, failures = dashboard
    app.last_rows = paint(app, ["selected line", "unselected line"], context="old")
    assert text.handle_mouse(app, 3, 2, button="left")
    app.analytics_job = app.selected_id = "different-job"
    if repaint:
        app.last_rows = paint(app, ["replacement line", "another replacement"], context="new")
    app.run_command(key[1:]) if key.startswith(":") else app.handle(key)
    assert copies == []
    assert failures and "source changed" in failures[-1]


@pytest.mark.parametrize("key", ["y", ":copy"])
def test_unselected_or_deselected_document_never_copies_entire_screen(dashboard, key):
    app, copies, failures = dashboard
    app.last_rows = paint(app, ["unselected text", "unrelated text"])
    for selected_before in (False, True):
        if selected_before:
            assert text.handle_mouse(app, 3, 2, button="left")
            text.clear(app)
        app.run_command(key[1:]) if key.startswith(":") else app.handle(key)
        assert copies == []
        assert "Select a row or text lines" in failures[-1]


@pytest.mark.parametrize("key", ["y", ":copy"])
@pytest.mark.parametrize("last", [3, 5])
def test_single_and_multiple_rendered_lines_copy_only_their_pane(dashboard, key, last):
    app, copies, failures = dashboard
    app.last_rows = paint(app, [f"neighbor   selected {n}" for n in range(5)], rect=(3, 11, 8, 70))
    assert text.handle_mouse(app, 3, 15, button="press")
    assert text.handle_mouse(app, last, 15, button="release")
    # A fresh frame must preserve the selected text even as metrics update.
    app.last_rows = paint(app, [f"neighbor   updated {n}" for n in range(5)], rect=(3, 11, 8, 70))
    app.run_command(key[1:]) if key.startswith(":") else app.handle(key)
    assert copies == ["".join(f"selected {n}\n" for n in range(last - 2))]
    assert not failures


@pytest.mark.parametrize("key", ["Y", ":copy all"])
def test_explicit_copy_all_replaces_subset_and_excludes_neighbor(dashboard, key):
    app, copies, failures = dashboard
    app.last_rows = paint(app, [f"neighbor   line {n}" for n in range(5)], rect=(3, 11, 8, 70))
    assert text.handle_mouse(app, 4, 15, button="left")
    app.run_command(key[1:]) if key.startswith(":") else app.handle(key)
    assert copies == ["".join(f"line {n}\n" for n in range(5))]
    assert not failures


def test_fresh_selection_recovers_after_invalidated_source(dashboard):
    app, copies, failures = dashboard
    paint(app, ["old line"], context="old")
    assert text.handle_mouse(app, 3, 2, button="press")
    paint(app, ["new line"], context="new")
    app.handle("y")
    assert not copies and failures
    # Consume the late release, then accept a complete fresh gesture.
    assert text.handle_mouse(app, 3, 2, button="release")
    assert text.handle_mouse(app, 3, 2, button="left")
    app.handle("y")
    assert copies == ["new line\n"]


def test_copy_all_without_published_pane_does_not_use_screen_fallback(dashboard):
    app, copies, failures = dashboard
    app.last_rows = [[("private neighboring content", "text")]]
    app.copy_visible_pane()
    assert copies == [] and "No current pane" in failures[-1]


def test_explicit_screen_range_replaces_previous_pane_range(dashboard):
    app, copies, failures = dashboard
    app.last_rows = paint(app, ["first line", "second line", "third line"])
    assert text.handle_mouse(app, 3, 2, button="left")
    app.run_command("copy 5 6")
    assert copies == ["second line\nthird line\n"]
    assert not failures


def test_selection_copy_respects_rebound_keys(dashboard):
    app, copies, failures = dashboard
    app.last_rows = paint(app, ["selected line"])
    assert text.handle_mouse(app, 3, 2, button="left")
    app.keymap.update(y="refresh", z="yank")
    assert not text.handle_key(app, "y")
    app.handle("z")
    assert copies == ["selected line\n"]
    assert not failures
