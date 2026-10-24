"""Recorded scheduler conflicts veto weaker run labels without hiding evidence."""

import json
from threading import Event

import pytest

from raven.tracing import ui as UI
from raven.tracing import workspace_discovery as D
from raven.tracing.capture import Recorder


def context(root, **extra):
    return dict(
        job_id="123",
        cluster="local",
        submit="submit",
        start="new-start",
        run_id="project-run",
        root=str(root),
        run_root="",
        remote=False,
        **extra,
    )


@pytest.mark.parametrize(
    "anchors",
    [
        {"slurm_submit": "submit", "slurm_start": "old-start"},
        {"slurm_submit": "old-submit", "slurm_start": "new-start"},
        {"slurm_start": "old-start"},
    ],
)
def test_explicit_anchor_conflicts_veto_matching_run_id_but_remain_viewable(
    tmp_path, anchors
):
    with Recorder(
        tmp_path,
        job_id="123",
        attempt="project-run",
        cluster="local",
        identity_extra={"run_id": "project-run", **anchors},
        background_flush=False,
    ) as rec:
        rec.emit("call", {"frame": "f", "function": "previous execution"})
        rec.emit("return", {"frame": "f"})
    ctx = context(tmp_path)
    automatic = UI._read(ctx, "", 0, "", None, Event())
    assert not automatic["sources"] and not automatic["verified"]
    explicit = UI._read(ctx, str(rec.path), 0, "", None, Event())
    assert not explicit["verified"]
    assert any(
        event["data"].get("function") == "previous execution"
        for event in explicit["snapshot"]["events"]
    )
    declared = D.read(
        ctx,
        str(rec.path),
        0,
        Event(),
        launch_sources=lambda context: ([str(rec.path)], []),
        matches=UI._matches,
        process=UI._process,
    )
    assert not declared["association_verified"] and declared["sources"] == [
        str(rec.path)
    ]


def test_conflicting_anchor_cannot_fall_back_to_encoded_attempt_either():
    identity = dict(
        job_id="123",
        cluster="local",
        attempt="submit/new-start",
        slurm_submit="submit",
        slurm_start="old-start",
    )
    assert not UI._matches(identity, context("/unused"))


def test_matching_anchors_and_unique_run_without_anchors_keep_existing_paths():
    ctx = context("/unused")
    for identity in (
        dict(
            job_id="123", cluster="local", attempt="project-run", run_id="project-run"
        ),
        dict(job_id="123", cluster="local", attempt="submit/new-start"),
        dict(
            job_id="123",
            cluster="local",
            attempt="source",
            slurm_submit="submit",
            slurm_start="new-start",
        ),
    ):
        assert UI._matches(identity, ctx)
    assert not UI._matches(
        dict(job_id="123", cluster="local", attempt="legacy-undeclared"), ctx
    )


def test_real_reporting_requeues_generate_distinct_attempt_scoped_run_ids(tmp_path):
    # runtime._initialize_run calls research_reporting.start; its restart
    # counter controls the run directory. autotrace._Attachment.start uses
    # that directory name for BOTH Recorder.attempt and identity.run_id.
    from raven import research_reporting as reporting

    (tmp_path / "job.sh").write_text("#!/bin/bash\ntrue\n")
    token = "a" * 32
    launches = tmp_path / ".raven" / "launches"
    launches.mkdir(mode=0o700, parents=True)
    (launches / (token + ".json")).write_text(
        json.dumps(
            {
                "schema": "raven.launch/v1",
                "token": token,
                "run_id_base": "launch-" + token,
                "script": "job.sh",
                "experiment": "trace-association",
            }
        )
    )
    env = dict(
        SLURM_JOB_ID="123",
        SLURM_CLUSTER_NAME="local",
        SLURM_RESTART_COUNT="0",
        RAVEN_SUBMISSION_TOKEN=token,
    )
    first = reporting.start(
        tmp_path, experiment="trace-association", script="job.sh", environ=env
    )
    second = reporting.start(
        tmp_path,
        experiment="trace-association",
        script="job.sh",
        environ=dict(env, SLURM_RESTART_COUNT="1"),
    )
    assert first.name != second.name
    assert first.name == "launch-" + token + "-a1"
    assert second.name == "launch-" + token + "-a2"
    assert first.name.endswith("-a1") and second.name.endswith("-a2")
    ctx = context(tmp_path)
    ctx["run_id"] = second.name
    current = dict(
        job_id="123", cluster="local", attempt=second.name, run_id=second.name
    )
    previous = dict(
        job_id="123", cluster="local", attempt=first.name, run_id=first.name
    )
    assert UI._matches(current, ctx)
    assert not UI._matches(previous, ctx)
