"""Automatic presentation commands preserve explicit choices and undo scope."""
from types import SimpleNamespace

from raven import job_groups as G, manual_job_groups as M, grouping_toolbar as T
from raven.model import Job


def fixture():
    app = SimpleNamespace(table_state={"groups": True, "collapsed": []}, tab="jobs", selected_id=None,
        cursor={"jobs": 0}, interaction_state={}, job_selection_state={}, job_group_drag_state={})
    jobs = [Job(str(i), "experiment", "main", "PENDING", submit="2026-10-08T12:00:00", cluster="carc",
                user="alex", account="lab", workdir="/project", command="/project/train.sh") for i in range(100, 106)]
    return app, {"jobs": jobs}


def test_auto_collapses_only_automatic_and_keeps_manual_detachment():
    app, snap = fixture()
    manual = M.create(app, snap, ["100", "101"])
    M.detach(app, snap, ["102"])
    assert T.execute(app, "auto", snap).changed
    registry = G.registry(app)
    index = registry.ensure(snap)
    assert index.for_job("102") is None
    assert index.groups[manual.group_id].members == ("100", "101")
    assert not registry.is_collapsed(index.groups[manual.group_id])
    assert registry.is_collapsed(index.for_job("103"))


def test_undo_auto_restores_previous_inference_and_folds_without_manual_edits():
    app, snap = fixture()
    app.table_state.update(groups=False, auto_groups=False)
    assert T.execute(app, "auto", snap).changed
    M.create(app, snap, ["100", "105"])
    assert T.execute(app, "undo", snap).changed
    index = G.registry(app).ensure(snap)
    assert app.table_state["groups"]
    assert not app.table_state["auto_groups"]
    assert len(index.groups) == 1
    assert index.for_job("100").kind == "manual"
    assert index.for_job("102") is None


def test_repeated_auto_retains_original_undo():
    app, snap = fixture()
    app.table_state["auto_groups"] = False
    T.execute(app, "auto", snap)
    undo = app.grouping_auto_undo
    T.execute(app, "auto", snap)
    assert app.grouping_auto_undo is undo
    T.execute(app, "undo", snap)
    assert not app.table_state["auto_groups"]


def test_undo_keeps_preexisting_collapsed_manual_and_auto_groups():
    app, snap = fixture()
    made = M.create(app, snap, ["100", "101"])
    G.fold(app, made.group_id, True)
    before = set(app.table_state["collapsed"])
    T.execute(app, "auto", snap)
    T.execute(app, "undo", snap)
    assert set(app.table_state["collapsed"]) == before


def test_disclosure_click_takes_ownership_over_automatic_fold():
    app, snap = fixture()
    T.execute(app, "auto", snap)
    group = G.registry(app).ensure(snap).for_job("100")
    G.fold(app, group.id, False)
    G.fold(app, group.id, True)
    T.execute(app, "undo", snap)
    assert group.id in app.table_state["collapsed"]


def test_auto_does_not_evict_manual_fold_when_preference_limit_is_full():
    app, snap = fixture()
    app.table_state["collapsed"] = ["manual:" + str(i) for i in range(G.MAX_COLLAPSED)]
    before = list(app.table_state["collapsed"])
    T.execute(app, "auto", snap)
    assert set(app.table_state["collapsed"]) == set(before)


def test_clear_all_stays_off_across_new_jobs_and_manual_grouping_returns_only_explicit_group():
    app, snap = fixture()
    M.create(app, snap, ["100", "101"])
    T.execute(app, "clear", snap)
    assert not G.registry(app).ensure(snap).groups
    snap["jobs"].append(Job("106", "experiment", "main", "PENDING", submit="2026-10-08T12:00:00", cluster="carc",
                          user="alex", account="lab", workdir="/project", command="/project/train.sh"))
    assert not G.registry(app).ensure(snap).groups
    M.create(app, snap, ["100", "101"])
    assert len(G.registry(app).ensure(snap).groups) == 1
    assert not T.execute(app, "restore", snap).changed


def test_clear_restore_keeps_groups_labels_exclusions_and_folded_state():
    app, snap = fixture()
    made = M.create(app, snap, ["100", "101"])
    M.detach(app, snap, ["102"])
    G.fold(app, made.group_id, True)
    before = G.registry(app).ensure(snap)
    T.execute(app, "clear", snap)
    assert T.execute(app, "restore", snap).changed
    after = G.registry(app).ensure(snap)
    assert before.groups == after.groups
    assert G.registry(app).is_collapsed(after.groups[made.group_id])
    assert after.for_job("102") is None


def test_expand_does_not_turn_grouping_on_or_clear_manual_groups():
    app, snap = fixture()
    made = M.create(app, snap, ["100", "101"])
    G.fold(app, made.group_id, True)
    app.table_state["groups"] = False
    assert T.execute(app, "expand", snap).changed
    assert not app.table_state["groups"]
    assert G.registry(app).ensure(snap).groups[made.group_id]
    assert not app.table_state["collapsed"]


def test_collapse_preserves_membership_exclusions_and_owns_auto_folds():
    app, snap = fixture()
    made = M.create(app, snap, ["100", "101"])
    M.detach(app, snap, ["102"])
    T.execute(app, "auto", snap)
    before = G.registry(app).ensure(snap).groups.copy()
    assert T.execute(app, "collapse", snap).changed
    assert all(G.registry(app).is_collapsed(group) for group in before.values())
    assert not T.execute(app, "collapse", snap).changed
    T.execute(app, "undo", snap)
    registry = G.registry(app)
    assert registry.ensure(snap).groups == before
    assert all(registry.is_collapsed(group) for group in before.values())
    assert registry.index.for_job("102") is None
    assert registry.index.groups[made.group_id].members == ("100", "101")
    T.execute(app, "expand", snap)
    assert not G.registry(app).collapsed


def test_collapse_is_atomic_at_fold_limit_and_never_enables_inference(monkeypatch):
    app, snap = fixture()
    M.create(app, snap, ["100", "101"])
    M.create(app, snap, ["102", "103"])
    app.table_state.update(groups=False, auto_groups=False)
    assert not T.execute(app, "collapse", {"jobs": []}).changed
    monkeypatch.setattr(G, "MAX_COLLAPSED", 1)
    assert not T.execute(app, "collapse", snap).changed
    assert not app.table_state["collapsed"]
    assert not app.table_state["groups"] and not app.table_state["auto_groups"]
    monkeypatch.setattr(G, "MAX_COLLAPSED", 2)
    app.table_state["collapsed"] = ["manual:archived-a", "manual:archived-b"]
    assert T.execute(app, "collapse", snap).changed
    registry = G.registry(app)
    assert all(registry.is_collapsed(group) for group in registry.index.groups.values())
    assert len(registry.collapsed) == 2
    assert not app.table_state["groups"] and not app.table_state["auto_groups"]


def test_commands_cancel_stale_gestures_and_invalidate_geometry():
    app, snap = fixture()
    app.job_selection_state["capture"] = object()
    app.job_group_drag_state["capture"] = object()
    app.group_queue_drag_state = {"capture": object()}
    app.job_group_drag_state.update(pending=object(), armed=object())
    T.execute(app, "auto", snap)
    assert app.job_selection_state["capture"] is None
    assert app.job_group_drag_state["capture"] is None
    assert app.job_selection_state["discard_release"]
    assert app.job_group_drag_state["discard_release"]
    assert app.group_queue_drag_state["discard_release"]
    assert app.job_group_drag_state["pending"] is None
    assert app.job_group_drag_state["armed"] is None
    assert app.interaction_state["frame_required"]


def test_grouping_without_capture_does_not_discard_next_pointer_release():
    app, snap = fixture()
    app.group_queue_drag_state = {"capture": None, "discard_release": False}
    T.execute(app, "auto", snap)
    assert not app.job_selection_state.get("discard_release")
    assert not app.job_group_drag_state.get("discard_release")
    assert not app.group_queue_drag_state["discard_release"]


def test_auto_preference_changes_registry_without_reinferring_jobs():
    app, snap = fixture()
    registry = G.registry(app)
    registry.ensure(snap)
    count = registry.inference_count
    T.execute(app, "clear", snap)
    T.execute(app, "auto", snap)
    assert registry.inference_count == count
