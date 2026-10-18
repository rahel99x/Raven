"""Reviewed allocation health workflow in the existing Operations workbench."""
from . import allocation_health as H, operations as O, science_files as SF

SPECIFICATIONS = [{"key": "allocation-health", "title": "Check Heavy allocation performance", "group": "Research", "proposal": "W31",
    "summary": "Opt-in bounded memory/scratch and conditional same-host MPI fingerprints of Raven's verified Heavy allocation.",
    "fields": [{"key": "tests", "label": "Local probes", "default": "memory,scratch", "choices": ["memory", "scratch", "memory,scratch", "mpi", "memory,scratch,mpi"]},
               {"key": "bytes", "label": "Bytes per probe (4 KiB–16 MiB)", "default": "1048576"},
               {"key": "seconds", "label": "Time budget in seconds (0.1–30)", "default": "10"},
               {"key": "repeats", "label": "Measured repeats (2–8)", "default": "3"},
               {"key": "warmup", "label": "Excluded warmup repeats (0–2)", "default": "1"},
               {"key": "scratch", "label": "Existing private scratch directory"},
               {"key": "mpi_prefix", "label": "Optional Open MPI development prefix"},
               {"key": "baseline", "label": "Previous receipt JSON (optional)"},
               {"key": "destination", "label": "New receipt directory", "required": True}]}]


def run(feature, params, ctx):
    O.local_only(ctx)
    execution = H.identity(ctx.verified_execution)
    settings = H.settings(params)
    if "scratch" in settings["tests"] and not params.get("scratch"):
        raise ValueError("Select an explicit private scratch directory")
    scratch = H.scratch_identity(params["scratch"]) if "scratch" in settings["tests"] else None
    baseline = O.read_json(ctx, params["baseline"], limit=256 << 10) if params.get("baseline") else None
    mpi = None
    if "mpi" in settings["tests"]:
        from . import mpi_health
        mpi = mpi_health.review(execution, params.get("mpi_prefix", ""))
    payload = {"mpi": mpi, "settings": settings, "execution": execution, "scratch": scratch,
               "destination": SF.destination(params["destination"]), "baseline": baseline}
    plan = O.prepare_plan(ctx, feature, params, payload)
    return O.report(feature, "Review diagnostic resource use in Raven's Heavy allocation before running probes.",
        rows=["Target: " + str(execution), "Tests: " + ", ".join(settings["tests"]),
              f"Time budget: {settings['seconds']} s; buffers ≤ {settings['maximum_buffer_bytes']} bytes",
              f"Scratch peak: {settings['peak_scratch_bytes']} bytes; total read/write ≤ {settings['maximum_io_bytes']} bytes",
              "Anonymous MPI compilation storage: " + str(settings["anonymous_mpi_compile_bytes"]) + " bytes",
              "No scheduler submission or cancellation; scientific jobs are unchanged."],
        warnings=["MPI requires a reviewed two-CPU Open MPI worker and is same-host only. GPU and inter-node probes remain unavailable; local results do not diagnose node failures.",
                  "Probes consume CPU/memory and scratch I/O. Cancellation may leave an incomplete receipt directory; inspect before retrying."], plan=plan)


def apply(feature, plan, ctx):
    O.local_only(ctx)
    payload = O.validate_plan(plan, ctx, feature)
    if H.identity(ctx.verified_execution) != payload["execution"]:
        raise ValueError("Verified Heavy allocation changed; prepare a new review")
    SF.destination(payload["destination"])
    receipt = H.probe(payload["settings"], payload["execution"], payload["scratch"], cancel=ctx.cancel, baseline=payload["baseline"], mpi=payload.get("mpi"))
    receipt["review_digest"] = plan["digest"]
    # A cancelled process has no implicit retry. Successfully returned partial
    # observations can be retained; supervisor termination leaves no false success.
    path = SF.publish(payload["destination"], files=[("health.json", O.canonical(receipt) + b"\n", False)],
                      receipt={"schema": H.SCHEMA, "review_digest": plan["digest"], "status": receipt["status"]})
    return O.report(feature, "Allocation diagnostic " + receipt["status"], data=receipt, rows=[path],
                    warnings=receipt["uncertainty"], status="ok" if receipt["status"] == "completed" else "partial")
