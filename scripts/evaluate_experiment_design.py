#!/usr/bin/env python3
"""Held-out synthetic effects plus actual local Python/MPI reduction trials."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from raven import diagnostic_trials as D, experiment_design as E, operations as O, ops_investigation as Ops


def base():
    return json.loads(subprocess.run([sys.executable,str(ROOT/'examples/factorial-experiment/make_design.py')],capture_output=True,text=True,check=True).stdout)


def synthetic(document):
    records=[]
    for seed in range(3000,3020):
        rng=random.Random(seed); observations=[]
        for trial in E.prepare(document)['schedule']:
            a,b=trial['coding'];value=10+2*a+3*b+4*a*b+.2*trial['block']+rng.gauss(0,.1)
            observations.append({'id':trial['id'],'value':value,'unit':'sum','semantic':document['equivalence']['semantic'],
                'controls':trial['levels'],'output_sha256':hashlib.sha256(repr(value).encode()).hexdigest()})
        result=E.analyze({'schema':E.RESULT_SCHEMA,'design':document,'observations':observations})
        truths={'order':4.,'method':6.,'order:method':8.}
        records.append({'seed':seed,'effects':result['fit']['effects'],'condition_infinity_R':result['fit']['condition_infinity_R'],
            'family_covered':all(row['interval'][0]<=truths[row['term']]<=row['interval'][1] for row in result['fit']['effects']),
            'mean_absolute_error':sum(abs(row['contrast']-truths[row['term']]) for row in result['fit']['effects'])/3})
    return records


def actual(document,root):
    recipe=E.prepare(document)['recipe']; path=root/'recipe-input.json';path.write_text(json.dumps(recipe))
    params={'manifest':str(path),'destination':str(root/'work'),'action':'launch'}
    ctx=O.Context(scope={'cluster':'explicit-local-qualification','user':'fixture-owner'},state_dir=str(root/'state'))
    original=Ops._launch_local
    Ops._launch_local=lambda recipe,workspace,ctx:D.run(recipe,workspace,cancel=ctx.cancel)
    try:
        reviewed=Ops.run('diagnostic-campaign',params,ctx)
        Ops.apply('diagnostic-campaign',reviewed['plan'],ctx)
    finally: Ops._launch_local=original
    collected=E.collect(document,[params['destination']]); analysis=E.analyze(collected['results'])
    if len(analysis['checks'])!=E.prepare(document)['trial_count']:
        logs=[p.read_text()[:2048] for p in sorted((root/'work/trials').glob('*/output.log'))[:2]]
        raise ValueError('Qualification did not finish all trials: '+repr(logs))
    return {'analysis':analysis,'results':collected['results'],'recipe_sha256':E.digest(recipe)}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',required=True,type=Path);parser.add_argument('--mpi-prefix',type=Path,help='Optional extracted matching MPI development prefix');args=parser.parse_args()
    document=base();report={'schema':'raven.factorial-evaluation/v1','synthetic':synthetic(document),
        'qualification':'Actual local diagnostic processes; MPI transport when present. No Slurm/GPU/BLAS/site deployment qualification.',
        'source_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ['raven/experiment_design.py','raven/diagnostic_trials.py','examples/factorial-experiment/reduction.py','examples/factorial-experiment/reduction_mpi.c','examples/factorial-experiment/mpi_adapter.py']}}
    with tempfile.TemporaryDirectory(prefix='raven-factorial-qualification-') as temporary:
        root=Path(temporary);python_root=root/'python';python_root.mkdir()
        report['python']=actual(document,python_root)
        compiler,launcher=shutil.which('mpicc'),shutil.which('mpiexec')
        if compiler and launcher:
            mpi_root=root/'mpi';mpi_root.mkdir();binary=mpi_root/'reduction-mpi'
            flags=[] if args.mpi_prefix is None else ['-I'+str(args.mpi_prefix/'usr/lib/x86_64-linux-gnu/openmpi/include'),'-L'+str(args.mpi_prefix/'usr/lib/x86_64-linux-gnu/openmpi/lib')]
            command=[compiler,*flags,'-O0',str(ROOT/'examples/factorial-experiment/reduction_mpi.c'),'-o',str(binary)]
            compiled=subprocess.run(command,timeout=20,capture_output=True,text=True)
            if compiled.returncode:
                raise ValueError('MPI development toolchain failed: '+compiled.stderr[:4096])
            report['mpi_compile']={'argv':command,'header_override':str(args.mpi_prefix) if args.mpi_prefix else None}
            mpi=copy.deepcopy(document);mpi['id']='controlled-mpi-reduction'
            adapter=ROOT/'examples/factorial-experiment/mpi_adapter.py'
            mpi['recipe']['files']=[{'name':p.name,'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in (adapter,binary)]
            mpi['recipe']['command']=[str(Path(sys.executable).resolve()),'{file:mpi_adapter.py}','{result}','--launcher',str(Path(launcher).resolve()),'--binary','{file:reduction-mpi}']
            mpi['recipe']['limits'].update(cpus=3,memory_mb=1024,trial_seconds=8,total_seconds=120,output_bytes=1048576)
            report['mpi']=actual(mpi,mpi_root)
            report['mpi']['runtime']=subprocess.run([launcher,'--version'],capture_output=True,text=True,timeout=5).stdout[:4096]
        else:report['mpi']={'status':'blocked','reason':'mpicc/mpiexec unavailable','unblock':'Provide an MPI development/runtime toolchain for the explicit three-rank fixture'}
    args.output.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')


if __name__=='__main__':main()
