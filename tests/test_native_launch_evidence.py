"""Launch evidence arrives through existing queue polls, without inspectors."""
from dataclasses import fields
import os

import pytest

from raven.job_groups import Registry
from raven.model import Job
from raven.slurm import FakeBackend, GROUP_FMT, JOB_FMT, Slurm, parse_group, parse_jobs

LEGACY_JOB_FMT = "%i|%j|%P|%T|%M|%l|%D|%C|%b|%N|%m|%S|%V|%r|%Q|%E|%a|%q|%e|%o"
LEGACY_GROUP_FMT = "%i|%u|%j|%P|%T|%M|%l|%D|%C|%b|%r|%Q|%N|%V|%S"


def user_row(jid="100", *, workdir=None, command="/project/train.sh"):
    values = [jid, "experiment", "main", "PENDING", "0:00", "01:00:00", "1", "4", "N/A", "(Priority)",
              "1G", "N/A", "2026-10-08T12:00:00", "Priority", "1", "(null)", "lab", "normal", "N/A", command]
    if workdir is not None:
        values.append(workdir)
    return "|".join(values) + "\n"


def account_row(jid="100", *, user="alex", workdir=None, command="/project/train.sh"):
    values = [jid, user, "experiment", "main", "PENDING", "0:00", "01:00:00", "1", "4", "N/A", "Priority", "1",
              "(Priority)", "2026-10-08T12:00:00", "N/A"]
    if workdir is not None:
        values.extend(("lab", command, workdir))
    return "|".join(values) + "\n"


class QueueBackend:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def run(self, command, timeout=8):
        self.calls.append(list(command))
        assert command[0] == "squeue", f"Evidence triggered an extra scheduler query: {command}"
        return self.text, .001


def test_job_trailing_field_keeps_existing_positional_dataclass_contract():
    names = [field.name for field in fields(Job)]
    assert names[23:25] == ["user", "workdir"]
    assert names[25:27] == ["comment", "cluster"]
    assert names[27:] == ["restarts", "collection_stale"]
    record = Job("7", "legacy", "cpu", "RUNNING")
    assert record.restarts is None and record.collection_stale is False
    assert Job("1", "name", "main", "PENDING").workdir == ""


def test_legacy_twenty_field_user_rows_keep_exact_command_and_unknown_workdir():
    record, = parse_jobs(user_row(command="/project with spaces/train.sh --seed 7"))
    assert record.command == "/project with spaces/train.sh --seed 7"
    assert record.workdir == "" and record.account == "lab"


def test_native_user_queue_has_reliable_owner_absolute_workdir_and_no_extra_poll():
    backend = QueueBackend(user_row(workdir="/project with spaces"))
    record, = Slurm(backend, "alex").jobs()
    assert record.user == "alex" and record.workdir == "/project with spaces"
    assert record.command == "/project/train.sh"
    assert backend.calls == [["squeue", "-u", "alex", "-h", "-o", JOB_FMT]]
    assert JOB_FMT.endswith("|%o|%Z|%k|RAVEN_GROUP_V1")


def test_ordinary_native_jobs_infer_launch_without_inspection_or_filesystem_access(monkeypatch):
    backend = QueueBackend(user_row("100", workdir="/project") + user_row("101", workdir="/project"))
    jobs = Slurm(backend, "alex").jobs()
    monkeypatch.setattr(os.path, "exists", lambda *_args: pytest.fail("Grouping inspected a path"))
    group, = Registry().ensure({"jobs": jobs}).groups.values()
    assert group.kind == "burst" and group.members == ("100", "101")
    assert len(backend.calls) == 1


@pytest.mark.parametrize("workdir", ["relative/project", "Unknown", "N/A", "(null)", "", "/invalid\0path", "/path|ambiguous"])
def test_unusable_or_ambiguous_user_workdir_does_not_become_grouping_evidence(workdir):
    backend = QueueBackend(user_row("100", workdir=workdir) + user_row("101", workdir=workdir))
    records = Slurm(backend, "alex").jobs()
    assert all(record.workdir == "" for record in records)
    assert not Registry().ensure({"jobs": records}).groups


def test_extra_delimiter_in_command_does_not_create_false_workdir():
    record, = parse_jobs(user_row(command="/project/train.sh | /project/another.sh", workdir="/project"))
    assert record.workdir == ""
    assert record.command == ""  # Ambiguous command text is unknown, never a truncated launch recipe.


def test_legacy_fifteen_field_account_rows_remain_compatible():
    record, = parse_group(account_row())
    assert record.user == "alex" and record.submit == "2026-10-08T12:00:00"
    assert record.account == record.command == record.workdir == ""


def test_account_poll_supplies_launch_evidence_for_each_owner_without_extra_queries():
    backend = QueueBackend(account_row("100", workdir="/project with spaces") +
                           account_row("101", workdir="/project with spaces") +
                           account_row("102", user="bea", workdir="/project with spaces"))
    records = Slurm(backend, "alex").group("lab")
    assert backend.calls == [["squeue", "-h", "-A", "lab", "-o", GROUP_FMT]]
    assert GROUP_FMT.endswith("|%a|%o|%Z|%k|RAVEN_GROUP_V1")
    group, = Registry().ensure({"group": records}).groups.values()
    assert group.members == ("100", "101") and "102" not in group.members
    assert all(record.workdir == "/project with spaces" for record in records)


@pytest.mark.parametrize("workdir", ["relative", "Unknown", "/invalid\0path", "/path|ambiguous"])
def test_account_workdir_evidence_has_the_same_absolute_unambiguous_rule(workdir):
    records = parse_group(account_row("100", workdir=workdir) + account_row("101", workdir=workdir))
    assert all(record.workdir == "" for record in records)
    assert not Registry().ensure({"group": records}).groups


def test_fake_backend_uses_native_wire_format_and_current_workdir(tmp_path, monkeypatch):
    root = tmp_path / "project with spaces"
    root.mkdir()
    monkeypatch.chdir(root)
    backend = FakeBackend(user="alex")
    slurm = Slurm(backend, "alex")
    records = slurm.jobs()
    assert records and all(record.user == "alex" and record.workdir == str(root) for record in records)
    account = slurm.group("lab_01")
    assert account and all(record.workdir == str(root) and record.account == "lab_01" and record.command for record in account)
    assert {call[0] for call in backend.calls} <= {"squeue", "scontrol"}
    assert not any("job" in call for call in backend.calls)


def recording(tmp_path, entries):
    import json
    path = tmp_path / "legacy.jsonl"
    path.write_text("\n".join(json.dumps(value) for value in [{"kind": "header", "user": "alex"}, *entries]) + "\n")
    return path


def test_legacy_user_recording_replays_at_exact_time_without_false_launch_evidence(tmp_path):
    from raven.record import ReplayBackend
    legacy = LEGACY_JOB_FMT
    command = ["squeue", "-u", "alex", "-h", "-o", legacy]
    early = user_row("100") + user_row("101")
    path = recording(tmp_path, [{"t": 100, "cmd": command, "out": early, "dt": .01},
                               {"t": 200, "cmd": command, "out": user_row("200"), "dt": .02}])
    replay = ReplayBackend(str(path), paused=True)
    slurm = Slurm(replay, "alex")
    records = slurm.jobs()
    assert [record.id for record in records] == ["100", "101"]
    assert all(record.user == "alex" and not record.workdir for record in records)
    assert not Registry().ensure({"jobs": records}).groups
    replay.clock.seek(200)
    assert [record.id for record in slurm.jobs()] == ["200"]
    assert replay.calls == []


def test_legacy_account_recording_preserves_user_account_scope(tmp_path):
    from raven.record import ReplayBackend
    legacy = LEGACY_GROUP_FMT
    command = ["squeue", "-h", "-A", "lab", "-o", legacy]
    path = recording(tmp_path, [{"t": 100, "cmd": command, "out": account_row("100") + account_row("101")}])
    replay = ReplayBackend(str(path), paused=True)
    records = Slurm(replay, "alex").group("lab")
    assert [record.id for record in records] == ["100", "101"]
    assert all(record.user == "alex" and record.workdir == "" for record in records)
    assert not Registry().ensure({"group": records}).groups


def test_new_recorded_queue_response_takes_priority_over_legacy_alias(tmp_path):
    from raven.record import ReplayBackend
    current = ["squeue", "-u", "alex", "-h", "-o", JOB_FMT]
    legacy = [*current[:-1], LEGACY_JOB_FMT]
    path = recording(tmp_path, [{"t": 100, "cmd": legacy, "out": user_row("100")},
                               {"t": 100, "cmd": current, "out": user_row("200", workdir="/project")}])
    records = Slurm(ReplayBackend(str(path), paused=True), "alex").jobs()
    assert [record.id for record in records] == ["200"] and records[0].workdir == "/project"


def test_new_recorded_queue_error_is_not_hidden_by_successful_legacy_alias(tmp_path):
    from raven.record import ReplayBackend
    from raven.slurm import CommandError
    current = ["squeue", "-u", "alex", "-h", "-o", JOB_FMT]
    path = recording(tmp_path, [{"t": 100, "cmd": [*current[:-1], LEGACY_JOB_FMT], "out": user_row("100")},
                               {"t": 100, "cmd": current, "err": "recorded current-format error"}])
    with pytest.raises(CommandError, match="recorded current-format error"):
        Slurm(ReplayBackend(str(path), paused=True), "alex").jobs()


@pytest.mark.parametrize("requested", [["squeue", "-u", "bea", "-h", "-o", JOB_FMT],
                                       ["squeue", "-u", "alex", "-h", "-o", JOB_FMT + "|%u"],
                                       ["other", "-u", "alex", "-h", "-o", JOB_FMT]])
def test_replay_format_alias_does_not_match_different_scope_or_arbitrary_command(tmp_path, requested):
    from raven.record import ReplayBackend
    from raven.slurm import CommandError
    legacy = ["squeue", "-u", "alex", "-h", "-o", LEGACY_JOB_FMT]
    path = recording(tmp_path, [{"t": 100, "cmd": legacy, "out": user_row("100")}])
    with pytest.raises(CommandError, match="not in the recording"):
        ReplayBackend(str(path), paused=True).run(requested)
