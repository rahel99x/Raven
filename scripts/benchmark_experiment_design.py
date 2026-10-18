#!/usr/bin/env python3
"""Equivalent factorial inputs: simple main contrasts versus complete inference."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import resource
import statistics
import sys
import time
import tracemalloc
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from raven import experiment_design as E
from scripts.evaluate_experiment_design import base


def fixture(factors, repetitions):
    document=base();document['repetitions']=repetitions
    document['factors']=[{'id':'factor'+str(i),'levels':[{'id':str(level),'arguments':['--factor'+str(i),str(level)]} for level in (0,1)]} for i in range(factors)]
    document['recipe']['limits']['max_trials']=160
    schedule=E.prepare(document)['schedule'];observations=[]
    for row in schedule:
        value=10+sum((i+1)*x for i,x in enumerate(row['coding']))+.1*row['block']+.03*(int(row['id'][-2:])%3)
        observations.append({'id':row['id'],'value':value,'unit':'sum','output_sha256':hashlib.sha256(repr(value).encode()).hexdigest(),'semantic':document['equivalence']['semantic'],'controls':row['levels']})
    return {'schema':E.RESULT_SCHEMA,'design':document,'observations':observations}


def simple(data):
    # Same observations and known schedule; no interactions, block adjustment,
    # rank check, provenance validation or uncertainty. This is not equivalent
    # inference and cannot replace the reviewed production capability.
    schedule={r['id']:r for r in E.prepare(data['design'])['schedule']}
    return [statistics.mean(row['value'] for row in data['observations'] if schedule[row['id']]['coding'][i]==1)-statistics.mean(row['value'] for row in data['observations'] if schedule[row['id']]['coding'][i]==-1) for i in range(len(data['design']['factors']))]


def measure(function, value, repeats):
    wall=[];cpu=[]
    for _ in range(repeats):
        start=time.perf_counter_ns();cstart=time.process_time_ns();function(value)
        cpu.append((time.process_time_ns()-cstart)/1e6);wall.append((time.perf_counter_ns()-start)/1e6)
    tracemalloc.start();function(value);_,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
    return {'wall_ms':wall,'median_wall_ms':statistics.median(wall),'cpu_ms':cpu,'python_peak_bytes':peak}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--repeats',type=int,default=5);args=parser.parse_args()
    rows=[]
    for factors,repetitions in ((1,4),(2,4),(4,10)):
        data=fixture(factors,repetitions)
        rows.append({'factors':factors,'trials':len(data['observations']),'fixture_sha256':E.digest(data),'json_bytes':len(json.dumps(data).encode()),'simple_main_contrasts':measure(simple,data,args.repeats),'validated_full_inference':measure(E.analyze,data,args.repeats),'compile':measure(E.prepare,data['design'],args.repeats)})
    result={'schema':'raven.factorial-benchmark/v1','rows':rows,'process_maxrss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,'python':sys.version,'source_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ('raven/experiment_design.py','scripts/benchmark_experiment_design.py')},'limits':'Local no-scheduler CPU; RSS is whole process high-water mark, Python peaks are separate incremental allocations. Simple contrast baseline omits required inference and validation.'}
    args.output.write_text(json.dumps(result,indent=2)+'\n')
if __name__=='__main__': main()
