"""Independent labels, input fidelity, gap-aware correspondence and finite work."""
import copy
import math
import random
import threading

import pytest

from raven import measurement_series as M, phase_alignment as P


def capture(lengths=(32, 32, 32), levels=(20., 80., 40.), *, seed=1, scale=1., identity="first", missing=()):
    rng = random.Random(seed)
    values, times = [], []
    for size, level in zip(lengths, levels):
        for i in range(size):
            values.append(level + 2 * math.sin(2 * math.pi * i / size) + rng.gauss(0, .1))
            times.append(len(times) * scale)
    samples = [{"t": t, "value": None if i in missing else value} for i, (t, value) in enumerate(zip(times, values))]
    return {"schema": M.SCHEMA, "series": [{"identity": {"scope": "synthetic", "attempt": identity, "job": identity, "verified": True},
             "metric": "synthetic-signal", "unit": "arbitrary", "samples": samples}]}


def comparison(a=None, b=None, **settings):
    return {"schema": "raven.phase-comparison/v1", "reference": a or capture(),
            "candidate": b or capture((24, 48, 32), seed=1001, scale=1.5, identity="second"), **settings}


def test_known_boundaries_and_time_scaling_leave_original_observations_intact():
    document = comparison()
    original = copy.deepcopy(document)
    result = P.analyze(document)
    assert document == original
    assert [item["start"] for item in result["reference_segments"]] == [0, 32, 64]
    assert [item["start"] for item in result["candidate_segments"]] == [0, 24, 72]
    assert all(item["status"] == "match" for item in result["correspondences"])
    assert result["aligned_indices"]
    for pair in result["aligned_indices"]:
        assert pair["reference_seconds"] == document["reference"]["series"][0]["samples"][pair["reference_index"]]["t"]
        assert pair["candidate_seconds"] == document["candidate"]["series"][0]["samples"][pair["candidate_index"]]["t"]
    assert result["dtw_cells"] <= P.MAX_DTW_CELLS and result["work_units"] <= P.MAX_WORK
    assert "unproved" in result["qualification"]


def test_unrelated_phases_are_gaps_not_forced_endpoint_matches():
    document = comparison(capture(), capture(levels=(500., 600., 700.), identity="unrelated"))
    result = P.analyze(document)
    assert not result["aligned_indices"]
    assert all(item["status"] != "match" for item in result["correspondences"])


def test_missing_blocks_remain_visible_and_no_pair_crosses_missing_observation():
    document = comparison(capture(missing=range(30, 40)))
    result = P.analyze(document)
    assert result["reference"]["missing_rows"] == 10
    assert not any(30 <= pair["reference_index"] < 40 for pair in result["aligned_indices"])
    assert all(not (item["start"] < 30 and item["end"] > 40) for item in result["reference_segments"])


def test_constant_signal_does_not_invent_temporal_correspondence():
    a = capture((32,), (5,))
    for row in a["series"][0]["samples"]: row["value"] = 5.
    result = P.analyze(comparison(a, copy.deepcopy(a)))
    assert not result["aligned_indices"]
    assert "indeterminate" in result["correspondences"][0]["status"]


@pytest.mark.parametrize("issue", ["units", "attempts", "metric", "settings", "duplicate", "timestamp"])
def test_ambiguous_or_incompatible_inputs_refused(issue):
    document = comparison()
    if issue == "units": document["candidate"]["series"][0]["unit"] = "fraction"
    if issue == "attempts":
        extra = copy.deepcopy(document["reference"]["series"][0]); extra["metric"] = "cpu"; extra["identity"]["attempt"] = "other"
        document["reference"]["series"].append(extra)
    if issue == "metric": document["metrics"] = [{}]
    if issue == "settings": document["minimum"] = 0
    if issue == "duplicate": document["reference"]["series"] *= 2
    if issue == "timestamp": document["reference"]["series"][0]["samples"][3]["t"] = 0
    with pytest.raises(ValueError): P.analyze(document)


def test_cancel_and_work_budget_are_actionable():
    event = threading.Event(); event.set()
    with pytest.raises(ValueError, match="cancelled"):
        P.analyze(comparison(), cancel=event)
    budget = P.Budget(1)
    with pytest.raises(ValueError, match="work budget"):
        budget.spend(2)


def test_user_can_switch_original_and_aligned_views_without_mutation(tmp_path):
    import json
    from raven import operations as O
    path = tmp_path / "comparison.json"
    text = json.dumps(comparison()); path.write_text(text)
    original = O.run("phase-alignment", {"path": str(path), "view": "original"}, O.Context())
    aligned = O.run("phase-alignment", {"path": str(path), "view": "aligned"}, O.Context())
    assert original["rows"] != aligned["rows"]
    assert original["data"] == aligned["data"]
    assert path.read_text() == text and "plan" not in aligned
