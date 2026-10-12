"""Reversible column cascades use source identities and typed resource values."""
import copy
import json
from types import SimpleNamespace

import pytest

from raven import table_sort, table_ui
from raven.config import Config
from raven.controller import App
from raven.layout import Column
from raven.model import Finished, Job, Store


@pytest.fixture
def app():
    value = SimpleNamespace(tab="jobs", sort={"jobs": "state", "history": "end"}, reverse={},
                            messages=[], failures=[], filter="", days_options=[1, 7, 30])
    value.say = value.messages.append
    value.fail = value.failures.append
    value.analytics_days_value = lambda: 7
    table_ui.initialize(value)
    return value


def identities(rows):
    return [row["id"] for row in rows]


def test_mouse_cycle_keeps_precedence_and_readding_places_column_last(app):
    assert table_sort.chain(app, "jobs") is None
    assert table_sort.cycle_sort(app, "jobs", "name") == [("name", "asc")]
    assert table_sort.cycle_sort(app, "jobs", "cpus") == [("name", "asc"), ("cpus", "asc")]
    assert table_sort.cycle_sort(app, "jobs", "name") == [("name", "desc"), ("cpus", "asc")]
    assert table_sort.cycle_sort(app, "jobs", "name") == [("cpus", "asc")]
    assert table_sort.cycle_sort(app, "jobs", "name") == [("cpus", "asc"), ("name", "asc")]
    assert app.sort["jobs"] == "state"  # The inherited sort was not added as an implicit priority.


def test_mixed_direction_cascade_uses_later_columns_only_within_ties(app):
    rows = [{"id": "A-low", "name": "A", "cpus": 2},
            {"id": "B-high", "name": "B", "cpus": 32},
            {"id": "A-high", "name": "A", "cpus": 16},
            {"id": "B-low", "name": "B", "cpus": 4}]
    table_sort.set_sort(app, "jobs", "name", "asc")
    table_sort.set_sort(app, "jobs", "cpus", "desc")
    assert identities(table_sort.sort_rows(app, "jobs", rows)) == ["A-high", "A-low", "B-high", "B-low"]
    table_sort.set_sort(app, "jobs", "name", "off")
    assert identities(table_sort.sort_rows(app, "jobs", rows)) == ["B-high", "A-high", "B-low", "A-low"]
    table_sort.set_sort(app, "jobs", "cpus", "off")
    assert table_sort.chain(app, "jobs") == []
    assert table_sort.sort_rows(app, "jobs", rows) == rows
    assert identities(rows) == ["A-low", "B-high", "A-high", "B-low"]


@pytest.mark.parametrize("direction, expected", [("asc", ["zero", "small", "big", "none", "nan", "inf", "invalid"]),
                                                  ("desc", ["big", "small", "zero", "none", "nan", "inf", "invalid"])])
def test_zero_is_known_and_missing_numeric_values_stay_last_in_both_directions(app, direction, expected):
    rows = [{"id": label, "cpus": count} for label, count in
            [("none", None), ("big", 20), ("zero", 0), ("nan", float("nan")),
             ("small", 3), ("inf", float("inf")), ("invalid", "garbage")]]
    table_sort.set_sort(app, "jobs", "cpus", direction)
    assert identities(table_sort.sort_rows(app, "jobs", rows)) == expected


def test_equal_keys_do_not_add_undocumented_id_tie_breakers(app):
    rows = [{"id": "9", "name": "same", "cpus": 4},
            {"id": "2", "name": "SAME", "cpus": 4},
            {"id": "4", "name": "same", "cpus": 4}]
    table_sort.set_sort(app, "jobs", "name", "asc")
    table_sort.set_sort(app, "jobs", "cpus", "desc")
    assert table_sort.sort_rows(app, "jobs", rows) == rows


@pytest.mark.parametrize("table,key,values,expected", [
    ("jobs", "id", ["10_10", "9", "10.batch", "10_2", "10", "2", "10_1"], ["2", "9", "10", "10.batch", "10_1", "10_2", "10_10"]),
    ("nodes", "name", ["node10", "Node2", "node1"], ["node1", "Node2", "node10"]),
    ("history", "exit", ["10:0", "2:9", "0:0", "2:1"], ["0:0", "2:1", "2:9", "10:0"]),
    ("history", "elapsed", ["01:00:00", "2-00:00:00", "00:09:00", "00:59:00"], ["00:09:00", "00:59:00", "01:00:00", "2-00:00:00"]),
    ("jobs", "left", ["1h02m", "waited 9m", "0s", "waited 12m"], ["0s", "waited 9m", "waited 12m", "1h02m"]),
    ("history", "rss", ["10 GB", "900 MB", "2 GB", "n/a"], ["900 MB", "2 GB", "10 GB", "n/a"]),
    ("history", "ce", ["100%", "9%", "12%", "n/a"], ["9%", "12%", "100%", "n/a"]),
    ("sources", "state", ["pending", "error", "ok", "off"], ["error", "off", "ok", "pending"]),
    ("cluster", "limit", ["UNLIMITED", "9:00:00", "1-00:00:00", "N/A"], ["9:00:00", "1-00:00:00", "UNLIMITED", "N/A"]),
])
def test_display_fallbacks_sort_by_semantics_not_lexical_numbers(app, table, key, values, expected):
    table_sort.set_sort(app, table, key, "asc")
    rows = [{"id": str(index), key: value} for index, value in enumerate(values)]
    assert [row[key] for row in table_sort.sort_rows(app, table, rows)] == expected


def test_typed_raw_values_take_priority_over_rounded_clipped_cells(app):
    rows = [{"id": "later", "start": "01-01 00:00", "ce": "10%", "_sort": {"start": 200, "ce": .101}},
            {"id": "earlier", "start": "01-01 00:00", "ce": "10%", "_sort": {"start": 100, "ce": .104}},
            {"id": "unknown", "start": "01-01 00:00", "ce": "0%", "_sort": {"start": None, "ce": None}}]
    table_sort.set_sort(app, "history", "start", "asc")
    assert identities(table_sort.sort_rows(app, "history", rows)) == ["earlier", "later", "unknown"]
    table_sort.set_sort(app, "history", "start", "off")
    table_sort.set_sort(app, "history", "ce", "asc")
    assert identities(table_sort.sort_rows(app, "history", rows)) == ["later", "earlier", "unknown"]


def test_partial_ratio_and_mixed_native_values_never_compare_incompatible_types(app):
    rows = [{"id": str(index), "_sort": {"cpus": value}}
            for index, value in enumerate([(2, 8), (2, None), None, (0, 8), "garbage", 1])]
    table_sort.set_sort(app, "nodes", "cpus", "asc")
    assert identities(table_sort.sort_rows(app, "nodes", rows)) == ["5", "3", "0", "1", "2", "4"]


def test_enormous_numeric_identifier_does_not_trigger_integer_conversion_limit(app):
    ids = ["1" * 5000, "9" * 4999, "2"]
    table_sort.set_sort(app, "jobs", "id", "asc")
    assert identities(table_sort.sort_rows(app, "jobs", [{"id": value} for value in ids])) == ["2", ids[1], ids[0]]


def test_tabs_and_recent_section_keep_independent_cascades(app):
    table_sort.set_sort(app, "jobs", "name", "desc")
    table_sort.set_sort(app, "recent", "end", "asc")
    assert table_sort.chain(app, "jobs") == [("name", "desc")]
    assert table_sort.chain(app, "recent") == [("end", "asc")]
    assert table_sort.chain(app, "history") is None
    table_sort.clear_sort(app, "jobs")
    assert table_sort.chain(app, "recent") == [("end", "asc")]
    assert table_sort.chain(app, "jobs") == []
    table_sort.reset_sort(app, "jobs")
    assert table_sort.chain(app, "jobs") is None


def test_column_indicators_show_priority_direction_and_survive_hidden_criteria(app):
    original = [Column("id", "ID", 2, 3), Column("cpus", "CPU", 3, 3), Column("name", "NAME", 4, 8)]
    app.table_state["hidden"]["jobs"] = ["cpus"]
    table_sort.set_sort(app, "jobs", "cpus", "desc")
    table_sort.set_sort(app, "jobs", "name", "asc")
    displayed = table_ui.columns(app, "jobs", original)
    assert [(column.key, column.title) for column in displayed] == [("id", "ID"), ("name", "NAME ^2")]
    assert table_sort.chain(app, "jobs") == [("cpus", "desc"), ("name", "asc")]
    app.table_state["hidden"]["jobs"] = []
    displayed = table_ui.columns(app, "jobs", original)
    cpu = next(column for column in displayed if column.key == "cpus")
    assert cpu.title == "CPU v1" and cpu.lo >= len(cpu.title) and cpu.hi >= cpu.lo
    table_sort.clear_sort(app, "jobs")
    assert [column.title for column in table_ui.columns(app, "jobs", original)] == ["ID", "CPU", "NAME"]


def test_save_restore_distinguishes_unsorted_from_inherited_and_isolated_mutable_state(app):
    table_sort.clear_sort(app, "jobs")
    table_sort.set_sort(app, "history", "end", "desc")
    saved = json.loads(json.dumps(table_ui.save(app)))
    table_ui.initialize(app)
    table_ui.restore(app, saved)
    assert table_sort.chain(app, "jobs") == []
    assert table_sort.chain(app, "history") == [("end", "desc")]
    assert table_sort.chain(app, "recent") is None
    saved["sorts"]["history"][0][1] = "asc"
    assert table_sort.chain(app, "history") == [("end", "desc")]


@pytest.mark.parametrize("bad", [None, "name", 1, [["id", "up"]], [["nope", "asc"]],
                                [["id", "asc"], ["id", "desc"]], [[True, "asc"]],
                                [["id", False]], [["id"]], [["id", "asc", 1]],
                                [["id", "asc"]] * 50, [{"id": "asc"}]])
def test_bad_restored_chain_is_discarded_as_a_whole(app, bad):
    table_ui.restore(app, {"sorts": {"jobs": bad, "history": [["end", "desc"]], "unexpected": []}})
    assert table_sort.chain(app, "jobs") is None
    assert table_sort.chain(app, "history") == [("end", "desc")]
    assert "unexpected" not in app.table_state["sorts"]


def test_old_ui_state_without_cascade_still_uses_inherited_preferences(app):
    table_ui.restore(app, {"hidden": {"jobs": ["gpu"]}, "facets": {"jobs": {"state": "RUNNING"}}})
    assert table_sort.chain(app, "jobs") is None
    assert app.table_state["hidden"]["jobs"] == ["gpu"]
    assert table_ui.matches(app, "jobs", Job("1", "job", "gpu", "RUNNING"), {})
    assert not table_ui.matches(app, "jobs", Job("2", "job", "gpu", "PENDING"), {})
    table_sort.clear_sort(app, "jobs")
    assert table_ui.matches(app, "jobs", Job("1", "job", "gpu", "RUNNING"), {})
    assert "state=RUNNING" in table_ui.chips(app, "jobs", 80)[0][0]


def test_selection_fingerprint_changes_on_sort_and_explicit_empty(app):
    original = table_ui.fingerprint(app, "jobs")
    table_sort.clear_sort(app, "jobs")
    empty = table_ui.fingerprint(app, "jobs")
    table_sort.set_sort(app, "jobs", "id", "asc")
    ascending = table_ui.fingerprint(app, "jobs")
    assert len({original, empty, ascending}) == 3
    assert table_ui.facets_fingerprint(app, "jobs") == ()


@pytest.mark.parametrize("table", list(table_sort.TABLE_KEYS))
def test_sortby_commands_support_each_table_and_callback_only_after_success(app, table):
    changes = []
    app.table_sort_changed = lambda tab: changes.append((tab, table_sort.chain(app, tab)))
    key = table_sort.TABLE_KEYS[table][0]
    assert table_ui.run_command(app, ["sortby", table, key, "asc"])
    assert table_sort.chain(app, table) == [(key, "asc")]
    assert changes == [(table, [(key, "asc")])]
    assert table_ui.run_command(app, ["sortby", table, key, "off"])
    assert table_sort.chain(app, table) == []
    table_ui.run_command(app, ["sortby", table, key])
    assert table_sort.chain(app, table) == [(key, "asc")]
    table_ui.run_command(app, ["sortby", table, "clear"])
    assert table_sort.chain(app, table) == []
    assert not app.failures


@pytest.mark.parametrize("words", [["jobs", "name", "up"], ["jobs", "bad", "asc"],
                                  ["jobs", "name", "asc", "extra"], ["jobs", "clear", "desc"],
                                  ["unknown", "name", "asc"]])
def test_invalid_sort_command_is_atomic_and_does_not_publish_change(app, words):
    table_sort.set_sort(app, "jobs", "name", "asc")
    before = copy.deepcopy(app.table_state)
    app.table_sort_changed = lambda tab: pytest.fail("Invalid commands must not publish or persist changes")
    assert table_ui.run_command(app, ["sortby"] + words)
    assert app.failures and app.table_state == before


def test_sortby_without_arguments_reports_settings_without_mutating(app):
    before = copy.deepcopy(app.table_state)
    assert table_ui.run_command(app, ["sortby"])
    assert "sortby COLUMN" in app.messages[-1]
    assert app.table_state == before


@pytest.mark.parametrize("current,column", [("history", "nodes"), ("recent", "nodes"), ("nodes", "jobs")])
def test_sortby_current_column_can_share_a_table_name(app, current, column):
    app.tab = current
    assert table_ui.run_command(app, ["sortby", column, "asc"])
    assert table_sort.chain(app, current) == [(column, "asc")]
    assert table_ui.run_command(app, ["sortby", column])
    assert table_sort.chain(app, current) == [(column, "desc")]
    assert table_ui.run_command(app, ["sortby", column, "name", "asc"])
    assert table_sort.chain(app, column) == [("name", "asc")]
    assert table_sort.chain(app, current) == [(column, "desc")]
    assert not app.failures


def test_named_views_round_trip_cascades_and_explicit_unsorted(tmp_path):
    cfg = Config({"clipboard": {"tools": False, "osc52": False}, "log_lines": 0})
    store = Store(state_dir=str(tmp_path / "state"))
    app = App(store, None, None, cfg, "reviewer", interactive=False)
    table_sort.set_sort(app, "jobs", "name", "asc")
    table_sort.set_sort(app, "jobs", "cpus", "desc")
    app.run_command('savedview save "cascade"')
    app.run_command("savedview load cascade")
    assert table_sort.chain(app, "jobs") == [("name", "asc"), ("cpus", "desc")]
    table_sort.clear_sort(app, "jobs")
    app.run_command("savedview save unsorted")
    table_sort.set_sort(app, "jobs", "id", "desc")
    app.run_command("savedview load unsorted")
    assert table_sort.chain(app, "jobs") == []
    table_sort.reset_sort(app, "jobs")
    app.run_command("savedview save legacy")
    table_sort.set_sort(app, "jobs", "id", "asc")
    app.run_command("savedview load legacy")
    assert table_sort.chain(app, "jobs") is None
    app.save()
    restored = App(store, None, None, cfg, "reviewer", interactive=False)
    restored.run_command("savedview load cascade")
    assert table_sort.chain(restored, "jobs") == [("name", "asc"), ("cpus", "desc")]


def test_malformed_named_view_cascade_is_rejected_before_navigation(tmp_path):
    cfg = Config({"clipboard": {"tools": False, "osc52": False}})
    app = App(Store(state_dir=str(tmp_path / "state")), None, None, cfg, "reviewer", interactive=False)
    app.table_state["views"]["damaged"] = {"tab": "history", "sorts": [["end", "up"]]}
    before = (app.tab, app.filter, copy.deepcopy(app.table_state), dict(app.sort))
    app.run_command("savedview load damaged")
    assert not app.command_ok
    assert (app.tab, app.filter, app.table_state, app.sort) == before


def test_history_value_uses_raw_metrics_and_real_job_identity():
    record = Finished("123_2", "experiment", "COMPLETED", "01:00:00", 4, gpus=0,
                      cpu_time=7200, req_mem=2 ** 30, rss=2 ** 29,
                      start="2026-10-01T00:00:00", end="2026-10-01T01:00:00",
                      partition="gpu", exit="0:0", nodelist="node2")
    assert table_sort.history_value(record, "id") == "123_2"
    assert table_sort.history_value(record, "elapsed") == 3600
    assert table_sort.history_value(record, "ce") == .5
    assert table_sort.history_value(record, "me") == .5
    assert table_sort.history_value(record, "gpus") == 0
    assert table_sort.history_value(record, "start") < table_sort.history_value(record, "end")
    assert table_sort.history_value(record, "nodes") == "node2"
    assert table_sort.history_value(record, "tags", {"tags": {record.id: {"tags": ["b", "a"]}}}) == "b a"
    pending = Job("123_3", "pending", "gpu", "RUNNING")
    assert table_sort.history_value(pending, "state") == "ACCOUNTING"
    assert table_sort.history_value(pending, "ce") is None
    assert table_sort.history_value(pending, "rss") is None
    assert table_sort.history_value(pending, "end") is None


@pytest.mark.parametrize("payload", [None, (), ("jobs", "name", 1), ("jobs", "bad", 1, 4),
                                    ("unknown", "name", 1, 4), ("jobs", "name", -1, 4),
                                    ("jobs", "name", 4, 4), ("jobs", "name", True, 4),
                                    ("jobs", "name", 1, 100001), (["jobs"], "name", 1, 4)])
def test_header_hit_validation_rejects_malformed_or_invisible_ranges(payload):
    assert not table_sort.valid_header(payload)


def test_header_hits_keep_exact_fitted_bounds_and_ignore_marks():
    cells = [("_mark", 1, 2), ("id", 4, 12), ("name", 14, 30), ("cpus", 32, 32)]
    assert table_sort.header_hits("jobs", cells, 9) == [
        (9, "sort_header", ("jobs", "id", 4, 12)),
        (9, "sort_header", ("jobs", "name", 14, 30)),
    ]
