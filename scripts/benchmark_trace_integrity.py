#!/usr/bin/env python3
"""Compare integrity encoding costs with identical synthetic event payloads.

The block case is a format design experiment, not a supported journal encoder.
It does not remove existing v1 checksums or establish crash-recovery correctness.
"""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from raven import trace_protocol as protocol


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--events', type=int, default=10000)
    parser.add_argument('--repeats', type=int, default=5)
    args = parser.parse_args()
    if not 100 <= args.events <= 100000 or not 1 <= args.repeats <= 20:
        parser.error('events must be 100–100000 and repeats 1–20')
    records = [{'schema': 'raven.trace-event/v1', 'stream': 'abcdef0123456789', 'seq': i + 1,
        'prev': '0' * 64, 'event': {'kind': 'call', 'mono_ns': i * 1000, 'thread': '1',
        'data': {'function': 'train.step', 'file': '/project/train.py', 'line': 7, 'frame': '/project/train.py:7:step'}}}
        for i in range(args.events)]
    def old():
        return sum(len(protocol.encoded(dict(record, sha256=protocol.event_digest(record)))) + 1 for record in records)
    def single():
        return sum(len(protocol.encode_event_record(record)[0]) for record in records)
    def block():
        count = 0
        for offset in range(0, len(records), 64):
            # Proposed v2: one chained hash covers one complete commit block.
            # Values, sequence, identity and ordering remain encoded. Partial
            # blocks would require rejection by a separately versioned reader.
            raw = protocol.encoded({'schema': 'raven.trace-block/v2-experiment', 'prev': '0' * 64,
                'stream': 'abcdef0123456789', 'first_seq': offset + 1,
                'events': [record['event'] for record in records[offset:offset + 64]]})
            count += len(raw) + len(hashlib.sha256(raw).hexdigest()) + 15
        return count
    samples = []
    variants = [('v1_two_encodes', old), ('v1_single_encode', single), ('v2_block64_experiment', block)]
    for repetition in range(args.repeats):
        for name, function in variants if repetition % 2 == 0 else list(reversed(variants)):
            start, cpu = time.perf_counter(), time.process_time()
            size = function()
            samples.append({'variant': name, 'repetition': repetition, 'bytes': size,
                'wall_seconds': time.perf_counter() - start, 'cpu_seconds': time.process_time() - cpu})
    report = {'schema': 'raven.trace-integrity-experiment/v1', 'python': platform.python_version(),
        'events': args.events, 'samples': samples,
        'limitations': ['In-memory encoding only: no disk writes, fsync, transport, reader, migration or crash recovery.',
            'v2 block is an experimental encoding-cost lower bound, not a production format.']}
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({name: {'wall_ms': statistics.median(x['wall_seconds'] for x in samples if x['variant'] == name) * 1000,
        'bytes_per_event': next(x['bytes'] for x in samples if x['variant'] == name) / args.events} for name, _ in variants}, indent=2))


if __name__ == '__main__':
    main()
