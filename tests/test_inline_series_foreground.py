"""Inline running advice and comparisons use cached foreground series views."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from raven import job_panels, layout as L
from raven.config import Config
from raven.model import Job, Live
from raven.views import Views


@pytest.mark.parametrize("page", ["advisor", "advisor-document", "compare"])
@pytest.mark.parametrize("interactive", [False, True])
def test_inline_pages_use_immediate_series_only_in_interactive_frames(page, interactive):
    calls = []
    observations = [{"k": "live", "t": 100, "cpu": .5, "rss": 1024 ** 3}]

    def read(kind, jid):
        calls.append((kind, jid))
        if kind != ("view" if interactive else "file"):
            pytest.fail("inline page used the wrong foreground series reader")
        return observations

    store = SimpleNamespace(series_view=lambda jid: read("view", jid),
                            series_of=lambda jid: read("file", jid))
    proxy = SimpleNamespace(
        store=store, interactive=interactive,
        analytics_view="compare" if page == "compare" else "advisor",
        analytics_virtual_advisor=page == "advisor-document",
        analytics_days_value=lambda: 2, analytics_retained={}, compare_ids=["7"],
    )
    job = Job("7", "trial", "cpu", "RUNNING", cpus=4, mem_req="4G", elapsed="01:00:00")
    snap = {"jobs": [job], "finished": [], "live": {"7": Live(rate=.5, avg=.5)}}
    views = Views(L.Glyphs(False), Config())
    body = [[("Comparison", "heading")]] if page == "compare" else None
    rows = job_panels._analytics_cards(views, snap, proxy, body, width=80)
    if page == "advisor-document":
        document = proxy.analytics_advisor_document
        rows += document.window(0, document.count)
    assert calls == [("view" if interactive else "file", "7")]
    text = "\n".join(L.row_text(row) for row in rows)
    assert "7" in text
    assert "1.0 GB" in text


@pytest.mark.parametrize("page", ["advisor", "advisor-document", "compare"])
@pytest.mark.parametrize("interactive", [False, True])
def test_malformed_saved_observations_cannot_break_inline_summaries(page, interactive):
    observations = [
        {"k": "live", "t": 100., "cpu": .5, "rss": 1024 ** 3},
        {"k": "live", "t": 101., "cpu": 1., "rss": 1024 ** 3},
        "interrupted producer",
        {"k": "live", "cpu": 100., "rss": 100 * 1024 ** 3},
        {"k": "live", "t": [], "cpu": 100., "rss": 100 * 1024 ** 3},
        {"k": {}, "t": 102.},
        {"k": "live", "t": 103., "cpu": "invalid", "eff": float("inf"), "rss": {"invalid": 1}},
        {"k": "gpu", "t": 104., "gpu": {"node:0": "invalid"}},
    ]
    original = deepcopy(observations)
    store = SimpleNamespace(series_view=lambda _: observations, series_of=lambda _: observations)
    proxy = SimpleNamespace(
        store=store, interactive=interactive,
        analytics_view="compare" if page == "compare" else "advisor",
        analytics_virtual_advisor=page == "advisor-document",
        analytics_days_value=lambda: 2, analytics_retained={}, compare_ids=["7"],
    )
    job = Job("7", "trial", "cpu", "RUNNING", cpus=4, mem_req="4G", elapsed="01:00:00")
    snap = {"jobs": [job], "finished": [], "live": {"7": Live(rate=.5, avg=.5)}}
    views = Views(L.Glyphs(False), Config())
    body = [[("Comparison", "heading")]] if page == "compare" else None
    rows = job_panels._analytics_cards(views, snap, proxy, body, width=80)
    if page == "advisor-document":
        document = proxy.analytics_advisor_document
        rows += document.window(0, document.count)
    text = "\n".join(L.row_text(row) for row in rows)
    assert "7" in text and "1.0 GB" in text
    assert "100.0 GB" not in text
    if page == "compare":
        assert "mean 75% / max 100%" in text
        assert "GPU mean unavailable" in text
    assert observations == original, "Rendering must not rewrite the saved observations"
