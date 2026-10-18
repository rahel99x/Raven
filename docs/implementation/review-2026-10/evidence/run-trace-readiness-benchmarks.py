import argparse,datetime,json,pathlib,statistics,subprocess
root=pathlib.Path('/workspace/Raven');frozen=pathlib.Path('/tmp/raven-review-final');out=root/'docs/implementation/review-2026-10/evidence'
parser=argparse.ArgumentParser();parser.add_argument('phase',choices=['new','ui','readiness']);args=parser.parse_args()
python=str(root/'.venv/bin/python')
def run(script,arguments,path):
 command=[python,str(frozen/'scripts'/script),*arguments,'--output',str(path)]
 print('START',datetime.datetime.now(datetime.timezone.utc).isoformat(),path.name,flush=True)
 result=subprocess.run(command,cwd=frozen,capture_output=True,text=True)
 if result.returncode:raise RuntimeError(result.stdout+result.stderr)
 data=json.loads(path.read_text());print('END',datetime.datetime.now(datetime.timezone.utc).isoformat(),path.name,flush=True);return data
if args.phase=='new':
 for package,script in [('W27','benchmark_log_divergence.py'),('W36','benchmark_trace_summary.py'),('W29','benchmark_process_profile.py'),('W43','benchmark_terminal_raster.py')]:
  data=run(script,[],out/(package+'-measurements.json'));print(json.dumps(data)[:800],flush=True)
elif args.phase=='ui':
 samples=[]
 for repeat in range(3):
  for label,source in [('baseline','/tmp/raven-review-baseline'),('capture-only','/tmp/raven-review-w08-isolated'),('integrated',str(frozen))]:
   for trace in (False,True):
    report=run('benchmark_ui.py',['--source-root',source,'--gesture','render','--jobs','100','--history','100','--repeats','100']+(['--ui-trace'] if trace else []),pathlib.Path('/tmp/raven-review-evidence')/f'frame-{repeat}-{label}-{trace}.json')
    samples.append(dict(repeat=repeat,source=label,trace=trace,report=report))
 summary=[]
 for label in ('baseline','capture-only','integrated'):
  for trace in (False,True):
   selected=[s['report']['results'][0]['phases_ms']['frame'] for s in samples if s['source']==label and s['trace']==trace]
   summary.append(dict(source=label,trace=trace,frame_median_ms=statistics.median(s['median'] for s in selected),frame_p95_ms=statistics.median(s['p95'] for s in selected)))
 (out/'W08-final-frame-measurements.json').write_text(json.dumps(dict(summary=summary,samples=samples),indent=2)+'\n');print(json.dumps(summary),flush=True)
else:
 rows=[]
 for probe in (False,True):
  for shell in (False,True):
   for label,source in [('baseline','/tmp/raven-review-baseline'),('integrated',str(frozen))]:
    arguments=['--source-root',source,'--variant','light','--mode','idle','--duration','10' if probe else '45','--warmup','1' if probe else '5','--sample-seconds','1']
    if probe:arguments.append('--probe-latency')
    if shell:arguments.append('--terminal-shell')
    path=out/f'W18-final-{label}-{int(shell)}-{int(probe)}.json'
    data=run('benchmark_raven_resources.py',arguments,path)
    row=dict(source=label,shell=shell,probe=probe,cpu=data['cpu_mean_percent_one_core'],rss=data['summary']['rss_bytes']['max'],readiness=data['readiness'],commands=data['child']['commands'],io=data['io_observed_bytes'],path=path.name)
    rows.append(row);print(json.dumps(row),flush=True)
 (out/'W18-final-measurements.json').write_text(json.dumps(rows,indent=2)+'\n')
