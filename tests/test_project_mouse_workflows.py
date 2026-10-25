"""Project forms expose the same bounded actions to mouse and keyboard users."""

import curses
import json

import pytest

from raven import interaction, layout as L, project_ui as P, screen
from raven.config import Config
from raven.controller import App
from raven.model import Store
from raven.remote import LocalFiles
from raven.views import Views


class DeferredReader:
    def __init__(self):
        self.files = LocalFiles()
        self.settings = dict(metrics_file="", contract="", workdir="", passport="")
        self.passport = self.passport_diff = self.pending = None

    def configure(self, **values):
        self.settings.update(values)

    def start_task(self, worker, complete):
        if self.pending:
            return False
        self.pending = worker, complete
        return True

    def finish(self):
        worker, complete = self.pending
        self.pending = None
        try:
            result = worker()
        except Exception as exc:
            result = exc
        complete(result)


@pytest.fixture
def dashboard(tmp_path):
    cfg = Config({"animations": False, "startup_animation": False, "log_lines": 0})
    app = App(Store(persist=False), None, None, cfg, "reader", interactive=False)
    app.research = DeferredReader()
    app.files = app.research.files
    app.width, app.height = 100, 34
    views = Views(L.Glyphs(True), cfg)
    app.views_ref = views
    root = tmp_path / "project"
    run = root / "runs" / "sample-a1"
    run.mkdir(parents=True)
    (run / "run.json").write_text(
        json.dumps(
            {
                "schema": "raven.run/v1",
                "run_id": "sample-a1",
                "experiment_id": "sample",
                "attempt": 1,
                "state": "COMPLETED",
            }
        )
    )
    app.run_command("project " + str(root))
    app.research.finish()
    yield app, views, root, run


def paint(app, views):
    # The document is published first, then each modal's exact absolute cells.
    rows, hits = views.compose(app.store.snapshot(), app, app.width, app.height)
    overlays = views.overlay(app.store.snapshot(), app, app.width, app.height)
    interaction.publish(app, rows, hits, app.width, app.height, overlays=overlays)
    return overlays


def control(app, identity):
    return next(item for item in interaction.controls(app) if item.id == identity)


def click(app, item, *, release=True):
    y, x = item.rect.top, item.rect.left
    screen._apply_input(
        app, ("mouse", (0, x, y, 0, curses.BUTTON1_PRESSED)), app.last_hits, curses
    )
    if release:
        screen._apply_input(
            app, ("mouse", (0, x, y, 0, curses.BUTTON1_RELEASED)), app.last_hits, curses
        )


def test_runs_filter_apply_refresh_and_back_complete_via_native_clicks(dashboard):
    app, views, _, _ = dashboard
    paint(app, views)
    click(app, control(app, "project-action:filter"))
    assert app.project_state["filtering"]
    app.handle("s")
    app.handle("a")
    paint(app, views)
    click(app, control(app, "project-action:apply-filter"))
    assert app.project_state["filter"] == "sa" and not app.project_state["filtering"]
    paint(app, views)
    click(app, control(app, "project-action:refresh"))
    assert app.project_state["busy"]
    app.research.finish()
    paint(app, views)
    click(app, control(app, "project-action:back"))
    assert app.mode == "main"


def test_notices_early_return_has_working_back_control(dashboard):
    app, views, _, _ = dashboard
    app.project_state["warnings"] = ["Source one was unavailable", "Retry later"]
    paint(app, views)
    click(app, control(app, "project-action:notices"))
    assert app.project_state["notices_open"]
    overlays = paint(app, views)
    assert any(
        "Source one was unavailable" in L.row_text(row) for _, _, row in overlays
    )
    click(app, control(app, "project-action:back"))
    assert app.mode == "project_runs" and not app.project_state["notices_open"]


def test_back_during_pending_project_read_prevents_late_reopen(dashboard):
    app, views, _, _ = dashboard
    paint(app, views)
    click(app, control(app, "project-action:refresh"))
    paint(app, views)
    click(app, control(app, "project-action:back"))
    assert app.mode == "main" and not app.project_state["busy"]
    app.research.finish()
    assert app.mode == "main" and not app.project_state["busy"]


def test_back_during_artifact_preview_read_rejects_late_modal_reopen(dashboard):
    app, views, root, run = dashboard
    outputs(app, root, run, "report.txt", "retained output\n", "text")
    paint(app, views)
    click(app, control(app, "project-action:open"))
    assert app.research.pending and app.project_state["busy"]
    paint(app, views)
    click(app, control(app, "project-action:back"))
    assert app.mode == "main"
    app.research.finish()
    assert app.mode == "main" and app.project_state["preview"] is None


def outputs(app, root, run, name, data, format_):
    directory = root / ".raven" / "contracts"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "outputs.v1.json").write_text(
        json.dumps({"version": 1, "outputs": [{"path": name, "format": format_}]})
    )
    (run / name).write_text(data)
    app.run_command("run select sample-a1")
    app.research.finish()
    # Outputs are an owned modal over Jobs as well as Research; the fake
    # reader deliberately implements only the project-file boundary.
    app.enter_tab("jobs")
    app.run_command("outputs")
    app.research.finish()


def test_outputs_open_preview_page_refresh_and_back_controls_use_real_artifacts(
    dashboard,
):
    app, views, root, run = dashboard
    outputs(app, root, run, "report.txt", "first line\nsecond line\n", "text")
    paint(app, views)
    click(app, control(app, "project-action:open"))
    app.research.finish()
    assert app.mode == "project_preview"
    paint(app, views)
    assert control(app, "project-action:previous")
    click(app, control(app, "project-action:next"))
    assert app.mode == "project_preview" and not app.research.pending
    paint(app, views)
    click(app, control(app, "project-action:refresh"))
    assert app.research.pending
    app.research.finish()
    paint(app, views)
    click(app, control(app, "project-action:back"))
    assert app.mode == "project_outputs"


def test_csv_column_sort_and_hide_have_mouse_buttons(dashboard):
    app, views, root, run = dashboard
    outputs(app, root, run, "scores.csv", "epoch,loss\n2,0.2\n1,0.8\n", "csv")
    paint(app, views)
    click(app, control(app, "project-action:open"))
    app.research.finish()
    paint(app, views)
    assert control(app, "project-action:next-column")
    click(app, control(app, "project-action:sort-column"))
    app.research.finish()
    assert app.project_state["preview"]["sort"] == (0, "asc")
    paint(app, views)
    click(app, control(app, "project-action:toggle-column"))
    app.research.finish()
    assert app.project_state["preview"]["columns"] == [1]
    paint(app, views)
    assert control(
        app, "project-action:back"
    )  # Column controls preserve navigation hits.


def test_json_node_click_uses_exact_published_node_and_rejects_changed_tree(dashboard):
    app, views, root, run = dashboard
    outputs(
        app, root, run, "results.json", '{"training": {"loss": 0.2}, "seed": 7}', "json"
    )
    paint(app, views)
    click(app, control(app, "project-action:open"))
    app.research.finish()
    paint(app, views)
    node = control(app, "project-json:0")
    click(app, node, release=False)
    P.handle_key(app, "enter")
    collapsed = list(app.project_state["preview_collapsed"])
    assert collapsed
    paint(app, views)
    screen._apply_input(
        app,
        ("mouse", (0, node.rect.left, node.rect.top, 0, curses.BUTTON1_RELEASED)),
        app.last_hits,
        curses,
    )
    assert app.project_state["preview_collapsed"] == collapsed


def test_stale_notice_and_filter_controls_cannot_act_after_state_change(dashboard):
    app, views, _, _ = dashboard
    paint(app, views)
    stale = control(app, "project-action:filter")
    app.project_state["notices_open"] = True
    P.handle_mouse(app, stale.rect.top, stale.rect.left)
    assert not app.project_state["filtering"]
    paint(app, views)
    back = control(app, "project-action:back")
    click(app, back, release=False)
    app.project_state["notices_open"] = False
    paint(app, views)
    screen._apply_input(
        app,
        ("mouse", (0, back.rect.left, back.rect.top, 0, curses.BUTTON1_RELEASED)),
        app.last_hits,
        curses,
    )
    assert app.mode == "project_runs"


def test_f8_activation_uses_owner_stale_context_guard(dashboard):
    app, views, _, _ = dashboard
    paint(app, views)
    item = control(app, "project-action:filter")
    app.interaction_state.update(active=True, focused=item.id)
    app.project_state["notices_open"] = True
    app.handle("f8")
    app.interaction_state.update(active=True, focused=item.id)
    app.handle("enter")
    assert app.project_state["notices_open"] and not app.project_state["filtering"]


@pytest.mark.parametrize("width,height", [(24, 8), (40, 12), (80, 24), (120, 40)])
@pytest.mark.parametrize("mode", ["project_runs", "project_outputs", "project_preview"])
def test_project_mouse_geometry_stays_on_visible_ascii_cells(
    dashboard, width, height, mode
):
    app, views, _, _ = dashboard
    app.mode = mode
    app.width, app.height = width, height
    rendered = P.overlay(views, {}, app, width, height)
    assert all(
        0 <= y < height and 0 <= x < width and L.vlen(L.row_text(row)) <= width - x
        for y, x, row in rendered
    )
    assert all(L.row_text(row).isascii() for _, _, row in rendered)
    assert all(
        0 <= y < height and 0 <= value["left"] < value["right"] <= width
        for y, _, value in app.project_state["control_hits"]
    )
