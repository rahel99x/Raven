"""Large unfiltered tables avoid preparing fields while filters remain exact."""
from datetime import datetime, timedelta
import random
from types import SimpleNamespace

import pytest

from raven import clock, table_tools, table_ui
from raven.config import Config
from raven.controller import App
from raven.model import Finished, Job, Live, Store, stamp


TABLES = ("jobs", "recent", "history", "group", "nodes", "cluster", "sources")


def filter_app():
    app = SimpleNamespace()
    table_ui.initialize(app)
    table_tools.initialize(app)
    return app


@pytest.fixture
def records():
    rows = [Job("7", "Train Alpha", "GPU", "RUNNING", cpus=8, user="Alex"),
            Job("8", "Inference", "GPU", "PENDING", cpus=2, user="Sam"),
            Finished("9", "Failed Alpha", "FAILED", partition="CPU", cpus=4),
            Job("10", "Gone", "GPU", "COMPLETING", cpus=1),
            {"id": "11", "name": "Metric Beta", "state": "RUNNING", "partition": "CPU", "user": "", "cpus": 16}]
    snap = {"departed_jobs": {"10": rows[3]},
            "tags": {"7": {"tags": ["Blue", "Long Tag"], "pinned": True},
                     "8": {"tags": ["red"], "pinned": False}, "9": {"tags": ["BLUE"]},
                     "11": {"tags": ["deepblue", "white"]}}}
    return rows, snap


@pytest.mark.parametrize("tab", TABLES)
def test_unfiltered_rows_need_no_record_or_snapshot_fields_or_fingerprint(tab, monkeypatch):
    app = filter_app()

    class UntouchedRecord:
        def __getattribute__(self, name):
            pytest.fail("Unfiltered record field was read: " + name)

    class UntouchedSnapshot(dict):
        def get(self, *args):
            pytest.fail("Unfiltered pass read a snapshot field")

    monkeypatch.setattr(table_ui, "facets_fingerprint", lambda *args: pytest.fail("Per-row fingerprint was prepared"))
    monkeypatch.setattr(table_tools, "numeric_matches", lambda *args: pytest.fail("Unused numeric evaluator was called"))
    row, snap = UntouchedRecord(), UntouchedSnapshot()
    for _ in range(10000):
        assert table_ui.matches(app, tab, row, snap)


@pytest.mark.parametrize("facets,expected", [
    ({}, ["7", "8", "9", "10", "11"]),
    ({"state": "running"}, ["7", "11"]),
    ({"state": "RUNNING,FAILED"}, ["7", "9", "11"]),
    ({"state": "ACCOUNTING"}, ["10"]),
    ({"state": "COMPLETING"}, []),
    ({"partition": "gpu"}, ["7", "8", "10"]),
    ({"id": "7,11"}, ["7", "11"]),
    ({"id": "1"}, []),
    ({"user": "?"}, ["9", "10", "11"]),
    ({"user": "alex"}, ["7"]),
    ({"name": "ALPHA"}, ["7", "9"]),
    ({"name": "train,missing"}, ["7"]),
    ({"name": " beta "}, []),
    ({"tag": "blue"}, ["7", "9"]),
    ({"tag": "BLUE,red"}, ["7", "8", "9"]),
    ({"tag": "Long Tag"}, []),
    ({"tag": "long"}, ["7"]),
    ({"state": "RUNNING", "partition": "gpu"}, ["7"]),
    ({"name": "alpha", "tag": "blue"}, ["7", "9"]),
    ({"state": "RUNNING, FAILED"}, ["7", "11"]),
    ({"state": ""}, []),
])
def test_facets_preserve_exact_fields_casefold_choices_and_tag_tokens(records, facets, expected):
    app = filter_app()
    rows, snap = records
    app.table_state["facets"]["jobs"] = facets
    identifiers = [row.get("id") if isinstance(row, dict) else row.id
                   for row in rows if table_ui.matches(app, "jobs", row, snap)]
    assert identifiers == expected


@pytest.mark.parametrize("facets", [None, [], ["state"], "state=RUNNING", 5])
def test_invalid_restored_facet_container_remains_ignored(records, facets):
    app = filter_app()
    rows, snap = records
    app.table_state["facets"]["jobs"] = facets
    assert all(table_ui.matches(app, "jobs", row, snap) for row in rows)


@pytest.mark.parametrize("numeric", [None, {}, "cpus>=8", 5, [], ()])
def test_inactive_or_invalid_numeric_container_remains_ignored(records, numeric):
    app = filter_app()
    rows, snap = records
    app.table_tools_state["numeric"]["jobs"] = numeric
    assert all(table_ui.matches(app, "jobs", row, snap) for row in rows)


def test_active_facets_only_read_requested_fields(monkeypatch):
    app = filter_app()
    app.table_state["facets"]["jobs"] = {"partition": "gpu"}

    class Record:
        @property
        def partition(self):
            return "GPU"

        def __getattr__(self, key):
            pytest.fail("Unrelated field prepared for partition filter: " + key)

    class Snapshot(dict):
        def get(self, *args):
            pytest.fail("Partition filter inspected unrelated metadata")

    assert table_ui.matches(app, "jobs", Record(), Snapshot())


def test_filter_changes_in_place_take_effect_on_the_next_row(records):
    app = filter_app()
    rows, snap = records
    app.table_state["facets"]["jobs"] = {}
    facets = app.table_state["facets"]["jobs"]
    assert table_ui.matches(app, "jobs", rows[0], snap)
    facets["state"] = "PENDING"
    assert not table_ui.matches(app, "jobs", rows[0], snap)
    assert table_ui.matches(app, "jobs", rows[1], snap)
    facets["state"] = "RUNNING"
    assert table_ui.matches(app, "jobs", rows[0], snap)
    facets.clear()
    rules = app.table_tools_state["numeric"].setdefault("jobs", [])
    rules.append(table_tools.parse_rule("cpus>=16"))
    assert not table_ui.matches(app, "jobs", rows[0], snap)
    assert table_ui.matches(app, "jobs", rows[4], snap)
    rules[0] = table_tools.parse_rule("cpus>=8")
    assert table_ui.matches(app, "jobs", rows[0], snap)
    rules.clear()
    assert table_ui.matches(app, "jobs", rows[1], snap)


@pytest.mark.parametrize("text", ["cpus=8", "cpus!=2", "cpus>2", "cpus>=8", "cpus<16", "cpus<=8",
    "gpus=2", "memory=32GiB", "rss=4GiB", "cpu_eff=30%", "mem_eff=12.5%", "elapsed=2h", "priority=7", "nodes=1"])
def test_numeric_filters_still_evaluate_typed_live_values(text):
    app = filter_app()
    record = Job("7", "worker", "gpu", "RUNNING", cpus=8, gpus=2, mem_req="32G", elapsed="02:00:00", priority=7)
    snap = {"live": {"7": Live(rss=4 * 1024 ** 3, avg=.3)}}
    app.table_tools_state["numeric"]["jobs"] = [table_tools.parse_rule(text)]
    assert table_ui.matches(app, "jobs", record, snap)
    assert table_ui.matches(app, "jobs", record, snap) == table_tools.numeric_matches(app, "jobs", record, snap)


@pytest.mark.parametrize("text", ["cpus>=8", "rss=0B", "cpu_eff=0%", "mem_eff<50%"])
def test_numeric_filters_keep_unknown_distinct_from_zero_and_compose_with_facets(text):
    app = filter_app()
    app.table_state["facets"]["jobs"] = {"partition": "GPU"}
    app.table_tools_state["numeric"]["jobs"] = [table_tools.parse_rule(text)]
    job = Job("7", "worker", "gpu", "RUNNING", cpus=8, mem_req="32G")
    snap = {"live": {"7": Live(rss=0, avg=0)}}
    assert table_ui.matches(app, "jobs", job, snap)
    assert not table_ui.matches(app, "jobs", Job("8", "wrong", "cpu", "RUNNING", cpus=32), snap)
    if not text.startswith("cpus"):
        assert not table_ui.matches(app, "jobs", job, {})


def test_history_dates_recents_window_text_and_pinned_sort_remain_separate(monkeypatch):
    store = Store(persist=False)
    store.jobs = [Job("7", "active", "gpu", "RUNNING")]
    store.finished = [Finished("9", "old", "FAILED", end="2026-10-01T12:00:00", partition="gpu"),
                      Finished("10", "new", "COMPLETED", end="2026-10-08T11:00:00", partition="cpu")]
    store.tags = {"9": {"tags": ["red"], "pinned": True}, "10": {"tags": ["blue"]}}
    app = App(store, None, None, Config({"animations": False}), "reader", interactive=False)
    now = datetime(2026, 10, 8, 12).timestamp()
    monkeypatch.setattr(clock, "now", lambda: now)
    app.table_tools_state["dates"] = {"start": datetime(2026, 10, 8).timestamp(),
                                     "end": datetime(2026, 10, 9).timestamp(), "label": "today"}
    assert [record.id for record in app.history_jobs(store.snapshot())] == ["10"]
    app.table_tools_state["dates"] = None
    app.table_state["filters"]["history"] = "#red"
    assert [record.id for record in app.history_jobs(store.snapshot())] == ["9"]
    app.table_state["filters"]["history"] = "new"
    assert [record.id for record in app.history_jobs(store.snapshot())] == ["10"]
    app.table_tools_state["recents"]["window"] = 86400
    assert [record.id for record in app.recent_jobs(store.snapshot())] == ["10"]
    app.table_tools_state["recents"]["window"] = None
    app.table_state["facets"]["recent"] = {"tag": "red"}
    assert [record.id for record in app.recent_jobs(store.snapshot())] == ["9"]
    assert store.tags["9"]["pinned"] is True


def full_recent_reference(app, snap):
    """The released full-window selection, before an optimization can stop."""
    accepted = [record for record in snap["finished"]
                if table_ui.matches(app, "recent", record, snap) and table_tools.recent_matches(app, record)]
    active = {record.id for record in snap["jobs"]}
    accepted = [record for record in accepted if record.id not in active]
    text = table_tools.filter_text(app, "recent").lower()
    if text.startswith("#"):
        accepted = [record for record in accepted
                    if text[1:] in [tag.lower() for tag in snap.get("tags", {}).get(record.id, {}).get("tags", [])]]
    elif text:
        accepted = [record for record in accepted
                    if any(text in field.lower() for field in (record.name, record.id, record.state, record.partition))]
    return accepted[:table_tools.recent_limit(app)]


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("limit", [5, 10, 25])
def test_recent_early_stop_preserves_full_window_selection_across_filters(seed, limit, monkeypatch):
    generator = random.Random(seed)
    base = datetime(2026, 10, 8, 12)
    store = Store(persist=False)
    states = ["COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY"]
    names = ["Train Alpha", "Inference", "Straße", "Δοκιμή", "Missing output"]
    store.finished = [Finished(str(1000 + index), generator.choice(names), generator.choice(states),
        partition=generator.choice(["GPU", "CPU"]), cpus=generator.choice([2, 4, 8, 16]),
        elapsed=generator.choice(["00:15:00", "02:00:00", "1-00:00:00"]), cpu_time=generator.choice([0, 600, None]),
        end=(base - timedelta(hours=generator.randrange(72))).isoformat(), rss=generator.choice([0, 1024 ** 3]), req_mem=8 * 1024 ** 3)
        for index in range(200)]
    generator.shuffle(store.finished)
    store.jobs = [Job(record.id, "current attempt", record.partition, "RUNNING")
                  for record in generator.sample(store.finished, 15)]
    store.tags = {record.id: {"tags": [generator.choice(["Red", "BLUE", "Straße"])], "pinned": generator.choice([True, False])}
                  for record in store.finished}
    app = App(store, None, None, Config({"animations": False}), "reader", interactive=False)
    monkeypatch.setattr(clock, "now", lambda: base.timestamp())
    app.table_tools_state["recents"].update(count=limit, window=[None, 3600, 86400, 3 * 86400][seed % 4])
    app.table_state["facets"]["recent"] = [{}, {"state": "FAILED,TIMEOUT"}, {"partition": "gpu"},
        {"name": "train,inference"}, {"tag": "red,blue"}, {"partition": "CPU", "state": "COMPLETED"}][seed % 6]
    app.table_tools_state["numeric"]["recent"] = [[], [table_tools.parse_rule("cpus>=8")],
        [table_tools.parse_rule("elapsed>=1h")], [table_tools.parse_rule("rss=0B")]][seed % 4]
    app.table_state["filters"]["recent"] = ["", "alpha", "#Red", "#BLUE", "failed", "STRAßE"][seed % 6]
    app.table_state["sorts"]["history"] = [("id", "desc"), ("name", "asc")]
    app.table_state["sorts"]["recent"] = [("name", "desc"), ("id", "asc")]
    # History settings and cascades must not alter the accounting-order subset.
    app.table_state["facets"]["history"] = {"state": "COMPLETED"}
    app.table_tools_state["dates"] = {"start": base.timestamp() - 3600, "end": base.timestamp(), "label": "history only"}
    snap = store.snapshot()
    expected = full_recent_reference(app, snap)
    actual = app.finished_jobs(snap, recent=True)
    assert actual == expected
    assert all(record.id not in {job.id for job in store.jobs} for record in actual)
    assert len(actual) <= limit
    assert actual == [record for record in store.finished if record in expected]


@pytest.mark.parametrize("active_count", [0, 2])
def test_default_recents_visit_only_enough_accounting_records(active_count, monkeypatch):
    store = Store(persist=False)
    store.finished = [Finished(str(index), "completed", "COMPLETED") for index in range(10000)]
    store.jobs = [Job(str(index), "new attempt", "cpu", "RUNNING") for index in range(active_count)]
    app = App(store, None, None, Config({"animations": False}), "reader", interactive=False)
    old_matches, old_window = table_ui.matches, table_tools.recent_matches
    facet_visits, window_visits = [], []

    def match(application, table, record, snap):
        facet_visits.append(record.id)
        return old_matches(application, table, record, snap)

    def window(application, record):
        window_visits.append(record.id)
        return old_window(application, record)

    monkeypatch.setattr(table_ui, "matches", match)
    monkeypatch.setattr(table_tools, "recent_matches", window)
    actual = app.finished_jobs(store.snapshot(), recent=True)
    assert [record.id for record in actual] == [str(index) for index in range(active_count, active_count + 5)]
    assert facet_visits == window_visits == [str(index) for index in range(active_count + 5)]


def test_sparse_recent_filter_stops_after_fifth_successful_match(monkeypatch):
    store = Store(persist=False)
    store.finished = [Finished(str(index), "keep" if index % 10 == 0 else "skip", "COMPLETED") for index in range(10000)]
    app = App(store, None, None, Config({"animations": False}), "reader", interactive=False)
    app.table_state["filters"]["recent"] = "keep"
    visited = []
    original = table_ui.matches

    def match(application, table, record, snap):
        visited.append(record.id)
        return original(application, table, record, snap)

    monkeypatch.setattr(table_ui, "matches", match)
    assert [record.id for record in app.finished_jobs(store.snapshot(), recent=True)] == ["0", "10", "20", "30", "40"]
    assert visited == [str(index) for index in range(41)]


@pytest.mark.parametrize("limit", [0, -1, -3, True, False, 5.0, "5", None])
def test_unusual_direct_recent_limits_keep_legacy_slice_behavior(limit):
    store = Store(persist=False)
    store.finished = [Finished(str(index), "completed", "COMPLETED") for index in range(8)]
    app = App(store, None, None, Config({"animations": False}), "reader", interactive=False)
    app.table_tools_state["recents"]["count"] = limit
    snap = store.snapshot()
    if isinstance(limit, (str, float)):
        with pytest.raises(TypeError):
            full_recent_reference(app, snap)
        with pytest.raises(TypeError):
            app.finished_jobs(snap, recent=True)
    else:
        assert app.finished_jobs(snap, recent=True) == full_recent_reference(app, snap)


def test_recent_text_keeps_lowercase_matching_without_casefold_expansion():
    store = Store(persist=False)
    store.finished = [Finished("7", "Straße", "COMPLETED"), Finished("8", "STRASSE", "COMPLETED")]
    store.tags = {"7": {"tags": ["Straße"]}, "8": {"tags": ["STRASSE"]}}
    app = App(store, None, None, Config({"animations": False}), "reader", interactive=False)
    for text in ("STRASSE", "#STRASSE"):
        app.table_state["filters"]["recent"] = text
        assert [record.id for record in app.finished_jobs(store.snapshot(), recent=True)] == ["8"]


def test_completed_recent_limit_precedes_pending_dedup_and_sort():
    store = Store(persist=False)
    store.finished = [Finished(str(index), "completed", "COMPLETED", end=f"2026-10-08T00:00:{index:02d}") for index in range(10)]
    store.departed_jobs = {"0": Job("0", "awaiting accounting", "cpu", "RUNNING"),
                           "pending": Job("pending", "awaiting accounting", "cpu", "RUNNING")}
    app = App(store, None, None, Config({"animations": False}), "reader", interactive=False)
    app.table_state["sorts"]["recent"] = [("id", "desc")]
    snap = store.snapshot()
    assert [record.id for record in app.finished_jobs(snap, recent=True)] == ["0", "1", "2", "3", "4"]
    # Pending entries retain priority, their completed duplicate is removed,
    # and sorting applies to the resulting five-record subset afterward.
    assert {record.id for record in app.recent_jobs(snap)} == {"pending", "0", "1", "2", "3"}
    assert "4" not in [record.id for record in app.recent_jobs(snap)]
