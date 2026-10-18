#!/usr/bin/env python3
"""Bounded comparison against normalized pairwise SequenceMatcher; no file I/O timing."""
import argparse
import difflib
import json
from pathlib import Path
import resource
import statistics
import sys
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from raven.log_divergence import compare
from raven.log_match_index import template_key


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--output', required=True); args = parser.parse_args()
    results = []
    for repeated in (False, True):
        sources = [dict(path=f'{rank}.log', label=str(rank), rank_identity=dict(rank=rank, cohort='workers'),
                        lines=[f'rank={rank} step {i % 3 if repeated else i}' for i in range(240)]) for rank in range(16)]
        sources[-1]['lines'][179] = 'rank=15 ERROR 137'
        def baseline():
            keys = [[template_key(line, s['rank_identity']['rank']) for line in s['lines']] for s in sources]
            return [difflib.SequenceMatcher(None, keys[0], other, autojunk=False).get_opcodes() for other in keys[1:]]
        for label, fn in [('normalized-pairwise-diff', baseline), ('cohort-first-divergence', lambda: compare(sources))]:
            samples = []
            for _ in range(7):
                start = time.perf_counter_ns(); value = fn(); samples.append((time.perf_counter_ns() - start) / 1e6)
            tracemalloc.start(); fn(); _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
            if label.startswith('cohort'):
                assert value['cohorts'][0]['first_divergence'] == [179] * 16
            results.append(dict(fixture='repeated' if repeated else 'unique', implementation=label,
                                median_ms=statistics.median(samples), samples_ms=samples, python_peak_bytes=peak))
    Path(args.output).write_text(json.dumps(dict(results=results, process_maxrss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        scope='16 ranks, 240 rows; normalized pairwise diff returns more edit operations than first-divergence analysis; neither establishes causality'), indent=2)+'\n')

if __name__ == '__main__': main()
