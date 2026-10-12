"""Optional real PyTorch training example; supports existing torchrun launches.

No PyTorch import is required by Raven itself. CPU is an explicit valid fallback.
CUDA timing is asynchronous, bounded and labeled as a stream interval. To publish
into an existing Raven Research run, pass --run-dir from `raven report start`.
"""
import argparse
import json
import os

import torch

from raven.collective_runtime import CollectiveRuntime, TorchDistributedCommunicator, TorchMetrics
from raven.runtime_control import RuntimeControl


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mailbox")
    parser.add_argument("--attempt", default="job123:attempt1")
    parser.add_argument("--steps", type=int, default=300)
    parser.add_argument("--control-every", type=int, default=10)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--run-dir")
    parser.add_argument("--initialize", help="Configuration JSON; create a new mailbox on rank zero's host")
    args = parser.parse_args()
    if not 1 <= args.steps <= 100000 or not 1 <= args.control_every <= args.steps or not 1 <= args.threads <= 256:
        parser.error("Choose bounded positive steps, control interval and threads")
    torch.set_num_threads(args.threads)
    distributed = int(os.environ.get("WORLD_SIZE", "1")) > 1
    local_rank = int(os.environ.get("LOCAL_RANK", "0")) if distributed else 0
    if args.device == "cuda":
        if not torch.cuda.is_available():
            parser.error("CUDA was requested but is unavailable")
        device_index = 0 if torch.cuda.device_count() == 1 else local_rank
        if device_index >= torch.cuda.device_count():
            parser.error("Local rank has no visible allocated CUDA device")
        device = torch.device("cuda", device_index)
        torch.cuda.set_device(device)
    else:
        device = torch.device("cpu")
    if distributed:
        import torch.distributed as dist
        dist.init_process_group("nccl" if device.type == "cuda" else "gloo")
        rank = dist.get_rank()
        comm = TorchDistributedCommunicator(dist)
    else:
        rank = 0
    error = None
    if args.initialize and rank == 0:
        try:
            from raven.runtime_control import initialize
            from raven.science_files import json_file
            initialize(args.mailbox, json_file(args.initialize)[0])
        except Exception as exc:
            error = str(exc)
    if distributed:
        error = comm.bcast(error, root=0)
    if error:
        raise RuntimeError("Mailbox initialization failed: " + error)
    if distributed:
        control = CollectiveRuntime(comm, args.mailbox, args.attempt)
    else:
        control = RuntimeControl(args.mailbox, args.attempt)
    model = torch.nn.Sequential(torch.nn.Linear(64, 64), torch.nn.ReLU(), torch.nn.Linear(64, 8)).to(device)
    if distributed:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[device.index] if device.type == "cuda" else None)
    optimizer = torch.optim.SGD(model.parameters(), lr=.1)
    sampler = TorchMetrics(torch, device)

    def prepare(command, parameters, request_id):
        if command != "learning-rate" or not 0 < parameters["value"] <= 1:
            raise ValueError("Unsupported learning rate")
        return parameters["value"]

    def commit(value):
        for group in optimizer.param_groups:
            group["lr"] = value
        return {"learning_rate": value}

    try:
        for step in range(args.steps):
            # Every rank enters controls in the same order, at an existing
            # iteration boundary. No sampler or control call polls Slurm.
            if step % args.control_every == 0:
                if distributed:
                    control.safe_point("epoch", prepare, commit)
                else:
                    control.safe_point("epoch", lambda command, parameters, request: commit(prepare(command, parameters, request)))
            timed = sampler.begin() if step % args.control_every == 0 else False
            data = torch.randn(32, 64, device=device)
            target = torch.randn(32, 8, device=device)
            optimizer.zero_grad(set_to_none=True)
            loss = torch.nn.functional.mse_loss(model(data), target)
            loss.backward()
            optimizer.step()
            if timed:
                sampler.end()
            if step % args.control_every == 0:
                metrics = sampler.sample()
                if rank == 0:
                    print(json.dumps({"step": step, "learning_rate": optimizer.param_groups[0]["lr"], "device": str(device), **metrics}), flush=True)
                    if args.run_dir:
                        from raven.reporting import write_metric
                        numeric = {key: value for key, value in metrics.items() if isinstance(value, (int, float)) and not isinstance(value, bool)}
                        if metrics.get("gpu_stream_seconds"):
                            numeric["gpu_stream_seconds"] = metrics["gpu_stream_seconds"][-1]
                        write_metric(args.run_dir, numeric, step=step, phase="training")
    finally:
        if distributed:
            dist.destroy_process_group()


if __name__ == "__main__":
    main()
