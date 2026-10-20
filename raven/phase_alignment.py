"""Experimental bounded segmentation and reversible, gap-aware alignment plans.

Binary segmentation has an explicit work cap. Segment matching permits gaps;
windowed DTW is evaluated only for compatible phase summaries. Input observations
are never interpolated, relabeled as ground truth, or modified.
"""
from __future__ import annotations

import hashlib
import heapq
import json
import math
from statistics import median

from . import measurement_series as M

MAX_FEATURES = 8
MAX_SEGMENTS = 32
MAX_WORK = 2_000_000
MAX_DTW_CELLS = 200_000


class Budget:
    def __init__(self, limit=MAX_WORK, cancel=None):
        self.remaining, self.cancel = limit, cancel
    def spend(self, count=1):
        self.remaining -= count
        if self.remaining < 0:
            raise ValueError("Signal analysis work budget exhausted; select fewer samples/features")
        if self.cancel is not None and self.cancel.is_set():
            raise ValueError("Signal analysis cancelled")


def _capture(document, metrics):
    M.validate(document)
    by_metric = {item["metric"]: item for item in document["series"]}
    tokens = {M.identity(item["identity"]) for item in document["series"]}
    if len(tokens) != 1:
        raise ValueError("Each alignment side must contain exactly one verified attempt")
    sources = [by_metric[name] for name in metrics]
    observations = [{sample["t"]: (i, sample["value"], (item["source"]["source_rows"][i] if "source" in item else None)) for i, sample in enumerate(item["samples"])} for item in sources]
    stamps = sorted(set().union(*(source.keys() for source in observations)))
    if len(stamps) > M.MAX_SAMPLES:
        raise ValueError("Combined timestamp grid exceeds 4096; explicitly select a smaller observation window")
    rows = []
    for stamp in stamps:
        points = [source.get(stamp) for source in observations]
        values = [point[1] if point else None for point in points]
        rows.append({"t": stamp, "values": values, "sample_indices": [point[0] if point else None for point in points],
                     "source_rows": [point[2] if point else None for point in points],
                     "available": all(value is not None for value in values)})
    digest = hashlib.sha256(json.dumps(document, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()).hexdigest()
    return {"identity": dict(sources[0]["identity"]), "source_sha256": digest, "metrics": metrics,
            "units": [source["unit"] for source in sources],
            "sources": [dict(source["source"]) if "source" in source else None for source in sources], "rows": rows}


def _normalization(reference):
    scales = []
    for feature in range(len(reference["metrics"])):
        values = [row["values"][feature] for row in reference["rows"] if row["available"]]
        if not values:
            scales.append((0., 1.)); continue
        center = median(values)
        differences = [right - left for left, right in zip(values, values[1:])]
        delta_center = median(differences) if differences else 0.
        noise = 1.4826 * median(abs(delta - delta_center) for delta in differences) / math.sqrt(2) if differences else 0.
        # Smooth within-phase variation is correlated: adjacent differences
        # alone understate it and invent mean shifts inside a short oscillation.
        # A fixed local lag remains independent of unrelated global level jumps.
        lagged = [right - left for left, right in zip(values, values[8:])]
        if lagged:
            lag_center = median(lagged)
            noise = max(noise, 1.4826 * median(abs(delta - lag_center) for delta in lagged) / math.sqrt(2))
        # A constant reference stays meaningful when a candidate differs; the
        # magnitude floor is explicit and units are required to match.
        # Numeric resolution is an arithmetic floor, not an undeclared
        # scientific minimum effect proportional to an arbitrary DC offset.
        scale = max(noise, 16 * math.ulp(center), 1e-9)
        if not all(math.isfinite(value) for value in (center, scale)):
            raise ValueError("Feature numeric range cannot be normalized safely")
        scales.append((center, scale))
    return scales


def _segments(capture, scales, *, max_gap, minimum, penalty, budget):
    rows = capture["rows"]
    vectors = []
    blocks, start = [], None
    for i, row in enumerate(rows):
        vector = [(value - center) / scale for value, (center, scale) in zip(row["values"], scales)] if row["available"] else None
        if vector is not None and any(not math.isfinite(v) or abs(v) > 1e100 for v in vector):
            raise ValueError("Normalized feature range exceeds stable analysis bounds")
        vectors.append(vector)
        gap = i > 0 and row["t"] - rows[i - 1]["t"] > max_gap
        if vector is None or gap:
            if start is not None: blocks.append((start, i))
            start = None
        if vector is not None and start is None: start = i
    if start is not None: blocks.append((start, len(rows)))
    capture["omitted_blocks"] = []
    if len(blocks) > MAX_SEGMENTS:
        # Preserve the most informative continuous blocks and identify every
        # omitted source range. Never bridge a missing-data boundary.
        def significance(block):
            left, right = block
            count = right - left
            score = 0.
            for feature in range(len(scales)):
                values = [row[feature] for row in vectors[left:right]]
                mean = math.fsum(values) / count
                score += math.fsum((value - mean) ** 2 for value in values)
            budget.spend(count * len(scales))
            return -score, -count, left
        ranked = sorted(blocks, key=significance)
        capture["omitted_blocks"] = [{"start": left, "end": right, "reason": "32-block display/analysis bound; lower within-block variation"}
                                     for left, right in sorted(ranked[MAX_SEGMENTS:])]
        blocks = sorted(ranked[:MAX_SEGMENTS])
    leaves, candidates = {}, []
    feature_count = len(scales)
    for begin, finish in blocks:
        count = finish - begin
        sums = [[0.] * feature_count]
        squares = [[0.] * feature_count]
        for row in vectors[begin:finish]:
            sums.append([a + v for a, v in zip(sums[-1], row)])
            squares.append([a + v * v for a, v in zip(squares[-1], row)])
        def cost(left, right, sums=sums, squares=squares):
            budget.spend(feature_count)
            n = right - left
            return sum(max(0., squares[right][f] - squares[left][f] - (sums[right][f] - sums[left][f]) ** 2 / n) for f in range(feature_count))
        # Bind the recursive callback to this block; earlier block callbacks
        # remain independent when the global heap revisits them.
        def make_offer(begin, sums, cost):
            def add(left, right):
                key = begin + left
                mean = [(sums[right][f] - sums[left][f]) / (right - left) for f in range(feature_count)] if right - left >= minimum else None
                leaves[key] = {"start": key, "end": begin + right, "status": "observed" if mean is not None else "insufficient samples", "mean": mean}
                parent, best, split = cost(left, right), 0., None
                for candidate in range(left + minimum, right - minimum + 1):
                    gain = parent - cost(left, candidate) - cost(candidate, right)
                    if gain > best:
                        best, split = gain, candidate
                threshold = penalty * feature_count * math.log(max(2, right - left))
                if split is not None and best > threshold:
                    heapq.heappush(candidates, (-best / threshold, key, left, right, split, add))
            return add
        make_offer(begin, sums, cost)(0, count)
    while candidates and len(leaves) < MAX_SEGMENTS:
        _, key, left, right, split, offer = heapq.heappop(candidates)
        del leaves[key]
        offer(left, split)
        offer(split, right)
    if candidates:
        for _, key, *_ in candidates:
            leaves[key]["status"] = "segment budget reached"
    output = list(leaves.values())
    output.sort(key=lambda item: item["start"])
    for item in output:
        item.update(start_seconds=rows[item["start"]]["t"], end_seconds=rows[item["end"] - 1]["t"],
                    samples=item["end"] - item["start"], uncertainty="Sample-interval boundary; scientific meaning is unverified")
    return output, vectors


def _distance(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)) / len(a))


def _phase_matches(left, right, *, maximum=2., window=.35):
    # Sequence alignment permits gaps instead of forcing unrelated phases into
    # endpoint matches. Local DTW below only refines a supported correspondence.
    n, m = len(left), len(right)
    gap = 1.
    costs = [[math.inf] * (m + 1) for _ in range(n + 1)]
    path = {}
    costs[0][0] = 0.
    for i in range(n + 1):
        for j in range(m + 1):
            if not i and not j: continue
            options = []
            if i: options.append((costs[i - 1][j] + gap, (i - 1, j), "reference gap"))
            if j: options.append((costs[i][j - 1] + gap, (i, j - 1), "candidate gap"))
            if i and j and left[i - 1]["mean"] is not None and right[j - 1]["mean"] is not None:
                distance = _distance(left[i - 1]["mean"], right[j - 1]["mean"])
                if abs((i - .5) / max(1, n) - (j - .5) / max(1, m)) <= window and distance <= maximum:
                    options.append((costs[i - 1][j - 1] + distance, (i - 1, j - 1), "match"))
            value, previous, action = min(options, key=lambda item: (item[0], item[2] != "match"))
            costs[i][j], path[i, j] = value, (previous, action)
    result, cursor = [], (n, m)
    while cursor != (0, 0):
        previous, action = path[cursor]
        i, j = cursor
        result.append({"reference": i - 1 if action != "candidate gap" else None,
                       "candidate": j - 1 if action != "reference gap" else None, "status": action})
        cursor = previous
    return list(reversed(result))


def _indices(segment, total):
    count = segment["end"] - segment["start"]
    target = min(count, max(2, int(128 * count / max(1, total))))
    if target == 1: return [segment["start"]]
    return [segment["start"] + round(i * (count - 1) / (target - 1)) for i in range(target)]


def _dtw(left, right, li, ri, *, budget):
    n, m = len(li), len(ri)
    # At most 128x128 observed points: a full bounded matrix includes both
    # endpoints even for highly unequal phase lengths.
    width = max(n, m)
    cost, paths = {(0, 0): 0.}, {}
    for i in range(1, n + 1):
        center = i * m / n
        for j in range(max(1, math.floor(center - width)), min(m, math.ceil(center + width)) + 1):
            budget.spend()
            choices = [(cost.get(previous, math.inf), previous) for previous in ((i - 1, j), (i, j - 1), (i - 1, j - 1))]
            previous_cost, previous = min(choices, key=lambda item: (item[0], item[1][0] + item[1][1]))
            if math.isfinite(previous_cost):
                cost[i, j] = previous_cost + _distance(left[li[i - 1]], right[ri[j - 1]])
                paths[i, j] = previous
    if (n, m) not in paths: return [], None
    pairs, cursor = [], (n, m)
    while cursor != (0, 0):
        i, j = cursor
        pairs.append([li[i - 1], ri[j - 1]])
        cursor = paths[cursor]
    pairs.reverse()
    return pairs, cost[n, m] / len(pairs)


def analyze(document, *, cancel=None):
    if not isinstance(document, dict) or document.get("schema") != "raven.phase-comparison/v1" or set(document) - {"schema", "reference", "candidate", "metrics", "max_gap", "minimum", "penalty"}:
        raise ValueError("Use an explicit raven.phase-comparison/v1 document")
    for key in ("reference", "candidate"):
        M.validate(document.get(key))
    available = [{item["metric"] for item in document[key]["series"]} for key in ("reference", "candidate")]
    metrics = document.get("metrics", sorted(available[0] & available[1]))
    if not isinstance(metrics, list) or not 1 <= len(metrics) <= MAX_FEATURES or any(not isinstance(v, str) for v in metrics) or len(set(metrics)) != len(metrics) or not set(metrics) <= available[0] & available[1]:
        raise ValueError("Select 1..8 unique metrics available on both attempts")
    maximum_gap, minimum, penalty = document.get("max_gap", 120.), document.get("minimum", 8), document.get("penalty", 8.)
    if not M.finite(maximum_gap) or not 0 < maximum_gap <= 3600 or type(minimum) is not int or not 4 <= minimum <= 256 or not M.finite(penalty) or not .5 <= penalty <= 100:
        raise ValueError("Phase gap, minimum and penalty exceed bounded analysis settings")
    left, right = (_capture(document[key], metrics) for key in ("reference", "candidate"))
    if any(not any(row["available"] for row in capture["rows"]) for capture in (left, right)):
        raise ValueError("Selected metrics have no shared observation timestamps; collectors may use different clocks or cadences. Select one metric or align observations explicitly with provenance")
    if left["units"] != right["units"] or any(unit.lower() in {"unknown", "unspecified", ""} for unit in left["units"]):
        raise ValueError("Selected metrics need the same explicit units on both attempts")
    scales = _normalization(left)
    budget = Budget(cancel=cancel)
    a, av = _segments(left, scales, max_gap=maximum_gap, minimum=minimum, penalty=penalty, budget=budget)
    b, bv = _segments(right, scales, max_gap=maximum_gap, minimum=minimum, penalty=penalty, budget=budget)
    matches = _phase_matches(a, b)
    dtw = Budget(MAX_DTW_CELLS, cancel)
    pairs = []
    for match in matches:
        if match["status"] != "match": continue
        ai, bi = match["reference"], match["candidate"]
        if len(a) == len(b) == 1 and len({tuple(v) for v in av if v is not None}) <= 1 and len({tuple(v) for v in bv if v is not None}) <= 1:
            match["status"] = "constant signals; alignment is indeterminate"
            continue
        sample_pairs, distance = _dtw(av, bv, _indices(a[ai], len(av)), _indices(b[bi], len(bv)), budget=dtw)
        if distance is None or distance > 2:
            match.update(status="uncertain correspondence", distance=distance)
            continue
        match.update(distance=distance, sampling="Bounded representative indices; original samples remain authoritative")
        pairs.extend({"reference_index": i, "candidate_index": j,
                      "reference_seconds": left["rows"][i]["t"], "candidate_seconds": right["rows"][j]["t"],
                      "reference_source_rows": left["rows"][i]["source_rows"], "candidate_source_rows": right["rows"][j]["source_rows"],
                      "reference_sample_indices": left["rows"][i]["sample_indices"], "candidate_sample_indices": right["rows"][j]["sample_indices"]} for i, j in sample_pairs)
    for capture in (left, right):
        capture["missing_rows"] = sum(not row["available"] for row in capture["rows"])
        capture["sample_count"] = len(capture.pop("rows"))
    return {"schema": "raven.phase-alignment/v1", "status": "partial-experimental" if left["omitted_blocks"] or right["omitted_blocks"] else "experimental", "reference": left, "candidate": right,
            "normalization": [{"metric": name, "unit": unit, "reference_median": center, "scale": scale} for name, unit, (center, scale) in zip(metrics, left["units"], scales)],
            "reference_segments": a, "candidate_segments": b, "correspondences": matches, "aligned_indices": pairs,
            "reversible": "Original timestamps/values remain unchanged in source documents; indices refer to the sorted union timestamp grid",
            "work_units": MAX_WORK - budget.remaining, "dtw_cells": MAX_DTW_CELLS - dtw.remaining,
            "qualification": "Synthetic/reporting-example evaluation only; real scientific phase labels and held-out deployment efficacy remain unproved"}
