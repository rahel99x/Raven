#!/usr/bin/env python3
"""Controlled floating-point reduction; no GPU/BLAS controls are assumed."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import struct

parser = argparse.ArgumentParser()
parser.add_argument('result')
parser.add_argument('--order', choices=['forward', 'paired'], required=True)
parser.add_argument('--method', choices=['plain', 'accurate'], required=True)
args = parser.parse_args()
values = [1e16, 1., -1e16] if args.order == 'forward' else [1e16, -1e16, 1.]
if args.method == 'plain':
    value = 0.
    for element in values:
        value += element
else:
    value = math.fsum(values)
result = {'value': value, 'unit': 'sum', 'output_sha256': hashlib.sha256(struct.pack('>d', value)).hexdigest(),
          'semantic': 'sum(1e16,1,-1e16)', 'controls': {'order': args.order, 'method': args.method}}
Path(args.result).write_text(json.dumps(result, allow_nan=False))
print('MEASURED')
