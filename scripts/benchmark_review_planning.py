#!/usr/bin/env python3
"""Controlled new-planning costs; same fixtures, explicit difference in work.

Run serially without live qualification/builds. This measures CPU, Python
allocations and separate process RSS, never scientific efficacy or site latency.
"""
import copy
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile


def fixtures(folder):
    from raven import orchestrator as X
    request = dict(name="solver", partition="cpu", account="lab", qos="normal", cpus=1, nodes=1, gpus=0, mem_bytes=104857600, time_seconds=60)
    context = dict(policy_digest="fixture", reservations_visible=True, policy_complete=True)
    outcomes = [dict(id=str(i), request=request, context=context, origin="synthetic", submit=i*1000, issued_at=i*1000+1,
                     actual_start=i*1000+21+(i%5), actual_end=i*1000+31+(i%5), state="completed", model_wait_seconds=10, scheduler_wait_seconds=20) for i in range(500)]
    forecast = dict(schema="raven.forecast-scenarios/v1", now=1000000, request=request, context=context, alternatives=[], observations=outcomes, model_wait_seconds=10)
    scope = dict(schema="raven.execution-scope/v1", backend="local", connection_host="fixture", configured_host="", profile="", user="fixture", uid=os.getuid(), cluster="fixture")
    nodes, jobs = [], []
    script = folder / "job.sh"
    script.write_text("#!/bin/sh\ntrue\n")
    for i in range(32):
        ident = f"task-{i:02}"
        nodes.append(dict(id=ident, script=str(script), resources=dict(nodes=1, ntasks=1, cpus_per_task=1, mem="100M", time="00:01:00", account="lab", qos="normal")))
        jobs.append(dict(id=ident, submit=0, duration=1+i%10, wall_limit=60, nodes=1, cpus_per_node=1, memory_mb_per_node=100, gpus_per_node=0, priority=1, account="lab", qos="normal", depends=[]))
    queue = dict(schema="raven.queue-model/v1", id="benchmark", nodes=[dict(id="n", cpus=8, memory_mb=8000, gpus=0)],
                 policy=dict(backfill=True, fairshare_weight=0, usage_half_life_seconds=86400, accounts={"lab":dict(weight=1,usage_cpu_seconds=0)}, qos={"normal":dict(max_running=64,max_cpus=64,max_wall_seconds=1000)}),jobs=jobs)
    campaign = dict(schema="raven.campaign-policy/v1", review=X.prepare_review("workflow",dict(version=1,kind="raven.workflow",nodes=nodes),workdir=folder,scope=scope),queue_model=queue,
                    limits=dict(max_running=8,max_submit_jobs=16,grp_cpus=8,grp_memory_mb=8000,grp_gpus=0,commands_per_minute=120,external_jobs=0,external_cpus=0,external_memory_mb=0,external_gpus=0,complete=True))
    return forecast, campaign


def child(case):
    import resource
    import time
    import tracemalloc
    from raven import checkpoint_advisor as C, forecast_scenarios as F, campaign_policy as P, slurm_simulator as S
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="raven-planning-benchmark-") as folder:
        forecast, campaign = fixtures(Path(folder))
        advice = json.loads((root/"examples/checkpoint-advice.json").read_text())
        queue = copy.deepcopy(campaign["queue_model"])
        queue["policy"]["backfill"] = case != "queue-fifo"
        functions = {
            "checkpoint-advice": lambda:C.advise(advice),
            "forecast-cohort-point": lambda:F._fit(forecast["observations"],None,.8,30,residual=False),
            "forecast-current-point": lambda:F._current(forecast["observations"],dict(request=forecast["request"],scheduler_wait_seconds=20),forecast["now"],.8,30),
            "forecast-residual-point": lambda:F._fit(forecast["observations"],10,.8,30,residual=True),
            "forecast-complete-audit": lambda:F.analyze(forecast),
            "queue-fifo": lambda:S.simulate(queue),
            "queue-backfill": lambda:S.simulate(queue),
            "campaign-fifo": lambda:P.schedule(campaign,"fifo"),
            "campaign-complete-comparison": lambda:P.analyze(campaign),
        }
        run=functions[case]
        for _ in range(2): run()
        times=[]
        for _ in range(5):
            before=time.process_time(); result=run(); times.append((time.process_time()-before)*1000)
        del result
        tracemalloc.start(); result=run(); _,peak=tracemalloc.get_traced_memory(); tracemalloc.stop()
        print(json.dumps(dict(case=case,cpu_ms=times,median_cpu_ms=statistics.median(times),peak_python_bytes=peak,
                              peak_process_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                              forecast_records=500,campaign_jobs=32,result_status=result.get("status"),
                              placement_operations=result.get("placement_operations"))))


if __name__=="__main__":
    if len(sys.argv)>1:
        child(sys.argv[1])
    else:
        cases=("checkpoint-advice","forecast-cohort-point","forecast-current-point","forecast-residual-point","forecast-complete-audit","queue-fifo","queue-backfill","campaign-fifo","campaign-complete-comparison")
        root=Path(__file__).resolve().parents[1]
        env=dict(os.environ,PYTHONPATH=str(root))
        samples=[]
        for case in cases:
            result=subprocess.run([sys.executable,__file__,case],cwd=root,env=env,text=True,capture_output=True,check=True)
            samples.append(json.loads(result.stdout))
        print(json.dumps(dict(scope="New functionality cost, no claimed before/after speedup; identical 500-outcome and 32-job fixtures",memory="Python peak separate from process RSS including imports/fixture construction",samples=samples),indent=2))
