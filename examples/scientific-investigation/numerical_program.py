"""Measure a deliberately order-sensitive sum; emit real computed values."""
import json
import math
from pathlib import Path
import sys
import time

mode, output = sys.argv[1:]
values = [1e16, 1.0, -1e16]
started = time.perf_counter()
if mode == "compensated":
    answer = math.fsum(values)
else:
    # Python 3.12+ sum() itself uses improved summation. Explicit accumulation
    # demonstrates the intended naive floating-point algorithm on every version.
    answer = 0.0
    for value in values:
        answer += value
Path(output).write_text(json.dumps({"answer": answer, "seconds": time.perf_counter()-started}), encoding="utf-8")
print("TOWER_DEMO_MEASURED")
