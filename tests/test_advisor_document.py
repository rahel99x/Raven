"""Advisor indexes retain complete data while painting only visible cards."""
from types import SimpleNamespace

import pytest

from raven import advisor, job_panels as J, layout as L, scrollbars as S, workspace_layout as W
from raven.advisor_document import AdvisorDocument
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Job, Live, Store
from raven.views import Views


@pytest.mark.parametrize("names", [340, 1000])
@pytest.mark.parametrize("density", ["compact", "comfortable"])
def test_large_inline_advisor_is_indexed_and_derives_only_visible_cards(monkeypatch, names, density):
    cfg = Config({"animations": False, "smooth_scrolling": False, "log_lines": 0})
    store = Store(persist=False)
    store.finished = [Finished(str(1000 + index), f"unique-family-{index:04}", "COMPLETED",
                               cpus=4, elapsed="00:10:00", rss=1024 ** 3,
                               req_mem=8 * 1024 ** 3, cpu_time=600, limit="01:00:00")
                      for index in range(names)]
    store.jobs = [Job(str(index + 1), f"experiment-{index:03}", "cpu", "RUNNING", cpus=4,
                      mem_req="8G", limit="01:00:00") for index in range(60)]
    app = App(store, None, None, cfg, "test", interactive=False)
    views = app.views_ref = Views(L.Glyphs(False), cfg)
    app.table_state["groups"] = False
    W.initialize(app).density = density
    app.run_command("jobpanel analytics advisor")
    cards, derived, reads = [], [], []
    render, advise, series = AdvisorDocument.render_card, advisor.advise_running, store.series_of
    monkeypatch.setattr(AdvisorDocument, "render_card", lambda self, index:
                        (cards.append(index), render(self, index))[1])
    monkeypatch.setattr(advisor, "advise_running", lambda job, *args, **kwargs:
                        (derived.append(job.id), advise(job, *args, **kwargs))[1])
    monkeypatch.setattr(store, "series_of", lambda identifier:
                        (reads.append(identifier), series(identifier))[1])
    def draw():
        return views.compose(store.snapshot(), app, 200, 60)
    rows, _ = draw()
    document = J.initialize(app)["virtual_document"]
    pane = next(item for item in S.initialize(app)["panes"] if item.key == "workspace:jobs:details")
    assert document.count > 2048 and pane.count == document.count
    assert len(document.offsets) == names + 60 + 2
    assert not derived and not reads
    assert len(cards) <= pane.page + 1
    assert not hasattr(document, "rows"), "The complete rendered report must not be retained"
    initial, marked = app.selected_id, {"2", "3"}
    app.marks = marked.copy()
    cards.clear()
    def forbidden(*args, **kwargs):
        raise AssertionError("A cached scrollbar action attempted source I/O")
    with monkeypatch.context() as patch:
        patch.setattr(store, "snapshot", forbidden)
        patch.setattr(app, "save", forbidden)
        assert S.activate(app, pane.key, "bottom")
    rows, _ = draw()
    last = next(item for item in S.initialize(app)["panes"] if item.key == pane.key)
    assert J.initialize(app)["virtual_document"] is document
    assert last.painted == last.limit
    assert "60 / experiment-059" in L.to_text(rows, 200)
    assert derived == reads and "60" in derived
    assert len(cards) <= last.page + 1 and len(derived) <= last.page + 1
    assert app.selected_id == initial and app.marks == marked
    assert len(rows) == 60 and all(L.vlen(L.row_text(row)) <= 200 for row in rows)
    # Each historical card, including the last, retains flags and every fact.
    final_historical = L.to_text(document.render_card(names - 1), document.width)
    assert f"unique-family-{names - 1:04}" in final_historical
    assert "--mem=1.5G" in final_historical
    assert "--cpus-per-task=2" in final_historical
    assert "--time=00:15:00" in final_historical


@pytest.mark.parametrize("width", [1, 7, 12, 24, 50, 100])
@pytest.mark.parametrize("ascii_", [False, True])
def test_running_cards_wrap_complete_notes_and_actionable_flags(width, ascii_):
    job = Job("900", "unicode-研究", "cpu", "RUNNING", cpus=8, mem_req="8G",
              elapsed="02:00:00", limit="00:10:00")
    past = Finished("700", job.name, "COMPLETED", elapsed="01:00:00", rss=4 * 1024 ** 3)
    live = Live(rss=4 * 1024 ** 3, avg=.1)
    document = AdvisorDocument([], [job], width, ascii_, live={job.id: live}, groups={job.name: [past]})
    calls = []
    document.bind([], [job], lambda item: (calls.append(item.id), advisor.advise_running(item, live, [], [past]))[1])
    rows = document.window(0, document.count)
    assert calls == [job.id]
    assert len(rows) == document.count
    assert all(L.vlen(L.row_text(row)) <= width for row in rows)
    # Width one necessarily removes inter-line whitespace; all original text
    # characters remain in order even at the narrowest physical viewport.
    text = "".join(L.row_text(row) for row in rows).replace(" ", "")
    for phrase in ("--mem5G", "--cpus-per-task2", "--time01:30:00",
                   "earlierrunstooklongerthanthislimit:01:00:00",
                   "already01:00:00pastthelongestcompletedrun", "sofar"):
        assert phrase in text


@pytest.mark.parametrize("cpus,efficiency", [(0, .1), (8, None), (8, .1), (8, .9)])
@pytest.mark.parametrize("history", [False, True])
def test_running_notes_match_the_existing_advisor_business_rules(cpus, efficiency, history):
    job = Job("9", "same", "cpu", "RUNNING", cpus=cpus, elapsed="02:00:00", limit="00:10:00")
    live = Live(avg=efficiency)
    records = [Finished("7", "same", "COMPLETED", elapsed="01:00:00"),
               Finished("8", "same", "FAILED", elapsed="09:00:00")] if history else []
    expected = (["earlier runs took longer than this limit: 01:00:00",
                 "already 01:00:00 past the longest completed run"] if history else [])
    if cpus and efficiency is not None and efficiency < .3:
        expected.append("so far")
    assert advisor.running_notes(job, live, records) == expected
    assert advisor.advise_running(job, live, [{"k": "live", "rss": 999}], records).notes == expected


def test_out_of_range_memory_expands_the_visible_card_without_losing_text():
    job, live = Job("9", "extreme", "cpu", "RUNNING", cpus=4), Live(rss=1e99)
    document = AdvisorDocument([], [job], 12, False)
    document.bind([], [job], lambda item: advisor.advise_running(item, live, [], []))
    initial = document.count
    document.window(0, initial)
    assert document.count > initial
    rows = document.window(0, document.count)
    assert len(rows) == document.count
    assert advisor.round_mem(1e99 * advisor.MEM_HEADROOM) in "".join(L.row_text(row) for row in rows).replace(" ", "")


@pytest.mark.parametrize("state,extra", [("OUT_OF_MEMORY", "ran out of memory: doubled"),
                                         ("TIMEOUT", "timed out: doubled")])
@pytest.mark.parametrize("width", [7, 24, 100])
def test_transient_terminal_job_preserves_prior_failure_notes_and_order(state, extra, width):
    job = Job("9", "same", "cpu", state, cpus=4, elapsed="02:00:00", limit="00:10:00")
    live = Live(avg=.1)
    history = [Finished("7", "same", "COMPLETED", elapsed="01:00:00")]
    # A series-only memory peak must not require scanning offscreen cards.
    derive = lambda item: advisor.advise_running(item, live, [{"k": "live", "rss": 1024 ** 3}], history)
    observed = derive(job)
    warnings = ["earlier runs took longer than this limit: 01:00:00",
                "already 01:00:00 past the longest completed run"]
    expected = warnings + ([extra, "so far"] if state == "OUT_OF_MEMORY" else ["so far", extra])
    assert observed.notes == expected
    document = AdvisorDocument([], [job], width, False, live={job.id: live}, groups={job.name: history})
    document.bind([], [job], derive)
    count = document.count
    rows = document.window(0, count)
    assert document.count == count and len(rows) == count
    text = "".join(L.row_text(row) for row in rows).replace(" ", "")
    assert extra.replace(" ", "") in text
    assert all(L.vlen(L.row_text(row)) <= width for row in rows)


def test_transient_oom_without_a_memory_measurement_does_not_invent_failure_note():
    job = Job("9", "same", "cpu", "OUT_OF_MEMORY", cpus=4)
    assert advisor.advise_running(job, None, [], []).notes == []
