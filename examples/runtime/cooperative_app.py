"""Run with an initialized mailbox, then submit a learning-rate command.

python examples/runtime/cooperative_app.py /tmp/tower-runtime-demo
"""
import argparse
import json
import time

from tower.runtime_control import RuntimeControl


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mailbox")
    parser.add_argument("--attempt", default="job123:attempt1")
    parser.add_argument("--iterations", type=int, default=30)
    args = parser.parse_args()
    control = RuntimeControl(args.mailbox, args.attempt)
    parameters = {"learning_rate": .1}

    def apply(command, values, request_id):
        # A real application first agrees on this update across its workers.
        # The callback executes only at the declared application safe point.
        if command != "learning-rate":
            raise ValueError("Unsupported command")
        parameters["learning_rate"] = values["value"]
        return {"learning_rate": parameters["learning_rate"], "request_id": request_id}

    for iteration in range(args.iterations):
        acknowledgment = control.safe_point("epoch", apply)
        print(json.dumps({"iteration": iteration, **parameters, "acknowledgment": acknowledgment}), flush=True)
        time.sleep(.5)


if __name__ == "__main__":
    main()
