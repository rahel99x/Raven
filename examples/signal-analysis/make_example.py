#!/usr/bin/env python3
"""Emit a small, explicitly synthetic comparison with known phase boundaries."""
import json
import math


def capture(lengths, attempt):
    samples = []
    for phase, (length, level) in enumerate(zip(lengths, (20, 80, 40))):
        for index in range(length):
            samples.append({"t": len(samples), "value": level + 2 * math.sin(2 * math.pi * index / length), "phase": str(phase)})
    return {"schema": "raven.measurement-series/v1", "series": [{"identity": {"scope": "synthetic-example", "job": attempt, "attempt": attempt, "verified": True}, "metric": "gpu", "unit": "%", "samples": samples}]}


if __name__ == "__main__":
    print(json.dumps({"schema": "raven.phase-comparison/v1", "reference": capture((32, 32, 32), "reference"), "candidate": capture((24, 48, 32), "candidate")}, indent=2))
