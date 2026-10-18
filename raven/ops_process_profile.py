"""Reviewed, opt-in Heavy-owned local process observations."""
from . import operations as O, process_profile as P, science_files as SF

SPECIFICATIONS = [{"key": "process-profile", "title": "Observe an owned local process", "group": "Research", "proposal": "W29",
    "summary": "Read available Linux process/thread counters without modifying the workload; local PID scope is explicit.",
    "fields": [{"key": "pid", "label": "Owned PID on the Heavy worker node", "required": True},
               {"key": "seconds", "label": "Capture seconds (0.1–60)", "default": "5"},
               {"key": "interval_ms", "label": "Requested interval ms (100–2000)", "default": "100"},
               {"key": "cpu_budget_percent", "label": "Adaptive collector CPU target (0.1–10%)", "default": "2"},
               {"key": "destination", "label": "New private receipt directory", "required": True}]}]


def run(feature, params, ctx):
    O.local_only(ctx)
    owner = P.execution_identity(ctx.verified_execution)
    raw = params.get('pid')
    if isinstance(raw, bool):
        raise ValueError('Choose a local owned PID')
    try:
        pid = int(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError('Choose a local owned PID') from exc
    if isinstance(raw, float) and raw != pid:
        raise ValueError('PID must be an integer')
    target = P.inspect(pid, owner)
    options = P.settings(params)
    payload = dict(target=target, settings=options, execution=owner, destination=SF.destination(params['destination']))
    return O.report(feature, 'Review explicit local PID scope before sampling.',
        rows=[f"Target PID {pid}; start ticks {target['start_ticks']}; UID {target['uid']}; node {target['node']}",
              f"Requested {options['interval_ms']} ms for {options['seconds']} s, adaptive CPU target {options['cpu_budget_percent']}%",
              'At most 32 local processes, 128 retained task identities and 600 samples.',
              'Scientific job and rank attribution are unknown. Heavy identity describes the collector only.'],
        warnings=['Point samples cannot reconstruct exact scheduling history or identify a filesystem from D-state.',
                  'The collector reads counters and never signals or attaches a debugger to the workload.'],
        plan=O.prepare_plan(ctx, feature, params, payload))


def apply(feature, plan, ctx):
    O.local_only(ctx)
    payload = O.validate_plan(plan, ctx, feature)
    if P.execution_identity(ctx.verified_execution) != payload['execution']:
        raise ValueError('Heavy worker changed; prepare a new capture review')
    SF.destination(payload['destination'])
    receipt = P.collect(payload['target'], payload['settings'], payload['execution'], cancel=ctx.cancel)
    receipt['review_digest'] = plan['digest']
    path = SF.publish(payload['destination'], files=[('process-profile.json', O.canonical(receipt) + b'\n', False)],
                      receipt=dict(schema=P.SCHEMA, review_digest=plan['digest'], status=receipt['status']))
    return O.report(feature, 'Process observation ' + receipt['status'], data=receipt, rows=[path],
                    warnings=receipt['limitations'], status='ok' if receipt['status'] == 'completed' else 'partial')
