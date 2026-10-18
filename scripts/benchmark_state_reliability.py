import json, resource, statistics, sys, tempfile, time, tracemalloc
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from raven.model import Store, Job

def measure(fn, count):
    values=[]; start_cpu=time.process_time()
    for i in range(count):
        start=time.perf_counter_ns(); fn(i); values.append((time.perf_counter_ns()-start)/1e6)
    return {"count":count,"median_ms":statistics.median(values),"p95_ms":sorted(values)[int(.95*(count-1))],"cpu_seconds":time.process_time()-start_cpu}

with tempfile.TemporaryDirectory(prefix="raven-state-bench-") as d:
    store=Store(d)
    tags=measure(lambda i:store.note("7", "x"*1024+str(i)),80)
    sample={"t":1.,"k":"live","cpu":.5,"rss":1024.}
    memory=Store(persist=False)
    append=measure(lambda i:memory.record("7",dict(sample,t=i)),10000)
    jobs=[Job(str(i),"training","gpu","RUNNING",submit="2026-10-17T10:00:00",start="2026-10-17T10:00:01") for i in range(1000)]
    queue=measure(lambda i:memory.apply_jobs(jobs),30)
    def reopen(i):
        s=Store(str(Path(d)/str(i)))
        if hasattr(s,"close"):s.close()
    ownership=measure(reopen,30)
    tracemalloc.start()
    tracked=Store(persist=False)
    for i in range(1000):tracked.record("7",dict(sample,t=i))
    current, peak=tracemalloc.get_traced_memory();tracemalloc.stop()
    print(json.dumps({"source":sys.argv[1],"tags_atomic_publication":tags,"memory_append":append,"queue_1000_jobs":queue,"state_open_close":ownership,"python_allocation_1000_samples_peak_bytes":peak,"process_peak_rss_kib":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,"tags_bytes":(Path(d)/"tags.json").stat().st_size},indent=2))
    if hasattr(store,"close"):store.close()
