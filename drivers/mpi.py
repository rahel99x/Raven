import json,os,sys
from pathlib import Path
sys.path.insert(0,'/workspace/Raven')
from raven import client as raven
rank=int(os.environ.get('OMPI_COMM_WORLD_RANK',os.environ.get('PMI_RANK','0')))
path=Path(sys.argv[1])
if rank==0:
    raven.configure(path=path)
    raven.metric('early',1)
from mpi4py import MPI
comm=MPI.COMM_WORLD
raven.metric('sum',rank+1,reduce='sum')
assert comm.allreduce(rank+1,op=MPI.SUM)==3
if rank==0:
    assert raven._get_client().flush()
    rows=[json.loads(line) for line in path.read_text().splitlines()]
    assert any(row['metrics'].get('sum')==3 for row in rows),rows
    print(json.dumps({'ranks':comm.Get_size(),'metric_sum':3,'application_sum':3,'rank0_only_configure':True,'early_call_before_mpi':True}))
