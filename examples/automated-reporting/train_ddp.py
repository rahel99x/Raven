#!/usr/bin/env python3
"""Small real CPU DDP workload; requires PyTorch and a two-process torchrun.

torchrun --standalone --nproc-per-node=2 train_ddp.py
No GPU/NCCL or multi-node qualification is implied by this example.
"""
import os

import torch
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel

import raven


def main():
    dist.init_process_group("gloo")
    try:
        torch.manual_seed(7)
        model = DistributedDataParallel(torch.nn.Linear(4, 1))
        optimizer = torch.optim.SGD(model.parameters(), lr=0.01)
        generator = torch.Generator().manual_seed(100 + int(os.environ["RANK"]))
        inputs = torch.randn(16, 4, generator=generator)
        targets = inputs.sum(dim=1, keepdim=True)

        @raven.measure("loss", kind="objective", reduce="mean")
        def train_step():
            optimizer.zero_grad()
            loss = torch.nn.functional.mse_loss(model(inputs), targets)
            loss.backward()
            optimizer.step()
            return loss.detach()

        with raven.phase("fit"):
            for step in range(8):
                loss = train_step()  # Every rank participates in the reduction.
                raven.progress(step + 1, 8, unit="steps")
                if dist.get_rank() == 0:
                    print(f"step={step + 1} local_loss={loss.item():.6g}", flush=True)
    finally:
        dist.destroy_process_group()


if __name__ == "__main__":
    main()
