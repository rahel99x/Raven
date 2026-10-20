"""Exact scheduler PID membership is required when cgroup evidence is absent."""

from pathlib import Path

import pytest

from raven.placement_pids import parse
from raven.raven_collection import make_placement_verifier
from test_raven_collection import backend

_HEADER = "PID JOBID STEPID LOCALID GLOBALID\n"


def test_exact_step_membership_is_sorted_and_empty_is_not_invented():
    assert parse(_HEADER + "421 7 0 0 0\n420 7 0 - -\n", "7", "0") == [420, 421]
    assert parse(_HEADER, "7", "0") == []


@pytest.mark.parametrize(
    "text",
    [
        "No job steps exist on this node.\n",
        "PID JOBID STEPID\n420 7 0\n",
        _HEADER + "420 8 0 0 0\n",
        _HEADER + "420 7 1 0 0\n",
        _HEADER + "420 7 0 0 0 extra\n",
        _HEADER + "420 7 0 0 0\n420 7 0 0 0\n",
        _HEADER + "2147483648 7 0 0 0\n",
        _HEADER + "0 7 0 0 0\n",
        _HEADER + "not-a-pid 7 0 0 0\n",
    ],
)
def test_foreign_ambiguous_and_unavailable_process_membership_fails_closed(text):
    with pytest.raises(ValueError):
        parse(text, "7", "0")


def test_no_cgroup_proof_adds_exact_governed_local_step_read(tmp_path):
    raw, governed = backend(tmp_path)

    def run(argv, timeout=8):
        raw.commands.append(tuple(argv))
        if argv[1] == "listpids":
            return _HEADER + "420 7 0 0 0\n", 0.001
        return (
            "JobId=7 NodeList=system"
            if argv[2] == "job"
            else "StepId=7.0 NodeList=system Nodes=1"
        ), 0.001

    raw.run = run
    result = make_placement_verifier(governed)(
        {
            "job_id": "7",
            "step_id": "0",
            "pid": 420,
            "probe_id": "launch",
            "cgroup_proof": False,
        }
    )
    assert result["pids"] == [420]
    assert raw.commands == [
        ("scontrol", "show", "job", "7", "-o"),
        ("scontrol", "show", "step", "7.0", "-o"),
        ("scontrol", "listpids", "7.0"),
    ]
    assert governed.ledger.report()["rates_last_minute"]["validation"] == 3


def test_actual_slurm_26_listpids_recorded_response():
    source = (
        Path(__file__).parent / "fixtures" / "slurm-native" / "listpids-26.05.5.txt"
    )
    assert parse(source.read_text(), "1", "0") == [200]
