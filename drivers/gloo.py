import json,os,sys,tempfile
from pathlib import Path
from datetime import timedelta
sys.path.insert(0,'/workspace/Raven')
import torch
import torch.distributed as dist
import torch.multiprocessing as mp
from raven import client as raven

def worker(rank, rendezvous, output):
    if rank == 0:
        raven.configure(path=output)
        raven.metric('before_init', 99)
        assert raven._state.helper and not raven._state.allowed
    dist.init_process_group('gloo',init_method='file://'+rendezvous,rank=rank,world_size=2,timeout=timedelta(seconds=20))
    raven.metric('sum',rank+1,reduce='sum')
    value=torch.tensor(float(rank+1))
    dist.all_reduce(value)
    assert value.item()==3, (rank,value.item())
    raven.metric('invalid',float('nan') if rank==0 else 2,reduce='sum')
    value=torch.tensor(float(rank+1));dist.all_reduce(value);assert value.item()==3
    if rank==0:
        assert raven._state.allowed and not raven._state.helper
        assert raven._state.flush()
        rows=[json.loads(line) for line in Path(output).read_text().splitlines()]
        assert [row['metrics']['sum'] for row in rows if 'sum' in row['metrics']]==[3.0], rows
        assert not any('invalid' in row['metrics'] for row in rows), rows
    dist.destroy_process_group()
    raven._shutdown()

if __name__=='__main__':
    root=Path(tempfile.mkdtemp(prefix='raven-gloo-qualification-',dir='/workspace'))
    mp.spawn(worker,args=(str(root/'rendezvous'),str(root/'metrics.jsonl')),nprocs=2,join=True)
    result={'status':'passed','backend':'gloo','world_size':2,'torch':torch.__version__,'python':sys.version,'metrics':str(root/'metrics.jsonl'),'cases':['rank0 early plain metric before process-group init','rank1 without configured client','Raven reduction then application all_reduce','invalid-scalar readiness then application all_reduce']}
    Path('/workspace/review-injob-gloo-results.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
