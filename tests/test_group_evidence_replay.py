"""Released recordings survive queue/accounting grouping-field upgrades."""
import json

import pytest

from raven.record import ReplayBackend
from raven.slurm import CommandError, GROUP_FMT, JOB_FMT, SACCT_FIELDS, Slurm


# Literal released formats keep these compatibility checks independent of the
# upgraded constants and the aliases under test.
OLD_JOB = "%i|%j|%P|%T|%M|%l|%D|%C|%b|%N|%m|%S|%V|%r|%Q|%E|%a|%q|%e|%o"
OLD_GROUP = "%i|%u|%j|%P|%T|%M|%l|%D|%C|%b|%r|%Q|%N|%V|%S"
OLD_FINISHED = "JobID,JobName,State,Elapsed,AllocCPUS,TotalCPU,ReqMem,MaxRSS,Start,End,Partition,NNodes,ExitCode,AllocTRES,NodeList,Submit,WorkDir,Timelimit"
FORMATS = (
    (["squeue", "-u", "alex", "-h", "-o", JOB_FMT], OLD_JOB),
    (["squeue", "-u", "alex", "-h", "-o", JOB_FMT], OLD_JOB + "|%Z"),
    (["squeue", "-h", "-A", "lab", "-o", GROUP_FMT], OLD_GROUP),
    (["squeue", "-h", "-A", "lab", "-o", GROUP_FMT], OLD_GROUP + "|%a|%o|%Z"),
    (["sacct", "-u", "alex", "-n", "-P", "-S", "now-48hours", "-o", SACCT_FIELDS], OLD_FINISHED),
)


def replay(tmp_path, entries):
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(entry) for entry in [
        {"kind": "header", "user": "alex"}, *entries,
    ]) + "\n", encoding="utf-8")
    return ReplayBackend(str(path), paused=True)


@pytest.mark.parametrize("current,old_format", FORMATS)
def test_released_wire_format_replays_exact_observation_and_recorded_time(tmp_path, current, old_format):
    old_command = [*current[:-1], old_format]
    backend = replay(tmp_path, [
        {"t": 100, "cmd": old_command, "out": "older observations\n", "dt": .1},
        {"t": 200, "cmd": old_command, "out": "newer observations\n", "dt": .2},
    ])
    assert backend.run(current) == ("older observations\n", .1)
    backend.clock.seek(199)
    assert backend.run(current) == ("older observations\n", .1)
    backend.clock.seek(200)
    assert backend.run(current) == ("newer observations\n", .2)
    assert backend.calls == []


@pytest.mark.parametrize("current,old_format", FORMATS)
@pytest.mark.parametrize("error", [False, True])
def test_current_recording_wins_over_legacy_response_including_errors(tmp_path, current, old_format, error):
    record = {"t": 100, "cmd": current}
    record["err" if error else "out"] = "current response"
    backend = replay(tmp_path, [
        {"t": 100, "cmd": [*current[:-1], old_format], "out": "legacy response"}, record,
    ])
    if error:
        with pytest.raises(CommandError, match="current response"):
            backend.run(current)
    else:
        assert backend.run(current) == ("current response", 0.0)


@pytest.mark.parametrize("current,old_format", FORMATS)
def test_alias_keeps_scope_arguments_and_command_exact(tmp_path, current, old_format):
    backend = replay(tmp_path, [{"t": 100, "cmd": [*current[:-1], old_format], "out": "observations"}])
    scope = "-A" if "-A" in current else "-u"
    other_user = list(current)
    other_user[current.index(scope) + 1] = "different"
    requests = [other_user, [*current[:-1], current[-1] + "extra"],
                ["other-command", *current[1:]], [*current, "--extra"]]
    if "-S" in current:
        different_interval = list(current)
        different_interval[current.index("-S") + 1] = "now-72hours"
        requests.append(different_interval)
    for request in requests:
        with pytest.raises(CommandError, match="not in the recording"):
            backend.run(request)


@pytest.mark.parametrize("error", [False, True])
def test_newest_released_queue_format_wins_over_older_alias(tmp_path, error):
    command = ["squeue", "-u", "alex", "-h", "-o", JOB_FMT]
    latest = {"t": 100, "cmd": [*command[:-1], OLD_JOB + "|%Z"]}
    latest["err" if error else "out"] = "newer released format"
    backend = replay(tmp_path, [
        {"t": 100, "cmd": [*command[:-1], OLD_JOB], "out": "oldest format"}, latest,
    ])
    if error:
        with pytest.raises(CommandError, match="newer released format"):
            backend.run(command)
    else:
        assert backend.run(command)[0] == "newer released format"


def test_cold_legacy_history_still_loads_without_inventing_missing_provenance(tmp_path):
    command = ["sacct", "-u", "alex", "-n", "-P", "-S", "now-48hours", "-o", OLD_FINISHED]
    fields = ["101", "train", "COMPLETED", "00:01:00", "4", "00:02:00", "1G", "512M",
              "2026-10-08T12:00:00", "2026-10-08T12:01:00", "cpu", "1", "0:0", "cpu=4",
              "node1", "2026-10-08T11:59:00", "/project", "00:05:00"]
    backend = replay(tmp_path, [{"t": 100, "cmd": command, "out": "|".join(fields) + "\n"}])
    record, = Slurm(backend, "alex").finished(2)
    assert (record.id, record.name, record.state, record.cpus) == ("101", "train", "COMPLETED", 4)
    assert record.workdir == "/project"
    assert record.user == record.account == record.comment == ""
    assert backend.calls == []


@pytest.mark.parametrize("exact_error", [False, True])
def test_allocation_only_replay_upgrade_preserves_scope_and_exact_precedence(tmp_path, exact_error):
    current=["sacct","-u","alex","-X","-n","-P","-S","now-48hours","-o",SACCT_FIELDS]
    old=[part for part in current if part!="-X"]
    old[-1]=OLD_FINISHED
    exact={"t":200,"cmd":current,"err":"current accounting failed"} if exact_error else {"t":200,"cmd":current,"out":"current accounting"}
    backend=replay(tmp_path,[{"t":100,"cmd":old,"out":"legacy accounting"},exact])
    # Once this exact command has a recording, its future result must not be
    # borrowed early through an alias at an earlier replay time.
    with pytest.raises(CommandError,match="at or before"):
        backend.run(current)
    backend.clock.seek(200)
    if exact_error:
        with pytest.raises(CommandError,match="current accounting failed"):backend.run(current)
    else:assert backend.run(current)[0]=="current accounting"
    for flag,value in (("-u","other"),("-S","now-72hours")):
        changed=list(current);changed[changed.index(flag)+1]=value
        with pytest.raises(CommandError,match="not in the recording"):backend.run(changed)
