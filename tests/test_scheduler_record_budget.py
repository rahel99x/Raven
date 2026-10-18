"""Scheduler record admission uses data records, independent of JSON layout.

The Slurm replies are recorded fixtures or explicitly fixture-derived expansions;
these tests do not execute a live scheduler.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import shlex
import threading

import pytest

from raven import site_policy as P
from raven.platform_session import GovernedBackend, wrap_backend
from raven.shared_collection import SharedQueryCache
from raven.slurm import CommandError, Slurm, parse_sacct


CORPUS = Path(__file__).parent / "fixtures" / "slurm" / "recorded"


def recorded(name, version="24.05.8"):
    return (CORPUS / version / "final" / (name + ".stdout.txt")).read_text()


def expanded_accounting():
    value = json.loads(recorded("accounting-json"))
    value["jobs"] = [copy.deepcopy(value["jobs"][0]) for _ in range(20)]
    return value


class Backend:
    probe_json = True

    def __init__(self, response, legacy=None):
        self.response = response
        self.legacy = legacy
        self.commands = []

    def run(self, argv, timeout):
        self.commands.append(list(argv))
        if argv[-1] == "--help":
            return "--json optional structured output\n", .001
        if self.legacy is not None and not any(arg.startswith("--json") for arg in argv):
            return self.legacy, .001
        return self.response, .001


@pytest.fixture
def govern(tmp_path):
    wrappers = []

    def create(response, *, records=10000, byte_limit=4 << 20, legacy=None, cache=None):
        backend = Backend(response, legacy)
        policy = {
            "schema": P.SCHEMA, "id": "record-budget-test", "revision": "1",
            "sources": ["slurm-cli"],
            "operations": ["queue", "accounting", "nodes", "statistics", "configuration", "job-detail"],
            "limits": {"min_interval": .5, "max_duration": 5, "max_bytes": byte_limit,
                       "max_records": records, "max_inflight": 2},
            "allocation_required": False, "allow_intrusive": False,
        }
        if cache is None:
            cache = SharedQueryCache(tmp_path / str(len(wrappers)))
        wrapper = GovernedBackend(backend, policy, cache, {"cluster": "recorded", "user": "raven"}, interval=60)
        wrappers.append(wrapper)
        return backend, wrapper

    yield create
    for wrapper in wrappers:
        wrapper.acquisition.broker.close()


@pytest.mark.parametrize("indent", [None, 2])
def test_recorded_accounting_layout_does_not_spend_record_budget(govern, indent):
    value = expanded_accounting()
    response = json.dumps(value, indent=indent)
    assert len(value["jobs"]) == 20
    assert sum(len(job["steps"]) for job in value["jobs"]) == 20
    if indent is not None:
        assert len(response.splitlines()) > 10000
    backend, wrapper = govern(response)
    argv = ["sacct", "--json", "-u", "raven", "-S", "now-48hours"]
    assert wrapper.run(argv)[0] == response
    assert wrapper.run(argv)[0] == response
    assert backend.commands == [argv]


def test_old_parser_large_json_reaches_automatic_legacy_fallback(govern):
    response = json.dumps(expanded_accounting(), indent=2)
    legacy = recorded("accounting")
    backend, wrapper = govern(response, legacy=legacy)
    reader = Slurm(wrapper, "raven")
    assert reader.finished(2) == parse_sacct(legacy)
    assert reader.reader_status["finished"]["format"] == "legacy fallback"
    assert "v0.0.41" in reader.reader_status["finished"]["reason"]
    assert len(backend.commands) == 3
    assert "--json" in backend.commands[1]
    assert "--json" not in backend.commands[2]
    assert reader.finished(2) == parse_sacct(legacy)
    assert len(backend.commands) == 3  # Both capability and admitted reads are cached.


def test_old_parser_large_json_preserves_explicit_json_rejection(govern):
    backend, wrapper = govern(json.dumps(expanded_accounting(), indent=2), legacy=recorded("accounting"))
    reader = Slurm(wrapper, "raven", json_mode="json")
    with pytest.raises(CommandError, match="unqualified Slurm JSON data-parser.*v0.0.41"):
        reader.finished(2)
    assert len(backend.commands) == 2
    assert "--json" in backend.commands[-1]


@pytest.mark.parametrize("indent", [None, 2])
@pytest.mark.parametrize("argv", [
    ["squeue", "--json"],
    ["sacct", "--json"],
    ["/usr/bin/sacct", "--json=data_parser/v0.0.45"],
    ["scontrol", "show", "job", "42", "--json"],
])
@pytest.mark.parametrize("jobs", [
    [{"job_id": 1}, {"job_id": 2}, {"job_id": 3}],
    [{"job_id": 1, "steps": [{}, {}]}],
    [{"job_id": 1, "steps": [{}]}, {"job_id": 2}],
    [None, "malformed", {}],
])
def test_true_excess_counts_jobs_and_steps_even_in_compact_json(govern, indent, argv, jobs):
    _, wrapper = govern(json.dumps({"jobs": jobs}, indent=indent), records=2)
    with pytest.raises(CommandError, match="record budget") as failure:
        wrapper.run(argv)
    message = str(failure.value)
    assert Path(argv[0]).name in message
    assert "3" in message and "2" in message


@pytest.mark.parametrize("indent", [None, 2])
def test_exact_record_limit_accepts_all_nested_step_data_without_truncation(govern, indent):
    response = json.dumps({"jobs": [{"job_id": 1, "steps": [{"name": "batch"}, {"name": "0"}]}]}, indent=indent)
    _, wrapper = govern(response, records=3)
    assert wrapper.run(["sacct", "--json"])[0] == response


@pytest.mark.parametrize("response", [
    "not json",
    '{"jobs":[],"jobs":[{}]}',
    '{"jobs":[],"extension":NaN}',
    '{"jobs":[],"extension":1e999}',
    '{"jobs":[],"extension":"\\u0001"}',
    '{"jobs":{}}',
    "[]",
])
def test_malformed_json_cannot_bypass_admission_or_trigger_legacy_fallback(govern, response):
    backend, wrapper = govern(response, legacy=recorded("accounting"))
    reader = Slurm(wrapper, "raven")
    with pytest.raises(CommandError):
        reader.finished(2)
    assert len(backend.commands) == 2
    assert "--json" in backend.commands[-1]


def test_unknown_extensions_retain_bounded_depth(govern):
    value = {"jobs": []}
    child = value
    for _ in range(26):
        child["extension"] = {}
        child = child["extension"]
    _, wrapper = govern(json.dumps(value))
    with pytest.raises(CommandError, match="nesting"):
        wrapper.run(["squeue", "--json"])


def test_record_count_does_not_remove_transport_byte_limit(govern):
    response = json.dumps({"jobs": [{"description": "x" * 1024}]})
    _, wrapper = govern(response, byte_limit=512)
    with pytest.raises(CommandError, match="output.*limit|byte budget"):
        wrapper.run(["squeue", "--json"])


def test_cancelled_collection_never_starts_scheduler_command(govern):
    backend, wrapper = govern('{"jobs":[]}')
    cancel = threading.Event()
    cancel.set()
    with pytest.raises((CommandError, ValueError), match="cancel"):
        wrapper.observe(["squeue", "--json"], 5, 4096, cancel)
    assert backend.commands == []


def test_cached_response_under_looser_policy_cannot_bypass_tighter_record_limit(govern):
    response = json.dumps({"jobs": [{"steps": [{}, {}]}]})
    backend, permissive = govern(response, records=3)
    assert permissive.run(["sacct", "--json"])[0] == response
    stricter_backend, stricter = govern(response, records=2, cache=permissive.cache)
    with pytest.raises(CommandError, match="record budget"):
        stricter.run(["sacct", "--json"])
    assert len(backend.commands) == len(stricter_backend.commands) == 1


def test_cached_old_line_count_admission_cannot_bypass_new_semantic_limit(govern):
    response = json.dumps({"jobs": [{"steps": [{}, {}]}]})
    _, legacy = govern(response, records=2)
    legacy.scope["generation"] = "native-slurm-cli/v1"
    argv = ["sacct", "--json"]
    request = {"source": "slurm-cli", "operation": "accounting", "interval": 60,
               "duration": 5, "max_bytes": 4 << 20, "max_records": 2,
               "allocation": None, "intrusive": False}
    query = {"request": request, "parameters": {"argv": argv, "timeout": 8.0, "output_limit": 4 << 20}}
    # A pre-fix process admitted this one-line response despite its three records.
    legacy.acquisition.query(legacy.scope, query, lambda _: {"stdout_chunks": [response]},
                             legacy.policy, legacy.capabilities, read_only=True)
    backend, upgraded = govern(response, records=2, cache=legacy.cache)
    with pytest.raises(CommandError, match="record budget"):
        upgraded.run(argv)
    assert backend.commands == [argv]


def test_descriptive_arrays_are_not_scheduler_records(govern):
    response = json.dumps({"jobs": [{"description": {"steps": list(range(100))}}],
                           "metadata": list(range(100))}, indent=2)
    _, wrapper = govern(response, records=1)
    assert wrapper.run(["squeue", "--json"])[0] == response


def test_legacy_text_keeps_record_budget(govern):
    response = "1|RUNNING\n2|PENDING\n3|PENDING\n"
    _, wrapper = govern(response, records=2)
    with pytest.raises(CommandError, match="record budget"):
        wrapper.run(["squeue", "--noheader", "--format=%i|%T"])


@pytest.mark.parametrize("shell_wrapper", [False, True])
def test_managed_collection_defaults_accept_recorded_large_pretty_json(tmp_path, shell_wrapper):
    response = json.dumps(expanded_accounting(), indent=2)
    raw = Backend(response)
    wrapper = wrap_backend(raw, {"raven": {"collection": {}}, "collection_cache_dir": str(tmp_path / "managed")},
                           {"cluster": "recorded", "user": "raven"}, None)
    argv = ["sacct", "--json", "-u", "raven", "-S", "now-48hours"]
    if shell_wrapper:
        argv = ["sh", "-c", "cd " + shlex.quote("/work with spaces") + " && " + shlex.join(argv)]
    try:
        assert wrapper.run(argv)[0] == response
        assert wrapper._last.receipt["descriptor"]["family"] == "accounting"
    finally:
        wrapper.acquisition.broker.close()
