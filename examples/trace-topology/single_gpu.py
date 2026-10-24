#!/usr/bin/env python3
"""Two-minute diffusion, spectral analysis and validation on one GPU device.

Use GPU-enabled PyTorch (CUDA or ROCm) in an owned one-GPU allocation. Python
tracing shows host calls; the optional Raven profiling wrapper adds supported
device activities. This program never substitutes CPU computation for a GPU.
"""

from gpu_workload import main


if __name__ == "__main__":
    raise SystemExit(main(multiple=False))
