"""Peer forms keep exact context through named effects composed above them."""

from dataclasses import fields
from types import SimpleNamespace

import pytest

from raven import data_export_ui, notification_ui, project_ui, setup_ui, workbench
from raven.ui import (
    export_state,
    form_state,
    navigation_ports as ports,
    project_state,
    prose,
    research_binding,
)


def test_extracted_project_and_form_queries_keep_public_identity():
    assert project_ui.initialize is project_state.initialize
    assert project_ui.selected_binding is project_state.selected_binding
    assert project_ui._backend_token is project_state._backend_token
    assert setup_ui._wrap is form_state._wrap
    assert setup_ui._pending_review is form_state._pending_review
    assert notification_ui._wrap is prose._wrap
    assert notification_ui.Wrapped is prose.Wrapped
    assert data_export_ui.initialize is export_state.initialize
    assert data_export_ui._invalidate is export_state._invalidate
    assert data_export_ui._failure is export_state._failure


@pytest.mark.parametrize(
    "record",
    [
        ports.FormNavigation,
        ports.ProjectBindingEffects,
        ports.ResearchNavigation,
        ports.ExportBackground,
    ],
)
def test_navigation_effects_require_explicit_callable_fields(record):
    callbacks = {field.name: lambda *args, **kwargs: None for field in fields(record)}
    value = record(**callbacks)
    for field in fields(record):
        assert getattr(value, field.name) is callbacks[field.name]
        with pytest.raises(TypeError, match=field.name):
            record(**{**callbacks, field.name: None})


def test_unbound_navigation_reports_the_missing_composition():
    for query in (ports.forms, ports.projects, ports.research, ports.exports):
        with pytest.raises(ValueError, match="not been bound"):
            query(SimpleNamespace())


def test_late_private_callback_patch_observes_exact_launch_receipt(monkeypatch):
    from raven import run_home_ui

    owner = SimpleNamespace()
    workbench.bind_navigation(owner)
    calls = []
    monkeypatch.setattr(
        run_home_ui, "open_receipt", lambda app, receipt: calls.append((app, receipt))
    )
    receipt = {"run_id": "attempt-two", "token": "verified-source"}
    ports.forms(owner).launch_receipt(owner, receipt)
    assert calls == [(owner, receipt)]
    assert calls[0][1] is receipt


def test_shared_launch_setup_queue_retains_export_worker_and_exact_callbacks(
    monkeypatch,
):
    owner = SimpleNamespace()
    workbench.bind_navigation(owner)
    calls = []
    work = lambda event, progress: None
    complete = lambda result: None
    monkeypatch.setattr(
        data_export_ui, "_queue", lambda *args: calls.append(args) or True
    )
    assert ports.exports(owner).queue(
        owner, "Read project declarations", work, complete
    )
    assert calls == [(owner, "Read project declarations", work, complete)]


def test_run_binding_publishes_original_root_before_applying_selected_evidence(
    monkeypatch,
):
    owner = SimpleNamespace()
    workbench.bind_navigation(owner)
    selected = {"binding": {"run_id": "verified-attempt", "attempt": 3}}
    calls = []
    monkeypatch.setattr(
        project_ui,
        "_apply_run",
        lambda app, result: calls.append(
            (app, result, dict(project_ui.initialize(app)))
        ),
    )
    ports.projects(owner).bind_run(owner, "/chosen/project", selected)
    assert calls[0][:2] == (owner, selected)
    assert calls[0][1] is selected
    assert calls[0][2]["root"] == calls[0][2]["registered_root"] == "/chosen/project"


@pytest.mark.parametrize("record_back", [True, False])
def test_research_selection_preserves_navigation_clear_resume_order(
    monkeypatch, record_back
):
    from raven import job_selection, navigation_ui

    owner = SimpleNamespace(
        tab="research", research_job_id="old", selected_id="old", research_scroll=19
    )
    project_state.initialize(owner)["binding"] = {
        "job_id": "old",
        "run_id": "old-attempt",
    }
    workbench.bind_navigation(owner)
    events = []
    monkeypatch.setattr(
        navigation_ui,
        "record",
        lambda app, view, **kwargs: events.append(("record", view, kwargs)),
    )
    monkeypatch.setattr(
        project_ui,
        "clear_binding",
        lambda app, **kwargs: events.append(("clear", kwargs)),
    )
    monkeypatch.setattr(
        job_selection, "resume", lambda app, page: events.append(("resume", page))
    )
    research_binding.select_job(
        owner, "new", view="experiment", record_back=record_back
    )
    expected = [("record", "research", {"force": True})] if record_back else []
    assert events == expected + [("clear", {}), ("resume", "research")]
    assert owner.selected_id == owner.research_job_id == "new"
    assert owner.research_scroll == 0 and owner.research_view == "experiment"
