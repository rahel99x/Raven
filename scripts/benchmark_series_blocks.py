"""Controlled storage candidates and exact envelope build/query fixtures.

Run when tests/other benchmarks are idle. Prototypes beyond the production
codec apply only to the declared numeric fixture, not arbitrary JSON metadata.
"""
import argparse
import hashlib
import json
import math
import resource
import statistics
import struct
import sys
import time
import tracemalloc
import tempfile
import os
import platform
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from raven import charts, series_blocks as blocks
from raven.series_summary import EnvelopeIndex

parser = argparse.ArgumentParser()
parser.add_argument('--samples', type=int, default=8192)
parser.add_argument('--repetitions', type=int, default=8)
args = parser.parse_args()
rows = [{'t': 1792279500. + index / 2, 'cpu': math.sin(index / 13), 'rss': 2**30 + index * 4096} for index in range(args.samples)]

def timed(function):
    times = []; cpu = time.process_time()
    for _ in range(args.repetitions):
        start = time.perf_counter_ns(); value = function(); times.append((time.perf_counter_ns()-start)/1e6)
    return value, {'median_ms': statistics.median(times), 'p95_ms': sorted(times)[int(.95*(len(times)-1))], 'cpu_seconds': time.process_time()-cpu}

results = {}
raw, cost = timed(lambda: b''.join((json.dumps(row, separators=(',', ':'))+'\n').encode() for row in rows))
results['jsonl'] = {**cost, 'bytes': len(raw)}
chunks = [rows[i:i+blocks.MAX_ROWS] for i in range(0, len(rows), blocks.MAX_ROWS)]
packed, cost = timed(lambda: [zlib.compress(json.dumps(chunk, separators=(',', ':')).encode(), 6) for chunk in chunks])
assert [row for chunk in packed for row in json.loads(zlib.decompress(chunk))] == rows
results['simple_json_array_zlib'] = {**cost, 'bytes': sum(map(len, packed)), 'framing_bytes_excluded': len(packed) * (blocks.HEADER.size + blocks.FOOTER.size)}
encoded, cost = timed(lambda: list(blocks.frames(rows, 'fixture.rvb')))
assert [row for frame in encoded for row in blocks.decode(frame, blocks._identity('fixture.rvb'))] == rows
results['production_blocks'] = {**cost, 'bytes': sum(map(len, encoded))}
_, decode_cost = timed(lambda: [row for frame in encoded for row in blocks.decode(frame, blocks._identity('fixture.rvb'))])
results['production_decode'] = decode_cost
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / 'append.rvb'
    start = time.perf_counter(); cpu = time.process_time()
    for row in rows[:128]: blocks.append(path, [row])
    results['single_sample_append_128'] = {'seconds': time.perf_counter()-start, 'cpu_seconds': time.process_time()-cpu,
        'bytes': path.stat().st_size, 'durability': 'fsync each committed append'}
    path = Path(directory) / 'append.jsonl'
    start = time.perf_counter(); cpu = time.process_time()
    for row in rows[:128]:
        with path.open('ab') as stream:
            stream.write((json.dumps(row, separators=(',', ':'))+'\n').encode())
            stream.flush(); os.fsync(stream.fileno())
    results['single_sample_jsonl_append_128'] = {'seconds': time.perf_counter()-start, 'cpu_seconds': time.process_time()-cpu,
        'bytes': path.stat().st_size, 'durability': 'fsync each complete append; matched to compact durability for this comparison'}
    original = Path(directory) / 'source.jsonl'; original.write_bytes(raw)
    from raven import series_retention as retention
    policy = dict(raw_seconds=60, ten_second_seconds=600, minute_seconds=7200, gap_seconds=2, protect_raw=False)
    retained, retention_build = timed(lambda: retention.build(original, settings=policy, as_of=rows[-1]['t']))
    retention.build(original, settings=policy, as_of=rows[-1]['t'], apply=True)
    query, retention_query = timed(lambda: retention.query(retention.target_path(original)))
    results['retention_working_copy'] = {'build': retention_build, 'query': retention_query,
        'source_raw_bytes_preserved': len(raw), 'additional_archive_bytes': retained['archive_bytes'],
        'raw_records': retained['raw_records'], 'rollup_records': retained['rollup_records'],
        'warning': 'Rollups lose within-bin information; these bytes and timings are not equivalent-fidelity replacements for raw data.'}

flat = [float(row[key]) for row in rows for key in ('t', 'cpu', 'rss')]
numeric_chunks = [flat[i:i+blocks.MAX_ROWS*3] for i in range(0, len(flat), blocks.MAX_ROWS*3)]
def dense(): return [zlib.compress(struct.pack('<' + 'd'*len(chunk), *chunk), 6) for chunk in numeric_chunks]
packed, cost = timed(dense)
assert [number[0] for chunk in packed for number in struct.iter_unpack('<d', zlib.decompress(chunk))] == flat
results['fixed_numeric_array_zlib_prototype'] = {**cost, 'bytes': sum(map(len, packed)), 'limit': 'Only the fixed three-number schema; fixture integers are exactly representable as binary64.'}
def xor_block(values):
    previous = [0, 0, 0]; out = bytearray()
    for index, value in enumerate(values):
        bits = struct.unpack('<Q', struct.pack('<d', value))[0]
        column = index % 3
        out.extend(struct.pack('<Q', bits ^ previous[column])); previous[column] = bits
    return zlib.compress(out, 6)
packed, cost = timed(lambda: [xor_block(chunk) for chunk in numeric_chunks])
restored = []
for chunk in packed:
    previous = [0,0,0]
    for index, (delta,) in enumerate(struct.iter_unpack('<Q', zlib.decompress(chunk))):
        column = index % 3; previous[column] ^= delta
        restored.append(struct.unpack('<d', struct.pack('<Q', previous[column]))[0])
assert restored == flat
results['xor_float_bits_zlib_prototype'] = {**cost, 'bytes': sum(map(len, packed)), 'limit': 'Fixed three-number schema only; general integer/metadata sidecars omitted, so this is not a general-format byte comparison.'}
values = [row['cpu'] for row in rows]; times = [row['t'] for row in rows]
index, build = timed(lambda: EnvelopeIndex(values, times))
bounds = (times[0], times[-1])
expected, scan = timed(lambda: charts._time_points(values, times, 120, bounds, .5, True))
actual, indexed = timed(lambda: index.points(120, bounds, .5))
assert actual == expected
tracemalloc.start(); owned = EnvelopeIndex(values, times); _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
print(json.dumps({'samples': args.samples, 'repetitions': args.repetitions, 'storage': results,
                  'environment': {'python': platform.python_version(), 'platform': platform.platform(),
                    'fixture_sha256': hashlib.sha256(raw).hexdigest(),
                    'source_sha256': {name: hashlib.sha256((Path(__file__).resolve().parents[1] / 'raven' / name).read_bytes()).hexdigest()
                        for name in ('series_blocks.py', 'series_storage.py', 'series_retention.py', 'series_summary.py')}},
                  'render': {'build': build, 'scan_query': scan, 'indexed_query': indexed, 'python_index_peak_bytes': peak,
                             'note': 'Exact continuous envelope fixture. Build and query separated; UI signature/freezing cost is additional. Gaps and interpolation use existing renderer.'},
                  'process_peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}, indent=2))
