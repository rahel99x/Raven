"""Bounded plot data with units, transformations and exact native lineage.

This is a companion for plots/exports, not a new scientific expression engine.
It preserves caller-supplied calculation and native source semantics and never
promotes sampled coverage or an integrity digest to ground truth.
"""

from __future__ import annotations

import copy
import csv
from decimal import Decimal, InvalidOperation
import hashlib
import io
import os
import re

from .. import evidence as E
from . import _storage as S

SCHEMA = "raven.research-output/v1"


def _filters(value):
    if not isinstance(value, list) or len(value) > 64:
        raise ValueError("Plot filters must be a bounded ordered list")
    for item in value:
        E.keys(item, {"field", "operator", "value"}, label="plot filter")
        E.text(item["field"], "filter source field", 256)
        operator = item["operator"]
        if not isinstance(operator, str) or operator not in {"eq", "ne", "lt", "le", "gt", "ge", "in", "range"}:
            raise ValueError("Use a declarative filter operator")
        if operator == "in" and (not isinstance(item["value"], list) or len(item["value"]) > 256):
            raise ValueError("Membership filters need a bounded value list")
        if operator == "range" and (not isinstance(item["value"], list) or len(item["value"]) != 2):
            raise ValueError("Range filters need exactly two endpoints")
        values = item["value"] if operator in {"in", "range"} else [item["value"]]
        if any(type(value) not in (str, int, float) for value in values):
            raise ValueError("Filter values must be bounded text or finite numbers")
        kinds = {"text" if isinstance(value, str) else "number" for value in values}
        if len(kinds) > 1:
            raise ValueError("Filter endpoints and membership values must have compatible types")
        for scalar in values:
            if isinstance(scalar, str):
                E.text(scalar, "filter value", 4096, empty=True)
            else:
                E.finite(scalar, "filter value")
        if operator == "range" and item["value"][0] > item["value"][1]:
            raise ValueError("Filter range end precedes its start")
    return value


def _matches(row, filters):
    for item in filters:
        field, operation, target = item["field"], item["operator"], item["value"]
        if field not in row:
            raise ValueError("CSV does not contain filter field " + field)
        value = row[field]
        targets = target if operation in {"in", "range"} else [target]
        numeric = targets and all(type(item) in (int, float) for item in targets)
        if numeric:
            if value == "":
                return False
            try:
                value = Decimal(value)
                targets = [Decimal(str(item)) for item in targets]
            except InvalidOperation as exc:
                raise ValueError("CSV numeric filter value is not a finite decimal") from exc
            if not value.is_finite():
                raise ValueError("CSV numeric filter value is not finite")
            target = targets if operation in {"in", "range"} else targets[0]
        if operation == "eq":
            matches = value == target
        elif operation == "ne":
            matches = value != target
        elif operation == "in":
            matches = value in target
        elif operation == "range":
            matches = target[0] <= value <= target[1]
        elif operation == "lt":
            matches = value < target
        elif operation == "le":
            matches = value <= target
        elif operation == "gt":
            matches = value > target
        else:
            matches = value >= target
        if not matches:
            return False
    return True


def _numeric(value):
    """Keep decimal lexical integers exact; JSON floats retain float semantics."""
    if re.fullmatch(r"[+-]?[0-9]+", value, re.ASCII):
        if len(value) > 102:
            raise ValueError("CSV integer exceeds its bounded numeric range")
        return E.finite(int(value), "CSV integer value")
    return E.finite(float(value), "CSV numeric value")


def validate(value):
    E.canonical(value)
    E.keys(value, {"schema", "identity", "title", "data", "units", "calculation", "filters",
                   "coverage", "clock", "sources"}, {"digest", "native_evidence", "native_scope", "association", "status"}, "research output")
    if value["schema"] != SCHEMA:
        raise ValueError("Expected " + SCHEMA)
    from ..schedulers.core import validate_identity

    validate_identity(value["identity"])
    E.text(value["title"], "output title", 256)
    data = value["data"]
    if not isinstance(data, list) or len(data) > 20000:
        raise ValueError("Research output data must contain at most 20000 rows")
    units = value["units"]
    if not isinstance(units, dict) or not 1 <= len(units) <= 64:
        raise ValueError("Every numeric data field needs explicit units (or unknown)")
    for name, unit in units.items():
        E.text(name, "data field", 128)
        E.text(unit, "native metric unit", 128)
    for row in data:
        if not isinstance(row, dict) or set(row) != set(units):
            raise ValueError("Every row must contain exactly the declared unit-bearing fields")
        for item in row.values():
            if item is not None:
                E.finite(item, "research data value")
    calculation = value["calculation"]
    E.keys(calculation, {"name", "version", "parameters"}, label="plot calculation")
    E.text(calculation["name"], "calculation name", 256)
    E.text(calculation["version"], "calculation version", 128)
    if not isinstance(calculation["parameters"], dict):
        raise ValueError("Calculation parameters must be descriptive JSON, never executable code")
    _filters(value["filters"])
    coverage = value["coverage"]
    E.keys(coverage, {"mode", "complete", "omitted", "reason"}, label="source coverage")
    if not isinstance(coverage["mode"], str) or coverage["mode"] not in {"events", "samples", "aggregates", "provenance", "mixed"}:
        raise ValueError("Declare actual source coverage semantics")
    if coverage["complete"] is not None and type(coverage["complete"]) is not bool:
        raise ValueError("Coverage completeness must be true, false or unknown")
    E.integer(coverage["omitted"], "omitted rows")
    E.text(coverage["reason"], "coverage reason", 4096)
    if coverage["complete"] is True and coverage["omitted"]:
        raise ValueError("Omitted data cannot have complete coverage")
    clock = value["clock"]
    E.keys(clock, {"domain", "unit", "uncertainty"}, label="clock")
    E.text(clock["domain"], "clock domain", 256)
    if not isinstance(clock["unit"], str) or clock["unit"] not in {"s", "ms", "us", "ns", "unknown"}:
        raise ValueError("Declare actual clock units")
    if clock["uncertainty"] is not None:
        E.finite(clock["uncertainty"], "clock uncertainty", 0)
    sources = value["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= 256:
        raise ValueError("Every research output needs 1..256 exact source references")
    seen = set()
    for source in sources:
        E.keys(source, {"id", "sha256", "format", "version"}, {"path"}, "output source")
        for key, item in source.items():
            E.text(item, "source " + key, 4096)
        if not E.HASH.fullmatch(source["sha256"]) or source["id"] in seen:
            raise ValueError("Sources require unique IDs and SHA256 digests")
        seen.add(source["id"])
    if "native_scope" in value:
        from ..schedulers.core import scope_digest

        if not isinstance(value["native_scope"], dict) or scope_digest(value["native_scope"]) != value["identity"]["scope"]:
            raise ValueError("Declared native connection scope differs from the output identity")
    if "native_evidence" in value:
        from ..schedulers.core import scope_digest

        receipt = E.verify_receipt(value["native_evidence"])
        native = value["identity"]
        original = receipt["identity"]
        scope = value.get("native_scope")
        if not isinstance(scope, dict) or scope_digest(scope) != native["scope"]:
            raise ValueError("Attaching native evidence requires the exact connection scope, not a matching numeric job ID")
        if (native["scheduler"] != "slurm" or native["native_id"] != original["job_id"]
                or native["attempt"] != original["attempt"] or scope.get("cluster") != original["cluster"]):
            raise ValueError("Native evidence identity differs from the selected scheduler attempt")
    if "association" in value:
        association = value["association"]
        E.keys(association, {"status", "reason", "source_identity", "requested_identity"}, {"experiment"}, "native provenance association")
        if not isinstance(association["status"], str) or association["status"] not in {"unverified", "recorded-link"} or value.get("status") != "partial":
            raise ValueError("Legacy export association must remain an explicitly partial recorded link or unverified selection")
        if association["requested_identity"] != value["identity"] or not isinstance(association["source_identity"], dict):
            raise ValueError("Original export identity and requested native identity must remain separate")
        E.text(association["reason"], "association reason", 4096)
    document = copy.deepcopy(value)
    expected = E.digest({key: item for key, item in document.items() if key != "digest"})
    if "digest" in document and document["digest"] != expected:
        raise ValueError("Research output provenance digest differs")
    document["digest"] = expected
    return document


def build(identity, title, data, *, units, calculation, sources, filters=(),
          coverage=None, clock=None, native_evidence=None, native_scope=None):
    document = {"schema": SCHEMA, "identity": identity, "title": title, "data": data,
                "units": units, "calculation": calculation, "sources": sources,
                "filters": list(filters),
                "coverage": coverage or {"mode": "mixed", "complete": None, "omitted": 0,
                                          "reason": "Caller-supplied data; full source coverage is unverified"},
                "clock": clock or {"domain": "unknown", "unit": "unknown", "uncertainty": None}}
    if native_evidence is not None:
        document["native_evidence"] = native_evidence
    if native_scope is not None:
        document["native_scope"] = native_scope
    return validate(document)


def write(project, name, document):
    """Publish an immutable companion; an existing output is never overwritten."""
    value = validate(document)
    with S.locked(project, "provenance") as folder:
        return S.write(folder, S.name(name) + ".json", value, exclusive=True)


def load(project, name):
    with S.project_store(project, "provenance") as folder:
        return validate(S.read(folder, S.name(name) + ".json"))


def compare(left, right):
    """Explain comparability; do not silently convert units or align clocks."""
    left, right = validate(left), validate(right)
    blockers, differences, warnings = [], [], []
    for field, label in (("units", "metric units"), ("calculation", "calculation definition"), ("filters", "selection filters")):
        if left[field] != right[field]:
            blockers.append("Different " + label + "; explicitly normalize and publish a new derived output")
    for field in ("identity", "coverage", "clock", "sources"):
        if left[field] != right[field]:
            differences.append(field)
    clocks = [item["clock"] for item in (left, right)]
    aligned = (clocks[0]["domain"] == clocks[1]["domain"] != "unknown"
               and clocks[0]["unit"] == clocks[1]["unit"] != "unknown"
               and all(item["uncertainty"] is not None for item in clocks))
    if not aligned:
        warnings.append("Cross-source timing alignment is unverified; do not infer distributed causal order")
    if any(item["coverage"]["complete"] is not True for item in (left, right)):
        warnings.append("Source coverage is incomplete or unknown; missing samples stay unknown")
    native_verified = all("association" not in item for item in (left, right))
    if not native_verified:
        warnings.append("Legacy export/native attempt association is recorded or unverified; do not infer verified scheduler lineage from this comparison")
    return {"compatible": not blockers, "timing_alignment_verified": aligned,
            "native_association_verified": native_verified,
            "blockers": blockers, "differences": differences, "warnings": warnings,
            "source_digests": [left["digest"], right["digest"]],
            "meaning": "Compatible provenance does not establish scientific equivalence or profiler ground truth"}


def from_export(bundle, relative_csv, identity, title, *, fields, units, calculation,
                filters=(), clock=None, experiment_project=None, experiment_id=None, cancel=None):
    """Build a chart companion from an existing immutable Raven export.

    COMPLETE and both manifest/payload hashes are checked. Every selected row
    is retained; an oversized selection is rejected instead of downsampled.
    The native attempt comes from the caller's selected scheduler identity:
    legacy export metadata cannot reconstruct Kubernetes UIDs or restart IDs.
    The original bundle and its full CSV remain unchanged.
    """
    from .. import projects, science_files
    from ..schedulers.core import validate_identity

    identity = validate_identity(identity)
    filters = _filters(list(filters))
    projects.relative_path(relative_csv)
    if (not isinstance(fields, list) or any(not isinstance(item, str) for item in fields)
            or not isinstance(units, dict) or not 1 <= len(fields) <= 64
            or len(set(fields)) != len(fields) or set(fields) != set(units)):
        raise ValueError("Choose unique numeric CSV fields and an explicit unit for each")
    root, folder = projects._root(str(bundle))
    try:
        before = os.fstat(folder)
        manifest_raw, _ = projects._read(folder, "manifest.json", 8 << 20)
        marker_raw, _ = projects._read(folder, "COMPLETE.json", 65536)
        manifest, marker = E.loads(manifest_raw), E.loads(marker_raw)
        manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
        if (manifest.get("schema") != "raven.export-manifest/v1"
                or marker.get("schema") != "raven.export-complete/v1"
                or marker.get("export_id") != manifest.get("export_id")
                or marker.get("manifest_sha256") != manifest_sha):
            raise ValueError("Export COMPLETE marker does not verify this immutable manifest")
        files = manifest.get("files")
        if not isinstance(files, list) or len(files) > 8192:
            raise ValueError("Export payload inventory is invalid")
        entries = [entry for entry in files if isinstance(entry, dict) and entry.get("path") == relative_csv]
        if len(entries) != 1 or not relative_csv.endswith(".csv"):
            raise ValueError("Choose one CSV declared in the export payload inventory")
        entry = entries[0]
        raw, length = projects._read(folder, relative_csv, 2 << 20)
        digest = hashlib.sha256(raw).hexdigest()
        if entry.get("bytes") != length or entry.get("sha256") != digest:
            raise ValueError("Export CSV differs from its immutable payload hash")
        science_files.cancelled(cancel)
        try:
            reader = csv.DictReader(io.StringIO(raw.decode("utf-8"), newline=""))
            if (not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames)
                    or set(fields) - set(reader.fieldnames)
                    or not {"job_id", "run_id", "attempt"} <= set(reader.fieldnames)):
                raise ValueError("CSV needs the chosen numeric fields and exact run/job/attempt identity columns")
            data, attempts, metrics, omitted, source_identity = [], set(), set(), 0, None
            for index, row in enumerate(reader):
                if index >= 20000:
                    raise ValueError("CSV exceeds the 20000-row chart bound; export an explicit smaller range")
                if index % 128 == 0:
                    science_files.cancelled(cancel)
                if None in row or None in row.values():
                    raise ValueError("CSV row has missing or excess cells")
                if row["job_id"] != identity["native_id"]:
                    raise ValueError("CSV belongs to another native job; select its exact attempt")
                if source_identity is None:
                    source_identity = {key: row[key] for key in ("project", "host", "run_id", "attempt", "cluster", "job_id", "submit_time", "script_sha256") if key in row}
                    if identity.get("native_cluster") and row.get("cluster") and identity["native_cluster"] != row["cluster"]:
                        raise ValueError("CSV cluster differs from the scheduler's observed native cluster")
                attempts.add(tuple(row.get(key, "") for key in ("project", "run_id", "attempt", "cluster", "job_id", "submit_time")))
                if len(attempts) > 1:
                    raise ValueError("Choose a CSV with one exact exported attempt before comparing runs")
                if not _matches(row, filters):
                    omitted += 1
                    continue
                known_units = {"elapsed_seconds": "s", "sequence": "count",
                               "utilization_percent": "percent", "memory_used_mib": "MiB", "memory_total_mib": "MiB",
                               "cpu_seconds": "s", "rss_bytes": "B", "process_count": "count"}
                if "timestamp" in fields:
                    known_units["timestamp"] = "s" if row.get("clock") == "unix-epoch-seconds UTC" else "unknown"
                if "source_time" in fields:
                    known_units["source_time"] = manifest.get("units", {}).get("source_time", "unknown")
                aliases = {"seconds": "s", "bytes": "B", "processes": "count"}
                for field in fields:
                    expected = known_units.get(field)
                    if field == "value" and "unit" in row:
                        expected = row["unit"] or "unknown"
                    if expected is not None and aliases.get(units[field], units[field]) != aliases.get(expected, expected):
                        raise ValueError("CSV source units differ for " + field + "; apply an explicit numerical transform before relabelling")
                if "value" in fields and row.get("metric"):
                    metrics.add(row["metric"])
                    if len(metrics) > 1:
                        raise ValueError("Select one scientific metric with a declarative filter before plotting its values")
                data.append({field: None if row[field] == "" else _numeric(row[field])
                             for field in fields})
        except (UnicodeError, csv.Error) as exc:
            raise ValueError("Export CSV must be valid UTF-8") from exc
        if not data:
            raise ValueError("The selected export CSV contains no recorded samples")
        _, actual_fd = projects._root(root)
        try:
            actual = os.fstat(actual_fd)
            if (before.st_dev, before.st_ino) != (actual.st_dev, actual.st_ino):
                raise ValueError("Export directory changed during inspection; reopen the original bundle")
        finally:
            os.close(actual_fd)
        source_format = "raven.export-columns/v1"
        document = build(identity, title, data, units=units, calculation=calculation,
                     sources=[{"id": "export-manifest", "sha256": manifest_sha,
                               "format": "raven.export-manifest/v1", "version": "1", "path": root + "/manifest.json"},
                              {"id": "export-csv", "sha256": digest, "format": source_format,
                               "version": "1", "path": root + "/" + relative_csv}],
                     filters=filters, clock=clock,
                     coverage={"mode": "samples", "complete": None, "omitted": omitted,
                               "reason": "All selected retained CSV rows are present; recording coverage and native attempt association remain distinct from export integrity"})
        document.pop("digest")
        association = {"status": "unverified", "reason": "Legacy exports cannot establish native UID or submission/restart epoch. The requested scheduler identity is a separate user selection, not a verified source identity.",
                       "source_identity": source_identity, "requested_identity": identity}
        if (experiment_project is None) != (experiment_id is None):
            raise ValueError("Choose both experiment_project and experiment_id to resolve a recorded run link")
        if experiment_id is not None:
            from . import experiments
            from .. import run_home

            experiment = experiments.load(experiment_project, experiment_id)
            linked = []
            for attempt in experiment["attempts"]:
                binding = attempt.get("run")
                if (isinstance(binding, dict) and str(binding.get("project_root", "")) == source_identity.get("project")
                        and binding.get("run_id") == source_identity["run_id"]
                        and str(binding.get("attempt")) == source_identity["attempt"]
                        and str(binding.get("job_id")) == source_identity["job_id"]):
                    linked.append(attempt)
            if len(linked) != 1 or linked[0]["scheduler_identity"] != identity:
                raise ValueError("The recorded experiment does not bind this exact exported run to the requested native attempt")
            current = run_home.load(linked[0]["run"])
            if experiments.retained_identity(current["selected"]["binding"]) != linked[0]["identity"]:
                raise ValueError("The retained run changed after its experiment link was recorded")
            retained_binding, inventory = current["selected"]["binding"], current["inventory"]
            cluster = (retained_binding.get("launch") or {}).get("cluster")
            if cluster and source_identity.get("cluster") and cluster != source_identity["cluster"]:
                raise ValueError("Exported cluster contradicts the retained run's native cluster")
            submitted = inventory.get("submit")
            if submitted is not None and source_identity.get("submit_time"):
                try:
                    same_submission = Decimal(source_identity["submit_time"]) == Decimal(str(submitted))
                except InvalidOperation as exc:
                    raise ValueError("Exported submission time cannot be matched to the retained epoch without guessing a timezone") from exc
                if not same_submission:
                    raise ValueError("Exported submission time contradicts the retained run")
            script_sha = inventory.get("provenance", {}).get("script_sha256")
            if script_sha and source_identity.get("script_sha256") and script_sha != source_identity["script_sha256"]:
                raise ValueError("Exported script digest contradicts the retained run")
            association.update(status="recorded-link", reason="Exact retained run identity and native identity match the persisted experiment link. A recorded user link is not independent scheduler attestation.",
                               experiment={"project": str(experiment_project), "id": experiment_id, "revision": experiment["revision"], "attempt": linked[0]["id"]})
        document.update(association=association, status="partial")
        return validate(document)
    finally:
        os.close(folder)
