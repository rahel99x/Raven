"""User-visible log prefixes survive persisted state and source changes."""
from __future__ import annotations

import pytest

from raven import clipboard, log_workbench
from raven.config import Config
from raven.controller import App
from raven import layout
from raven.layout import Glyphs, row_text
from raven.model import Finished, Store
from raven.remote import LocalFiles
from raven.views import Views


@pytest.fixture
def dashboard(tmp_path):
    out, err = tmp_path / "stdout.log", tmp_path / "stderr.log"
    out.write_bytes(b"ABfirst\tline\r\nCDsecond line\nEFthird line\n")
    err.write_bytes(b"GHerror output\nIJanother error\n")
    cfg = Config({"log_lines": 0, "clipboard": {"osc52": False, "tools": False}})
    store = Store(state_dir=str(tmp_path / "state"))
    store.finished = [Finished("77", "first", "FAILED", workdir=str(tmp_path)),
                      Finished("88", "second", "COMPLETED", workdir=str(tmp_path))]
    for job in store.finished:
        store.details[job.id] = {"StdOut": str(out), "StdErr": str(err), "WorkDir": str(tmp_path)}
    # Existing private state from v3.0.0 must migrate without hiding prefixes.
    store.save_ui({"workbench": {"log_workbench": {"pan": 2}}})
    app = App(store, None, None, cfg, "test", ascii_=False)
    app.files = app.logs.files = LocalFiles()
    views = Views(Glyphs(False), cfg, files=app.files)
    app.views_ref = views
    app.open_log("77")
    yield app, views, out, err
    if app.research:
        app.research.close()


def draw(app, views, width=100):
    rows, hits = views.compose(app.store.snapshot(), app, width, 24)
    return rows, [row_text(rows[y]) for y, kind, _ in hits if kind == "log_line"]


@pytest.mark.parametrize("width", [40, 64, 80, 100])
@pytest.mark.parametrize("ascii_", [False, True])
def test_old_saved_offset_never_hides_prefixes_and_active_pan_is_visible(dashboard, width, ascii_):
    app, views, out, err = dashboard
    views.set_ascii(ascii_)
    rows, lines = draw(app, views, width)
    assert "ABfirst" in lines[0]
    assert "CDsecond" in lines[1]
    assert "EFthird" in lines[2]
    app.run_command("logpan 2")
    rows, lines = draw(app, views, width)
    assert "first" in lines[0] and "ABfirst" not in lines[0]
    status = next(row_text(row) for row in rows if "pan 2" in row_text(row))
    assert "pan 2 (:logpan 0 resets)" in status
    app.run_command("logpan 0")
    assert "ABfirst" in draw(app, views, width)[1][0]


def test_pan_survives_append_and_yank_uses_the_original_prefixes(dashboard, monkeypatch):
    app, views, out, err = dashboard
    original = out.read_bytes()
    copied = []
    monkeypatch.setattr(clipboard, "copy", lambda value, state_dir=None, **kwargs: copied.append(value.encode()) or "copied")
    draw(app, views)
    app.handle("home")
    app.handle("v")
    app.handle("down")
    app.run_command("logpan 2")
    with out.open("ab") as stream:
        stream.write(b"KLappended line\n")
    draw(app, views)
    assert app.log_workbench_state["pan"] == 2
    app.handle("y")
    assert copied == [original.splitlines(keepends=True)[0] + original.splitlines(keepends=True)[1]]


def test_switching_stream_resets_pan_even_for_keys_received_between_frames(dashboard):
    app, views, out, err = dashboard
    draw(app, views)
    app.run_command("logpan 2")
    app.handle("e")
    assert "GHerror" in draw(app, views)[1][0]
    assert app.log_workbench_state["pan"] == 0
    app.run_command("logpan 2")
    app.handle("e")
    # No redraw between switching to stdout and deliberately panning it.
    app.handle("right")
    assert app.log_workbench_state["pan"] == 8
    draw(app, views)
    assert app.log_workbench_state["pan"] == 8


def test_another_job_sharing_the_same_file_opens_at_its_left_edge(dashboard):
    app, views, out, err = dashboard
    draw(app, views)
    app.run_command("logpan 2")
    app.open_log("88")
    assert "ABfirst" in draw(app, views)[1][0]
    assert app.log_workbench_state["pan"] == 0


def test_browser_source_change_and_back_restore_the_correct_file_position(dashboard):
    app, views, out, err = dashboard
    draw(app, views)
    app.run_command("logpan 2")
    app.logs.entries = [{"id": "out", "path": str(out), "label": "stdout"},
                        {"id": "err", "path": str(err), "label": "stderr"}]
    app.logs.browser, app.logs.browser_cursor = True, 1
    assert app.select_log_file()
    assert "GHerror" in draw(app, views)[1][0]
    assert app.log_workbench_state["pan"] == 0
    app.run_command("back")
    # Back returns to the old browser, then its already-open source.
    app.logs.browser = False
    rows, lines = draw(app, views)
    assert app.logs.path == str(out)
    assert app.log_workbench_state["pan"] == 2
    assert "first" in lines[0] and "ABfirst" not in lines[0]


def test_restart_starts_at_left_edge_without_changing_other_preferences(dashboard):
    app, views, out, err = dashboard
    draw(app, views)
    app.run_command("logpan 2")
    app.run_command("logpreview on")
    app.save()
    restarted = App(app.store, None, None, app.cfg, "test", ascii_=False)
    restarted.files = restarted.logs.files = app.files
    restarted.views_ref = views
    restarted.open_log("77")
    assert "ABfirst" in draw(restarted, views)[1][0]
    assert restarted.log_workbench_state["preview"] is True
    assert "pan" not in app.store.load_ui()["workbench"]["log_workbench"]


@pytest.mark.parametrize("line, offset, expected", [("a\u0301BC", 1, "BC"),
                                                    ("ab界\u0301CD", 3, "CD"),
                                                    ("a\u0301", 1, "")])
def test_pan_does_not_leave_the_accent_of_a_hidden_character(dashboard, line, offset, expected):
    app, views, out, err = dashboard
    draw(app, views)
    app.run_command(f"logpan {offset}")
    assert log_workbench.display_line(app, line) == expected


def test_wide_unicode_line_clipping_does_not_scan_offscreen_megabytes(monkeypatch):
    inspected = []
    original = layout.vlen
    def measure(text):
        inspected.append(len(text))
        return original(text)
    monkeypatch.setattr(layout, "vlen", measure)
    clipped = layout.cut("界" * 4_000_000, 100)
    assert clipped.endswith("…") and original(clipped) <= 100
    assert sum(inspected) < 200, "One terminal row must not scan millions of hidden characters"
