"""Cosmetic metric feedback checks current visible sources, with bounded locks."""
from types import SimpleNamespace

import pytest

from raven import chart_interaction as C, layout as L, metric_live as M
from raven.model import Job, Store


@pytest.fixture
def controls():
    store = Store(persist=False)
    store.jobs = [Job(str(index), "trial", "cpu", "RUNNING", submit="s", start="a")
                  for index in range(M.MAX_METRICS)]
    requests = []
    sampler = SimpleNamespace(
        sampling_interval=lambda *_: 5.0,
        set_metric_sampling=lambda values: requests.append(dict(values)),
    )
    app = SimpleNamespace(
        mode="main", tab="analytics", width=120, height=40,
        selected_id="0", analytics_job="0", analytics_view="job", store=store,
        cfg={}, sampler=sampler, toolbar_state={}, project_state={},
        job_panel_state={}, analysis_state={}, research=None,
        say=lambda *_: None,
    )
    keys = [("resource-series", job.id, "cpu:rate", "%", "s|a", None, None, None)
            for job in store.jobs]
    for key in keys:
        M.set_running(app, key, True)
        assert M.set_enabled(app, key, True)
    C.begin_frame(app, app.width, app.height)
    for index, key in enumerate(keys[:2]):
        M.controls(L.Glyphs(False), app, key, 70, row=2 + index * 2, column=4)
    C.publish(app, app.width, app.height)
    assert len(M.initialize(app)["entries"]) == M.MAX_METRICS
    assert len(M.initialize(app)["records"]) == 2
    return SimpleNamespace(app=app, store=store, keys=keys, requests=requests)


def test_hover_checks_only_visible_attempts_and_maintenance_checks_retained_requests(controls, monkeypatch):
    d = controls
    visits = []
    read = d.store.job_attempt

    def attempt(jid):
        visits.append(jid)
        return read(jid)

    monkeypatch.setattr(d.store, "job_attempt", attempt)
    for _ in range(20):
        M.feedback(d.app, L.Glyphs(False))
    assert len(visits) == 40 and set(visits) == {"0", "1"}
    visits.clear()
    M.tick(d.app)
    assert set(visits) == {job.id for job in d.store.jobs}


@pytest.mark.parametrize("change", ["completed", "attempt"])
def test_visible_live_source_invalidates_on_next_cosmetic_frame(controls, change):
    d = controls
    if change == "completed":
        d.store.jobs[0].state = "COMPLETED"
    else:
        d.store.apply_jobs([Job(job.id, job.name, job.partition, job.state,
                               submit=job.submit, start="replacement" if job.id == "0" else job.start)
                            for job in d.store.jobs])
    M.feedback(d.app, L.Glyphs(False))
    entries = M.initialize(d.app)["entries"]
    assert not entries[d.keys[0]]["enabled"] and not entries[d.keys[0]]["running"]
    assert entries[d.keys[1]]["enabled"]
    assert entries[d.keys[-1]]["enabled"]


def test_offscreen_sampling_expiration_still_publishes_during_maintenance(controls):
    d = controls
    assert M.set_rate(d.app, d.keys[-1], 100)
    assert d.requests[-1] == {d.keys[-1]: 100}
    d.store.jobs[-1].state = "COMPLETED"
    M.feedback(d.app, L.Glyphs(False))
    # Cosmetic input does not scan unrelated sources; the ordinary workbench
    # tick retains responsibility for their collector lifecycle.
    entry = M.initialize(d.app)["entries"][d.keys[-1]]
    assert entry["rate"] == 100
    M.tick(d.app)
    assert entry["rate"] == M.MIN_RATE and not entry["enabled"]
    assert d.requests[-1] == {}
