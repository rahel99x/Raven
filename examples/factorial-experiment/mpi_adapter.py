#!/usr/bin/env python3
"""Small MPI adapter with exact application evidence and no hidden retry."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import shutil
import subprocess

parser=argparse.ArgumentParser()
parser.add_argument('result')
parser.add_argument('--launcher',required=True)
parser.add_argument('--binary',required=True)
parser.add_argument('--order',choices=['forward','paired'],required=True)
parser.add_argument('--method',choices=['plain','accurate'],required=True)
args=parser.parse_args()
# The reviewed outer driver owns timeout, memory and process-group cancellation.
binary=Path.cwd() / 'reduction-executable'
shutil.copyfile(args.binary,binary); binary.chmod(0o700)
process=subprocess.run([args.launcher,'-n','3','--mca','btl','self,tcp','--mca','pml','ob1',str(binary),args.order,args.method],check=True,capture_output=True,text=True)
value,supported=process.stdout.strip().split()
value=float(value)
output={'value':value,'unit':'sum','output_sha256':hashlib.sha256(struct.pack('>d',value)).hexdigest(),
        'semantic':'sum(1e16,1,-1e16)','controls':{'order':args.order,'method':args.method if supported=='1' else 'unsupported'}}
Path(args.result).write_text(json.dumps(output,allow_nan=False))
print('MEASURED')
