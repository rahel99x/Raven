import json, statistics, tempfile, time, resource
from raven.actions import Actions
from raven.config import Config
from raven.controller import App
from raven.model import Store
from raven.slurm import FakeBackend, Slurm

def run(asynchronous):
    with tempfile.TemporaryDirectory() as directory:
        slurm = Slurm(FakeBackend('alex'), 'alex')
        store = Store(directory, persist=False)
        store.apply_jobs(slurm.jobs())
        app = App(store, None, Actions(slurm, store), Config(), 'alex', ascii_=True, interactive=asynchronous)
        app.visible_ids = [record.id for record in store.jobs]
        app.selected_id = app.visible_ids[0]
        original = slurm.cancel
        calls=[]
        def slow(ids):
            calls.append(list(ids));time.sleep(.1);return original(ids)
        slurm.cancel=slow
        app.run_command('cancel ' + app.selected_id)
        start=time.perf_counter();app.finish_confirm(True);returned=time.perf_counter()
        while app.research is not None and app.research.pending:
            app.research.poll_task();time.sleep(.001)
        end=time.perf_counter()
        assert len(calls)==1 and 'sent' in app.message
        if app.research: app.research.close()
        return {'handler_seconds':returned-start,'completion_seconds':end-start,'scheduler_calls':len(calls)}
values={name:[run(flag) for _ in range(7)] for name,flag in [('equivalent_synchronous_baseline',False),('interactive_worker',True)]}
result={'schema':'raven.mutation-latency/v1','fixture':'identical one-target FakeBackend cancellation with 100 ms scheduler delay','limits':'cloud Linux Python process; scheduler delay simulated; baseline retains synchronous CLI path as equivalent old blocking control; no live scheduler latency claim','repetitions':7,'results':{name:{'handler_median_ms':statistics.median(v['handler_seconds'] for v in values)*1000,'completion_median_ms':statistics.median(v['completion_seconds'] for v in values)*1000,'scheduler_calls_each':[v['scheduler_calls'] for v in values]} for name,values in values.items()},'max_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,'python_allocations':'not measured separately; process RSS reported only'}
open('/workspace/review-mutations-benchmark.json','w').write(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))
