"""Pure bounded telemetry reader contracts, shared with the confined SSH helper."""
from __future__ import annotations

import collections
import json
import math
import datetime as dt

INDEX_SCHEMA = "raven.telemetry-index/v2"
GPU_SCHEMA = "raven.gpu-sample/v1"
CPU_SCHEMA = "raven.cpu-memory-sample/v1"
MAX_INDEX_BYTES = 256 * 1024
MAX_READ_BYTES = 8 * 1024 * 1024
MAX_READ_ROWS = 20000
ROW_DEFAULT_KEYS = {'schema','run_id','node','scope','status','reason','quality','interval_seconds','clock','source_time','source','cpu_scope','memory_scope'}


def hydrate_row(row, source, run_id):
    """Reconstruct invariant metadata captured with a segmented stream."""
    defaults=source.get('row_defaults',{})
    if not isinstance(defaults,dict) or set(defaults)-ROW_DEFAULT_KEYS:
        raise ValueError('invalid telemetry row defaults')
    value=dict(defaults)
    device=row.get('device')
    if device is not None:
        devices=source.get('devices',[])
        if type(device) is not int or not 0<=device<len(devices) or not isinstance(devices[device],dict):
            raise ValueError('invalid telemetry device reference')
        value.update(devices[device])
    value.update(row)
    value.pop('device',None)
    if 'timestamp_utc' not in value and type(value.get('timestamp')) in (int,float) and math.isfinite(value['timestamp']):
        try:
            value['timestamp_utc']=dt.datetime.fromtimestamp(value['timestamp'],dt.timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError('telemetry timestamp is outside the supported UTC range') from exc
    if value.get('schema')!=source['schema'] or value.get('run_id')!=run_id:
        raise ValueError('telemetry row belongs to another schema or run')
    return value


def source_paths(source):
    return [segment['path'] for segment in source.get('segments',[])]+[source['path']]


def validate_index(value, run_id):
    if not isinstance(value, dict) or value.get("schema") not in {INDEX_SCHEMA,'raven.telemetry-index/v1'} or value.get("run_id") != run_id:
        raise ValueError("retained telemetry index does not match the exact run")
    sources = value.get("sources")
    if not isinstance(sources, list) or len(sources) > 16:
        raise ValueError("invalid telemetry source list")
    seen = set()
    for source in sources:
        if not isinstance(source, dict) or source.get("family") not in {"gpu", "cpu-memory"}:
            raise ValueError("unsupported retained telemetry family")
        if source.get("status") not in {"recording", "complete", "partial", "unavailable", "failed"}:
            raise ValueError("invalid telemetry source status")
        if not isinstance(source.get("reason", ""), str) or not isinstance(source.get("coverage", {}), dict):
            raise ValueError("invalid telemetry source coverage")
        for key, maximum in (("devices", 256), ("metrics", 64)):
            values = source.get(key, [])
            if not isinstance(values, list) or len(values) > maximum:
                raise ValueError("invalid bounded telemetry " + key)
        family = source["family"]
        if family in seen or source.get("path") != "telemetry/" + family + ".jsonl":
            raise ValueError("invalid or duplicate retained telemetry path")
        seen.add(family)
        if source.get("schema") != (GPU_SCHEMA if family == "gpu" else CPU_SCHEMA):
            raise ValueError("unsupported retained telemetry stream schema")
        defaults=source.get('row_defaults',{})
        if (not isinstance(defaults,dict) or set(defaults)-ROW_DEFAULT_KEYS
                or defaults and (defaults.get('schema')!=source['schema'] or defaults.get('run_id')!=run_id)):
            raise ValueError('invalid exact telemetry row defaults')
        segments=source.get('segments',[])
        if not isinstance(segments,list) or len(segments)>128:
            raise ValueError('invalid telemetry segment inventory')
        paths=set()
        import re
        for segment in segments:
            if (not isinstance(segment,dict) or not isinstance(segment.get('path'),str)
                    or not re.fullmatch(r'telemetry/'+re.escape(family)+r'\.segment-[0-9]{8}\.jsonl',segment['path'])
                    or segment['path'] in paths or type(segment.get('bytes')) is not int or segment['bytes']<0):
                raise ValueError('invalid or duplicate telemetry segment path')
            paths.add(segment['path'])
    return value


def parse_rows(data, source, run_id, *, max_rows=MAX_READ_ROWS):
    """Bounded dashboard tail. Export uses the original full retained streams."""
    rows = collections.deque(maxlen=max_rows)
    for line in data.splitlines(keepends=True):
        if not line.endswith(b"\n") or len(line) > 65536:
            continue
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            continue
        if not isinstance(row, dict):
            continue
        try:
            row=hydrate_row(row,source,run_id)
        except ValueError:
            continue
        stamp = row.get("timestamp")
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
            continue
        rows.append(row)
    return list(rows)
