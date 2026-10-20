"""Actual isolated local stdlib helper, serial cold/warm transport fixtures."""
from pathlib import Path
import json
import resource
import shlex
import statistics
import subprocess
import sys
import tempfile
import time
import raven
from raven import projects
from raven.remote import RemoteFiles
class SSH:
    def __init__(self): self.commands=[]
    def run(self, argv, timeout):
        self.commands.append(argv)
        value=subprocess.run(argv,cwd='/tmp',text=True,capture_output=True,timeout=timeout)
        if value.returncode: raise OSError(value.stderr)
        return value.stdout, 0
with tempfile.TemporaryDirectory(prefix='raven-review-project-') as root:
    files=RemoteFiles(SSH())
    start=time.monotonic(); expected=projects.discover_project(root,files=files)
    cold=time.monotonic()-start
    sent=sum(len(shlex.join(cmd).encode()) for cmd in files.ssh.commands)
    initial=len(files.ssh.commands)
    prior=resource.getrusage(resource.RUSAGE_CHILDREN)
    latencies=[]
    for _ in range(25):
        start=time.monotonic(); assert projects.discover_project(root,files=files)==expected
        latencies.append((time.monotonic()-start)*1000)
    usage=resource.getrusage(resource.RUSAGE_CHILDREN)
    warm=sum(len(shlex.join(cmd).encode()) for cmd in files.ssh.commands[initial:])
    report={'source':raven.__file__, 'reads':25, 'cold_wall_ms':cold*1000,'cold_command_bytes':sent,
            'cold_processes':initial,'warm_median_wall_ms':statistics.median(latencies),
            'warm_command_bytes_per_read':warm/25, 'warm_processes':len(files.ssh.commands)-initial,
            'warm_child_cpu_seconds':usage.ru_utime+usage.ru_stime-prior.ru_utime-prior.ru_stime,
            'child_peak_rss_kib':usage.ru_maxrss,'driver_peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            'qualification':'Actual isolated Python helper on local subprocess transport; no live SSH network or scheduler. Warm portable cache already exists where reported cold_processes=1.'}
    Path(sys.argv[1]).write_text(json.dumps(report,indent=2)+'\n')
