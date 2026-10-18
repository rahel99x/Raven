import hashlib,json,os,pty,statistics,sys,threading,time,tty
from pathlib import Path
sys.path.insert(0,'/workspace/Raven')
from raven.terminal_sync import TerminalSync
payload='x'*2048
results=[]
for repetition in range(3):
 for mode in ('baseline','off','on') if repetition%2==0 else ('on','off','baseline'):
  master,slave=pty.openpty(); tty.setraw(slave)
  count=[0]
  def consume():
   while True:
    try: data=os.read(master,65536)
    except OSError: break
    if not data: break
    count[0]+=len(data)
  thread=threading.Thread(target=consume);thread.start()
  stream=os.fdopen(slave,'w');sync=TerminalSync(stream,mode if mode!='baseline' else 'off');sync.start()
  start,cpu=time.perf_counter(),time.process_time()
  for _ in range(5000):
   if mode=='baseline': stream.write(payload);stream.flush()
   else:
    with sync.frame(): stream.write(payload);stream.flush()
  stream.close();thread.join(timeout=5)
  elapsed=time.perf_counter()-start;used=time.process_time()-cpu
  os.close(master)
  assert not thread.is_alive()
  expected=5000*(2048+(16 if mode=='on' else 0))
  assert count[0]==expected,(count,expected)
  results.append({'mode':mode,'frames':5000,'seconds':elapsed,'cpu_seconds_writer_and_reader':used,'bytes':count[0]})
path=Path('/workspace/Raven/raven/terminal_sync.py')
print(json.dumps({'source_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'measurements':results,'median_us_per_frame':{mode:statistics.median([r['seconds']/r['frames']*1e6 for r in results if r['mode']==mode]) for mode in ('baseline','off','on')},'limits':['Actual local PTY writes/drain; no terminal emulator rendering, SSH or tmux qualification','CPU includes reader thread; no Python allocation/RSS claim','Identical 2048-byte payloads; negotiated enable behavior covered by focused tests, on mode explicitly forced here']},indent=2))
