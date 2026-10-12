"""Job-pane previews use the same bounded worker instead of blocking scrolling."""
from threading import Event, current_thread

from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs
from raven.model import Store
from raven.remote import LocalFiles
from raven.research import ResearchHub
from raven.views import Views, tail_lines


def test_shared_filesystem_preview_does_not_block_and_keeps_safe_prefixes(tmp_path):
    path = tmp_path / "worker.log"
    path.write_bytes(b"\x1b[31mABerror\x1b[0m\r\nCDprefix\rcontent\x00\n")
    entered, release = Event(), Event()
    class SharedFiles(LocalFiles):
        def tail(self, path, max_bytes):
            assert current_thread().name.startswith("raven-research")
            entered.set()
            assert release.wait(3)
            return super().tail(path, max_bytes)
    files, cfg = SharedFiles(), Config({})
    app = App(Store(persist=False), None, None, cfg, "test")
    app.research = ResearchHub(cfg, files)
    views = Views(Glyphs(False), cfg, files=files)
    try:
        first = views.log_preview(app, str(path), 3)
        assert "background" in first[0]
        assert entered.wait(1)
        future = app.research.future
        for _ in range(100):
            assert views.log_preview(app, str(path), 3) == first
        assert app.research.future is future
        release.set()
        future.result(timeout=3)
        app.research.poll_task()
        assert views.log_preview(app, str(path), 3) == ["ABerror", "CDprefix^Mcontent^@"]
    finally:
        release.set()
        app.research.close()


def test_explicit_noninteractive_preview_keeps_physical_lines_and_safe_controls(tmp_path):
    path = tmp_path / "worker.log"
    path.write_bytes(b"ABfirst\r\n\nCDlast\tvalue\x00")
    assert tail_lines(str(path), 3) == ["ABfirst", "", "CDlast    value^@"]
