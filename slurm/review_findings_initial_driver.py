#!/usr/bin/env python3
"""Adversarial-review qualification inside the existing owned Slurm fixture.

This evidence-only driver never contacts a production scheduler. Warning-case
response augmentation is declared explicitly; scheduler state is always real.
"""
from __future__ import annotations
import argparse, copy, hashlib, json, os, re, shutil, signal, subprocess, sys, time, uuid
from pathlib import Path
SOURCE = Path(os.environ.get('RAVEN_QUALIFICATION_ROOT', '/raven'))
sys.path.insert(0, str(SOURCE))


def child(output):
    assert os.getuid() == 1100 and Path('/tmp/raven-services-started').exists()
    from raven.slurm import Backend, CommandError, Slurm, SINFO_NODE_FMT
    from raven.platform_session import wrap_backend
    from raven.model import Store
    from raven.sampler import Sampler
    from raven.raven_allocation import AllocationManager
    from raven.raven_allocation_adapters import prepare
    from raven.raven_collection import make_placement_verifier
    os.environ['PYTHONPATH'] = str(SOURCE)
    out = Path(output); out.mkdir(mode=0o700)
    commands, checks, owned = [], [], []
    result = {'schema':'raven.review-live-slurm/v1','scope':'owned disposable single-node Slurm',
              'production_qualified':False,'checks':checks,'limitations':['No physical GPU, remote SSH, multi-node or production-site qualification.'],
              'response_augmentation':[], 'heavy':{}}
    def capture(argv, timeout=20, required=True):
        start=time.monotonic(); proc=subprocess.run(argv,capture_output=True,text=True,timeout=timeout)
        record={'argv':argv,'returncode':proc.returncode,'seconds':time.monotonic()-start}
        for name in ('stdout','stderr'):
            text=getattr(proc,name); file=f'command-{len(commands):04d}.{name}.txt'; (out/file).write_text(text)
            record[name]={'file':file,'bytes':len(text.encode()),'sha256':hashlib.sha256(text.encode()).hexdigest()}
        commands.append(record)
        if required and proc.returncode: raise RuntimeError(str(argv)+': '+proc.stderr[-2048:])
        return proc.stdout
    def check(name, condition, **details):
        checks.append({'name':name,'passed':bool(condition),**details})
        if not condition: raise AssertionError(name+': '+str(details))
    class RecordingBackend(Backend):
        probe_json=True; probe_slurm_version=True
        warning=False
        def inspect_command(self,argv,timeout,limit,cancel=None,*,depth=0):
            raw=capture(list(argv),timeout)
            if len(raw.encode())>limit: raise CommandError('qualification command exceeded inspection bound')
            if self.warning and argv[0]=='squeue' and '--json' in argv:
                doc=json.loads(raw);doc['warnings']=[{'description':'qualification response warning, scheduler rows unchanged'}]
                raw=json.dumps(doc)
                result['response_augmentation'].append({'kind':'JSON warning only','command':len(commands)-1})
            return raw
        def run(self,argv,timeout=8):
            start=time.monotonic(); return self.inspect_command(argv,timeout,4<<20), time.monotonic()-start
    raw=RecordingBackend()
    settings={'queue_seconds':.005,'starts_seconds':.005,'history_seconds':.005,'inventory_seconds':.005,
              'details_seconds':.005,'controller_requests_per_minute':1000,'controller_burst':100,
              'accounting_requests_per_minute':1000,'action_validation_requests_per_minute':1000,
              'local_processes_per_minute':1000,'step_launches_per_minute':1000,'step_launch_interval_seconds':.1}
    result['fixture_collection_overrides']=settings
    cfg={'raven':{'collection':settings},'collection_cache_dir':str(out/'cache')}
    backend=wrap_backend(raw,cfg,{'user':'raven','cluster':'raven-ci','qualification':str(out)},None)
    scheduler=Slurm(backend,'raven')
    sampler=None
    def submit(name,wrap='true',hold=True,**kw):
        argv=['sbatch','--parsable','--account=labx','--partition=local','--nodes=1','--ntasks=1','--mem=32M',
              '--time=00:02:00','--job-name='+name,'--output=/home/raven/review-%j.log']
        if hold:argv+=['--hold']
        argv += ['--'+key.replace('_','-')+'='+str(value) for key,value in kw.items()]
        jid=capture(argv+['--wrap='+wrap]).strip().split(';')[0]
        assert jid.isdecimal();owned.append(jid);return jid
    def refresh():
        backend.invalidate();time.sleep(.025)
    def wait_terminal(ids, timeout=90):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            rows=capture(['sacct','-X','-n','-P','-j',','.join(ids),'-S','now-1hours','-o','JobID,State'])
            seen={}
            for row in rows.splitlines():
                if '|' not in row:continue
                identity,state=row.split('|')[:2]
                seen.setdefault(identity.split('+')[0],[]).append(state.split()[0].split('+')[0])
            terminal={'CANCELLED','COMPLETED','FAILED','TIMEOUT'}
            if all(seen.get(j) and all(state in terminal for state in seen[j]) for j in ids):return
            time.sleep(.2)
        raise RuntimeError('terminal accounting deadline: '+str(ids))
    def wait_manager(manager, wanted, timeout=45):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            manager.tick(); snap=manager.snapshot()
            if snap.get('state')==wanted:return snap
            if wanted=='heavy' and snap.get('state') in {'light','reconciling'} and snap.get('error'):
                return snap
            manager.wait_for_activity(min(.2,deadline-time.monotonic()))
        raise RuntimeError('Heavy state deadline: '+str(manager.snapshot()))
    def heavy_config(state):
        from raven.raven_config import DEFAULTS, merge
        return merge(copy.deepcopy(DEFAULTS), {'site':{'kind':'slurm','cluster_id':''},'connection':{'transport':'local'},
            'execution':{'heavy_target':'slurm','heavy_requires_allocation':True,'worker_python':sys.executable},
            'allocation':{'account':'labx','partition':'local','cpus_per_task':1,'memory_mib':256,
                'walltime_seconds':45,'idle_release_seconds':0,'pending_timeout_seconds':30},
            'resources':{'heavy':{'max_workers':1,'max_tasks':1,'memory_mib':256,'cpu_seconds':30}},
            'storage':{'allocation_receipts_dir':str(state/'receipts')}})
    try:
        version=capture(['scontrol','--version']).strip();result['version']=version
        capture(['scontrol','show','config']);capture(['scontrol','show','nodes','-o'])
        if not os.environ.get('RAVEN_REVIEW_HEAVY_ONLY'):
            for index in range(30):submit('raven-review-held-'+str(index))
            jobs=scheduler.jobs()
            check('COL-1 governed auto reader returns all thirty held jobs',len(jobs)==30,records=len(jobs),reader=scheduler.reader_status.get('jobs'))
            queue_commands=[c['argv'] for c in commands if c['argv'][0]=='squeue']
            modern=version.startswith('slurm 26.05.')
            check('COL-1 old versions avoid unqualified full JSON fetch',modern or not any('--json' in a for a in queue_commands),commands=queue_commands)
            if modern:
                queue_json=next(c for c in commands if c['argv'][0]=='squeue' and '--json' in c['argv'])
                text=(out/queue_json['stdout']['file']).read_text()
                check('COL-1 actual pretty JSON exceeds old physical-line budget but semantic records pass',len(text.splitlines())>10000,physical_lines=len(text.splitlines()),records=len(jobs))
            store=Store(persist=False);store.apply_jobs(jobs)
            sampler=Sampler(scheduler,store,{},[],gpu_sampling=False,workers=1,weather=False,budget=False)
            victim=owned[0];capture(['scancel',victim]);wait_terminal([victim]);refresh()
            raw.warning=modern
            sampler.src_jobs()
            check('COL-2 actual cancelled job departs Store under warning-compatible publication',victim not in {job.id for job in store.jobs},remaining=len(store.jobs),diagnostic=scheduler.parse_diagnostics.get('jobs'))
            if modern:
                check('COL-2 warning note does not mark complete listing partial',not scheduler.parse_diagnostics['jobs']['partial'] and bool(scheduler.parse_diagnostics['jobs']['note']))
            else:
                result['limitations'].append('COL-2 JSON-warning augmentation exercised only on qualified26.05JSON; older native text departure is unmodified real output.')
            raw.warning=False
            sinfo=capture(['sinfo','-h','-N','-O',SINFO_NODE_FMT])
            check('COL-11 real sinfo delimiter emits eight nontruncated fields',all(len(row.split('|'))==8 for row in sinfo.splitlines() if row),raw=sinfo)
            parts,totals,nodemap=scheduler.partitions([])
            check('COL-11 Raven decodes actual delimited node map',bool(nodemap) and 'raven-ci' in nodemap,nodes=list(nodemap))
            # Real heterogeneous component identity, with no running CPU demand.
            script=out/'hetero.sh';script.write_text('#!/bin/sh\n#SBATCH --account=labx\n#SBATCH --hold\n#SBATCH --nodes=1\n#SBATCH --ntasks=1\n#SBATCH --mem=32M\n#SBATCH --time=00:01:00\n#SBATCH hetjob\n#SBATCH --account=labx\n#SBATCH --nodes=1\n#SBATCH --ntasks=1\n#SBATCH --mem=32M\n#SBATCH --time=00:01:00\ntrue\n')
            het=capture(['sbatch','--parsable',str(script)]).strip().split(';')[0];owned.append(het)
            try:
                refresh();detail=scheduler.details(het+'+1')
                check('COL-6 real heterogeneous component details preserve group identity',detail.get('HetJobId')==het and detail.get('HetJobOffset')=='1',details=detail)
            except Exception as exc:
                result['heterogeneous_detail_failure']=str(exc)
                raise
            capture(['scancel',het])
            capture(['scontrol','release',','.join(owned[1:-1])])
            wait_terminal(owned)
            fast=[submit('raven-review-completed-'+str(index),hold=False) for index in range(2)]
            wait_terminal(fast)
            refresh(); history=scheduler.finished(1)
            check('COL-1 governed history returns at least thirty parent jobs',len(history)>=30,records=len(history),reader=scheduler.reader_status.get('finished'))
            bounded_settings={**settings,'max_response_records':10}
            bounded=wrap_backend(raw,{'raven':{'collection':bounded_settings},'collection_cache_dir':str(out/'bounded-cache')},
                                 {'user':'raven','cluster':'raven-ci','qualification':'bounded-history'},None)
            limited=Slurm(bounded,'raven',json_mode='legacy');retained=limited.finished(1)
            check('COL-14 actual native -X history truncates with explicit coverage',len(retained)==10 and limited.parse_diagnostics['finished']['partial'],
                  diagnostic=limited.parse_diagnostics['finished'],coverage=bounded.observation_coverage)
            result['reader_status']=scheduler.reader_status
        # Dedicated Heavy default adapter: prove actual scheduler owner and PID
        # membership. task/none cannot claim a cgroup it does not supply.
        state=out/'heavy-default';state.mkdir(mode=0o700)
        verifier = make_placement_verifier(backend)
        def recorded_verifier(candidate, cancel=None):
            result['heavy']['candidate'] = candidate
            return verifier(candidate, cancel)
        manager=AllocationManager(heavy_config(state),state_dir=state,placement_verifier=recorded_verifier)
        manager.collection_backend=backend
        try:
            manager.request();snapshot=wait_manager(manager,'heavy')
            result['heavy']['default_adapter']=snapshot
            if snapshot.get('state')=='heavy':
                task=manager.worker.submit('statistics',{'document':{'schema':'raven.statistics/v1','design':'independent','unit':'seed',
                     'control':[{'id':str(i),'value':i} for i in range(3)],'treatment':[{'id':str(i+3),'value':i+3} for i in range(3)]}}, {})
                deadline=time.monotonic()+10
                while time.monotonic()<deadline:
                    manager.tick();entry=manager.worker.tasks[task]
                    if entry.get('status')=='committed':break
                    manager.wait_for_activity(.2)
                check('Heavy actual verified worker commits scientific task',entry.get('status')=='committed')
                manager.worker.ack(task)
            else:
                result['limitations'].append('Heavy default adapter did not become ready in task/none fixture: '+str(snapshot.get('error')))
            manager.release();released=wait_manager(manager,'light')
            check('HV-3/6 dedicated actual allocation releases with confirmed cleanup',not released.get('cleanup_uncertain'),state=released)
        finally:manager.shutdown()
        # An owned foreground salloc with no scheduler step reserves the node.
        # It supplies a real pending-cancel case and then an idle reuse parent.
        parent_name='raven-review-parent-'+uuid.uuid4().hex
        parent_argv=['salloc','--account=labx','--partition=local','--nodes=1','--ntasks=1',
                     '--cpus-per-task=2','--mem=512M','--time=00:02:00','--job-name='+parent_name,'sleep','120']
        parent_log=(out/'parent-salloc.log').open('w')
        parent=subprocess.Popen(parent_argv,stdin=subprocess.DEVNULL,stdout=parent_log,stderr=subprocess.STDOUT,
                                start_new_session=True)
        result['heavy']['parent_launch_argv']=parent_argv
        try:
            parent_id=None;deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                rows=capture(['squeue','-u','raven','--name',parent_name,'-h','-o','%i|%T']).strip().splitlines()
                if len(rows)==1 and rows[0].endswith('|RUNNING'):
                    parent_id=rows[0].split('|')[0];owned.append(parent_id);break
                if parent.poll() is not None:raise RuntimeError('Owned parent allocation exited before grant')
                time.sleep(.1)
            check('Owned parent reserves both fixture CPUs',bool(parent_id),parent_job=parent_id)
            pending_state=out/'heavy-pending';pending_state.mkdir(mode=0o700)
            pending=AllocationManager(heavy_config(pending_state),state_dir=pending_state,placement_verifier=make_placement_verifier(backend))
            pending.collection_backend=backend
            try:
                pending.request();snapshot=wait_manager(pending,'pending')
                token_name=snapshot['request_name'];queued='';deadline=time.monotonic()+10
                while time.monotonic()<deadline:
                    queued=capture(['squeue','-u','raven','--name',token_name,'-h','-o','%i|%T']).strip()
                    pending.tick()
                    if queued.endswith('|PENDING'):break
                    time.sleep(.1)
                check('HV-3 dedicated Heavy is actually queued before cancel',queued.endswith('|PENDING'),scheduler_row=queued)
                pending.cancel();stopped=wait_manager(pending,'light')
                remaining=capture(['squeue','-u','raven','--name',token_name,'-h','-o','%i']).strip()
                check('HV-3 pending cancellation revokes actual queued request',not remaining and not stopped.get('cleanup_uncertain'),state=stopped)
                result['heavy']['pending_cancel']=stopped
            finally:pending.shutdown()
            reuse_state=out/'heavy-reuse';reuse_state.mkdir(mode=0o700)
            reuse_config=heavy_config(reuse_state)
            assert reuse_config['site']['cluster_id']==''
            reuse=AllocationManager(reuse_config,state_dir=reuse_state,placement_verifier=make_placement_verifier(backend))
            reuse.collection_backend=backend
            try:
                reuse.request_reuse_review(parent_id);deadline=time.monotonic()+20;review=None
                while time.monotonic()<deadline:
                    reuse.tick();review=reuse.snapshot().get('reuse_review',{})
                    if review.get('state') in ('ready','error'):break
                    reuse.wait_for_activity(.2)
                check('HV-1/5 empty-profile actual idle parent passes exact controller-bound reuse review',review and review.get('state')=='ready' and review.get('plan',{}).get('cluster_id')=='raven-ci',review=review)
                reuse.request(reuse=review['token'],plan=review['plan']);ready=wait_manager(reuse,'heavy')
                check('HV-5 reused worker proves real parent/step process membership',ready.get('state')=='heavy' and ready.get('identity',{}).get('allocation_id')==parent_id,state=ready)
                reuse.release();released=wait_manager(reuse,'light')
                parent_status=capture(['squeue','-u','raven','-j',parent_id,'-h','-o','%T']).strip()
                check('HV-5 releasing owned step preserves running parent',not released.get('cleanup_uncertain') and parent_status=='RUNNING' and parent.poll() is None,state=released,parent_state=parent_status)
                result['heavy']['reuse_release']=released
            finally:reuse.shutdown()
        finally:
            if parent.poll() is None:
                os.killpg(parent.pid,signal.SIGHUP)
                try:parent.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(parent.pid,signal.SIGTERM);parent.wait(timeout=20)
            parent_log.close()
        result['status']='passed-recorded-checks'
    except Exception as exc:
        result.update(status='failed',error=type(exc).__name__+': '+str(exc))
    finally:
        if sampler is not None:
            sampler.stop.set();sampler.worker_scheduler.shutdown(wait=False,cancel_futures=True)
        if owned:
            try:capture(['scancel',','.join(owned)],required=False)
            except Exception:pass
        result['commands']=commands
        (out/'manifest.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps({key:result.get(key) for key in ('status','version','error')}))
    return int(result['status']!='passed-recorded-checks')


def main():
    p=argparse.ArgumentParser();p.add_argument('--output');p.add_argument('--revision');p.add_argument('--child');a=p.parse_args()
    if a.child:return child(a.child)
    assert os.getuid()==0 and Path('/tmp/raven-services-started').exists()
    if Path('/raven/HEAVY_ONLY').exists():
        os.environ['RAVEN_REVIEW_HEAVY_ONLY']='1'
    target='/home/raven/review-'+uuid.uuid4().hex
    proc=subprocess.run(['runuser','-u','raven','--','python3',__file__,'--child',target],text=True,capture_output=True,timeout=240)
    output=Path(a.output);output.mkdir(parents=True,exist_ok=True)
    shutil.copytree(target,output,dirs_exist_ok=True)
    (output/'driver.stdout').write_text(proc.stdout);(output/'driver.stderr').write_text(proc.stderr)
    (output/'source-manifest.json').write_bytes(Path('/raven/source-manifest.json').read_bytes())
    print(proc.stdout.strip())
    if proc.returncode:sys.stderr.write(proc.stderr)
    return proc.returncode
if __name__=='__main__':raise SystemExit(main())
