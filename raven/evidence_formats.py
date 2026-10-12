"""Actual bounded interchange dialects; never execute or fetch referenced content.

Supported: Perfetto/Chrome JSON traceEvents (not protobuf), Caliper FORMAT json
records/object (not json-split or .cali), and literal-key Workflow Run RO-Crate
JSON-LD metadata (no remote JSON-LD expansion). Native source entities are kept
for faithful re-export; normalized observations carry explicit unknown coverage.
"""
from __future__ import annotations

import copy
import hashlib
from . import evidence as E

FORMATS = ("raven-evidence", "perfetto-json", "caliper-json-records", "workflow-run-ro-crate", "otlp-json-traces")


def _receipt(value, execution, dialect, records, *, clock=None, mode="mixed", warnings=(), metadata=None):
    meta = {"warnings": list(warnings), **(metadata or {})}
    return E.receipt(execution, {"id": dialect, "format": dialect, "version": "1", "sha256": E.digest(value)}, records,
                     clock=clock, coverage={"mode": mode, "complete": None, "scope": ["declared input document"], "gaps": [], "omitted": 0},
                     transforms=[{"name": "import:" + dialect, "version": "1", "parameters": {"network_fetch": False}}], metadata=meta)


def _array(value, label):
    if not isinstance(value, list) or len(value) > E.MAX_RECORDS:
        raise ValueError(label + " must contain at most 20000 records")
    return value


def import_document(value, dialect, execution):
    E.canonical(value)
    execution = E.identity(execution)
    if dialect == "raven-evidence":
        result = E.validate(value)
        if result["identity"] != execution:
            raise ValueError("Evidence execution identity differs from the requested identity")
        return result
    if dialect == "perfetto-json":
        return _perfetto(value, execution)
    if dialect == "caliper-json-records":
        return _caliper(value, execution)
    if dialect == "workflow-run-ro-crate":
        return _rocrate(value, execution)
    if dialect == "otlp-json-traces":
        return _otlp(value, execution)
    raise ValueError("Unsupported interchange dialect; choose an explicitly supported JSON format")


def _perfetto(value, execution):
    if not isinstance(value, dict) or not isinstance(value.get("traceEvents"), list):
        raise ValueError("Perfetto JSON requires traceEvents; binary protobuf and arbitrary JSON are unsupported")
    events = _array(value["traceEvents"], "traceEvents")
    records, stacks, warnings = [], {}, []
    timed = []
    for index, event in enumerate(events):
        if not isinstance(event, dict) or not isinstance(event.get("ph"), str):
            raise ValueError("Each trace event needs a phase string")
        E.text(event["ph"], "trace phase", 8)
        for key in ("name", "cat"):
            if key in event:
                E.text(event[key], "trace " + key, 4096, empty=True)
        for key in ("pid", "tid"):
            if key in event and not (type(event[key]) is int and 0 <= event[key] < 2**63) and not (isinstance(event[key], str) and len(event[key]) <= 256 and event[key].isprintable()):
                raise ValueError("Trace process/thread identities must be nonnegative integers or printable strings")
        if "args" in event and not isinstance(event["args"], dict):
            raise ValueError("Trace args must be an object")
        records.append({"id": f"event:{index}", "kind": "annotation", "data": {"source_index": index, "trace_event": event}})
        if event["ph"] != "M":
            E.finite(event.get("ts"), "trace timestamp")
            timed.append((event["ts"], index, event))
    for _, index, event in sorted(timed, key=lambda row: (row[0], row[1])):
        ph, ts, name = event["ph"], event["ts"], event.get("name") or "unnamed"
        lane = (event.get("pid"), event.get("tid"))
        if ph in ("B", "E", "X", "s", "t", "f") and None in lane:
            raise ValueError("Duration and flow events require pid and tid")
        span = None
        if ph == "X":
            E.finite(event.get("dur"), "trace duration", 0)
            span = {"name": name, "start": ts, "end": ts + event["dur"], "pid": lane[0], "tid": lane[1]}
        elif ph == "B":
            stacks.setdefault(lane, []).append((index, event))
        elif ph == "E":
            if not stacks.get(lane):
                warnings.append(f"Unmatched duration end at source event {index}")
            else:
                begin_index, begin = stacks[lane].pop()
                span = {"name": begin.get("name") or "unnamed", "start": begin["ts"], "end": ts, "pid": lane[0], "tid": lane[1], "attributes": {"begin_event": begin_index, "end_event": index}}
        elif ph in ("s", "t", "f"):
            token = event.get("id", event.get("id2"))
            if not (type(token) in (str, int) or isinstance(token, dict)):
                raise ValueError("Flow events require an explicit id/id2")
            if isinstance(token, dict):
                if len(token) != 1 or set(token) - {"local", "global"}:
                    raise ValueError("Flow id2 requires exactly local or global identity")
                scalar = next(iter(token.values()))
            else:
                scalar = token
            if type(scalar) is int:
                E.integer(scalar, "flow id")
            else:
                E.text(scalar, "flow id", 512)
            # Category/scope and local-vs-global id2 identity are retained. No
            # causal edge is guessed from adjacency or wall-clock proximity.
            records.append({"id": f"flow:{index}", "kind": "flow", "data": {"time": ts, "phase": ph, "flow_id": token, "category": event.get("cat", ""), "scope": event.get("scope", ""), "pid": lane[0], "tid": lane[1], "name": name}})
        elif ph == "C":
            for key, item in event.get("args", {}).items():
                E.finite(item, "trace counter")
                records.append({"id": f"counter:{index}:{key}", "kind": "metric", "data": {"name": name + "/" + key, "value": item, "unit": "unknown", "time": ts, "attributes": {"pid": event.get("pid"), "tid": event.get("tid")}}})
        elif ph not in ("i", "I", "R"):
            warnings.append("Retained unsupported phase without reinterpretation: " + ph)
        if span is not None:
            records.append({"id": f"span:{index}", "kind": "span", "data": span})
    for lane, pending in stacks.items():
        for index, event in pending:
            records.append({"id": f"open-span:{index}", "kind": "span", "data": {"name": event.get("name") or "unnamed", "start": event["ts"], "end": None, "pid": lane[0], "tid": lane[1]}})
            warnings.append(f"Unclosed duration begin at source event {index}")
    metadata = {"trace_header": {key: item for key, item in value.items() if key != "traceEvents"}}
    return _receipt(value, execution, "perfetto-json", records,
                    clock={"domain": "source trace clock (alignment unverified)", "unit": "us", "uncertainty": None}, mode="events",
                    warnings=sorted(set(warnings))[:256], metadata=metadata)


def _caliper(value, execution):
    if isinstance(value, list):
        rows, header = value, {"layout": "records"}
    elif isinstance(value, dict) and set(value) <= {"records", "globals", "attributes"} and "records" in value:
        rows, header = value["records"], {"layout": "object", **{k: v for k, v in value.items() if k != "records"}}
        for key in ("globals", "attributes"):
            if key in value and not isinstance(value[key], dict):
                raise ValueError("Caliper object globals/attributes must be objects")
    else:
        raise ValueError("Caliper adapter accepts FORMAT json records/object; json-split and binary .cali are unsupported")
    records = []
    for index, row in enumerate(_array(rows, "Caliper records")):
        if not isinstance(row, dict) or len(row) > 256:
            raise ValueError("Caliper record must be a bounded object")
        records.append({"id": f"caliper:{index}", "kind": "annotation", "data": {"source_index": index, "caliper_record": row}})
        for key, item in row.items():
            if type(item) in (int, float):
                E.finite(item, "Caliper measurement")
                records.append({"id": f"metric:{index}:{key}", "kind": "metric", "data": {"name": key, "value": item, "unit": "unknown", "attributes": {"path": row.get("path"), "source_index": index}}})
    return _receipt(value, execution, "caliper-json-records", records, metadata={"caliper_header": header},
                    warnings=["Caliper attribute units and query coverage are not inferred from names; original records and metadata are retained."])


def _rocrate(value, execution):
    if not isinstance(value, dict) or "@context" not in value or "@graph" not in value:
        raise ValueError("RO-Crate metadata needs @context and @graph")
    records, ids, actions = [], set(), 0
    for node in _array(value["@graph"], "RO-Crate @graph"):
        if not isinstance(node, dict):
            raise ValueError("RO-Crate entities must be objects")
        token = E.text(node.get("@id"), "RO-Crate entity id", 512)
        if token in ids:
            raise ValueError("Duplicate RO-Crate entity ID")
        ids.add(token)
        types = node.get("@type", [])
        if isinstance(types, str):
            types = [types]
        if not isinstance(types, list) or any(not isinstance(item, str) for item in types):
            raise ValueError("RO-Crate entity types must be strings")
        kind = "run" if "CreateAction" in types else "artifact" if "File" in types or "Dataset" in types else "annotation"
        actions += kind == "run"
        records.append({"id": token, "kind": kind, "data": {"entity": node}})
    if "./" not in ids or "ro-crate-metadata.json" not in ids or not actions:
        raise ValueError("Workflow Run RO-Crate requires root ./, ro-crate-metadata.json and a CreateAction")
    header = {key: item for key, item in value.items() if key != "@graph"}
    return _receipt(value, execution, "workflow-run-ro-crate", records, mode="provenance", metadata={"crate_header": header},
                    warnings=["Literal JSON-LD keys only; remote contexts and referenced files are never fetched. This importer does not certify full RO-Crate profile compliance or scientific validity."])


def _native_export(value, receipt):
    if E.digest(value) != receipt["source"]["sha256"]:
        raise ValueError("Retained native source differs from its declared digest; re-export refused")
    return value


def _native_rows(receipt, name):
    rows, seen = [], set()
    for item in receipt["records"]:
        data = item["data"]
        if item["kind"] != "annotation" or name not in data:
            continue
        E.integer(data.get("source_index"), "native source index", E.MAX_RECORDS)
        index = data["source_index"]
        if index in seen or not isinstance(data[name], dict):
            raise ValueError("Duplicate or malformed retained native source record")
        seen.add(index)
        rows.append((index, data[name]))
    return sorted(rows, key=lambda row: row[0])


def export_document(value, dialect):
    receipt = E.validate(value)
    if dialect == "raven-evidence":
        return receipt
    meta = receipt.get("metadata", {})
    if dialect == "perfetto-json":
        events = _native_rows(receipt, "trace_event")
        if receipt["source"]["format"] != dialect or "trace_header" not in meta:
            raise ValueError("Perfetto re-export requires retained native trace events")
        if not isinstance(meta["trace_header"], dict):
            raise ValueError("Retained trace header must be an object")
        return _native_export({**copy.deepcopy(meta["trace_header"]), "traceEvents": [copy.deepcopy(event) for _, event in events]}, receipt)
    if dialect == "caliper-json-records":
        if receipt["source"]["format"] != dialect or "caliper_header" not in meta:
            raise ValueError("Caliper re-export requires retained native records")
        rows = _native_rows(receipt, "caliper_record")
        rows = [copy.deepcopy(row) for _, row in rows]
        header = meta["caliper_header"]
        if not isinstance(header, dict) or header.get("layout") not in ("records", "object"):
            raise ValueError("Retained Caliper layout is invalid")
        native = rows if header["layout"] == "records" else {**{k: copy.deepcopy(v) for k, v in header.items() if k != "layout"}, "records": rows}
        return _native_export(native, receipt)
    if dialect == "otlp-json-traces":
        if receipt["source"]["format"] != dialect or "otlp_header" not in meta:
            raise ValueError("OTLP re-export requires retained native resourceSpans")
        if not isinstance(meta["otlp_header"], dict):
            raise ValueError("Retained OTLP header must be an object")
        return _native_export({**copy.deepcopy(meta["otlp_header"]), "resourceSpans": [copy.deepcopy(item["data"]["resource_spans"]) for item in receipt["records"] if "resource_spans" in item["data"]]}, receipt)
    if dialect == "workflow-run-ro-crate":
        if receipt["source"]["format"] == dialect and "crate_header" in meta:
            if not isinstance(meta["crate_header"], dict):
                raise ValueError("Retained RO-Crate header must be an object")
            return _native_export({**copy.deepcopy(meta["crate_header"]), "@graph": [copy.deepcopy(item["data"]["entity"]) for item in receipt["records"] if "entity" in item["data"]]}, receipt)
        # Generate a portable process-run record with explicit Raven identity and
        # measurement receipt; do not invent inaccessible input/output files.
        token = "#execution-" + E.digest(receipt["identity"])[:24]
        return {"@context": "https://w3id.org/ro/crate/1.1/context", "@graph": [
            {"@id": "ro-crate-metadata.json", "@type": "CreativeWork", "about": {"@id": "./"}, "conformsTo": {"@id": "https://w3id.org/ro/crate/1.1"}},
            {"@id": "./", "@type": "Dataset", "name": "Raven execution evidence", "description": "Exported declared execution evidence; source completeness remains explicit.", "hasPart": [{"@id": "raven-evidence.json"}], "mentions": [{"@id": token}], "conformsTo": [{"@id": "https://w3id.org/ro/wfrun/process/0.5"}]},
            {"@id": token, "@type": "CreateAction", "name": "Slurm job " + receipt["identity"]["job_id"], "identifier": E.canonical(receipt["identity"]).decode(), "instrument": {"@id": "#raven"}, "result": {"@id": "raven-evidence.json"}},
            {"@id": "#raven", "@type": "SoftwareApplication", "name": "Raven", "version": "evidence/v1"},
            {"@id": "raven-evidence.json", "@type": "File", "encodingFormat": "application/json", "description": "The evidence receipt must accompany this metadata", "sha256": hashlib.sha256(E.canonical(receipt) + b"\n").hexdigest()}
        ]}
    raise ValueError("Unsupported export dialect")



def _otlp(value, execution):
    import re
    if not isinstance(value, dict) or "resourceSpans" not in value:
        raise ValueError("OTLP JSON traces require resourceSpans; protobuf, profiles and other signals are unsupported")
    resources = _array(value["resourceSpans"], "OTLP resources")
    records, seen, dropped = [], set(), 0
    def timestamp(value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value):
            raise ValueError("OTLP nanosecond timestamps must be decimal strings")
        number = int(value)
        if number > 2**64 - 1:
            raise ValueError("OTLP timestamp exceeds uint64")
        return number
    for resource_index, resource in enumerate(resources):
        if not isinstance(resource, dict) or "scopeSpans" not in resource:
            raise ValueError("OTLP resources need scopeSpans; legacy aliases are not guessed")
        if "resource" in resource and not isinstance(resource["resource"], dict):
            raise ValueError("OTLP resource must be an object")
        records.append({"id": f"resource:{resource_index}", "kind": "annotation", "data": {"resource_spans": resource}})
        for scope in _array(resource["scopeSpans"], "OTLP scopes"):
            if not isinstance(scope, dict) or "spans" not in scope:
                raise ValueError("OTLP scopeSpans require spans")
            if "scope" in scope and not isinstance(scope["scope"], dict):
                raise ValueError("OTLP instrumentation scope must be an object")
            for span in _array(scope["spans"], "OTLP spans"):
                if not isinstance(span, dict):
                    raise ValueError("OTLP span must be an object")
                trace_id, span_id = span.get("traceId"), span.get("spanId")
                if not isinstance(trace_id, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", trace_id) or int(trace_id, 16) == 0 or not isinstance(span_id, str) or not re.fullmatch(r"[0-9a-fA-F]{16}", span_id) or int(span_id, 16) == 0:
                    raise ValueError("OTLP traces require nonzero hex traceId/spanId")
                key = trace_id.lower() + ":" + span_id.lower()
                if key in seen:
                    raise ValueError("Duplicate OTLP trace/span identity")
                seen.add(key)
                start, end = timestamp(span.get("startTimeUnixNano")), timestamp(span.get("endTimeUnixNano"))
                if end < start:
                    raise ValueError("OTLP span end precedes start")
                E.text(span.get("name"), "OTLP span name", 4096)
                for field in ("droppedAttributesCount", "droppedEventsCount", "droppedLinksCount"):
                    E.integer(span.get(field, 0), field, 2**32 - 1)
                    dropped += span.get(field, 0)
                parent = span.get("parentSpanId", "")
                if not isinstance(parent, str) or parent and not re.fullmatch(r"[0-9a-fA-F]{16}", parent):
                    raise ValueError("OTLP parentSpanId must be hexadecimal")
                records.append({"id": key, "kind": "span", "data": {"name": span["name"], "start": start, "end": end,
                    "attributes": {"trace_id": trace_id.lower(), "span_id": span_id.lower(), "parent_span_id": parent.lower(), "scope": scope.get("scope", {}), "resource": resource.get("resource", {}), "span": span}}})
                if len(records) > E.MAX_RECORDS:
                    raise ValueError("OTLP normalized record count exceeds its bound")
    return _receipt(value, execution, "otlp-json-traces", records, mode="events",
                    clock={"domain": "OTLP Unix epoch nanoseconds (node alignment unverified)", "unit": "ns", "uncertainty": None},
                    metadata={"otlp_header": {key: item for key, item in value.items() if key != "resourceSpans"}, "reported_dropped_span_fields": dropped},
                    warnings=["OTLP spans are instrumented intervals, not every function call. End-to-end delivery and uninstrumented regions are unknown.",
                              f"Source reports {dropped} dropped span attributes/events/links; this is not a count of lost spans."])
