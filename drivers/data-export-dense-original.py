import pathlib,tempfile,json,time,resource
from raven import reporting as R,auto_export as A
with tempfile.TemporaryDirectory() as directory:
 root=pathlib.Path(directory);(root/'job.sh').write_text('#!/bin/sh\ntrue\n');run=R.begin_run(root,'run-one',name='benchmark',script='job.sh')
 line=json.dumps({'t':1,'step':1,'metrics':{'loss-'+str(i):.123456789 for i in range(16)},'labels':{'label':'x'*100}},separators=(',',':'))+'\n'
 target=run/'metrics.jsonl';count=0
 with target.open('w') as stream:
  while stream.tell()<int(10.7*(1<<20)):stream.write(line);count+=1
 A.capture(run,{'include':['application']});R.finish_run(run,state='COMPLETED',exit_code=0,runtime_seconds=1)
 started=time.perf_counter();cpu=time.process_time();result=A.process_pending(root,deadline_seconds=5.)
 print(json.dumps({'case':'auto_export_dense','source_bytes':target.stat().st_size,'source_rows':count,'seconds':time.perf_counter()-started,'cpu_seconds':time.process_time()-cpu,'outcome':result,'process_peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024}))
