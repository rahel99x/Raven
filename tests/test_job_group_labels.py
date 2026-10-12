"""Readable shared job names remain separate from group and action identity."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from raven import job_groups as G, manual_job_groups as M
from raven.model import Finished, Job


def job(jid, name="study-01", **changes):
    fields = dict(partition="main", state="RUNNING", user="alex", account="lab",
                  workdir="/project", command="/project/train.sh", cluster="",
                  submit="2026-10-12T12:00:00")
    fields.update(changes)
    return Job(str(jid), name, **fields)


def app():
    return SimpleNamespace(table_state={"groups": True, "collapsed": []}, tab="jobs",
                           selected_id=None, cursor={"jobs": 0, "history": 0},
                           last_jobs_ids=[], last_history_ids=[])


@pytest.mark.parametrize("names,expected", [
    (["experiment_01", "experiment_02"], "experiment_0"),
    (["study-a", "study-b"], "study"),
    (["train /:._- a", "train /:._- b"], "train"),
    (["analysis", "analysis"], "analysis"),
    (["42_1", "42_2"], "42"),
    (["演算-甲", "演算-乙"], "演算"),
    (["alpha", "beta"], ""),
    (["a1", "a2"], ""),
    (["--a", "--b"], ""),
    (["", "study"], ""),
    ([], ""),
])
def test_shared_name_prefix_accepts_streaming_names_and_trims_only_suffix(names, expected):
    assert G.shared_name_prefix(name for name in names) == expected


def test_prefix_normalizes_terminal_sequences_and_exposes_controls_safely():
    names = ["\x1b[31mstudy-a\x1b[0m", "\x1b[32mstudy-b\x1b[0m"]
    assert G.shared_name_prefix(names) == "study"
    prefix = G.shared_name_prefix(["study\n-a", "study\n-b"])
    assert prefix == "study^J" and prefix.isprintable()
    assert not any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in prefix)


def test_prefix_is_capped_at_the_public_display_limit():
    assert G.MAX_GROUP_PREFIX == 160
    common = "x" * (G.MAX_GROUP_PREFIX + 100)
    assert G.shared_name_prefix([common + "-a", common + "-b"]) == "x" * G.MAX_GROUP_PREFIX


def test_prefix_stops_reading_when_no_useful_prefix_remains():
    def names():
        yield "alpha"
        yield "beta"
        pytest.fail("An unrelated pair already excludes a common prefix")
    assert G.shared_name_prefix(names()) == ""


def test_prefix_character_work_is_linear_in_streamed_bounded_names():
    class Counted(str):
        accesses = 0

        def __getitem__(self, key):
            value = super().__getitem__(key)
            Counted.accesses += len(value) if isinstance(key, slice) else 1
            return Counted(value) if isinstance(key, slice) else value

    count = 2000
    prefix = "shared-experiment-" + "x" * 96
    consumed = 0

    def names():
        nonlocal consumed
        for number in range(count):
            consumed += 1
            yield Counted(prefix + f"-{number:04}")

    assert G.shared_name_prefix(names()) == prefix
    assert consumed == count
    assert 0 < Counted.accesses <= 8 * count * G.MAX_GROUP_PREFIX


def test_long_plain_names_only_inspect_the_shrinking_prefix(monkeypatch):
    class LongName(str):
        def __hash__(self):
            pytest.fail("Prefix selection must not hash a long job name")

        def isprintable(self):
            pytest.fail("Prefix selection must not scan a complete long name")

    probe = G._NON_PRINTABLE_ASCII
    limits = []

    class BoundedProbe:
        def search(self, name, start, limit):
            limits.append(limit)
            return probe.search(name, start, limit)

    monkeypatch.setattr(G, "_NON_PRINTABLE_ASCII", BoundedProbe())
    names = [LongName("study-a" + "x" * 4000), LongName("study-b" + "x" * 4000),
             LongName("study-c" + "x" * 4000)]
    assert G.shared_name_prefix(iter(names)) == "study"
    assert limits == [G.MAX_GROUP_PREFIX, G.MAX_GROUP_PREFIX, len("study-")]


@pytest.mark.parametrize("kind,records,expected", [
    ("array", [job("12_0", "study-a"), job("12_1", "study-b")], "study / Array 12"),
    ("heterogeneous", [job("12+0", "study-a"), job("12+1", "study-b")], "study / Heterogeneous 12"),
    ("explicit", [job(100, "study-a", comment="launch:release"),
                  job(900, "study-b", comment="launch:release")], "study"),
    ("burst", [job(100, "study-1"), job(101, "study-2"), job(102, "study-3")], "study"),
    ("dependency", [job(100, "study-prepare"), job(101, "study-fit", dependency="afterok:100")], "study"),
])
def test_automatic_families_use_names_without_changing_membership(kind, records, expected):
    group, = G.Registry().ensure({"jobs": records}).groups.values()
    assert group.kind == kind and group.label == expected
    assert set(group.members) == {record.id for record in records}


@pytest.mark.parametrize("records,expected", [
    ([job("12_0", "alpha"), job("12_1", "beta")], "Array 12"),
    ([job("12+0", "alpha"), job("12+1", "beta")], "Heterogeneous 12"),
    ([job(100, "alpha", comment="launch:release"), job(900, "beta", comment="launch:release")], "Launch release"),
    ([job(100, "a-1"), job(101, "a-2"), job(102, "a-3")], "Launch a"),
    ([job(100, "alpha"), job(101, "beta", dependency="afterok:100")], "Pipeline 100"),
])
def test_unrelated_or_one_character_prefix_keeps_the_existing_fallback(records, expected):
    group, = G.Registry().ensure({"jobs": records}).groups.values()
    assert group.label == expected


@pytest.mark.parametrize("cluster", ["", "discovery"])
def test_array_name_changes_keep_exact_group_id_and_saved_collapse_alias(cluster):
    records = [job("12_0", "study-a", cluster=cluster), job("12_1", "study-b", cluster=cluster)]
    snap = {"jobs": records}
    registry = G.Registry()
    first, = registry.ensure(snap).groups.values()
    assert registry.fold(first.id, True)
    stored = first.id if cluster else "12"
    assert registry.collapsed == {stored}
    # Presentation text is never a source of scheduler allocation identity.
    assert registry.is_collapsed(replace(first, label="Readable heading 999"))
    for number, record in enumerate(records):
        record.name = f"renamed-{number}"
    current, = registry.ensure(snap).groups.values()
    assert current.id == first.id and current.label == "renamed / Array 12"
    assert registry.is_collapsed(current)
    restored = G.Registry(registry.collapsed)
    restored_group, = restored.ensure(snap).groups.values()
    assert restored_group.id == first.id and restored.is_collapsed(restored_group)
    assert restored.fold(restored_group.id, False) and not restored.collapsed


def test_scoped_array_does_not_inherit_another_clusters_numeric_fold_alias():
    records = [job("12_0", "study-a", cluster="discovery"), job("12_1", "study-b", cluster="discovery")]
    registry = G.Registry(["12"])
    group, = registry.ensure({"jobs": records}).groups.values()
    assert not registry.is_collapsed(group)


def test_cached_projection_does_not_recompute_shared_names(monkeypatch):
    value = app()
    records = [job("12_0", "study-a"), job("12_1", "study-b")]
    snap = {"jobs": records}
    G.project_records(value, snap, records, "jobs")
    registry = G.registry(value)
    before = registry.inference_count
    monkeypatch.setattr(G, "shared_name_prefix", lambda *_: pytest.fail("Unchanged render recomputed names"))
    for _ in range(3):
        assert G.project_records(value, snap, records, "jobs") == records
        assert G.project_records(value, snap, records, "recent") == records
    records[0].state = "COMPLETING"
    G.project_records(value, snap, records, "jobs")
    assert registry.inference_count == before


def test_new_manual_label_is_saved_once_and_retained_after_renaming_jobs():
    value = app()
    records = [job(100, "study-a"), job(900, "study-b")]
    snap = {"jobs": records}
    made = M.create(value, snap, [record.id for record in records])
    assert made.changed
    assert G.registry(value).ensure(snap).groups[made.group_id].label == "study"
    saved = value.table_state["manual_groups"]["groups"][0]
    assert saved["label"] == "study"
    for record in records:
        record.name = "different-experiment"
        record.state = "COMPLETING"
    assert G.registry(value).ensure(snap).groups[made.group_id].label == "study"
    restored = app()
    restored.table_state["manual_groups"] = json.loads(json.dumps(value.table_state["manual_groups"]))
    assert G.registry(restored).ensure(snap).groups[made.group_id].label == "study"


def test_existing_custom_manual_label_survives_membership_and_job_changes():
    value = app()
    records = [job(100, "study-a"), job(900, "study-b"), job(1500, "study-c")]
    snap = {"jobs": records}
    made = M.create(value, snap, ["100", "900"])
    saved = json.loads(json.dumps(value.table_state["manual_groups"]))
    saved["groups"][0]["label"] = "Lab review: accepted runs"
    value.table_state["manual_groups"] = saved
    assert M.add(value, snap, made.group_id, ["1500"]).changed
    for record in records:
        record.name = "new-name"
        record.state = "COMPLETING"
    group = G.registry(value).ensure(snap).groups[made.group_id]
    assert group.members == ("100", "900", "1500")
    assert group.label == "Lab review: accepted runs"
    assert not M.create(value, snap, [record.id for record in records]).changed
    assert G.registry(value).ensure(snap).groups[made.group_id].label == "Lab review: accepted runs"


def test_collapsed_jobs_and_recents_render_the_same_readable_array_label():
    from raven.config import Config
    from raven.controller import App
    from raven.layout import Glyphs, to_text
    from raven.model import Store
    from raven.views import Views

    cfg = Config({"log_lines": 0})
    store = Store(persist=False)
    store.jobs = [job("12_0", "study-running"), job("12_1", "study-waiting", state="PENDING")]
    store.finished = [Finished("12_2", "study-finished", "COMPLETED"),
                      Finished("12_3", "study-failed", "FAILED")]
    value = App(store, None, None, cfg, "alex", interactive=False)
    views = Views(Glyphs(True), cfg)
    value.views_ref = views
    value.table_state["collapsed"] = ["12"]
    try:
        rows, _ = views.compose(store.snapshot(), value, 400, 60)
        assert len(value.visible_ids) == 1 and len(value.recent_ids) == 1
        for tab, jid in (("jobs", value.visible_ids[0]), ("recent", value.recent_ids[0])):
            meta = G.metadata_for_record(value, tab, jid)
            assert meta.collapsed and meta.group.label == "study / Array 12"
            assert meta.group.id == "array:12"
        assert to_text(rows, 400).count("study / Array 12") == 2
    finally:
        if value.research:
            value.research.close()
