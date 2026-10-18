#!/usr/bin/env python3
"""Interleaved identical owned CPU workload, collector off/on; no scheduler/GPU."""
import argparse
import json
import os
from pathlib import Path
import resource
import socket
import statistics
import subprocess
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from raven.process_profile import inspect, collect

CODE='import time,json; start=time.perf_counter(); answer=sum((i*i)%1000003 for i in range(15000000)); print(json.dumps(dict(seconds=time.perf_counter()-start,answer=answer)),flush=True)'

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args(); rows=[]
    owner=dict(site_kind='desktop',host=socket.gethostname(),uid=os.getuid(),pid=os.getpid(),verified=True,verification='explicit benchmark-owned process')
    for repetition in range(3):
        for enabled in ((False,True) if repetition%2==0 else (True,False)):
            process=subprocess.Popen([sys.executable,'-c',CODE],stdout=subprocess.PIPE,text=True)
            report=None
            try:
                if enabled: report=collect(inspect(process.pid,owner),dict(seconds=4,interval_ms=100,cpu_budget_percent=2),owner)
                out,_=process.communicate(timeout=10); assert process.returncode==0
                result=json.loads(out)
                rows.append(dict(repetition=repetition,enabled=enabled,**result,
                    collector_cpu_seconds=report['collector_cpu_seconds'] if report else 0,
                    samples=report['samples'] if report else 0,intervals=report['actual_intervals_seconds'] if report else [],
                    tasks=report['coverage']['tasks'] if report else 0,issues=report['issues'] if report else {}))
            finally:
                if process.poll() is None: process.terminate();process.wait(timeout=5)
    assert len({row['answer'] for row in rows})==1
    medians={str(enabled):statistics.median(row['seconds'] for row in rows if row['enabled']==enabled) for enabled in (False,True)}
    Path(args.output).write_text(json.dumps(dict(results=rows,median_seconds=medians,slowdown_percent=100*(medians['True']/medians['False']-1),
        process_maxrss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        scope='One local owned Python CPU workload; no live Slurm, GPU, multi-node or universal overhead claim'),indent=2)+'\n')
if __name__=='__main__':main()
