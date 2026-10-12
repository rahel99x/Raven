"""Cold accounting, fresh batch launches and safe, disjoint summaries."""
from dataclasses import replace

import pytest

from raven import job_groups as G
from raven.model import Finished, Job
from raven.slurm import (CommandError, GROUP_FMT, GROUP_WIRE_MARKER, JOB_FMT, SACCT_FIELDS,
                         SACCT_LEGACY_FIELDS, Slurm, parse_group, parse_jobs, parse_sacct)


def record(jid, **changes):
    values = dict(id=str(jid), name="train_1", partition="gpu", state="RUNNING", user="alex",
                  account="lab", workdir="/project", submit="2026-10-09T12:00:00", command="/project/train.sh")
    values.update(changes)
    return Job(**values)


def accounting(jid, **changes):
    values = dict(id=str(jid), name="train_1", state="COMPLETED", user="alex", account="lab",
                  workdir="/project", submit="2026-10-09T12:00:00")
    values.update(changes)
    return Finished(**values)


def test_three_historical_jobs_group_on_cold_start_without_live_details_or_commands():
    records = [accounting(100 + n, name=f"train_{n}", cpus=2 ** n) for n in range(3)]
    group, = G.Registry().ensure({"finished": records}).groups.values()
    assert group.members == ("100", "101", "102")
    assert group.label == "Launch train" and group.confidence == "likely"
    assert "30 seconds" in group.reason


@pytest.mark.parametrize("field", ["user", "account", "workdir", "submit"])
def test_history_needs_owner_and_project_and_time(field):
    records = [accounting(n, **{field: ""}) for n in range(3)]
    assert not G.Registry().ensure({"finished": records}).groups


def test_dense_interleaved_sweep_varied_arguments_resources_and_names():
    records = [record(100 + 3*n, name=f"train_{n}", cpus=2 ** n,
                      command=f"/project/train.sh --seed {n}", submit=f"2026-10-09T12:00:{n*15:02d}")
               for n in range(3)]
    group, = G.Registry().ensure({"jobs": records}).groups.values()
    assert group.members == ("100", "103", "106")


@pytest.mark.parametrize("numbers", [(100, 101, 110), (100, 108, 116), (100, 200, 300)])
def test_sparse_ids_do_not_become_a_sweep(numbers):
    records = [accounting(number) for number in numbers]
    assert not G.Registry().ensure({"finished": records}).groups


def test_command_script_conflicts_cannot_form_sweep():
    records = [record(100 + n, command=f"/project/other{n}.sh") for n in range(3)]
    assert not G.Registry().ensure({"jobs": records}).groups


def test_inline_python_commands_do_not_share_only_interpreter_identity():
    records = [record(100 + n, command=f"python -c 'print({n})'") for n in range(3)]
    assert not G.Registry().ensure({"jobs": records}).groups


def test_anchored_sweep_does_not_chain_across_30_seconds():
    records = [accounting(100+n, submit=f"2026-10-09T12:00:{n*10:02d}") for n in range(5)]
    index = G.Registry().ensure({"finished": records})
    assert index.for_job("100").members == ("100", "101", "102", "103")
    assert index.for_job("104") is None


def test_live_dependency_pipeline_groups_different_names_and_scripts():
    records = [record(100, name="prepare", command="/project/prepare.sh"),
               record(102, name="fit", command="/project/fit.sh", dependency="afterok:100(unfulfilled)"),
               record(104, name="report", command="/project/report.sh", dependency="afterany:102")]
    group, = G.Registry().ensure({"jobs": records}).groups.values()
    assert group.kind == "dependency" and group.members == ("100", "102", "104")
    assert group.confidence == "likely"


@pytest.mark.parametrize("changes", [dict(account="another"), dict(workdir="/other"), dict(user="bea"),
                                    dict(cluster="other"), dict(submit="2026-10-09T12:01:01"), dict(id="400")])
def test_dependency_alone_does_not_merge_other_scopes_or_launches(changes):
    records = [record(100), replace(record(101, name="report", dependency="afterok:100"), **changes)]
    assert not G.Registry().ensure({"jobs": records}).groups


def test_dependency_chain_cannot_extend_window_transitively():
    records = [record(100+n, name=f"stage{n}", dependency=f"afterok:{99+n}" if n else "",
                      submit=f"2026-10-09T12:{n//2:02d}:{30*(n%2):02d}") for n in range(4)]
    index = G.Registry().ensure({"jobs": records})
    assert index.for_job("100").members == ("100", "101", "102")
    assert index.for_job("103") is None


def test_explicit_comments_support_new_queue_and_cold_history():
    records = [record(100, comment="launch:experiment-a"), accounting(900, name="report", comment="launch:experiment-a")]
    group, = G.Registry().ensure({"jobs": records[:1], "finished": records[1:]}).groups.values()
    assert group.kind == "explicit" and group.members == ("100", "900")


def test_comment_must_be_one_full_valid_marker_not_free_text():
    records = [record(100, comment="note launch:a"), record(900, comment="note launch:a")]
    assert not G.Registry().ensure({"jobs": records}).groups


def test_fold_persists_on_growth_backfill_and_completion_without_details():
    registry = G.Registry()
    initial = [record(101+n, name=f"train_{n}", submit=f"2026-10-09T12:00:0{n+1}") for n in range(3)]
    group, = registry.ensure({"jobs": initial}).groups.values()
    registry.fold(group.id, True)
    completed = accounting(101, name=initial[0].name, submit=initial[0].submit)
    index = registry.ensure({"jobs": [record(100, name="train_9"), *initial[1:]], "finished": [completed]})
    current = index.for_job("100")
    assert current.id == group.id and registry.is_collapsed(current)
    assert current.members == ("100", "101", "102", "103")


def test_single_original_group_split_cannot_reuse_same_id_for_both_components():
    registry = G.Registry()
    records = [record(100+n) for n in range(6)]
    original, = registry.ensure({"jobs": records}).groups.values()
    registry.fold(original.id, True)
    records[2].command = "/other.sh"
    records[3].command = "/other.sh"
    index = registry.ensure({"jobs": records})
    # The two separated same-script pairs must not overwrite each other.
    assert set(index.by_job) == {str(100+n) for n in range(6)}
    assert len(index.groups) == 3


def test_mutating_state_and_reason_changes_summary_without_reinferring():
    records = [record(100), record(101)]
    registry = G.Registry()
    index = registry.ensure({"jobs": records})
    assert G.status_counts(records)["running"] == 2
    records[1].state, records[1].reason = "PENDING", "DependencyNeverSatisfied"
    assert registry.ensure({"jobs": records}) is index
    counts = G.status_counts(records)
    assert counts["running"] == counts["blocked"] == 1
    assert not counts["pending"] and not counts["dependent"]


def test_status_buckets_are_disjoint_and_repeated_rows_count_once():
    records = [record(100), record(101, state="PENDING", reason="Resources"),
               record(102, state="PENDING", reason="Dependency"),
               record(103, state="PENDING", reason="DependencyNeverSatisfied"),
               record(104, state="COMPLETED"), record(105, state="CANCELLED by 1000"),
               record(106, state="OUT_OF_MEMORY"), record(107, state="SUSPENDED")]
    counts = G.status_counts([*records, records[0]])
    assert all(count == 1 for count in counts.values())
    assert "1 dependency never satisfied" in G.summary(records)
    assert all(ord(c) < 128 for text, _ in G.summary_segments(records, ascii_=True) for c in text)


def test_compressed_array_counts_are_labelled_records_without_expansion():
    records = [record("100_[1-1000000000]", state="PENDING"), record("100_0")]
    assert sum(G.status_counts(records).values()) == 2
    assert G.summary(records).endswith("(records)")


def acct_line(jid="100", *, comment="launch:a", workdir="/project", trailing=True):
    fields = [jid, "train", "COMPLETED", "00:01:00", "4", "00:02:00", "1G", "", "2026-10-09T12:00:01",
              "2026-10-09T12:01:01", "gpu", "1", "0:0", "cpu=4", "node", "2026-10-09T12:00:00", workdir, "01:00:00"]
    return "|".join(fields + (["alex", "lab", comment] if trailing else []))


def test_accounting_parser_loads_cold_launch_provenance_and_keeps_old_recordings():
    current, = parse_sacct(acct_line())
    assert (current.user, current.account, current.comment, current.cluster) == ("alex", "lab", "launch:a", "")
    legacy, = parse_sacct(acct_line(trailing=False))
    assert legacy.id == "100" and legacy.state == "COMPLETED"
    assert not legacy.user and not legacy.account and not legacy.comment


@pytest.mark.parametrize("changes", [dict(comment="launch:a|extra"), dict(workdir="/project|injected"), dict(workdir="relative")])
def test_ambiguous_accounting_provenance_never_supplies_a_group_marker(changes):
    current, = parse_sacct(acct_line(**changes))
    assert not current.user and not current.account and not current.comment


def test_actual_source_accounting_groups_historical_batch_without_queue():
    text = "\n".join(acct_line(str(100+n), comment="") for n in range(3))
    group, = G.Registry().ensure({"finished": parse_sacct(text)}).groups.values()
    assert group.members == ("100", "101", "102")


def test_new_queue_wire_marker_prevents_pipe_shift_becoming_launch_evidence():
    fields = ["100", "train", "gpu", "PENDING", "0:00", "01:00:00", "1", "4", "N/A", "", "1G", "N/A",
              "2026-10-09T12:00:00", "Resources", "1", "", "lab", "normal", "N/A", "/project/train.sh", "/project"]
    current, = parse_jobs("|".join([*fields, "launch:a", GROUP_WIRE_MARKER]))
    assert current.workdir == "/project" and current.comment == "launch:a"
    legacy, = parse_jobs("|".join(fields))
    assert legacy.workdir == "/project" and not legacy.comment
    shifted, = parse_jobs("|".join([*fields, "launch:a|other", GROUP_WIRE_MARKER]))
    assert not shifted.workdir and not shifted.comment


def test_sacct_unsupported_optional_fields_retry_once_and_cache_legacy_mode():
    class Backend:
        def __init__(self):
            self.fields = []
        def run(self, command, timeout):
            self.fields.append(command[-1])
            if command[-1] == SACCT_FIELDS:
                raise CommandError("sacct: error: Invalid field requested: Comment")
            return acct_line(trailing=False), 0
    backend = Backend()
    slurm = Slurm(backend, "alex")
    assert slurm.finished(2)[0].id == "100"
    assert slurm.finished(2)[0].id == "100"
    assert backend.fields == [SACCT_FIELDS, SACCT_LEGACY_FIELDS, SACCT_LEGACY_FIELDS]


def test_sacct_transient_errors_do_not_degrade_fields_or_add_retries():
    class Backend:
        def __init__(self):
            self.calls = 0
        def run(self, command, timeout):
            self.calls += 1
            raise CommandError("Slurm database unavailable")
    backend = Backend()
    slurm = Slurm(backend, "alex")
    with pytest.raises(CommandError):
        slurm.finished(2)
    assert backend.calls == 1 and slurm._sacct_group_fields_supported is None


@pytest.mark.parametrize("dependency,expected", [("afterok:123(fulfilled)", "pending"),
                                               ("afterok:123(unfulfilled)", "dependent"),
                                               ("afterok:123(fulfilled),afterany:124", "dependent"),
                                               ("afterok:123(fulfilled),afterany:124(fulfilled)", "pending"),
                                               ("None", "pending"), ("not-a-dependency", "pending"),
                                               ("afterok:garbage", "pending"), ("badkind:123", "pending")])
def test_pending_resources_uses_only_valid_unfulfilled_dependencies(dependency, expected):
    counts = G.status_counts([record(100, state="PENDING", reason="Resources", dependency=dependency)])
    assert counts[expected] == 1 and sum(counts.values()) == 1


def test_accounting_omitted_command_survives_later_partial_detail_refresh():
    records = [record(100), record(101)]
    registry = G.Registry()
    gid = registry.ensure({"jobs": records}).for_job("100").id
    finished = accounting(100)
    snap = {"jobs": records[1:], "finished": [finished]}
    assert registry.ensure(snap).for_job("100").id == gid
    snap["details"] = {"100": {"LogPathSource": "sacct", "Account": "lab"}}
    assert registry.ensure(snap).for_job("100").id == gid
    snap["details"]["100"]["Command"] = ""
    assert registry.ensure(snap).for_job("100") is None


def test_projection_status_cache_is_shared_and_rebuilt_on_next_projection():
    from types import SimpleNamespace
    records = [record(100), record(101)]
    snap = {"jobs": records}
    app = SimpleNamespace(table_state={"groups": True, "collapsed": []})
    G.project_records(app, snap, records)
    gid = G.registry(app).index.for_job("100").id
    G.fold(app, gid, True)
    G.project_records(app, snap, records)
    first = G.metadata_for_record(app, "history", "100").stats
    assert first is G.metadata_for_record(app, "history", "101").stats
    assert G.status_counts(first)["running"] == 2
    with pytest.raises(TypeError):
        first.counts["running"] = 900
    records[1].state, records[1].reason = "PENDING", "DependencyNeverSatisfied"
    G.project_records(app, snap, records)
    second = G.metadata_for_record(app, "history", "100").stats
    assert second is not first and G.status_counts(second)["blocked"] == 1
    assert G.status_counts(first)["running"] == 2
