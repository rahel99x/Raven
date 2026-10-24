#!/usr/bin/env python3
"""Two-minute concurrent diffusion and spectral analysis on multiple devices.

Request at least two GPUs/devices on one node and choose their count with
--workers. Each visible device has its own stream. One host thread submits work
to all devices before numerical readbacks. CUDA/ROCm visibility can expose
partitions, so the summary does not infer physical chips or measured overlap.
"""

from gpu_workload import main


if __name__ == "__main__":
    raise SystemExit(main(multiple=True))
