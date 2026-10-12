"""Optional mpi4py application-side control demonstration.

Initialize the mailbox on rank zero's host before launching the application.
Use the site's existing MPI launch command; Raven does not allocate extra ranks.
"""
import argparse
import json
import time

from mpi4py import MPI
from raven.collective_runtime import CollectiveRuntime


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mailbox")
    parser.add_argument("--attempt", default="job123:attempt1")
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--initialize", help="Configuration JSON; create mailbox on rank zero's host")
    args = parser.parse_args()
    comm = MPI.COMM_WORLD
    error = None
    if args.initialize and comm.rank == 0:
        try:
            from raven.runtime_control import initialize
            from raven.science_files import json_file
            initialize(args.mailbox, json_file(args.initialize)[0])
        except Exception as exc:
            error = str(exc)
    error = comm.bcast(error, root=0)
    if error:
        raise RuntimeError("Mailbox initialization failed: " + error)
    control = CollectiveRuntime(comm, args.mailbox, args.attempt, initial_epoch=0)
    state = {"learning_rate": .1}

    def prepare(command, parameters, request_id):
        if command != "learning-rate" or not 0 < parameters["value"] <= 1:
            raise ValueError("Unsupported application parameter")
        return parameters["value"]

    def commit(value):
        state["learning_rate"] = value
        return dict(state)

    for epoch in range(args.epochs):
        receipt = control.safe_point("epoch", prepare, commit)
        value = comm.allreduce((comm.rank + 1) * state["learning_rate"], op=MPI.SUM)
        if comm.rank == 0:
            print(json.dumps({"epoch": epoch, "parameter_epoch": control.epoch, "sum": value, "receipt": receipt}), flush=True)
        time.sleep(.5)


if __name__ == "__main__":
    main()
