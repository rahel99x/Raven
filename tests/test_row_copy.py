"""Native resource cursors copy exact published rows, never the dashboard."""
from types import SimpleNamespace

import pytest

from raven import clipboard, interaction, row_copy, text_selection


@pytest.fixture
def app(monkeypatch):
    obj = SimpleNamespace(mode="main", tab="sources", cursor={"sources": 1},
        width=80, height=12, body_origin=2, cfg={"clipboard": {}},
        source_ids=["jobs", "live", "gpu"], table_tools_state={},
        toolbar_state={}, job_panel_state={}, history_browser_state={},
        project_state={}, failures=[], messages=[], copies=[], last_hits=[],
        selected_id=None, state_dir=None)
    obj.fail = obj.failures.append
    obj.say = obj.messages.append
    from raven.workbench import bind_actions
    bind_actions(obj)
    monkeypatch.setattr(clipboard, "copy", lambda value, *a, **k: obj.copies.append(value) or "copied")
    return obj


def publish(app, lines, hits, *, overlays=()):
    rows = [[("TOOLBAR MUST NOT COPY", "")], [("HEADER MUST NOT COPY", "")]]
    rows.extend([(line, "")] for line in lines)
    rows.extend([[("", "")]] * max(0, app.height - len(rows)))
    app.last_hits = hits
    text_selection.publish(app, rows, app.width, app.height, overlays=overlays)
    interaction.publish(app, rows, hits, app.width, app.height, overlays=overlays)


def test_selected_source_copies_one_row_and_no_other_source_or_chrome(app):
    publish(app, ["jobs healthy", "live error sstat unavailable", "gpu healthy"],
            [(2, "source", "jobs"), (3, "source", "live"), (4, "source", "gpu")])
    assert row_copy.copy_selection(app)
    assert app.copies == ["live error sstat unavailable\n"]


@pytest.mark.parametrize("tab,kind,name", [("cluster", "partition_row", "cpu"),
                                           ("nodes", "node_row", "n001")])
def test_resource_cursors_copy_selected_identity_only(app, tab, kind, name):
    app.tab = tab
    app.cursor = {tab: 1}
    app.table_tools_state = {"resource_ids": {tab: ["other", name]}}
    publish(app, ["other resource", name + " 32 CPU 64 GB"],
            [(2, kind, "other"), (3, kind, name)])
    assert row_copy.copy_selection(app)
    assert app.copies == [name + " 32 CPU 64 GB\n"]


def test_node_cell_excludes_other_cells_and_preserves_unicode(app):
    app.tab, app.cursor = "nodes", {"nodes": 1}
    app.table_tools_state = {"resource_ids": {"nodes": ["n0", "n1", "n2"]}}
    publish(app, ["n0  n1界é n2"], [(2, "node_cell", ("n0", 0, 4)),
                                   (2, "node_cell", ("n1", 4, 10)),
                                   (2, "node_cell", ("n2", 10, 12))])
    assert row_copy.copy_selection(app)
    assert app.copies == ["n1界é\n"]


def test_repeated_node_identity_prefers_table_instead_of_duplicate_matrix(app):
    app.tab, app.cursor = "nodes", {"nodes": 0}
    app.table_tools_state = {"resource_ids": {"nodes": ["n0"]}}
    publish(app, ["n0 matrix", "table header", "n0 detailed row"],
            [(2, "node_row", "n0"), (4, "node_row", "n0")])
    assert row_copy.copy_selection(app)
    assert app.copies == ["n0 detailed row\n"]


def test_log_catalog_copies_selected_label_and_path_only(app):
    app.tab, app.cursor = "log", {}
    app.logs = SimpleNamespace(browser=True, browser_cursor=1)
    app.log_entries = lambda: [{"id": "a"}, {"id": "b"}]
    app.history_browser_content_rect = SimpleNamespace(x=0, y=2, width=24, height=8)
    publish(app, ["first label             PRIVATE HISTORY", "/first.log              PRIVATE HISTORY",
                  "second label            PRIVATE HISTORY", "/second.log             PRIVATE HISTORY"],
            [(2, "log_file", "a"), (3, "log_file", "a"),
             (4, "log_file", "b"), (5, "log_file", "b")])
    assert row_copy.copy_selection(app)
    assert app.copies == ["second label\n/second.log\n"]


def test_array_cursor_uses_exact_cluster_and_excludes_job_sidebar(app):
    app.tab, app.research_view, app.cursor = "research", "arrays", {"research": 1}
    app.research_groups = [{"id": "41", "cluster": "one"}, {"id": "41", "cluster": "two"}]
    app.history_browser_content_rect = SimpleNamespace(x=0, y=2, width=20, height=8)
    publish(app, ["41 cluster one      PRIVATE HISTORY", "41 cluster two      PRIVATE HISTORY"],
            [(2, "research_array", ("41", "one")), (3, "research_array", ("41", "two"))])
    assert row_copy.copy_selection(app)
    assert app.copies == ["41 cluster two\n"]


def test_evidence_cursor_does_not_copy_adjacent_evidence(app):
    app.tab, app.research_view = "research", "evidence"
    app.analysis_state = {"evidence_ids": ["e1", "e2"], "evidence_cursor": 1}
    publish(app, ["[e1] first citation", "[e2] selected citation"],
            [(2, "research_evidence", "e1"), (3, "research_evidence", "e2")])
    assert row_copy.copy_selection(app)
    assert app.copies == ["[e2] selected citation\n"]


def test_explicit_legacy_range_remains_owned_by_its_range_handler(app):
    app.sel_anchor = app.sel_end = 3
    publish(app, ["jobs", "live", "gpu"], [(2, "source", "jobs"), (3, "source", "live")])
    assert not row_copy.copy_selection(app)
    assert not app.copies


def test_new_array_cursor_restores_copy_after_universal_clear(app):
    app.tab, app.research_view, app.cursor = "research", "arrays", {"research": 0}
    app.research_groups = [{"id": "41", "cluster": "one"}]
    app.job_selection_state = {"lines_deselected": True, "deselected": {"research": False}}
    publish(app, ["41 array row"], [(2, "research_array", "41")])
    assert row_copy.copy_selection(app)
    assert app.copies == ["41 array row\n"]


@pytest.mark.parametrize("change", ["cursor", "resize", "scroll", "mode", "tab", "clear", "sidebar", "details"])
def test_changed_or_cleared_selection_never_copies_a_different_pane(app, change):
    publish(app, ["jobs", "live", "gpu"], [(2, "source", "jobs"), (3, "source", "live"), (4, "source", "gpu")])
    if change == "cursor":
        app.cursor["sources"] = 2
    elif change == "resize":
        app.width += 1
    elif change == "scroll":
        app.top = {"sources": 1}
    elif change == "mode":
        app.mode = "operations"
    elif change == "tab":
        app.tab = "nodes"
    elif change == "clear":
        app.job_selection_state = {"lines_deselected": True}
    elif change == "sidebar":
        app.history_browser_state["focused"] = True
    elif change == "details":
        app.layout_state = SimpleNamespace(focus="details")
    row_copy.copy_selection(app)
    assert app.copies == []


def test_offscreen_selected_identity_is_guarded_without_copying_whole_page(app):
    publish(app, ["jobs only"], [(2, "source", "jobs")])
    assert row_copy.copy_selection(app)
    assert app.copies == []
    assert "outside" in app.failures[-1]


def test_selection_under_overlay_cannot_copy_hidden_content(app):
    publish(app, ["jobs", "live PRIVATE", "gpu"],
            [(2, "source", "jobs"), (3, "source", "live"), (4, "source", "gpu")],
            overlays=[(3, 4, [("OVERLAY", "")])])
    assert row_copy.copy_selection(app)
    assert app.copies == []
    assert app.failures


def test_native_source_copy_through_full_frame(app, monkeypatch):
    from raven.config import Config
    from raven.controller import App
    from raven.layout import Glyphs
    from raven.model import Health, Store
    from raven.screen import _FrameCache
    from raven.views import Views

    cfg = Config({"animations": False, "startup_animation": False,
                  "smooth_scrolling": False, "workspace": {"density": "compact"}})
    store = Store(persist=False)
    store.health = {name: Health(name, last_ok=1) for name in ("gpu", "jobs", "live")}
    live = App(store, None, None, cfg, "tester", interactive=False)
    live.views_ref = Views(Glyphs(False), cfg)
    live.enter_tab("sources")
    cache = _FrameCache()
    cache.rebuild(live, live.views_ref, store, None, 140, 35)
    y = next(y for y, kind, key in cache.hits if kind == "source" and key == "live")
    live.click(y, 8, cache.hits)
    cache.rebuild(live, live.views_ref, store, None, 140, 35)
    try:
        live.handle("y")
        assert len(app.copies) == 1
        assert app.copies[0].strip().startswith("live")
        assert len(app.copies[0].splitlines()) == 1
        assert "Metric sampling" not in app.copies[0]
    finally:
        if live.research:
            live.research.close()
