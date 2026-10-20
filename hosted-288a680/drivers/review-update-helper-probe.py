"""Observe the real Git timeout group cleanup without changing production code."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from raven import update_source as U

parser=argparse.ArgumentParser()
parser.add_argument('--iterations',type=int,default=30)
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()


def state(pid):
    if pid is None:return None
    try:
        fields=Path('/proc/%s/stat'%pid).read_text().split(') ',1)[1].split()
        status=Path('/proc/%s/status'%pid).read_text().splitlines()
        return {'pid':pid,'state':fields[0],'ppid':int(fields[1]),'pgid':int(fields[2]),'sid':int(fields[3]),'start':fields[19],**{line.split(':',1)[0]:line.split(':',1)[1].strip() for line in status if line.startswith(('SigPnd:','ShdPnd:','SigBlk:','SigIgn:','SigCgt:'))}}
    except (FileNotFoundError,ProcessLookupError):return None

original_popen,original_killpg,original_waitid=subprocess.Popen,os.killpg,os.waitid
reports=[]
with tempfile.TemporaryDirectory(prefix='raven-helper-probe-') as directory:
    root=Path(directory);binary=root/'bin';binary.mkdir();pid_file=root/'helper.pid'
    fake=binary/'git'
    fake.write_text('#!/bin/sh\n/bin/sh -c \'echo $$ > "$1"; exec /bin/sleep 30\' helper "$RAVEN_TEST_HELPER_PID" &\nexit 0\n')
    fake.chmod(0o700)
    os.environ['PATH']=str(binary);os.environ['RAVEN_TEST_HELPER_PID']=str(pid_file)
    U.COMMAND_TIMEOUT=.1
    sentinel=original_popen(['/bin/sleep','120'],start_new_session=True)
    try:
        for iteration in range(args.iterations):
            pid_file.unlink(missing_ok=True)
            report={'iteration':iteration,'events':[],'signal_sigchld':repr(signal.getsignal(signal.SIGCHLD))}
            process_box=[]
            def helper():
                try:return int(pid_file.read_text())
                except (FileNotFoundError,ValueError):return None
            def snapshot(label,**extra):
                process=process_box[0] if process_box else None
                report['events'].append({'label':label,'time':time.monotonic(),'leader_returncode':getattr(process,'returncode',None),'leader':state(getattr(process,'pid',None)),'helper':state(helper()),**extra})
            def popen(*a,**kw):
                process=original_popen(*a,**kw);process_box.append(process);snapshot('popen');return process
            def killpg(pid,sig):
                snapshot('killpg-before',group=pid,signal=int(sig))
                try:return original_killpg(pid,sig)
                finally:snapshot('killpg-after',group=pid,signal=int(sig))
            def waitid(*a):
                result=original_waitid(*a)
                snapshot('waitid',result=repr(result))
                return result
            subprocess.Popen=popen;os.killpg=killpg;os.waitid=waitid
            try:
                begin=time.monotonic()
                try:U.git(root,'status')
                except U.SourceUpdateError as error:report['error']=str(error)
                report['elapsed']=time.monotonic()-begin
                snapshot('returned')
                deadline=time.monotonic()+2
                while (state(helper()) or {}).get('state') not in (None,'Z') and time.monotonic()<deadline:
                    time.sleep(.01)
                snapshot('after-survival-bound')
                report['survived']=(state(helper()) or {}).get('state') not in (None,'Z')
                report['sentinel_alive']=sentinel.poll() is None
                assert report['sentinel_alive']
                reports.append(report)
                if report['survived']:break
            finally:
                subprocess.Popen=original_popen;os.killpg=original_killpg;os.waitid=original_waitid
                current=state(helper())
                if current and current['state']!='Z':
                    # Only the observed owned helper in this command's exact
                    # new session may be stopped by probe failure cleanup.
                    assert process_box and current['sid']==process_box[0].pid and current['pgid']==process_box[0].pid
                    descriptor=os.pidfd_open(current['pid'])
                    try:signal.pidfd_send_signal(descriptor,signal.SIGKILL)
                    finally:os.close(descriptor)
    finally:
        sentinel.kill();sentinel.wait(timeout=2)
result={'schema':'raven.git-helper-cleanup-probe/v1','python':__import__('sys').version,'iterations':len(reports),'survivors':sum(item['survived'] for item in reports),'reports':reports,'limits':'Actual owned local subprocess groups; no scheduler/network. Timing is diagnostic, not a controlled performance benchmark.'}
args.output.write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({key:value for key,value in result.items() if key!='reports'}))
