"""Historical log discovery stays bound to the selected accounting record."""
from __future__ import annotations

from types import SimpleNamespace
from dataclasses import replace

import pytest

from raven.model import Finished, Job, Store
from raven.sampler import Sampler
from raven.slurm import CommandError, FakeBackend, MAX_LOG_DETAIL_BYTES, SACCT_FIELDS, SACCT_LOG_FIELDS, Slurm


IDENTITY = {"submit": "2026-10-01T12:00:00", "start": "2026-10-01T12:01:00", "user": "alice"}


def accounting_reply(text):
    # Match the requested Submit/Start/Cluster columns while preserving malformed
    # fixture rows verbatim so corruption assertions still exercise the parser.
    rows = []
    for line in text.splitlines():
        fields = line.split("|")
        if len(fields) == 8 and fields[-1] == "":
            fields.pop()
        if len(fields) == 7:
            fields.extend((IDENTITY["submit"], IDENTITY["start"], "alpha"))
            line = "|".join(fields)
        rows.append(line)
    return "\n".join(rows)


class AccountingBackend:
    def __init__(self, result="", error=None):
        self.result, self.error = result, error
        self.calls = []

    def run(self, command, timeout):
        self.calls.append((command, timeout))
        if self.error:
            raise CommandError(self.error)
        return accounting_reply(self.result), 0.01


class PurgedAccountingBackend(AccountingBackend):
    def run(self, command, timeout):
        if command[0] == "scontrol":
            self.calls.append((command, timeout))
            raise CommandError("scontrol: Invalid job id specified")
        return super().run(command, timeout)


def sampler_for(slurm, finished, previous=None):
    store = Store(persist=False)
    store.finished = [replace(item, submit=IDENTITY["submit"], start=IDENTITY["start"], user="alice")
                      if isinstance(slurm, Slurm) else item for item in finished]
    store.details.update(previous or {})
    sampler = Sampler(slurm, store, {}, [], workers=1)
    return sampler, store


def test_historical_paths_query_is_lazy_and_does_not_change_bulk_history_fields():
    backend = AccountingBackend("77|77|failed job|/scratch/run 1|logs/%j.out|logs/%j.err|alice|\n")
    slurm = Slurm(backend, "alice", timeout=7)
    assert backend.calls == []
    result = slurm.historical_details("77", identity=IDENTITY)
    assert result == {"JobId": "77", "JobIDRaw": "77", "JobName": "failed job", "WorkDir": "/scratch/run 1",
                      "StdOut": "logs/%j.out", "StdErr": "logs/%j.err", "User": "alice", "LogPathSource": "sacct",
                      "SubmitTime": IDENTITY["submit"], "StartTime": IDENTITY["start"], "Cluster": "alpha"}
    assert backend.calls == [(["sacct", "-j", "77", "-u", "alice", "-X", "-D", "-S", "2026-09-29T00:00:00",
                               "-E", "2026-10-03T00:00:00", "-n", "-P", "-o", SACCT_LOG_FIELDS], 7)]
    assert "StdOut" not in SACCT_FIELDS and "StdErr" not in SACCT_FIELDS


def test_array_task_paths_cannot_come_from_parent_sibling_or_step():
    backend = AccountingBackend("\n".join([
        "100|100|parent|/wrong|parent.out|parent.err|alice",
        "100_1|101|sibling|/wrong|sibling.out|sibling.err|alice",
        "100_2.batch|102.batch|batch|/wrong|batch.out|batch.err|alice",
        "100_2|102|task|/correct|logs/%A_%a_%j.out|logs/%A_%a_%j.err|alice",
    ]))
    result = Slurm(backend, "alice").historical_details("100_2", identity=IDENTITY)
    assert result["JobIDRaw"] == "102"
    assert (result["ArrayJobId"], result["ArrayTaskId"]) == ("100", "2")
    assert result["WorkDir"] == "/correct"
    assert result["StdOut"] == "logs/%A_%a_%j.out"


@pytest.mark.parametrize("text", ["", "8|8|wrong|/wrong|wrong.out|wrong.err|alice\n",
                                     "7.batch|7.batch|batch|/wrong|wrong.out|wrong.err|alice\n",
                                     "7|7|incomplete|/wrong\n",
                                     "7|7|ambiguous|/run|literal|pipe.out|other.err|alice\n"])
def test_absent_exact_record_never_uses_another_jobs_paths(text):
    result = Slurm(AccountingBackend(text), "alice").historical_details("7", identity=IDENTITY)
    assert result["JobId"] == "7"
    assert "LogPathError" in result
    assert "StdOut" not in result and "StdErr" not in result


def test_accounting_null_paths_are_explicitly_missing():
    result = Slurm(AccountingBackend("7|7|cancelled|/run|(null)|Unknown|alice\n"), "alice").historical_details("7", identity=IDENTITY)
    assert result["WorkDir"] == "/run"
    assert "StdOut" not in result and "StdErr" not in result
    assert "no stdout/stderr paths" in result["LogPathError"]


@pytest.mark.parametrize("second", ["7|7|reused|/other|other.out|other.err|alice",
                                      "7|7|same|/run|first.out|first.err|alice",
                                      "7|7|incomplete|/other"])
def test_reused_or_duplicate_exact_ids_never_choose_the_first_accounting_paths(second):
    backend = AccountingBackend("7|7|first|/run|first.out|first.err|alice\n" + second + "\n")
    result = Slurm(backend, "alice").historical_details("7", identity=IDENTITY)
    assert result["JobId"] == "7"
    assert "multiple accounting records" in result["LogPathError"]
    assert "ambiguous" in result["LogPathError"]
    assert "StdOut" not in result and "StdErr" not in result


@pytest.mark.parametrize("reply", ["x" * (MAX_LOG_DETAIL_BYTES + 1), "é" * (MAX_LOG_DETAIL_BYTES // 2 + 1)])
def test_oversized_accounting_replies_are_rejected_without_parsing(reply):
    with pytest.raises(CommandError, match="1 MiB"):
        Slurm(AccountingBackend(reply), "alice").historical_details("7", identity=IDENTITY)


def test_oversized_accounting_paths_are_never_truncated_to_a_different_file():
    result = Slurm(AccountingBackend(f"7|7|huge|/run|{'x' * 4097}|other.err|alice\n"), "alice").historical_details("7", identity=IDENTITY)
    assert "oversized" in result["LogPathError"]
    assert "StdOut" not in result and "StdErr" not in result


@pytest.mark.parametrize("field", ["StdOut", "StdErr"])
def test_older_slurm_unsupported_log_columns_are_checked_only_once(field):
    backend = AccountingBackend(error=f'sacct exit 1: Invalid field requested: "{field}"')
    slurm = Slurm(backend, "alice")
    first, second = slurm.historical_details("7", identity=IDENTITY), slurm.historical_details("8", identity=IDENTITY)
    assert len(backend.calls) == 1
    assert first["JobId"] == "7" and second["JobId"] == "8"
    assert first["LogPathError"] == second["LogPathError"]


@pytest.mark.parametrize("error", ["sacct: timed out after 8s", "ssh login: connection failed",
                                        'sacct exit 1: Invalid field requested: "JobIDRaw"'])
def test_transient_or_unrelated_errors_do_not_disable_future_accounting_queries(error):
    backend = AccountingBackend(error=error)
    slurm = Slurm(backend, "alice")
    with pytest.raises(CommandError, match=".*"):
        slurm.historical_details("7", identity=IDENTITY)
    backend.error = None
    backend.result = "7|7|retry|/run|retry.out|retry.err|alice\n"
    assert slurm.historical_details("7", identity=IDENTITY)["StdOut"] == "retry.out"
    assert len(backend.calls) == 2


def test_fake_history_uses_the_same_accounting_log_query(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    slurm = Slurm(FakeBackend("alice"), "alice")
    finished = slurm.finished(2)
    result = slurm.record_details(finished[0], historical=True)
    assert result["WorkDir"] == str(tmp_path)
    assert finished[0].id in result["StdOut"]
    assert result["StdOut"] == result["StdErr"]


def test_finished_selection_falls_back_after_scontrol_purge_and_is_cached():
    calls = []

    def details(jid):
        calls.append(("scontrol", jid))
        raise CommandError("scontrol: Invalid job id specified")

    def historical(jid):
        calls.append(("sacct", jid))
        return {"JobId": jid, "WorkDir": "/historical/run", "StdOut": "/historical/failed.out", "StdErr": "/historical/failed.err"}

    sampler, store = sampler_for(SimpleNamespace(details=details, historical_details=historical),
                                 [Finished("7", name="failed", state="FAILED", workdir="/historical/run")])
    try:
        sampler.select("7")
        sampler.src_details()
        sampler.src_details()
        assert calls == [("scontrol", "7"), ("sacct", "7")]
        assert store.details["7"]["StdOut"] == "/historical/failed.out"
        assert store.details["7"]["JobState"] == "FAILED"
    finally:
        sampler.shutdown()


@pytest.mark.parametrize("initial", ["", "7|7|delayed|/run|(null)|(null)|alice\n"])
@pytest.mark.parametrize("refresh", ["cadence", "manual"])
def test_delayed_accounting_paths_retry_then_cache_success(initial, refresh):
    backend = PurgedAccountingBackend(initial)
    sampler, store = sampler_for(Slurm(backend, "alice"), [Finished("7", state="FAILED", workdir="/run")])
    for health in store.health.values():
        health.enabled = health.name == "details"
    try:
        sampler.select("7")
        sampler.round(now=100, wait=True)
        assert "LogPathError" in store.details["7"]
        assert "7" not in sampler._historical_details
        assert len(backend.calls) == 2
        backend.result = "7|7|delayed|/run|late.out|late.err|alice\n"
        if refresh == "manual":
            sampler.refresh_all()
            next_sample = 101
        else:
            sampler.round(now=129, wait=True)
            assert len(backend.calls) == 2, "retry must respect the normal details cadence"
            next_sample = 130
        sampler.round(now=next_sample, wait=True)
        assert store.details["7"]["StdOut"] == "late.out"
        assert store.details["7"]["StdErr"] == "late.err"
        assert "LogPathError" not in store.details["7"]
        assert "7" in sampler._historical_details
        assert len(backend.calls) == 4
        sampler.round(now=next_sample + 30, wait=True)
        sampler.refresh_all()
        sampler.round(now=next_sample + 31, wait=True)
        assert len(backend.calls) == 4, "successful immutable paths must remain cached"
    finally:
        sampler.shutdown()


def test_retrying_unsupported_history_does_not_repeat_invalid_accounting_commands():
    backend = PurgedAccountingBackend(error='sacct: Invalid field requested: "StdOut"')
    sampler, store = sampler_for(Slurm(backend, "alice"), [Finished("7", state="FAILED", workdir="/run")])
    try:
        sampler.select("7")
        sampler.src_details()
        sampler.src_details()
        sampler.refresh_all()
        sampler.src_details()
        assert sum(command[0] == "sacct" for command, timeout in backend.calls) == 1
        assert "does not expose" in store.details["7"]["LogPathError"]
        assert "StdOut" not in store.details["7"]
    finally:
        sampler.shutdown()


def test_real_scontrol_paths_survive_job_completion_without_additional_queries():
    slurm = SimpleNamespace(details=lambda jid: pytest.fail("cached real paths must not be refetched"),
                            historical_details=lambda jid: pytest.fail("cached real paths must not be guessed"))
    sampler, store = sampler_for(slurm, [Finished("7", name="finished", state="TIMEOUT", workdir="/run")],
                                 {"7": {"StdOut": "/actual.out", "StdErr": "/actual.err", "JobState": "RUNNING"}})
    try:
        sampler.select("7")
        sampler.src_details()
        assert store.details["7"]["StdOut"] == "/actual.out"
        assert store.details["7"]["StdErr"] == "/actual.err"
        assert store.details["7"]["JobState"] == "TIMEOUT"
        assert store.details["7"]["WorkDir"] == "/run"
    finally:
        sampler.shutdown()


def test_previous_details_from_another_attempt_cannot_seed_completed_cache():
    backend = PurgedAccountingBackend("7|7|new|/run|correct.out|correct.err|alice")
    sampler, store = sampler_for(Slurm(backend, "alice"), [Finished("7", state="COMPLETED")],
                                 {"7": {"JobId": "7", "SubmitTime": "2025-01-01T00:00:00",
                                        "StdOut": "stale.out", "UserId": "alice(1000)"}})
    try:
        sampler.select("7")
        sampler.src_details()
        assert store.details["7"]["StdOut"] == "correct.out"
        assert any(command[0] == "sacct" for command, _ in backend.calls)
    finally:
        sampler.shutdown()


def test_inflight_accounting_identity_change_does_not_publish_old_paths():
    backend = PurgedAccountingBackend("7|7|old|/run|old.out|old.err|alice")
    sampler, store = sampler_for(Slurm(backend, "alice"), [Finished("7", state="COMPLETED")])
    original = backend.run
    def change_record(command, timeout):
        if command[0] == "sacct":
            store.finished = [replace(store.finished[0], submit="2026-10-02T12:00:00")]
        return original(command, timeout)
    backend.run = change_record
    try:
        sampler.select("7")
        sampler.src_details()
        assert "7" not in store.details
    finally:
        sampler.shutdown()


def test_unsupported_history_columns_preserve_known_workdir_and_missing_path_error():
    def details(jid):
        raise CommandError("scontrol: Invalid job id specified")

    sampler, store = sampler_for(SimpleNamespace(details=details,
                                 historical_details=lambda jid: {"JobId": jid, "LogPathError": "unsupported accounting paths"}),
                                 [Finished("7", name="cancelled", state="CANCELLED", workdir="/real/run")])
    try:
        sampler.select("7")
        sampler.src_details()
        assert store.details["7"]["WorkDir"] == "/real/run"
        assert "StdOut" not in store.details["7"]
        assert store.details["7"]["LogPathError"] == "unsupported accounting paths"
    finally:
        sampler.shutdown()


def test_live_job_details_continue_refreshing_instead_of_using_history_cache():
    calls = []
    slurm = SimpleNamespace(details=lambda jid: calls.append(jid) or {"JobId": jid, "StdOut": f"{len(calls)}.out"})
    sampler, store = sampler_for(slurm, [])
    store.jobs = [Job("7", "running", "main", "RUNNING")]
    try:
        sampler.select("7")
        sampler.src_details()
        sampler.src_details()
        assert calls == ["7", "7"]
        assert store.details["7"]["StdOut"] == "2.out"
    finally:
        sampler.shutdown()


def test_historical_detail_cache_and_published_records_are_bounded():
    slurm = SimpleNamespace(details=lambda jid: {"JobId": jid, "StdOut": f"/logs/{jid}.out"})
    sampler, store = sampler_for(slurm, [Finished(str(i), state="FAILED") for i in range(60)])
    try:
        for i in range(60):
            sampler.select(str(i))
            sampler.src_details()
        assert len(sampler._historical_details) == 50
        assert len(store.details) == 50
        assert "0" not in store.details and "59" in store.details
    finally:
        sampler.shutdown()


def test_inflight_historical_fetch_cannot_remove_newly_selected_job_details():
    def details(jid):
        sampler.select("8")
        store.details["8"] = {"StdOut": "/correct-eight.out"}
        return {"JobId": jid, "StdOut": "/old-seven.out"}

    sampler, store = sampler_for(SimpleNamespace(details=details), [Finished("7", state="FAILED")])
    try:
        sampler.select("7")
        sampler.src_details()
        assert "7" not in store.details
        assert store.details["8"]["StdOut"] == "/correct-eight.out"
    finally:
        sampler.shutdown()


def test_unknown_submission_uses_bounded_horizon_but_does_not_borrow_paths():
    backend = AccountingBackend("7|7|unknown|/run|other.out|other.err|alice")
    slurm = Slurm(backend, "alice")
    result = slurm.historical_details("7")
    assert "no verified submission time" in result["LogPathError"]
    assert "StdOut" not in result
    command = backend.calls[0][0]
    assert command[command.index("-S") + 1] == "now-14days"
    assert command[command.index("-E") + 1] == "now"
    assert "-D" in command
    assert slurm.accounting_diagnostics["details"]["commands"] == 1


def test_explicit_bounded_history_lookup_can_locate_missing_attempt_identity():
    backend = AccountingBackend("7|7|unknown|/run|exact.out|exact.err|alice")
    slurm = Slurm(backend, "alice")
    slurm.set_history_range("2026-10-01T00:00:00", "2026-10-02T00:00:00")
    result = slurm.historical_details("7")
    assert result["StdOut"] == "exact.out" and result["SubmitTime"] == IDENTITY["submit"]
    assert slurm.accounting_diagnostics["details"]["window"] == "explicit history range"
    slurm.set_history_range()
    assert "StdOut" not in slurm.historical_details("7")


def test_old_attempt_window_is_centered_on_retained_submission_not_today():
    backend = AccountingBackend()
    slurm = Slurm(backend, "alice")
    slurm.historical_details("7", identity={**IDENTITY, "submit": "2010-01-03T23:59:59"})
    command = backend.calls[0][0]
    assert command[command.index("-S") + 1] == "2010-01-01T00:00:00"
    assert command[command.index("-E") + 1] == "2010-01-05T00:00:00"


@pytest.mark.parametrize("field,value", [("submit", "2026-10-02T12:00:00"),
                                         ("start", "2026-10-01T13:00:00"), ("user", "other"), ("cluster", "other")])
def test_accounting_identity_mismatch_is_unknown_not_another_attempt(field, value):
    backend = AccountingBackend("7|7|recycled|/wrong|wrong.out|wrong.err|alice")
    result = Slurm(backend, "alice").historical_details("7", identity={**IDENTITY, field: value})
    assert "LogPathError" in result and "StdOut" not in result


def test_equivalent_aware_timestamps_match_but_remote_zone_is_never_guessed():
    backend = AccountingBackend("7|7|job|/run|exact.out|exact.err|alice|2026-10-01T12:00:00Z|2026-10-01T12:01:00Z|alpha")
    slurm = Slurm(backend, "alice")
    result = slurm.historical_details("7", identity={"submit": "2026-10-01T05:00:00-07:00", "start": "2026-10-01T05:01:00-07:00"})
    assert result["StdOut"] == "exact.out"
    result = slurm.historical_details("7", identity=IDENTITY)
    assert "cannot verify" in result["LogPathError"] and "StdOut" not in result


def test_recycled_live_scontrol_record_cannot_supply_historical_logs():
    class RecycledBackend(AccountingBackend):
        def run(self, command, timeout):
            if command[0] == "scontrol":
                return "JobId=7 UserId=alice(1000) SubmitTime=2026-10-02T12:00:00 StartTime=2026-10-02T12:01:00 StdOut=/wrong.out", 0
            return super().run(command, timeout)
    sampler, store = sampler_for(Slurm(RecycledBackend("7|7|old|/right|right.out|right.err|alice"), "alice"),
                                 [Finished("7", state="COMPLETED")])
    try:
        sampler.select("7")
        sampler.src_details()
        assert store.details["7"]["StdOut"] == "right.out"
    finally:
        sampler.shutdown()


@pytest.mark.parametrize("start,end", [("1970-01-01T00:00:00", "2026-10-01T00:00:00"),
                                        ("2026-10-02T00:00:00", "2026-10-01T00:00:00"), ("bad", "bad")])
def test_explicit_history_interval_has_finite_valid_bounds(start, end):
    with pytest.raises(ValueError, match="3650 days"):
        Slurm(AccountingBackend(), "alice").set_history_range(start, end)


REAL_2311_STEPS = (
    "1|Raven spaces = sample|COMPLETED|00:00:06|00:00.002|||||0:0|raven-ci|2026-10-18T00:05:20|2026-10-18T00:05:11|2026-10-18T00:05:14|raven|raven-ci\n"
    "1.batch|batch|COMPLETED|00:00:06|00:00.002|5196K|raven-ci|0|1|0:0|raven-ci|2026-10-18T00:05:20|2026-10-18T00:05:14|2026-10-18T00:05:14||raven-ci\n"
)


def test_real_2311_empty_step_owner_inherits_only_unique_verified_parent():
    backend = AccountingBackend(REAL_2311_STEPS)
    backend.probe_json = False
    reader = Slurm(backend, "raven")
    identity = dict(submit="2026-10-18T00:05:11", start="2026-10-18T00:05:14", user="raven", cluster="raven-ci")
    rows = reader._accounting_attempt_rows(REAL_2311_STEPS, "1", 12, identity, steps=True)
    assert [row[0] for row in rows] == ["1", "1.batch"]
    # Missing or contradictory parent evidence never licenses its child.
    assert reader._accounting_attempt_rows(REAL_2311_STEPS.split("\n", 1)[1], "1", 12, identity, steps=True) == []
    assert reader._accounting_attempt_rows(REAL_2311_STEPS.replace("|raven|", "|another|"), "1", 12, identity, steps=True) == []
    child_conflict = REAL_2311_STEPS.replace("||raven-ci\n", "|another|raven-ci\n")
    assert [row[0] for row in reader._accounting_attempt_rows(child_conflict, "1", 12, identity, steps=True)] == ["1"]
    outside = REAL_2311_STEPS.replace("2026-10-18T00:05:14|2026-10-18T00:05:14||", "2026-10-18T00:05:21|2026-10-18T00:05:21||")
    assert [row[0] for row in reader._accounting_attempt_rows(outside, "1", 12, identity, steps=True)] == ["1"]
    with pytest.raises(CommandError, match="Multiple accounting steps"):
        reader._accounting_attempt_rows(REAL_2311_STEPS + REAL_2311_STEPS.split("\n", 1)[1], "1", 12, identity, steps=True)
