"""Published Live graphs cannot turn decoded pointer traffic into navigation."""
from collections import deque
import curses

import pytest

from raven import chart_interaction, cli, clock, metric_live, screen
from raven.config import Config


class PointerWindow:
    def __init__(self, sequence):
        self.values = deque(sequence)

    def get_wch(self):
        if not self.values:
            raise curses.error("input drained")
        return self.values.popleft()

    def timeout(self, _):
        pass


def pointer(protocol, x, y, held=False):
    code = 32 if held else 35
    if protocol == "sgr":
        sequence = f"\x1b[<{code};{x + 1};{y + 1}M"
    elif protocol == "urxvt":
        sequence = f"\x1b[{code + 32};{x + 1};{y + 1}M"
    else:
        sequence = "\x1b[M" + "".join(chr(value) for value in (code + 32, x + 33, y + 33))
    reader = screen._InputReader(PointerWindow(sequence))
    event = reader.read(curses)
    assert event is not None and event[0] == "mouse" and not reader.queue
    return event


@pytest.fixture
def native(monkeypatch):
    now = [1_791_500_000.0]
    monkeypatch.setattr(clock, "now", lambda: now[0])
    cfg = Config({"animations": False, "startup_animation": False, "log_lines": 0})
    session = cli.build(cli.parse(["--fake", "--no-state", "--no-plugins"]), cfg)
    session.sampler.src_jobs()
    job = next(job for job in session.store.jobs if not job.pending and job.gpus)
    ordered_ids = [row["id"] for row in session.views.job_rows(session.store.snapshot(), session.app)]
    session.app.cursor["jobs"] = ordered_ids.index(job.id)
    session.app.selected_id = session.app.analytics_job = job.id
    session.app.project_state["auto_suppressed"] = job.id
    for _ in range(4):
        session.sampler.src_live()
        session.sampler.src_gpu()
        now[0] += .5
    yield session, now
    session.close()


@pytest.mark.parametrize("tab", ["jobs", "analytics"])
@pytest.mark.parametrize("protocol", ["sgr", "urxvt", "x10"])
@pytest.mark.parametrize("ascii_", [False, True])
def test_native_live_publications_and_hover_leave_logs_and_other_tabs_unactivated(native, monkeypatch, tab, protocol, ascii_):
    session, now = native
    app, views, store = session.app, session.views, session.store
    app.tab = tab
    app.job_panel_state.update(mode="analytics", analytics_view="job")
    views.set_ascii(ascii_)
    cache = screen._FrameCache()
    cache.rebuild(app, views, store, session.actions, 190, 70)
    controls = metric_live.initialize(app)["records"]
    assert controls
    for control in controls:
        assert metric_live.set_enabled(app, control.key, True)
    selected = app.selected_id
    transitions, log_actions = [], []
    original_tab, original_log = app.enter_tab, app.open_log

    def enter(name):
        transitions.append(name)
        return original_tab(name)

    def open_log(*args, **kwargs):
        log_actions.append(args)
        return original_log(*args, **kwargs)

    monkeypatch.setattr(app, "enter_tab", enter)
    monkeypatch.setattr(app, "open_log", open_log)
    for _ in range(4):
        # Real Slurm-parsed publications and the production frame pipeline
        # exercise changing Live geometry and published hit maps together.
        session.sampler.src_live()
        session.sampler.src_gpu()
        now[0] += .5
        cache.rebuild(app, views, store, session.actions, 190, 70)
        plot = next(plot for plot in chart_interaction.initialize(app)["plots"] if plot.kind == "metric")
        log_tab = next(hit for hit in app.tab_hits if hit[3] == "log")
        points = [(plot.visible.left + 2, plot.visible.top + 1),
                  (plot.visible.right - 2, plot.visible.bottom - 1),
                  (log_tab[1] + 1, log_tab[0]), (76, plot.visible.top + 1)]
        points += [(control.toggle.left, control.toggle.top)
                   for control in metric_live.initialize(app)["records"]]
        backend_calls = len(session.backend.calls)
        for held in (False, True, False):
            for x, y in points:
                event = pointer(protocol, x, y, held)
                screen._apply_input(app, event, cache.hits, curses)
                cache.feedback(app, views)
                assert app.tab == tab and app.mode == "main" and app.selected_id == selected
        assert len(session.backend.calls) == backend_calls
        assert not transitions and not log_actions
    # The intentional shortcut still opens the exact selected job's Logs.
    app.handle("l")
    assert app.tab == "log" and app.log_job == selected
    assert log_actions == [()]
