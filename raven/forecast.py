"""Bounded queue forecasts with observed, pre-start calibration evidence.

No scheduler queries or file access occur here.  Scheduler points are useful but
are never presented as calibrated without recorded predictions and outcomes.
Historical waits are a separate model, fitted on the oldest half of eligible
records and calibrated on the newer half.  Neither model claims a guarantee
when queue policy or demand changes.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from itertools import islice
from bisect import bisect_left
import math
import threading

from . import clock
from .model import mem_request_bytes, secs

MIN_CALIBRATION = 20
MAX_RECORDS = 100_000
COHORT_FIELDS = ("name", "partition", "account", "qos", "cpus", "nodes", "gpus", "gpu_type", "mem_bytes", "time_seconds")
_NUMERIC_FIELDS = {"cpus", "nodes", "gpus", "mem_bytes", "time_seconds"}
_LEAD_BOUNDS = (60., 300., 900., 3600., 21600., 86400.)
_ABSENT = {"", "N/A", "NA", "UNKNOWN", "NONE", "NULL", "(NULL)"}
_BLOCKED_REASONS = (
    "jobheld", "dependency", "invalidaccount", "invalidqos", "badconstraints",
    "partitioninactive", "partitiondown", "partitionconfig", "qosnotallowed",
    "accountnotallowed", "licensesunavailable", "reservationdeleted",
)


def _get(record, name, default=None):
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def _text(value, limit=256):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    return "".join(c if c.isprintable() else " " for c in str(value)[:limit])


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except (ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _stamp(value):
    """Return (epoch, local-time-assumption); no date-only or guessed formats."""
    number = _number(value)
    if number is not None:
        return (number, False) if 0 <= number <= 253402300799 else (None, False)
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and len(value) <= 128:
        if value.strip().upper() in _ABSENT:
            return None, False
        if "T" not in value and " " not in value:
            return None, False
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None, False
    else:
        return None, False
    naive = parsed.tzinfo is None or parsed.utcoffset() is None
    try:
        result = parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None, naive
    return (result, naive) if math.isfinite(result) and 0 <= result <= 253402300799 else (None, naive)


def _timestamps(record, *names):
    parsed = [_stamp(_get(record, name)) for name in names]
    present = [local for stamp, local in parsed if stamp is not None]
    mixed = bool(present) and any(present) and not all(present)
    return [stamp for stamp, _ in parsed], any(present), mixed


def _cohort(record):
    result = {}
    for key in COHORT_FIELDS:
        raw = _get(record, key)
        if key == "mem_bytes" and raw is None:
            raw = _get(record, "req_mem")
            if raw is None and isinstance(_get(record, "mem_req"), str):
                cpu = _number(_get(record, "cpus", 0))
                nodes = _number(_get(record, "nodes", 1))
                if cpu is not None and nodes is not None:
                    raw = mem_request_bytes(_get(record, "mem_req")[:256], cpu, nodes)
            if raw == 0:
                raw = None  # Unknown memory request must not mean zero.
        if key == "time_seconds" and raw is None:
            raw = _get(record, "limit_s")
            limit = _get(record, "limit")
            if raw is None and isinstance(limit, str):
                raw = secs(limit[:256])
        if key in _NUMERIC_FIELDS:
            number = _number(raw)
            if number is not None and number >= 0 and (key in {"mem_bytes", "time_seconds"} or number.is_integer()):
                if key in {"mem_bytes", "time_seconds"} and number == 0:
                    continue
                result[key] = int(number) if number.is_integer() else number
        else:
            value = _text(raw)
            if key == "gpu_type" and raw == "" and (_number(_get(record, "gpus")) or 0) > 0:
                result[key] = ""  # Explicit generic GPU request, not unknown.
            elif value.upper() not in _ABSENT:
                result[key] = value
    return result


def _matches(target, record):
    candidate = _cohort(record)
    return all(candidate.get(key) == value for key, value in target.items())


def _lead_band(seconds):
    lower = 0.
    for upper in _LEAD_BOUNDS:
        if seconds <= upper:
            return {"lower_seconds": lower, "upper_seconds": upper}
        lower = upper
    return {"lower_seconds": lower, "upper_seconds": None}


def _bound_records(records, maximum):
    if isinstance(records, (str, bytes, Mapping)):
        raise ValueError("history and observations must be iterables of job records")
    try:
        iterator = iter(records)
    except TypeError as exc:
        raise ValueError("history and observations must be iterables of job records") from exc
    selected = list(islice(iterator, maximum + 1))
    return selected[:maximum], len(selected) > maximum


def _quantile(values, probability):
    """Finite-sample split-conformal absolute-error order statistic."""
    if not values:
        return None
    rank = math.ceil((len(values) + 1) * probability)
    if rank > len(values):
        return None
    return sorted(values)[max(0, rank - 1)]


def _median(values):
    values = sorted(values)
    n = len(values)
    return (values[(n - 1) // 2] + values[n // 2]) / 2


def _calibration(errors, coverage):
    quantile = _quantile(errors, coverage) if len(errors) >= MIN_CALIBRATION else None
    # Coverage is reported on chronological held-out outcomes, each evaluated
    # with a quantile computed only from earlier outcomes. It is not resubstitution.
    covered = trials = 0
    # A Fenwick order-statistic tree keeps this O(n log n), rather than sorting
    # every growing prefix. Counts contain only earlier outcomes; future values
    # provide coordinate locations and cannot affect a prefix quantile.
    coordinates = sorted(set(errors))
    tree = [0] * (len(coordinates) + 1)
    for i, error in enumerate(errors):
        rank = math.ceil((i + 1) * coverage)
        if i >= MIN_CALIBRATION and rank <= i:
            index = 0
            step = 1 << (len(coordinates).bit_length() - 1)
            remaining = rank
            while step:
                candidate = index + step
                if candidate < len(tree) and tree[candidate] < remaining:
                    remaining -= tree[candidate]
                    index = candidate
                step >>= 1
            q = coordinates[index]
            trials += 1
            covered += error <= q
        index = bisect_left(coordinates, error) + 1
        while index < len(tree):
            tree[index] += 1
            index += index & -index
    return {
        "count": len(errors), "requested_coverage": coverage,
        "quantile_seconds": quantile, "valid": quantile is not None,
        "empirical_coverage": covered / trials if trials else None,
        "coverage_trials": trials, "coverage_basis": "chronological held-out outcomes",
    }


def _base(job, now, coverage):
    return {
        "job_id": _text(_get(job, "id", _get(job, "job_id", ""))),
        "status": "insufficient", "method": "none", "as_of": now,
        "predicted_start": None, "lower_start": None, "upper_start": None,
        "wait_seconds": None, "queued_seconds": None, "samples": 0,
        "calibration": _calibration([], coverage), "revisions": {},
        "cohort": _cohort(job), "evidence": [], "limitations": [],
    }


def _residuals(records, cohort, now, target_identity, lead_band):
    """Use one latest comparable pre-start point per submitted-job identity."""
    by_job = {}
    local_assumption = False
    for record in records:
        if not _matches(cohort, record):
            continue
        stamps, local, mixed = _timestamps(record, "submit", "issued_at", "predicted_start", "actual_start")
        submit, issued, predicted, actual = stamps
        jid = _text(_get(record, "job_id", _get(record, "id", "")))
        if not jid or submit is None or actual is None or mixed:
            continue
        identity = (jid, submit)
        if identity == target_identity or not (submit <= actual <= now):
            continue
        first, first_local, first_mixed = _timestamps(record, "first_issued_at", "first_predicted_start")
        points = [(issued, predicted)]
        if not first_mixed:
            points.append(tuple(first))
        for issued, predicted in points:
            if issued is None or predicted is None or not (submit <= issued < actual) or predicted < issued:
                continue
            if _lead_band(predicted - issued) != lead_band:
                continue
            previous = by_job.get(identity)
            if previous is None or issued > previous[0]:
                by_job[identity] = (issued, actual, abs(actual - predicted))
            local_assumption |= local or first_local or _get(record, "time_basis") == "local_assumed"
    values = sorted(by_job.values(), key=lambda value: (value[1], value[0]))
    return [value[2] for value in values], local_assumption


def _waits(records, cohort, now, queued, target_identity):
    by_job = {}
    local_assumption = False
    for record in records:
        if not _matches(cohort, record):
            continue
        stamps, local, mixed = _timestamps(record, "submit", "start")
        submit, start = stamps
        jid = _text(_get(record, "id", _get(record, "job_id", "")))
        if not jid or submit is None or start is None or mixed:
            continue
        identity = (jid, submit)
        if identity == target_identity or not (submit <= start <= now):
            continue
        wait = start - submit
        if wait <= queued:
            continue
        by_job.setdefault(identity, (start, wait - queued))
        local_assumption |= local
    return sorted(by_job.values()), local_assumption


def _revisions(records, jid, submit, now):
    candidates = []
    for record in records:
        if _text(_get(record, "job_id", _get(record, "id", ""))) != jid:
            continue
        raw_submit, _ = _stamp(_get(record, "submit"))
        if submit is None or raw_submit != submit:
            continue
        issued, _ = _stamp(_get(record, "issued_at"))
        if issued is not None and issued <= now:
            candidates.append((issued, record))
    if not candidates:
        return {}
    record = max(candidates, key=lambda pair: pair[0])[1]
    latest_issue, _ = _stamp(_get(record, "issued_at"))
    first_issue, _ = _stamp(_get(record, "first_issued_at"))
    latest_prediction, _ = _stamp(_get(record, "predicted_start"))
    first_prediction, _ = _stamp(_get(record, "first_predicted_start"))
    if (first_issue is None or latest_prediction is None or first_prediction is None
            or not submit <= first_issue <= latest_issue <= now
            or latest_prediction < latest_issue or first_prediction < first_issue):
        return {}
    result = {}
    for field in ("first_issued_at", "first_predicted_start", "issued_at", "predicted_start", "last_observed_at"):
        stamp, _ = _stamp(_get(record, field))
        if stamp is not None and (field != "last_observed_at" or stamp <= now):
            result[field] = stamp
    count = _number(_get(record, "revision_count"))
    if count is not None and count >= 0 and count.is_integer():
        result["count"] = int(count)
    if "first_predicted_start" in result and "predicted_start" in result:
        result["shift_seconds"] = result["predicted_start"] - result["first_predicted_start"]
    return result


def forecast(job, history=(), *, observations=(), now=None, coverage=.8, max_records=10000):
    """Return a JSON-safe forecast or an explicit reason to abstain.

    Fields shared by every result: status, method, predicted_start/lower_start/
    upper_start (epoch seconds or null), wait_seconds, queued_seconds, samples,
    calibration, revisions, cohort, evidence, limitations. ``coverage`` is a
    requested statistical target, never a claim that queue conditions are stable.
    A valid interval requires at least 20 independent submitted-job outcomes.
    """
    now, now_local = _stamp(clock.now() if now is None else now)
    probability = _number(coverage)
    result = _base(job, now, probability if probability is not None else .8)
    if now is None or probability is None or not (0 < probability < 1):
        result.update(status="error", limitations=["now must be a valid timestamp and coverage must be strictly between 0 and 1."])
        return result
    if isinstance(max_records, bool) or not isinstance(max_records, int) or not 1 <= max_records <= MAX_RECORDS:
        result.update(status="error", limitations=[f"max_records must be an integer between 1 and {MAX_RECORDS}."])
        return result
    if not isinstance(job, Mapping) and not hasattr(job, "state"):
        result.update(status="error", limitations=["A job record with a state is required."])
        return result
    try:
        historical, history_truncated = _bound_records(history, max_records)
        recorded, observations_truncated = _bound_records(observations, max_records)
    except ValueError as exc:
        result.update(status="error", limitations=[str(exc)])
        return result
    if history_truncated or observations_truncated:
        result["limitations"].append("Input records were capped; the available sample is incomplete.")
    stamps, local, mixed = _timestamps(job, "submit", "est_start", "start")
    submit, estimated, actual = stamps
    if local or now_local:
        result["limitations"].append("Naive Slurm timestamps assume this process's local timezone; verify cluster and terminal clocks agree.")
    if mixed:
        result.update(status="error")
        result["limitations"].append("Mixed naive and timezone-aware timestamps are ambiguous; supply consistent timestamps.")
        return result
    if submit is not None and submit > now:
        result.update(status="error")
        result["limitations"].append("Submission is in the future relative to the observation clock.")
        return result
    if submit is not None:
        result["queued_seconds"] = max(0., now - submit)
    identity = (result["job_id"], submit)
    result["revisions"] = _revisions(recorded, *identity, now)
    state = _text(_get(job, "state")).upper()
    if state not in {"PENDING", "PD"}:
        if state:
            result.update(status="active", method="observed_state")
            if actual is not None and actual <= now:
                result.update(predicted_start=actual, wait_seconds=0.)
            result["evidence"].append(f"Scheduler state is {state}; there is no pending queue-start forecast.")
        else:
            result.update(status="error", limitations=["Job state is missing."])
        return result
    reason = _text(_get(job, "reason"))
    dependency = _text(_get(job, "dependency"))
    unresolved = dependency.upper() not in _ABSENT and dependency.lower() != "(null)"
    if unresolved or reason.lower().startswith(_BLOCKED_REASONS):
        result.update(status="blocked", method="scheduler_blocker")
        result["evidence"].append(f"Pending reason: {reason or 'unresolved dependency'}.")
        if unresolved:
            result["evidence"].append(f"Dependency: {dependency}.")
        result["limitations"].append("A hold, dependency, or policy blocker must clear before a queue-start estimate is meaningful.")
        return result
    allocation_known = (result["cohort"].get("cpus", 0) > 0
                        and result["cohort"].get("nodes", 0) >= 1
                        and "gpus" in result["cohort"]
                        and result["cohort"].get("mem_bytes", 0) > 0
                        and result["cohort"].get("time_seconds", 0) > 0
                        and (result["cohort"]["gpus"] == 0 or "gpu_type" in result["cohort"]))
    if not result["cohort"].get("partition"):
        result["limitations"].append("Partition is unknown; comparable scheduling history cannot be established.")
        cohort = None
    elif not allocation_known:
        result["limitations"].append("CPU, node, GPU counts/type, requested memory, and walltime must be concrete before pooling comparable scheduling outcomes.")
        cohort = None
    else:
        cohort = result["cohort"]
    if estimated is not None:
        if estimated < now:
            result.update(status="stale", method="scheduler_stale")
            result["evidence"].append(f"Scheduler estimate {estimated:g} has passed while the job remains pending.")
            result["limitations"].append("A passed scheduler estimate is not refreshed or treated as an imminent start.")
            return result
        result.update(status="forecast", method="scheduler_uncalibrated", predicted_start=estimated, wait_seconds=estimated - now)
        lead_band = _lead_band(estimated - now)
        errors, assumptions = _residuals(recorded, cohort, now, identity, lead_band) if cohort else ([], False)
        result["samples"] = len(errors)
        result["calibration"] = _calibration(errors, probability)
        result["calibration"]["lead_band"] = lead_band
        result["evidence"].append("Point estimate is the scheduler's current squeue --start projection.")
        if assumptions and not local:
            result["limitations"].append("Some calibration timestamps assume this process's local timezone.")
        if result["calibration"]["valid"]:
            q = result["calibration"]["quantile_seconds"]
            result.update(method="scheduler_conformal", lower_start=max(now, estimated - q), upper_start=estimated + q)
            result["evidence"].append(f"Absolute-error interval uses {len(errors)} distinct submitted jobs with comparable forecast lead and points issued before actual starts.")
        else:
            result["limitations"].append(f"At least {MIN_CALIBRATION} comparable pre-start forecast outcomes are needed; this scheduler point is uncalibrated.")
    else:
        queued = result["queued_seconds"]
        if cohort is None or queued is None:
            result["limitations"].append("No scheduler estimate or verifiable queued age is available.")
            return result
        waits, assumptions = _waits(historical, cohort, now, queued, identity)
        result["samples"] = len(waits)
        result["evidence"].append(f"{len(waits)} comparable observed starts waited longer than this job's queued age.")
        if assumptions and not local:
            result["limitations"].append("Historical timestamps assume this process's local timezone.")
        split = len(waits) // 2
        if split < MIN_CALIBRATION or len(waits) - split < MIN_CALIBRATION:
            result["limitations"].append(f"At least {2 * MIN_CALIBRATION} comparable conditional waits are needed for separate training and calibration sets.")
            return result
        point = _median([remaining for _, remaining in waits[:split]])
        errors = [abs(remaining - point) for _, remaining in waits[split:]]
        result["calibration"] = _calibration(errors, probability)
        if not result["calibration"]["valid"]:
            result["limitations"].append("The requested coverage needs more independent calibration outcomes.")
            return result
        q = result["calibration"]["quantile_seconds"]
        result.update(status="forecast", method="historical_split_conformal", predicted_start=now + point,
                      lower_start=now + max(0., point - q), upper_start=now + point + q, wait_seconds=point)
        result["evidence"].append(f"Point model fitted on {split} earlier waits; calibrated on {len(errors)} later waits, conditional on already waiting {queued:g} seconds.")
    if result["calibration"]["valid"]:
        result["limitations"].append("Requested coverage assumes comparable outcomes; changing policy, demand, or selective completed-job history can invalidate it.")
        if result["method"] == "scheduler_conformal":
            result["limitations"].append("Calibration is stratified by forecast lead; remaining queued-age or workload differences may still affect accuracy.")
        result["limitations"].append("Displayed empirical coverage uses chronological held-out outcomes; a null value means none are available yet.")
    return result


class ForecastTracker:
    """Thread-safe, bounded, in-memory collection of issued forecasts/outcomes.

    A repeated unchanged snapshot updates freshness without adding a revision.
    Submission timestamps distinguish reused IDs. First/latest points are
    retained, while only the latest pre-start revision enters calibration.
    """
    def __init__(self, max_jobs=256, max_observations=4096):
        for name, value in (("max_jobs", max_jobs), ("max_observations", max_observations)):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_RECORDS:
                raise ValueError(f"{name} must be an integer between 1 and {MAX_RECORDS}")
        self.max_jobs = max_jobs
        self.max_observations = max_observations
        self._jobs = OrderedDict()
        self._outcomes = OrderedDict()
        self._lock = threading.RLock()
        self._last_now = None

    def observe(self, jobs, now=None):
        records, _ = _bound_records(jobs, MAX_RECORDS)
        with self._lock:
            # Read the default clock after taking the lock: concurrent sampler
            # sources must not look like a recording rewind merely because
            # they acquired this lock in the opposite order from sampling time.
            at, _ = _stamp(clock.now() if now is None else now)
            if at is None:
                raise ValueError("now must be a valid timestamp")
            # Replay can move backwards. Future evidence must never survive a
            # rewind and leak into a forecast made earlier in that recording.
            if self._last_now is not None and at < self._last_now:
                self._jobs.clear()
                self._outcomes.clear()
            self._last_now = at
            for job in records:
                stamps, local, mixed = _timestamps(job, "submit", "est_start", "start")
                submit, predicted, actual = stamps
                jid = _text(_get(job, "id", _get(job, "job_id", "")))
                if not jid or submit is None or submit > at or mixed:
                    continue
                identity = (jid, submit)
                state = _text(_get(job, "state")).upper()
                entry = self._jobs.get(identity)
                if state in {"PENDING", "PD"}:
                    if identity in self._outcomes:
                        # A requeued job with the same submit timestamp is a new
                        # scheduling episode. Drop its previous residual.
                        self._outcomes.pop(identity, None)
                        entry = None
                    if predicted is None or predicted < at:
                        if entry is not None:
                            entry["last_observed_at"] = at
                            self._jobs.move_to_end(identity)
                        continue
                    if entry is None:
                        entry = dict(_cohort(job), job_id=jid, submit=submit,
                                     first_issued_at=at, first_predicted_start=predicted,
                                     issued_at=at, predicted_start=predicted,
                                     actual_start=None, revision_count=0,
                                     last_observed_at=at, time_basis="local_assumed" if local else "epoch")
                        self._jobs[identity] = entry
                    else:
                        # Resource or profile changes invalidate calibration of
                        # the previous request, even if the scheduler point stays.
                        if _cohort(job) != {key: entry[key] for key in COHORT_FIELDS if key in entry}:
                            self._jobs.pop(identity, None)
                            continue
                        if predicted != entry["predicted_start"]:
                            entry["issued_at"] = at
                            entry["predicted_start"] = predicted
                            entry["revision_count"] += 1
                        entry["last_observed_at"] = at
                    self._jobs.move_to_end(identity)
                elif actual is not None and submit <= actual <= at and entry is not None:
                    self._jobs.pop(identity, None)
                    if entry["issued_at"] < actual and _matches(_cohort(job), entry):
                        entry["actual_start"] = actual
                        entry["last_observed_at"] = at
                        self._outcomes[identity] = entry
                        self._outcomes.move_to_end(identity)
                while len(self._jobs) > self.max_jobs:
                    self._jobs.popitem(last=False)
                while len(self._outcomes) > self.max_observations:
                    self._outcomes.popitem(last=False)

    def observations(self):
        with self._lock:
            entries = list(self._outcomes.values()) + list(self._jobs.values())
            entries.sort(key=lambda entry: entry["last_observed_at"])
            return deepcopy(entries[-self.max_observations:])

    def restore(self, records, *, now=None):
        """Restore untrusted JSON state, discarding malformed/future evidence."""
        at, _ = _stamp(clock.now() if now is None else now)
        if at is None:
            raise ValueError("now must be a valid timestamp")
        incoming, _ = _bound_records(records, self.max_observations)
        jobs, outcomes = OrderedDict(), OrderedDict()
        for record in incoming:
            if not isinstance(record, Mapping):
                continue
            stamps, local, mixed = _timestamps(record, "submit", "issued_at", "predicted_start", "actual_start",
                                                 "first_issued_at", "first_predicted_start", "last_observed_at")
            submit, issued, predicted, actual, first_issue, first_prediction, last_seen = stamps
            jid = _text(_get(record, "job_id"))
            count = _number(_get(record, "revision_count"))
            if not jid or mixed or any(value is None for value in (submit, issued, predicted, first_issue, first_prediction, last_seen)):
                continue
            if count is None or count < 0 or not count.is_integer():
                continue
            if not (submit <= first_issue <= issued <= last_seen <= at and predicted >= issued and first_prediction >= first_issue):
                continue
            if _get(record, "actual_start") is not None and (actual is None or not issued < actual <= last_seen):
                continue
            entry = dict(_cohort(record), job_id=jid, submit=submit, issued_at=issued,
                         predicted_start=predicted, actual_start=actual,
                         first_issued_at=first_issue, first_predicted_start=first_prediction,
                         last_observed_at=last_seen, revision_count=int(count),
                         time_basis="local_assumed" if local or _get(record, "time_basis") == "local_assumed" else "epoch")
            identity = (jid, submit)
            dest = outcomes if actual is not None else jobs
            previous = dest.get(identity)
            if previous is None or entry["issued_at"] > previous["issued_at"]:
                dest[identity] = entry
        jobs = OrderedDict(sorted(jobs.items(), key=lambda item: item[1]["last_observed_at"])[-self.max_jobs:])
        outcomes = OrderedDict(sorted(outcomes.items(), key=lambda item: item[1]["last_observed_at"])[-self.max_observations:])
        with self._lock:
            self._jobs = jobs
            self._outcomes = outcomes
            self._last_now = at
