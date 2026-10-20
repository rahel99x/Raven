"""Reproduce the shared fake-Git PID-file ambiguity without altering cleanup."""
import json,os,signal,subprocess,tempfile,threading,time
from pathlib import Path
from raven import update_source as U

def state(pid):
    try:
        v=Path('/proc/%d/stat'%pid).read_text().split(') ',1)[1].split()
        return {'pid':pid,'state':v[0],'ppid':int(v[1]),'pgid':int(v[2]),'sid':int(v[3]),'start':v[19]}
    except (FileNotFoundError,ProcessLookupError):return None
with tempfile.TemporaryDirectory(prefix='raven-helper-interference-') as directory:
    root=Path(directory);binary=root/'bin';binary.mkdir();pidfile=root/'helper.pid'
    fake=binary/'git';fake.write_text('#!/bin/sh\n/bin/sh -c \'echo $$ > "$1"; exec /bin/sleep 30\' helper "$RAVEN_TEST_HELPER_PID" &\nexit 0\n');fake.chmod(0o700)
    os.environ['PATH']=str(binary);os.environ['RAVEN_TEST_HELPER_PID']=str(pidfile);U.COMMAND_TIMEOUT=.2
    real_popen=subprocess.Popen;owned=[];other=[];first=[]
    def tracked(*a,**kw):
        p=real_popen(*a,**kw);owned.append(p);return p
    def unrelated_git():
        deadline=time.monotonic()+2
        while not pidfile.exists() and time.monotonic()<deadline:time.sleep(.001)
        first.append(int(pidfile.read_text()))
        other.append(real_popen([str(fake),'background-operation'],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True))
    subprocess.Popen=tracked
    thread=threading.Thread(target=unrelated_git)
    thread.start()
    try:
        try:U.git(root,'status')
        except U.SourceUpdateError as exc:error=str(exc)
        thread.join(2)
        observed=int(pidfile.read_text())
        result={'schema':'raven.helper-fixture-interference/v1','command_error':error,'owned_leader':owned[0].pid,'owned_helper':state(first[0]),'pidfile_observed_helper':state(observed),'other_leader':other[0].pid,'two_second_survival_check':None,'limits':'Two explicitly owned local fake Git process groups. No production change, scheduler/network or performance claim.'}
        deadline=time.monotonic()+2
        while (state(observed) or {}).get('state') not in (None,'Z') and time.monotonic()<deadline:time.sleep(.01)
        result['two_second_survival_check']=(state(observed) or {}).get('state') not in (None,'Z')
        assert (state(first[0]) or {}).get('state') in (None,'Z')
        assert result['two_second_survival_check'] and result['pidfile_observed_helper']['pgid']==other[0].pid
        assert result['pidfile_observed_helper']['pgid']!=owned[0].pid
        print(json.dumps(result,indent=2))
    finally:
        subprocess.Popen=real_popen
        for p in other:
            # Its unreaped leader still reserves this probe-owned group ID.
            try:os.killpg(p.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            p.wait(timeout=2)
            p.stdout.close();p.stderr.close()
