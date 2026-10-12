"""A scrolled batch retains one usable disclosure without changing job identity."""
import pytest

from raven import history_browser as H, job_groups as G, layout as L
from raven.config import Config
from raven.controller import App
from raven.model import Job, Store
from raven.views import Views


@pytest.fixture
def raven():
    store = Store(persist=False)
    store.jobs = [Job(f"700_{index:02}", "batch", "cpu", "RUNNING") for index in range(40)]
    store.group = list(store.jobs)
    cfg = Config({"log_lines": 0, "animations": False, "smooth_scrolling": False,
                  "workspace": {"density": "compact"}})
    app = App(store, None, None, cfg, "tester", interactive=False)
    views = Views(L.Glyphs(False), cfg)
    app.views_ref = views
    yield app, views, store
    if app.research:
        app.research.close()


@pytest.mark.parametrize("context", ["jobs", "recent", "history", "group", "analytics:advisor"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("ascii_", [False, True])
def test_first_visible_member_has_one_arrow_after_sort_filter_and_scroll(raven, context, reverse, ascii_):
    app, views, store = raven
    views.set_ascii(ascii_)
    # Filtering omits the first allocations; sort then changes the real
    # representative. The viewport begins still farther into that projection.
    source = sorted(store.jobs[3:35], key=lambda job: job.id, reverse=reverse)
    projected = G.project_records(app, store.snapshot(), source, tab=context)
    visible = projected[8:15]
    representative = projected[0].id
    assert visible and not G.metadata_for_record(app, context, visible[0].id).header
    rows = [[(" " + job.id + " original row", "")] for job in visible]
    hits = [(y, "job", job.id) for y, job in enumerate(visible)]
    before = G.registry(app).inference_count
    views.group_controls(app, rows, hits, visible, 0, context, 70, first_visible=True)
    controls = [(y, value) for y, kind, value in hits if kind == "control"]
    assert len(controls) == 1
    y, control = controls[0]
    assert y == 0 and L.row_text(rows[0]).startswith("v" if ascii_ else "▾")
    assert control["action"] == ("command", "jobgroup close array:700")
    assert "representative job " + representative in control["label"]
    assert [(y, value) for y, kind, value in hits if kind == "job"] == list(enumerate(job.id for job in visible))
    assert all(job.id in L.row_text(rows[y]) for y, job in enumerate(visible))
    assert G.metadata_for_record(app, context, visible[0].id).representative_id == representative
    assert not G.metadata_for_record(app, context, visible[0].id).header
    assert G.registry(app).inference_count == before


@pytest.mark.parametrize("tab", H.TABS)
@pytest.mark.parametrize("dock", ["left", "right", "top", "bottom"])
def test_scrolled_job_dock_promotes_only_the_disclosure_cell(raven, tab, dock):
    app, views, store = raven
    app.tab = tab
    snap = store.snapshot()
    records = H._records(snap, H.initialize(app))
    items = H._items(app, snap, records)
    H.initialize(app)["reveal"] = False
    view = H._view(app)
    view.update(top=8, dock=dock)
    assert items[8].meta is not None and not items[8].meta.header
    representative = items[8].meta.representative_id
    rows, hits, _, _ = H._browser_rows(views, app, H.Rect(0, 0, 85, 8), items, None, view, dock)
    group_hits = [(y, value) for y, kind, value in hits if kind == "control" and ":group:" in value["id"]]
    assert len(group_hits) == 1
    y, control = group_hits[0]
    list_controls = [yy for yy, kind, value in hits if kind == "control" and value.get("group") == "job-list-controls"]
    assert list_controls and y == max(list_controls) + 1
    assert L.row_text(rows[y]).startswith("▾ " + items[8].record.id)
    assert control["action"] == ("command", "jobgroup close array:700")
    assert any(kind == "control" and value["id"].endswith(":job:" + items[8].record.id)
               and value["action"] == ("command", "history-job " + items[8].record.id)
               for _, kind, value in hits)
    assert items[8].meta.representative_id == representative and not items[8].meta.header


def test_advisor_uses_actual_scrolled_rows_instead_of_overscan_for_group_arrow(raven):
    app, views, store = raven
    app.tab, app.analytics_view = "analytics", "advisor"
    app.width, app.height = 120, 18
    views.analytics_tab(store.snapshot(), app, 120, 18)
    app.analytics_scroll_offsets["advisor"]["top"] = 15
    rows, hits = views.analytics_tab(store.snapshot(), app, 120, 18)
    controls = [(y, value) for y, kind, value in hits if kind == "control"
                and value["id"] == "jobgroup:analytics:advisor:array:700"]
    assert len(controls) == 1
    y, control = controls[0]
    assert control["action"] == ("command", "jobgroup close array:700")
    text = L.row_text(rows[y])
    assert text.startswith("▾") and "700_" in text and "700_00" not in text
    assert not any(L.row_text(row).startswith("▾") for row in rows[y + 1:])
