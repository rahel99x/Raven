import sys,json,time,tempfile,pathlib,tracemalloc,resource,os
from raven.model import Store,Job
from raven import series_blocks as B,series_storage as S,series_retention as R
mode=sys.argv[1]; size=int(sys.argv[2]) if len(sys.argv)>2 else 0
result={'case':mode,'n':size,'pid':os.getpid()}
with tempfile.TemporaryDirectory() as folder:
 root=pathlib.Path(folder)
 if mode=='store':
  st=Store(persist=False);st.apply_jobs([Job(str(i),'train','local','RUNNING') for i in range(size)])
  tracemalloc.start(); started=time.perf_counter();cpu=time.process_time()
  for job in st.jobs:st.record(job.id,{'t':1,'k':'benchmark','rss':1234},_persist=False)
  result.update(seconds=time.perf_counter()-started,cpu_seconds=time.process_time()-cpu,python_peak_bytes=tracemalloc.get_traced_memory()[1],retained=sum(len(v) for v in st.series.values()))
 elif mode=='retention':
  p=root/('v2-37-'+'a'*64+'.jsonl')
  with p.open('wb') as out:
   for n in range(size):out.write(S._canonical({'t':n*5,'k':'live','cpu':.5,'rss':1234}))
  tracemalloc.start();started=time.perf_counter();cpu=time.process_time()
  try:
   r=R.build(p,as_of=size*5,apply=True,settings={'gap_seconds':30} if size==35000 else None);result.update(status=r['status'],records=r['source']['records'],archive_bytes=r['archive_bytes'],gaps=len(r['observed_gaps']))
  except Exception as exc:result.update(status='refused',error=str(exc))
  result.update(seconds=time.perf_counter()-started,cpu_seconds=time.process_time()-cpu,python_peak_bytes=tracemalloc.get_traced_memory()[1],source_bytes=p.stat().st_size)
 elif mode=='tail':
  p=root/('v2-37-'+'a'*64+'.rvb')
  with p.open('wb') as out:
   for n in range(size):out.write(B.encode([{'t':n,'k':'live','cpu':.5}],B._identity(p)))
  tracemalloc.start();started=time.perf_counter();cpu=time.process_time();rows,status=B.read(p,4000)
  result.update(seconds=time.perf_counter()-started,cpu_seconds=time.process_time()-cpu,python_peak_bytes=tracemalloc.get_traced_memory()[1],first=rows[0]['t'],last=rows[-1]['t'],count=len(rows),read_bytes=status['bytes'],file_bytes=p.stat().st_size)
 elif mode=='batch':
  st=Store(str(root),series_storage='blocks-v1'); started=time.perf_counter();cpu=time.process_time()
  for n in range(size):
   st.record('7',{'t':n,'k':'live','cpu':.5,'rss':1234},_persist=False)
   try:st.flush_persistence(_defer_series=True)
   except TypeError:st.flush_persistence()
  st.close();files=list((root/'series').glob('*.rvb'));rows,status=B.read(files[0],size)
  result.update(seconds=time.perf_counter()-started,cpu_seconds=time.process_time()-cpu,file_bytes=files[0].stat().st_size,count=len(rows),blocks=status['blocks'],read_bytes=status['bytes'])
 result['process_peak_rss_bytes']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024
print(json.dumps(result))
