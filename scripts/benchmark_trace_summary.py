#!/usr/bin/env python3
"""Synthetic analysis fixtures; no claim of live scheduler/GPU trace efficacy."""
import argparse
import json
import math
from pathlib import Path
import random
import resource
import statistics
import sys
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from raven.trace_summary import summarize, answer


def snapshot(values):
    events, now = [], 0
    for i, value in enumerate(values):
        for kind in ('span_start', 'span_end'):
            events.append(dict(seq=len(events)+1, thread='one', pid=1, kind=kind, mono_ns=now,
                data=dict(span_id=str(i), name='phase', rank=i%4)))
            now += value if kind == 'span_start' else 1
    identity=dict(job_id='1',attempt='synthetic',cluster='fixture',node='local',boot_id='fixture',pid=1,process_start='1',stream='fixture01')
    return dict(status='COMPLETE',events=events,identity=identity,sha256='a'*64,covered={'scope':'synthetic full fixture'},evicted=0,warnings=[])


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args()
    rng=random.Random(221); results=[]
    for name,values in [('constant',[10**6]*4000),('heavy-tailed',[rng.randrange(1<<rng.randrange(1,55)) for _ in range(4000)]),('adversarial',[2**(i%50)+(i%17) for i in range(4000)])]:
        source=snapshot(values); timings=[]
        for _ in range(5):
            start=time.perf_counter_ns(); document=summarize(source); timings.append((time.perf_counter_ns()-start)/1e6)
        errors=[]; ordered=sorted(values); quantiles=(.5,.9,.95,.99,1)
        starts=time.perf_counter_ns()
        answers=[answer(document,'quantile',quantile=q) for q in quantiles]
        query_ms=(time.perf_counter_ns()-starts)/1e6
        for q,result in zip(quantiles,answers):
            exact=ordered[math.ceil(q*len(values))-1]
            assert result['lower']<=exact<=result['upper']
            errors.append(dict(q=q,exact_ns=exact,estimate_ns=result['value'],lower_ns=result['lower'],upper_ns=result['upper'],absolute_error_ns=abs(exact-result['value'])))
        tracemalloc.start();summarize(source);_,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
        results.append(dict(fixture=name,intervals=len(values),ingest_median_ms=statistics.median(timings),samples_ms=timings,
            query_five_ms=query_ms,python_peak_bytes=peak,summary_bytes=len(json.dumps(document)),raw_fixture_bytes=len(json.dumps(source)),queries=errors))
    Path(args.output).write_text(json.dumps(dict(results=results,process_maxrss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        scope='Synthetic complete interval fixtures; RSS is whole process, Python peak measured separately; no event-order/causality query'),indent=2)+'\n')
if __name__=='__main__':main()
