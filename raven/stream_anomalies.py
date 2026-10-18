"""Experimental bounded change detection; a change is not a cause diagnosis.

Only current verified attempt samples enter the live monitor. No filesystem,
scheduler query, external notification or application action runs in this module.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import asdict
import hashlib
import math
from itertools import islice
from statistics import median

from . import measurement_series as M

WINDOW = 32
WARMUP = 12
MAX_STREAMS = 128
MAX_ALERTS = 64


def _floor(metric, unit, center):
    if unit == "B": return max(1048576., abs(center) * .001)
    if unit == "%": return .5
    if unit == "fraction": return .01
    return max(1e-9, abs(center) * .001)


def trend(points):
    """OLS slope with a robust residual envelope; no calibrated CI claim."""
    n = len(points)
    if n < WARMUP:
        return None
    duration = points[-1][0] - points[0][0]
    if duration <= 0:
        return None
    x = [(t - points[0][0]) / duration for t, _ in points]
    y = [v for _, v in points]
    xm, ym = sum(x) / n, sum(v / n for v in y)
    denominator = sum((t - xm) ** 2 for t in x)
    if denominator <= 0:
        return None
    slope_span = sum((t - xm) * (v - ym) for t, v in zip(x, y)) / denominator
    residuals = [v - ym - slope_span * (t - xm) for t, v in zip(x, y)]
    residual_center = median(residuals)
    noise = 1.4826 * median(abs(v - residual_center) for v in residuals)
    slope = slope_span / duration
    if not all(math.isfinite(v) for v in (slope, noise, ym)):
        return None
    # Residual-based sensitivity envelope, not a statistical confidence level.
    radius = 6 * noise / duration
    return {"slope": slope, "lower": slope - radius, "upper": slope + radius,
            "noise": noise, "span": duration, "change": slope_span}


def forecast(points, *, limit, metric, unit):
    if unit != "B" or metric != "rss" or not M.finite(limit) or limit <= 0:
        return {"status": "abstain", "reason": "A known RSS limit in bytes is required"}
    if any(not M.finite(value) or value < 0 for _, value in points):
        return {"status": "abstain", "reason": "RSS observations must be finite nonnegative bytes"}
    fit = trend(points)
    if fit is None or fit["lower"] <= 0:
        return {"status": "abstain", "reason": "A stable positive memory trend is unavailable"}
    horizon = min(fit["span"], 3600.)
    remaining = limit - points[-1][1]
    interval = [max(0., remaining / fit["upper"]), max(0., remaining / fit["lower"])]
    if not all(math.isfinite(v) for v in interval) or interval[1] > horizon:
        return {"status": "abstain", "reason": "Crossing lies beyond the observed-trend validity horizon",
                "validity_horizon_seconds": horizon, "slope_unit": "B/s"}
    return {"status": "experimental", "remaining_seconds_interval": interval,
            "as_of": points[-1][0], "validity_horizon_seconds": horizon,
            "slope_interval": [fit["lower"], fit["upper"]], "slope_unit": "B/s", "limit": limit,
            "limit_unit": "B", "uncertainty": "Residual sensitivity envelope; uncalibrated, not a confidence interval",
            "assumption": "The observed trend continues without a phase change or missing samples"}


class Detector:
    def __init__(self, metric, unit, *, limit=None, window=WINDOW, warmup=WARMUP, threshold=6., max_gap=120.):
        if type(window) is not int or not 12 <= window <= 128 or type(warmup) is not int or not 8 <= warmup <= window:
            raise ValueError("Detector window must be 12..128 and warmup 8..window")
        if not M.finite(threshold) or not 3 <= threshold <= 20 or not M.finite(max_gap) or not 0 < max_gap <= 3600:
            raise ValueError("Detector threshold/gap exceed finite configured bounds")
        self.metric, self.unit, self.limit = metric, unit, limit
        self.window, self.warmup, self.threshold, self.max_gap = window, warmup, threshold, max_gap
        self.points = deque(maxlen=window)
        self.last_t = None
        self.phase = None
        self.pending = {"up": 0, "down": 0, "trend": 0}
        self.active = set()
        self.quiet = 0
        self.status = "warmup"
        self.episodes = 0

    def update(self, stamp, value, *, phase=""):
        if not isinstance(phase, str) or len(phase) > 128:
            self.status = "invalid phase label; observation ignored"
            return None
        if not M.finite(stamp) or self.last_t is not None and stamp <= self.last_t:
            self.status = "non-increasing timestamp; observation ignored"
            return None
        impossible = M.finite(value) and ((self.metric == "rss" and self.unit == "B" and value < 0) or
                    (self.unit == "%" and (self.metric.startswith("gpu") or self.metric == "utilization_percent") and not 0 <= value <= 100))
        if impossible:
            value = None
        reset = self.last_t is not None and stamp - self.last_t > self.max_gap
        changed_phase = self.phase is not None and phase != self.phase
        self.last_t, self.phase = stamp, phase
        if reset or changed_phase or value is None or not M.finite(value):
            self.points.clear(); self.active.clear(); self.pending = dict.fromkeys(self.pending, 0)
            self.status = "declared phase changed" if changed_phase else "gap exceeds maximum" if reset else "missing observation"
            if not M.finite(value):
                if impossible:
                    self.status = "physically invalid observation; warmup reset"
                return None
        if len(self.points) < self.warmup:
            self.points.append((stamp, value)); self.status = "warmup"
            return None
        previous = list(self.points)
        values = [point[1] for point in previous]
        center = median(values)
        mad = 1.4826 * median(abs(v - center) for v in values)
        floor = _floor(self.metric, self.unit, center)
        scale = max(floor, mad)
        gaps = [b[0] - a[0] for a, b in zip(previous, previous[1:])]
        uneven = bool(gaps and max(gaps) > 4 * min(gaps))
        noisy = mad > max(abs(center) * .5, floor * 20)
        score = (value - center) / scale
        if not math.isfinite(score):
            self.status = "numeric range is unsupported"
            return None
        hits = {"up": score > self.threshold, "down": score < -self.threshold, "trend": False}
        self.points.append((stamp, value))
        fit = trend(list(self.points)) if self.metric == "rss" and self.unit == "B" else None
        if fit is not None:
            hits["trend"] = fit["lower"] > 0 and fit["change"] > max(6 * fit["noise"], 4 * floor)
        if uneven or noisy:
            self.pending = dict.fromkeys(self.pending, 0)
            self.status = "uneven sampling" if uneven else "noisy baseline"
            return None
        for kind, hit in hits.items():
            self.pending[kind] = self.pending[kind] + 1 if hit else 0
        if not any(hits.values()):
            self.quiet += 1
            if self.quiet >= 3:
                self.active.clear()
        else:
            self.quiet = 0
        ready = [kind for kind in ("trend", "up", "down") if self.pending[kind] >= 3 and kind not in self.active]
        self.status = "change detected" if ready else "monitoring" if mad else "constant baseline"
        if not ready:
            return None
        kind = ready[0]
        self.active.update(key for key, hit in hits.items() if hit)
        self.episodes += 1
        return {"kind": kind, "episode": self.episodes, "timestamp": stamp,
                "metric": self.metric, "unit": self.unit, "baseline_median": center,
                "baseline_mad": mad, "threshold": self.threshold, "score": score,
                "window": [{"t": t, "value": v} for t, v in self.points],
                "forecast": forecast(list(self.points), limit=self.limit, metric=self.metric, unit=self.unit),
                "diagnosis": "A statistical change was observed; its cause is not established",
                "status": "experimental"}


class Monitor:
    """One owner, finite LRU streams and finite evidence episodes."""
    def __init__(self, max_streams=MAX_STREAMS):
        if type(max_streams) is not int or not 1 <= max_streams <= MAX_STREAMS:
            raise ValueError("Active stream budget must be 1..128")
        self.max_streams = max_streams
        self.streams = OrderedDict()
        self.recent = deque(maxlen=MAX_ALERTS)
        self.evictions = 0
        self.sequence = 0

    def update(self, identity, metric, unit, stamp, value, *, limit=None, phase=""):
        token = M.identity(identity)
        key = token, metric, unit
        detector = self.streams.pop(key, None)
        if detector is None:
            detector = Detector(metric, unit, limit=limit)
        else:
            detector.limit = limit
        self.streams[key] = detector
        while len(self.streams) > self.max_streams:
            self.streams.popitem(last=False); self.evictions += 1
        event = detector.update(stamp, value, phase=phase)
        if event:
            self.sequence += 1
            event = {**event, "identity": dict(identity), "id": hashlib.sha256((token + metric + event["kind"] + repr(event["timestamp"])).encode()).hexdigest()}
            event["text"] = (f"Experimental telemetry change: {metric} {event['kind']} at {event['timestamp']:g} s.\n"
                f"Attempt: {identity['attempt']} (job {identity['job']}). Unit: {unit}.\n"
                f"Baseline median: {event['baseline_median']:g}; MAD: {event['baseline_mad']:g}.\n"
                "Cause is unknown; inspect recordings and declared phase changes.\nEvidence (seconds, value):\n" +
                "\n".join(f"{point['t']:g}, {point['value']:g}" for point in event["window"]))
            self.recent.append(event)
        return event

    def observe(self, identity, sample):
        if not identity.verified or not isinstance(sample, dict):
            return
        context = asdict(identity)
        stamp = sample.get("t")
        phase = sample.get("phase", "")
        if sample.get("k") == "live":
            for metric, unit in (("cpu", "fraction"), ("rss", "B")):
                self.update(context, metric, unit, stamp, sample.get(metric), phase=phase)
        elif sample.get("k") == "gpu":
            devices = sample.get("gpu")
            if isinstance(devices, dict):
                for name, values in islice(devices.items(), 16):
                    if isinstance(values, (tuple, list)) and values:
                        self.update(context, "gpu-util:" + str(name)[:64], "%", stamp, values[0], phase=phase)


def analyze(document, *, cancel=None):
    M.validate(document)
    events, statuses = [], []
    for series in document["series"]:
        detector = Detector(series["metric"], series["unit"], limit=series.get("limit"))
        for i, sample in enumerate(series["samples"]):
            if i % 128 == 0 and cancel is not None and cancel.is_set():
                raise ValueError("Telemetry analysis cancelled")
            event = detector.update(sample["t"], sample["value"], phase=sample.get("phase", ""))
            if event:
                if len(events) == MAX_ALERTS:
                    raise ValueError("More than 64 episodes; inspect a smaller time window")
                events.append({**event, "identity": dict(series["identity"])})
        statuses.append({"identity": dict(series["identity"]), "metric": series["metric"], "state": detector.status,
                         "retained_samples": len(detector.points), "forecast": forecast(list(detector.points), limit=series.get("limit"), metric=series["metric"], unit=series["unit"])})
    return {"schema": "raven.anomaly-result/v1", "status": "experimental", "events": events, "series": statuses,
            "limits": {"window": WINDOW, "max_series": M.MAX_SERIES, "max_samples": M.MAX_SAMPLES, "max_events": MAX_ALERTS},
            "qualification": "Synthetic evaluation only; real-job efficacy and false-positive calibration are unproved"}
