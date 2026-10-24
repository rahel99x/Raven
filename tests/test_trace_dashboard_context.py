"""Published queue paths and exact job selection seed the trace subscription."""

from __future__ import annotations

from copy import deepcopy

import pytest

from raven import project_ui, trace_ui
from test_job_panels import dashboard as dashboard
from test_trace_timeline_workflow import connect


def queued_context(dashboard):
    app = connect(dashboard)
    job = dashboard.store.jobs[0]
    job.workdir = str(dashboard.paths[job.id][0].parent)
    job.cluster = "queue-cluster"
    dashboard.store.details[job.id] = {}
    app.project_state = {}
    return app, job


def test_trace_context_uses_published_queue_before_selected_details_arrive(
    dashboard, monkeypatch
):
    app, job = queued_context(dashboard)

    def forbidden(*args, **kwargs):
        pytest.fail("Trace context requested filesystem or scheduler I/O")

    # Context construction is a published-data operation, including its new
    # fallback. It cannot require a second scontrol read or a manifest open.
    monkeypatch.setattr(app.actions.slurm, "details", forbidden)
    monkeypatch.setattr(dashboard.views.files, "stat", forbidden)
    context = trace_ui._context(app)
    assert context["job_id"] == job.id
    assert context["root"] == job.workdir
    assert context["cluster"] == job.cluster
    assert context["run_id"] == "" and context["run_root"] == ""


def test_selected_details_keep_existing_priority_and_do_not_change_source_root(
    dashboard,
):
    app, job = queued_context(dashboard)
    dashboard.store.details[job.id] = {
        "WorkDir": "/scheduler/selected-job",
        "Cluster": "details-cluster",
    }
    context = trace_ui._context(app)
    assert context["root"] == "/scheduler/selected-job"
    assert context["cluster"] == "details-cluster"
    app.cfg.set("cluster_name", "configured-cluster")
    assert trace_ui._context(app)["cluster"] == "configured-cluster"
    assert job.workdir != context["root"]


def test_verified_job_binding_still_precedes_queue_workdir(dashboard):
    app, job = queued_context(dashboard)
    app.project_state = {
        "root": "/registered/project",
        "registered_root": "/registered/project",
        "binding": {
            "job_id": job.id,
            "run_id": "exact-run",
            "project_root": "/bound/project",
            "run_root": "/bound/project/runs/exact-run",
        },
    }
    before = deepcopy(app.project_state)
    context = trace_ui._context(app)
    assert context["root"] == "/bound/project"
    assert context["run_root"] == "/bound/project/runs/exact-run"
    assert context["run_id"] == "exact-run"
    assert app.project_state == before


def test_foreign_job_binding_does_not_redirect_current_trace(dashboard):
    app, job = queued_context(dashboard)
    app.project_state = {
        "registered_root": "/old/project",
        "binding": {
            "job_id": "different-job",
            "run_id": "old-run",
            "project_root": "/old/project",
            "run_root": "/old/project/runs/old-run",
        },
    }
    context = trace_ui._context(app)
    assert context["root"] == job.workdir
    assert context["run_id"] == context["run_root"] == ""
    assert context["binding"] == {}


def test_queue_metadata_change_retires_the_previous_context_key(dashboard):
    app, job = queued_context(dashboard)
    first = trace_ui._context(app)
    job.workdir += "-new"
    second = trace_ui._context(app)
    assert second["key"] != first["key"]
    assert second["root"] == job.workdir
    job.cluster = "different-cluster"
    assert trace_ui._context(app)["key"] != second["key"]


@pytest.mark.parametrize("tab", ["jobs", "history", "traces"])
def test_trace_and_project_discovery_use_the_same_selected_job(tab, dashboard):
    app, job = queued_context(dashboard)
    app.tab = tab
    app.research_job_id = "700"
    assert trace_ui._context(app)["job_id"] == job.id
    assert project_ui._target_job(app) == job.id


def test_empty_global_trace_selection_does_not_attach_stale_research_job(dashboard):
    app, _ = queued_context(dashboard)
    app.tab = "traces"
    app.selected_id = None
    app.research_job_id = "700"
    assert project_ui._target_job(app) is None
    assert trace_ui._context(app) is None


def test_research_and_log_keep_their_explicit_job_selection(dashboard):
    app, _ = queued_context(dashboard)
    app.tab = "research"
    app.research_job_id = "700"
    assert project_ui._target_job(app) == "700"
    app.tab = "log"
    app.log_job = "log-job"
    assert project_ui._target_job(app) == "log-job"
