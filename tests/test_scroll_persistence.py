"""Navigation cannot wait on private preference writes to a shared filesystem."""
from __future__ import annotations

import pytest

from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs
from raven.model import Finished, Job, Store
from raven.remote import LocalFiles
from raven.views import Views


@pytest.mark.parametrize("tab", ["jobs", "history", "log"])
def test_navigation_never_writes_ui_state_but_preferences_still_persist(tmp_path, monkeypatch, tab):
    cfg = Config({"log_lines": 0})
    store = Store(state_dir=str(tmp_path / "private state"))
    store.apply_jobs([Job(str(i), f"job {i}", "main", "RUNNING") for i in range(40)])
    store.finished = [Finished(str(100 + i), f"finished {i}", "COMPLETED") for i in range(40)]
    path = tmp_path / "job.log"
    path.write_text("".join(f"source line {i}\n" for i in range(200)))
    store.details["0"] = {"StdOut": str(path), "WorkDir": str(tmp_path)}
    app = App(store, None, None, cfg, "test")
    app.files = app.logs.files = LocalFiles()
    views = Views(Glyphs(False), cfg, files=app.files)
    app.views_ref = views
    if tab == "log":
        app.open_log("0")
    else:
        app.enter_tab(tab)
    views.compose(store.snapshot(), app, 100, 24)
    writes = []
    original = store.save_ui
    def save(data):
        writes.append(data)
        original(data)
    monkeypatch.setattr(store, "save_ui", save)
    for key in ["home", "up", "up", "pgdn", "down", "pgup", "end"] + ["up"] * 80:
        app.handle(key)
    assert not writes, "Scrolling must never wait for a preferences-file write"
    app.handle_action("theme")
    assert len(writes) == 1
    assert store.load_ui()["theme"] == app.theme
    # Closing the session still persists all preferences through App.save.
    app.save()
    assert len(writes) == 2
