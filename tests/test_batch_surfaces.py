"""Batch presentation across job selectors must keep allocation actions exact."""
import pytest

from raven import history_browser, job_groups, layout as L, table_ui
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Job, Store
from raven.views import Views


@pytest.fixture
def batch():
    store = Store(persist=False)
    store.jobs = [Job("600_1", "train", "cpu", "RUNNING"),
                  Job("600_2", "train", "cpu", "PENDING", reason="Resources"),
                  Job("600_3", "train", "cpu", "PENDING", reason="Dependency"),
                  Job("600_4", "train", "cpu", "PENDING", reason="DependencyNeverSatisfied")]
    store.finished = [Finished("500_1", "train", "COMPLETED"), Finished("500_2", "train", "FAILED")]
    store.group = list(store.jobs)
    app = App(store, None, None, Config({"log_lines": 0}), "test", interactive=False)
    views = Views(L.Glyphs(False), app.cfg)
    app.views_ref = views
    job_groups.registry(app).ensure(store.snapshot())
    job_groups.fold(app, "array:600", True)
    job_groups.fold(app, "array:500", True)
    yield app, views, store
    if app.research:
        app.research.close()


@pytest.mark.parametrize("tab", ["jobs", "history", "group"])
@pytest.mark.parametrize("ascii_", [False, True])
def test_collapsed_tables_show_info_and_keep_exact_real_ids(batch, tab, ascii_):
    app, views, store = batch
    app.tab, views.g = tab, L.Glyphs(ascii_)
    rows, hits = views.compose(store.snapshot(), app, 300, 50)
    context = "history" if tab == "history" else tab
    representatives = [meta for meta in app.job_group_metadata[context].values() if meta.header]
    assert representatives
    for meta in representatives:
        assert meta.representative_id in {record.id for record in store.jobs + store.finished}
        control = next(value for _, kind, value in hits if kind == "control" and
                       value["id"] == f"jobgroup:{context}:{meta.group.id}")
        assert "representative job " + meta.representative_id in control["label"]
        assert any(kind == "sort_header" and value[:2] == (context, "info") for _, kind, value in hits)
    text = L.to_text(rows, 300)
    assert "INFO" in text
    if ascii_:
        assert text.isascii()
    assert all(L.vlen(L.row_text(row)) <= 300 for row in rows)


def test_recent_and_history_metrics_do_not_imply_a_batch_aggregate(batch):
    app, views, store = batch
    for context, records in (("recent", app.recent_jobs(store.snapshot())),
                             ("history", app.history_jobs(store.snapshot()))):
        representative = next(record for record in records if record.id.startswith("500_"))
        row = views.finished_dict(representative, app=app, context=context)
        assert row["state"] == "BATCH" and row["id"] == representative.id
        assert "2 records" in row["name"]
        assert all(row[key] == "" for key in ("ce", "me", "rss", "elapsed", "cpus", "gpus"))
        assert row["info"] == job_groups.summary(row["_group"].records, compact=True)


@pytest.mark.parametrize("tab", history_browser.TABS)
def test_all_job_docks_share_folds_and_current_state_counts(batch, tab):
    app, views, store = batch
    app.tab = tab
    app.body_origin, app.width, app.height = 6, 280, 45
    app.analytics_job = app.log_job = app.research_job_id = app.selected_id = "600_1"
    def render():
        return history_browser.wrap_render(views, store.snapshot(), app, 280, 38,
            lambda _width, _height: ([[('data', '')]], []))
    rows, hits = render()
    items = history_browser.initialize(app)["items"]
    assert len([item for item in items if item.meta and item.meta.group.id == "array:600"]) == 1
    hit = next(hit for hit in hits if hit[1] == "control" and hit[2]["id"].endswith("group:array:600"))
    assert hit[2]["action"] == ("command", "jobgroup open array:600")
    before = job_groups.registry(app).inference_count
    state_before = L.row_text(rows[hit[0]])
    for reason in ("Dependency", "DependencyNeverSatisfied"):
        store.jobs[1].reason = reason
        rows, _ = render()
        assert L.row_text(rows[hit[0]]) != state_before
        assert job_groups.registry(app).inference_count == before
        state_before = L.row_text(rows[hit[0]])
    assert history_browser.activate(app, "600_1")
    assert history_browser.handle_key(app, "right")
    render()
    assert len([item for item in history_browser.initialize(app)["items"] if item.meta and item.meta.group.id == "array:600"]) == 4


def test_new_batch_members_refresh_fold_projection_and_keep_marks_exact(batch):
    app, views, store = batch
    app.tab, app.marks = "jobs", {"600_1"}
    views.compose(store.snapshot(), app, 180, 44)
    store.jobs.append(Job("600_5", "train", "cpu", "RUNNING"))
    rows, hits = views.compose(store.snapshot(), app, 180, 44)
    meta = job_groups.metadata_for_record(app, "jobs", "600_1")
    assert meta.visible_count == 5 and app.marks == {"600_1"}
    assert len([jid for jid in app.visible_ids if jid.startswith("600_")]) == 1
    control = next((y, value) for y, kind, value in hits if kind == "control" and value["id"] == "jobgroup:jobs:array:600")
    collapsed = list(app.table_state["collapsed"])
    for event in ("motion", "release", "right"):
        app.click(control[0], control[1]["left"], hits, button=event)
        assert app.table_state["collapsed"] == collapsed
        assert app.tab == "jobs"


def test_history_info_column_is_optional_and_saved_group_opt_out_is_honored(batch):
    app, views, store = batch
    table_ui.restore(app, {"groups": False})
    assert len(app.history_jobs(store.snapshot())) == 2
    assert "info" not in [column.key for column in views.finished_columns(app, "history")]
    assert len(views.job_rows(store.snapshot(), app)) == 4


def test_advisor_and_independent_dependencies_publish_group_fold_controls(batch):
    app, views, store = batch
    store.jobs[1].state = "RUNNING"
    app.tab, app.analytics_view = "analytics", "advisor"
    rows, hits = views.analytics_tab(store.snapshot(), app, 140, 36)
    assert any(kind == "control" and value["id"] == "jobgroup:analytics:advisor:array:600" for _, kind, value in hits)
    assert "2 records" in L.to_text(rows, 140)
    app.tab = "deps"
    rows, hits = views.deps_tab(store.snapshot(), app, 140, 36)
    assert any(kind == "control" and value["id"].startswith("jobgroup:deps:array:600") for _, kind, value in hits)
    assert "4 records" in L.to_text(rows, 140)


def test_narrow_summary_preserves_blocked_badge_and_uses_theme_roles(batch):
    from raven.job_group_ui import summary_row
    app, views, store = batch
    row = summary_row(store.jobs, 16)
    assert L.row_text(row).strip() == "R1 P1 D1 !1"
    assert dict(row)["!1"] == "red"
    assert all(not style.startswith("#") for _, style in row)
    for width in range(17):
        assert L.vlen(L.row_text(summary_row(store.jobs, width))) <= width


def test_compressed_array_rows_are_labeled_as_records_not_task_totals(batch):
    app, views, store = batch
    store.finished = [Finished("500_[1-900%8]", "train", "COMPLETED"),
                      Finished("500_901", "train", "FAILED")]
    records = app.history_jobs(store.snapshot())
    assert len(records) == 1
    row = views.finished_dict(records[0], app=app)
    assert "2 records" in row["name"] and "records" in row["info"]


@pytest.mark.parametrize("width", [1, 8, 24, 40, 60, 80, 120])
def test_fold_controls_never_escape_narrow_table_frames(batch, width):
    app, views, store = batch
    for tab in ("jobs", "history", "group", "deps"):
        app.tab = tab
        rows, hits = views.compose(store.snapshot(), app, width, 30)
        assert all(L.vlen(L.row_text(row)) <= width for row in rows)
        for y, kind, value in hits:
            if kind == "control" and value["id"].startswith("jobgroup:"):
                assert 0 <= y < len(rows) and 0 <= value["left"] < value["right"] <= width


def test_group_info_splices_once_with_wide_combining_and_selection_parity(batch, monkeypatch):
    import random
    from types import SimpleNamespace
    from raven import job_group_ui
    app, views, store = batch
    original = job_group_ui._replace
    calls = []
    def counted(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(job_group_ui, "_replace", counted)
    random_ = random.Random(90211)
    meta = SimpleNamespace(collapsed=True, records=store.jobs)
    def characters(row):
        return [(char, style) for text, style in row for char in text]
    for selected in (False, True):
        for _ in range(200):
            row = [("".join(random_.choice("abc界é\u0301") for _ in range(24)), "rev" if selected else "cyan"),
                   (" memory  CPU", "rev" if selected else "yellow")]
            width = L.vlen(L.row_text(row))
            left = random_.randrange(max(1, width - 10))
            right = min(width, left + random_.randrange(1, 18))
            expected, position = row, left
            for text, style in job_group_ui.summary_row(store.jobs, right - left, selected=selected):
                expected = original(expected, position, text, style, width)
                position += L.vlen(text)
            before = len(calls)
            actual = job_group_ui.paint_info(row, meta, (left, right), width)
            assert len(calls) == before + 1
            assert characters(actual) == characters(expected)
