"""The embedded context and its publication transform have one owner."""
from types import SimpleNamespace

import pytest

from raven import chart_interaction as C, scrollbars as S
from raven.pane_context import PaneContext, job_identity
from raven.pane_document import DocumentTransform, PaneDocument, source_window


def test_pane_context_rebinds_controller_methods_and_properties_without_copying_owner():
    class Owner:
        def __init__(self):
            self.cursor = {"jobs": 7}
            self.tab = "jobs"
            self.store = object()

        def __copy__(self):
            raise AssertionError("whole application copy")

        @property
        def selected(self):
            return self.cursor[self.tab]

        def advance(self):
            self.cursor[self.tab] += 1
            self.mode = "local"

    owner = Owner()
    pane = PaneContext(owner, identity="jobs:details:research:arrays",
                       tab="research", cursor={"research": 2})
    pane.advance()
    assert pane.selected == 3
    assert pane.mode == "local"
    assert owner.selected == 7 and not hasattr(owner, "mode")
    assert pane.store is owner.store
    assert pane._chart_owner is pane._pointer_owner_app is owner
    owner.metric_playback_state = {"new": object()}
    assert not hasattr(pane, "metric_playback_state")


def test_one_publication_transform_clips_rows_hits_axes_and_scrollbars_together():
    app = SimpleNamespace()
    C.initialize(app)
    S.initialize(app)
    key = ("resource-series", "7", "cpu", "%", "attempt")
    C.record(app, key, {"plot_rect": (2, 1, 6, 9), "axis_rect": (2, 0, 7, 9),
                        "x_bounds": (0, 10), "y_bounds": (0, 100)})
    S.register(app, "document", (2, 0, 8, 10), 50, 6, 0, 0, lambda _: None,
               header=(1, 0, 10))
    rows = [[(str(index), "")] for index in range(10)]
    hits = [(0, "nav", "discard"), (3, "control", {"left": -2, "right": 20}),
            (8, "control", {"left": 0, "right": 10})]
    document = PaneDocument.capture(app, rows, hits, chart_mark=0, scroll_mark=0)
    assert not C.initialize(app)["pending"] and not S.initialize(app)["staged"]
    document = document.body(2, prefix=[[("heading", "")]], width=10, limit=5)
    output, mapped = document.publish(app)
    assert [row[0][0] for row in output] == ["heading", "2", "3", "4", "5"]
    assert mapped == [(2, "control", {"left": 0, "right": 10})]
    plot = C.initialize(app)["pending"][0]
    scrollbar = S.initialize(app)["staged"][0]
    assert (plot.visible.top, plot.visible.bottom) == (1, 5)
    assert (plot.rect.top, plot.rect.bottom) == (1, 5)
    assert plot.axes.bottom == 6  # Axes retain source relation beyond the clip.
    assert (scrollbar.rect.top, scrollbar.rect.bottom) == (1, 5)
    assert scrollbar.header is None
    assert plot.x_bounds == (0, 10) and plot.y_bounds == (0, 100)


def test_empty_document_drops_all_source_targets():
    transform = DocumentTransform(-2, (0, 0, 0, 10))
    assert transform.hits([(2, "control", {})]) == ()


@pytest.mark.parametrize("top", [0, 40])
def test_source_window_first_publication_and_resize_never_seed_lower_view_with_top_blanks(top):
    window = source_window(None, None, top=top, height=30, header=4,
                           overscan=3, seed_full=True)
    assert window == ((0, 36) if top == 0 else None)


def test_source_window_extends_past_an_empty_publication_when_data_arrives():
    previous = {"mapping": {0: 0, 1: 1, 2: 2}, "sticky": 1, "header": 1}
    window = source_window(previous, (0, 3), top=0, height=30, header=1,
                           overscan=3, seed_full=True)
    assert window[0] == 0 and window[1] >= 30


def test_source_window_missing_scrolled_map_requests_bounded_full_recomposition():
    previous = {"mapping": {0: 0, 1: 1}, "sticky": 1, "header": 1}
    assert source_window(previous, (0, 3), top=40, height=30, header=1,
                         overscan=3, seed_full=True) is None


def test_attempt_identity_changes_without_changing_job_id():
    record = SimpleNamespace(id="7", submit="one", start="two")
    generation = [0]
    app = SimpleNamespace(store=SimpleNamespace(job_attempt=lambda jid: generation[0]))
    before = job_identity(app, record)
    record.start = "three"
    assert job_identity(app, record) != before
    before = job_identity(app, record)
    generation[0] = 1
    assert job_identity(app, record) != before
    assert job_identity(app, None) is None
