#!/usr/bin/env python3
"""Encoding and raw PTY transport cost; this is not emulator/display timing."""
import argparse
import json
import os
from pathlib import Path
import resource
import statistics
import sys
import threading
import time
import tracemalloc

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from raven.terminal_raster import encode


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args()
    cells=[['⢕']*80 for _ in range(24)];samples=[]
    for _ in range(50):
        start=time.perf_counter_ns();raw=encode(cells,7,0,0).encode();samples.append((time.perf_counter_ns()-start)/1e6)
    tracemalloc.start();encode(cells,7,0,0);_,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
    import pty,tty
    master,slave=pty.openpty();tty.setraw(slave);total=[0]
    def drain():
        while total[0]<len(raw)*20:
            total[0]+=len(os.read(master,65536))
    thread=threading.Thread(target=drain);thread.start();start=time.perf_counter_ns()
    try:
        for _ in range(20):
            view=memoryview(raw)
            while view:
                n=os.write(slave,view);view=view[n:]
        thread.join(timeout=5);assert not thread.is_alive()
        transport_ms=(time.perf_counter_ns()-start)/1e6/20
    finally:os.close(slave);os.close(master)
    Path(args.output).write_text(json.dumps(dict(encoding_median_ms=statistics.median(samples),encoding_samples_ms=samples,
        pty_write_and_drain_ms_per_image=transport_ms,bytes_per_image=len(raw),maximum_bytes_per_second=2*len(raw),
        python_peak_bytes=peak,process_maxrss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        scope='80x24 existing Braille cells; raw local PTY only, no Kitty/Sixel/emulator/tmux/SSH visual qualification'),indent=2)+'\n')
if __name__=='__main__':main()
