"""Discover and complete reversible column sorts through the real command UI."""
from __future__ import annotations

import pytest

from raven import command_ui as commands
from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs, row_text
from raven.model import Store
from raven.table_sort import TABLE_KEYS
from raven.views import Views


@pytest.fixture
def app():
    return App(Store(persist=False), None, None, Config({"log_lines": 0}), "tester", ascii_=True)


def palette(app, text):
    app.mode, app.palette_edit = "palette", text
    commands._sync_input(app)


def choices(app, text):
    palette(app, text)
    return {row["value"]: row["description"] for row in commands.suggestions(app)}


def test_sortby_command_is_described_and_completed(app):
    matches = choices(app, "sortb")
    assert "ascending" in matches["sortby"] and "off" in matches["sortby"]
    assert commands.complete(app)
    assert app.palette_edit == "sortby "


def test_current_sort_columns_and_explicit_tables_are_discoverable(app):
    assert set(choices(app, "sortby ")) == set(TABLE_KEYS["jobs"]) | set(TABLE_KEYS) | {"clear"}
    app.tab = "research"
    assert set(choices(app, "sortby ")) == set(TABLE_KEYS)


@pytest.mark.parametrize("table", list(TABLE_KEYS))
def test_explicit_table_completes_columns_clear_and_directions(app, table):
    app.tab = "log"
    assert set(choices(app, f"sortby {table} ")) == set(TABLE_KEYS[table]) | {"clear"}
    column = TABLE_KEYS[table][0]
    assert set(choices(app, f"sortby {table} {column} ")) == {"asc", "desc", "off"}
    palette(app, f"sortby {table} {column} de")
    assert commands.complete(app)
    assert commands.parse_command_line(app.palette_edit) == ["sortby", table, column, "desc"]
    app.run_command(app.palette_edit)
    assert app.table_state["sorts"][table] == [(column, "desc")]


@pytest.mark.parametrize("table,column", [("jobs", "id"), ("history", "nodes"), ("nodes", "jobs"),
                                            ("group", "id"), ("sources", "calls"), ("cluster", "nodes")])
def test_current_column_direction_completion_executes_in_current_table(app, table, column):
    app.tab = table
    assert {"asc", "desc", "off"} <= set(choices(app, f"sortby {column} "))
    palette(app, f"sortby {column} a")
    assert commands.complete(app)
    assert commands.parse_command_line(app.palette_edit) == ["sortby", column, "asc"]
    app.run_command(app.palette_edit)
    assert app.table_state["sorts"][table] == [(column, "asc")]


@pytest.mark.parametrize("current,ambiguous,column", [("history", "nodes", "load"), ("nodes", "jobs", "cpus")])
def test_ambiguous_table_or_column_offers_both_valid_continuations(app, current, ambiguous, column):
    app.tab = current
    assert set(choices(app, f"sortby {ambiguous} ")) == set(TABLE_KEYS[ambiguous]) | {"clear", "asc", "desc", "off"}
    palette(app, f"sortby {ambiguous} {column}")
    assert commands.complete(app)
    assert commands.parse_command_line(app.palette_edit) == ["sortby", ambiguous, column]
    assert set(choices(app, app.palette_edit)) == {"asc", "desc", "off"}
    palette(app, app.palette_edit + "de")
    assert commands.complete(app)
    app.run_command(app.palette_edit)
    assert app.table_state["sorts"][ambiguous] == [(column, "desc")]
    assert current not in app.table_state["sorts"]


def test_completed_clear_restores_source_order_for_only_requested_table(app):
    app.run_command("sortby id asc")
    app.run_command("sortby history id desc")
    palette(app, "sortby history cle")
    assert commands.complete(app)
    assert commands.parse_command_line(app.palette_edit) == ["sortby", "history", "clear"]
    app.run_command(app.palette_edit)
    assert app.table_state["sorts"] == {"jobs": [("id", "asc")], "history": []}


@pytest.mark.parametrize("text", ["sortby clear ", "sortby id asc ", "sortby history clear ",
                                  "sortby history id desc ", "sortby not-a-column ", "sortby jobs not-a-column "])
def test_completed_or_invalid_sort_arguments_do_not_offer_unusable_values(app, text):
    assert choices(app, text) == {}


@pytest.mark.parametrize("table", ["jobs", "history", "group", "cluster", "nodes", "sources"])
def test_sorting_help_explains_header_cycle_priorities_and_keyboard_controls(app, table):
    app.tab = table
    entries = commands._help_entries(app)
    sorting = "\n".join(" ".join(entry) for entry in entries if entry[0] == "Sorting")
    assert "Click a column header" in sorting
    assert "ascending, descending, then off" in sorting
    assert "off removes only that column" in sorting
    assert "later columns break ties" in sorting and "numbers show the order" in sorting
    assert ":sortby [TABLE] COLUMN [asc|desc|off]" in sorting
    assert "restore source order" in sorting
    if table in ("jobs", "history", "group"):
        assert "JOBID" in sorting and "_2 before _10" in sorting
    if table == "jobs":
        assert ":sortby recent" in sorting and "independently of active jobs" in sorting


def test_sorting_help_search_renders_the_actionable_mouse_instruction(app):
    app.mode, app.scroll = "help", 0
    commands.run_command(app, ["help", "header"])
    views = Views(Glyphs(True), app.cfg)
    text = "\n".join(row_text(row) for _, _, row in commands.overlay(views, app.store.snapshot(), app, 80, 24))
    assert "Click a column header" in text and "ascending, descending, then off" in text


def test_non_table_help_does_not_advertise_clickable_column_headers(app):
    app.tab = "log"
    assert not any(entry[0] == "Sorting" for entry in commands._help_entries(app))
