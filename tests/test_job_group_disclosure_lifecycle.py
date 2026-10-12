"""Disclosure metadata remains exact through filtering and source transitions."""

from types import SimpleNamespace

import pytest

from raven import job_groups as G
from raven.model import Finished, Job


CONTEXTS = ("jobs", "recent", "history", "group", "deps", "analytics:advisor",
            "history:analytics", "history:log", "history:research", "history:deps")


@pytest.fixture
def grouping():
    records = [Job("52_0", "run", "cpu", "RUNNING"),
               Job("52_1", "run", "cpu", "PENDING", reason="Resources"),
               Job("52_2", "run", "cpu", "PENDING", reason="Dependency"),
               Job("99", "unrelated", "cpu", "RUNNING")]
    app = SimpleNamespace(table_state={"groups": True, "collapsed": []}, tab="jobs",
                          selected_id=None, cursor={"jobs": 0, "history": 0},
                          last_jobs_ids=[], last_history_ids=[])
    return app, {"jobs": records, "finished": [], "departed_jobs": {}}


@pytest.mark.parametrize("context", CONTEXTS)
def test_disclosure_follows_first_filtered_sorted_record_without_reintroducing_siblings(grouping, context):
    app, snap = grouping
    source = [snap["jobs"][2], snap["jobs"][3], snap["jobs"][0]]
    displayed = G.project_records(app, snap, source, context)
    expected = ([source[1], source[0], source[2]] if context == "jobs" else
                [source[0], source[2], source[1]] if context == "recent" else source)
    assert all(actual is original for actual, original in zip(displayed, expected))
    first = G.metadata_for_record(app, context, "52_2")
    assert first.header and first.representative_id == "52_2"
    assert first.visible_count == 2 and first.total_count == 3
    assert not G.metadata_for_record(app, context, "52_0").header
    assert G.fold(app, first.group.id, True)
    assert G.fold(app, first.group.id, True)  # Repeated painted close is safe.
    displayed = G.project_records(app, snap, source, context)
    assert displayed == ([source[1], source[0]] if context == "jobs" else source[:2])
    assert "52_1" not in [record.id for record in displayed]
    folded = G.metadata_for_record(app, context, "52_2")
    assert folded.collapsed and folded.stats.counts["dependent"] == 1
    assert folded.stats.counts["running"] == 1
    assert folded.stats.counts["pending"] == 0
    assert G.fold(app, first.group.id, False)
    assert G.fold(app, first.group.id, False)
    assert G.project_records(app, snap, source, context) == expected


def test_disclosures_and_shared_fold_survive_new_jobs_and_completion(grouping):
    app, snap = grouping
    G.project_records(app, snap, snap["jobs"], "jobs")
    group = G.registry(app).index.for_job("52_0")
    assert G.fold(app, group.id, True)
    snap["jobs"].append(Job("52_3", "run", "cpu", "PENDING", reason="DependencyNeverSatisfied"))
    G.project_records(app, snap, snap["jobs"], "jobs")
    assert G.registry(app).index.for_job("52_3").id == group.id
    assert G.metadata_for_record(app, "jobs", "52_0").stats.counts["blocked"] == 1
    # The finished first allocation leaves Queue and enters both accounting
    # projections. Each pane keeps real representatives and scoped counts.
    finished = Finished("52_0", "run", "COMPLETED")
    snap["jobs"] = [record for record in snap["jobs"] if record.id != "52_0"]
    snap["finished"] = [finished]
    with G.frame(app, snap):
        queue = G.project_records(app, snap, snap["jobs"], "jobs")
        recent = G.project_records(app, snap, snap["finished"], "recent")
        history = G.project_records(app, snap, snap["finished"], "history")
    assert [record.id for record in queue] == ["52_1", "99"]
    assert recent == history == [finished]
    queue_meta = G.metadata_for_record(app, "jobs", "52_1")
    for context in ("recent", "history"):
        meta = G.metadata_for_record(app, context, "52_0")
        assert meta.header and meta.collapsed and meta.group.id == group.id
        assert meta.total_count == 4 and meta.visible_count == 1
        assert meta.stats.counts["completed"] == 1
        assert meta.stats.counts["pending"] == 0
    assert queue_meta.header and queue_meta.collapsed
    assert queue_meta.total_count == 4 and queue_meta.visible_count == 3
    assert queue_meta.stats.counts["completed"] == 0
    assert G.fold(app, group.id, False)
    assert [record.id for record in G.project_records(app, snap, snap["jobs"], "jobs")] == ["52_1", "52_2", "52_3", "99"]
    assert not G.metadata_for_record(app, "jobs", "52_1").collapsed


def test_state_only_updates_refresh_summaries_without_repeating_inference(grouping):
    app, snap = grouping
    G.project_records(app, snap, snap["jobs"], "jobs")
    assert G.fold(app, "array:52", True)
    G.project_records(app, snap, snap["jobs"], "jobs")
    registry = G.registry(app)
    index, count = registry.index, registry.inference_count
    for state, reason, field in (("PENDING", "DependencyNeverSatisfied", "blocked"),
                                  ("RUNNING", "", "running"), ("FAILED", "", "failed")):
        snap["jobs"][1].state, snap["jobs"][1].reason = state, reason
        G.project_records(app, snap, snap["jobs"], "jobs")
        assert registry.index is index and registry.inference_count == count
        meta = G.metadata_for_record(app, "jobs", "52_0")
        assert meta.stats.counts[field] == (2 if field == "running" else 1)


def test_viewport_metadata_lookup_does_not_reinfer_or_change_representative(grouping, monkeypatch):
    app, snap = grouping
    G.project_records(app, snap, snap["jobs"], "jobs")
    registry = G.registry(app)
    monkeypatch.setattr(registry, "ensure", lambda *_: pytest.fail("Scrolling reinferred grouping"))
    # Renderers can disclose the first visible continuation using the same
    # immutable metadata; the true allocation representative remains stable.
    for _ in range(100):
        for record in snap["jobs"][1:3]:
            meta = G.metadata_for_record(app, "jobs", record.id)
            assert meta.group.id == "array:52" and meta.representative_id == "52_0"
            assert not meta.header


def test_removed_group_rejects_stale_disclosure_without_retargeting(grouping):
    app, snap = grouping
    G.project_records(app, snap, snap["jobs"], "jobs")
    old = G.registry(app).index.for_job("52_0")
    snap["jobs"] = [snap["jobs"][-1]]
    G.project_records(app, snap, snap["jobs"], "jobs")
    assert not G.fold(app, old.id, True)
    assert app.table_state["collapsed"] == []
    assert G.metadata_for_record(app, "jobs", "52_0") is None
