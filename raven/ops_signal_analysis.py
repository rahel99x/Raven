"""Read-only bounded signal analyses in the existing Research workbench."""
from . import operations as O, stream_anomalies

SPECIFICATIONS = [
    {"key": "telemetry-anomalies", "title": "Telemetry changes (experimental)", "group": "Research", "proposal": "W26",
     "summary": "Inspect exact-attempt changes with evidence windows and explicit abstention; causes remain unknown.",
     "fields": [{"key": "path", "label": "Measurement series JSON", "required": True}]},
    {"key": "phase-alignment", "title": "Compare observed phases (experimental)", "group": "Research", "proposal": "W32",
     "summary": "Find bounded change points and possible phase correspondences while retaining original timestamps and gaps.",
     "fields": [{"key": "path", "label": "Phase comparison JSON", "required": True},
                {"key": "view", "label": "Time view", "default": "original", "choices": ["original", "aligned"]}]},
]


def run(feature, params, ctx):
    if feature == "phase-alignment":
        from . import phase_alignment
        result = phase_alignment.analyze(O.read_json(ctx, params["path"]), cancel=ctx.cancel)
        if params.get("view", "original") == "aligned":
            rows = [f"Reference phase {item['reference']} ↔ candidate phase {item['candidate']}: {item['status']}" for item in result["correspondences"]]
        else:
            rows = [f"{label} phase {i}: {item['start_seconds']:g}–{item['end_seconds']:g} s; {item['samples']} samples; {item['status']}"
                    for label, key in (("Reference", "reference_segments"), ("Candidate", "candidate_segments")) for i, item in enumerate(result[key])]
        return O.report(feature, "Experimental phase comparison; original recordings are unchanged", rows=rows, data=result,
                        warnings=[result["qualification"], result["reversible"]])
    if feature != "telemetry-anomalies":
        raise ValueError("Unknown signal analysis")
    result = stream_anomalies.analyze(O.read_json(ctx, params["path"]), cancel=ctx.cancel)
    rows = [f"{O.clean(item['metric'], 128)}: {item['state']}; retained samples {item['retained_samples']}" for item in result["series"]]
    rows += [f"{O.clean(item['metric'], 128)} {item['kind']} change at {item['timestamp']:g} s; cause unknown" for item in result["events"]]
    return O.report(feature, "Experimental timestamped change detection", rows=rows, data=result,
                    warnings=[result["qualification"]])


def apply(feature, plan, ctx):
    raise ValueError("Signal analysis is read-only and never changes a job")


run = O.managed_run(run)
apply = O.managed_apply(apply)
