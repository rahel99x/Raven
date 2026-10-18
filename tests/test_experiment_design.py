"""Declared randomization, independent synthetic effects and actual trial receipts."""
import copy
import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys
import threading

import pytest

from raven import diagnostic_trials as D, experiment_design as E, operations as O, ops_investigation as Ops


def design():
    result = subprocess.run([sys.executable, 'examples/factorial-experiment/make_design.py'], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def observed(doc, *, noise=True):
    plan = E.prepare(doc); rng = random.Random(3801)
    output = []
    for trial in plan['schedule']:
        a, b = trial['coding']
        value = 10 + 2*a + 3*b + 4*a*b + .2*trial['block'] + (rng.uniform(-.1, .1) if noise else 0)
        output.append({'id':trial['id'],'value':value,'unit':'sum','output_sha256':hashlib.sha256(repr(value).encode()).hexdigest(),
                       'semantic':doc['equivalence']['semantic'],'controls':trial['levels']})
    return {'schema':E.RESULT_SCHEMA,'design':doc,'observations':output}


def test_balanced_randomized_blocks_repeated_controls_and_seed_identity():
    doc = design(); original = copy.deepcopy(doc)
    plan = E.prepare(doc)
    assert E.prepare(doc) == plan and doc == original
    assert len(plan['schedule']) == 16 and sum(t['control'] for t in plan['schedule']) == 4
    for block in range(4):
        rows = [t for t in plan['schedule'] if t['block'] == block]
        assert {tuple(t['coding']) for t in rows} == {(-1,-1),(-1,1),(1,-1),(1,1)}
    changed = copy.deepcopy(doc); changed['seed'] += 1
    assert E.prepare(changed)['schedule'] != plan['schedule']
    assert [r['id'] for r in plan['recipe']['cases']] == [r['id'] for r in plan['schedule']]


def test_known_main_effects_interaction_and_uncertainty_with_block_drift():
    report = E.analyze(observed(design()))
    assert report['state'] == 'observed-associations'
    effects = {row['term']:row for row in report['fit']['effects']}
    for name, truth in [('order',4),('method',6),('order:method',8)]:
        assert abs(effects[name]['contrast'] - truth) < .1
        assert effects[name]['interval'][0] <= truth <= effects[name]['interval'][1]
    assert report['fit']['rank'] == report['fit']['parameters']
    assert report['fit']['residual_variance'] > 0 and 'causal percentages' in report['limit']


def test_confounded_missing_cells_and_unsupported_controls_do_not_get_attribution():
    data = observed(design())
    schedule = {r['id']:r for r in E.prepare(data['design'])['schedule']}
    data['observations'] = [row for row in data['observations'] if schedule[row['id']]['coding'][0] == -1]
    assert E.analyze(data)['state'] == 'confounded'
    data = observed(design())
    for row in data['observations']: row['controls'] = {'order':'unsupported'}
    report = E.analyze(data)
    assert report['state'] == 'underpowered' and not report['fit']['effects']
    assert all(row['unsupported_controls'] for row in report['checks'])


def test_equivalence_bitwise_tolerance_and_semantics_are_independent():
    data = observed(design(), noise=False)
    data['observations'][0]['semantic'] = 'different-scientific-output'
    data['observations'][1]['value'] = 1.001
    report = E.analyze(data)
    assert report['checks'][0]['semantic_match'] is False
    assert report['checks'][1]['tolerance_pass'] is True
    assert any(row['bitwise_change_from_controls'] for row in report['checks'])
    assert any(not row['tolerance_pass'] for row in report['checks'])


@pytest.mark.parametrize('issue',['incompatible','budget','repeats','reserved','duplicate','unit'])
def test_ambiguous_or_unsupported_designs_fail_visibly(issue):
    doc = design()
    if issue == 'incompatible': doc['incompatible'] = [{'order':'forward','method':'plain'}]
    if issue == 'budget': doc['recipe']['limits']['max_trials'] = 5
    if issue == 'repeats': doc['recipe']['limits']['repeats'] = 2
    if issue == 'reserved': doc['factors'][0]['id'] = 'intercept'
    if issue == 'duplicate': doc['factors'][1]['id'] = 'order'
    if issue == 'unit': doc['equivalence']['unit'] = ''
    with pytest.raises(ValueError): E.prepare(doc)


def launch(tmp_path, recipe, name, monkeypatch, cancel=None):
    path = tmp_path / (name + '.json'); path.write_text(json.dumps(recipe))
    ctx = O.Context(scope={'cluster':'explicit-local-fixture','user':'owner'}, state_dir=str(tmp_path/'state'), cancel=cancel)
    monkeypatch.setattr(Ops, '_launch_local', lambda recipe, root, ctx: D.run(recipe, root, cancel=ctx.cancel))
    params = {'manifest':str(path),'destination':str(tmp_path/name),'action':'launch'}
    reviewed = Ops.run('diagnostic-campaign',params,ctx)
    assert not (tmp_path/name).exists()
    Ops.apply('diagnostic-campaign',reviewed['plan'],ctx)
    return tmp_path/name


def test_real_controlled_reduction_cancellation_recovery_and_no_committed_reexecution(tmp_path,monkeypatch):
    doc=design(); plan=E.prepare(doc); cancel=threading.Event()
    execute=D._execute; calls=[]
    def stop_after_two(*args,**kwargs):
        result=execute(*args,**kwargs); calls.append(args[0])
        if len(calls)==2: cancel.set()
        return result
    monkeypatch.setattr(D,'_execute',stop_after_two)
    first=launch(tmp_path,plan['recipe'],'first',monkeypatch,cancel)
    partial=E.collect(doc,[str(first)])
    assert len(partial['committed'])==2 and partial['remaining']==14
    assert partial['recovery_recipe'] and not partial['uncertain']
    with pytest.raises(FileExistsError): D.run(plan['recipe'],first)
    monkeypatch.setattr(D,'_execute',execute)
    second=launch(tmp_path,partial['recovery_recipe'],'second',monkeypatch)
    all_results=E.collect(doc,[str(first),str(second)])
    assert len(all_results['committed'])==16 and all_results['remaining']==0
    assert not all_results['recovery_recipe']
    analysis=E.analyze(all_results['results'])
    assert len(analysis['checks'])==16 and any(not row['tolerance_pass'] for row in analysis['checks'])
    effects={row['term']:row['contrast'] for row in analysis['fit']['effects']}
    assert effects==pytest.approx({'order':.5,'method':.5,'order:method':-.5})
    # No uncertain intent can become a new recovery trial.
    (second/'trials/0000/result.json.receipt').unlink()
    uncertain=E.collect(doc,[str(first),str(second)])
    assert uncertain['uncertain'] and uncertain['recovery_recipe'] is None


def test_operations_plan_and_analysis_are_read_only(tmp_path):
    for feature,data in [('factorial-design',design()),('factorial-analysis',observed(design()))]:
        path=tmp_path/(feature+'.json');path.write_text(json.dumps(data));original=path.read_bytes()
        result=O.run(feature,{'manifest':str(path)},O.Context())
        assert 'plan' not in result and path.read_bytes()==original
        assert result['data']


def staged_evidence(tmp_path, doc, name='workspace', *, intents=1, receipt=False):
    """Exact driver-shaped records; no process is launched by this fixture."""
    root = tmp_path / name
    root.mkdir()
    recipe = E.prepare(doc)['recipe']
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
    write(root/'recipe.json', recipe)
    executable = str(Path(sys.executable).resolve())
    write(root/'execution.json', {'executable': {'path':executable}})
    declared = []
    for sequence in range(intents):
        directory = root/'trials'/f'{sequence:04d}'
        argv = []
        for arg in recipe['command'] + recipe['cases'][sequence]['arguments']:
            if arg == '{result}': arg = str(directory/'result.json')
            elif arg.startswith('{file:'): arg = str(root/'sources'/arg[6:-1])
            argv.append(arg)
        argv[0] = executable
        write(directory/'intent.json', {'case_id':recipe['cases'][sequence]['id'], 'sequence':sequence,
                                       'input_sha256':None, 'arguments':argv})
        if receipt:
            row = {'case_id':recipe['cases'][sequence]['id'], 'sequence':sequence,
                   'state':'finished', 'oracle_matched':False}
            write(directory/'result.json.receipt', row)
            declared.append(row)
    write(root/'result.json', {'schema':'raven.diagnostic-result/v1', 'recipe_sha256':E.digest(recipe),
                             'state':'partial', 'trials':declared})
    return root


@pytest.mark.parametrize('field',['case_id','sequence','arguments'])
def test_collection_rejects_valid_case_in_wrong_order_or_changed_execution(tmp_path,field):
    doc=design(); root=staged_evidence(tmp_path,doc)
    path=root/'trials/0000/intent.json'; intent=json.loads(path.read_text())
    if field == 'case_id': intent[field]=E.prepare(doc)['recipe']['cases'][1]['id']
    elif field == 'sequence': intent[field]=1
    else: intent[field][-1]='different-effective-level'
    path.write_text(json.dumps(intent))
    with pytest.raises(ValueError,match='ordered recipe|arguments differ'):
        E.collect(doc,[str(root)])


def test_duplicate_uncommitted_intent_across_recovery_workspaces_is_not_replayed(tmp_path):
    doc=design(); a=staged_evidence(tmp_path,doc,'a'); b=staged_evidence(tmp_path,doc,'b')
    with pytest.raises(ValueError,match='Duplicate trial intent'):
        E.collect(doc,[str(a),str(b)])


def test_cumulative_budget_charges_metadata_and_intents_without_receipts(tmp_path):
    doc=design(); root=staged_evidence(tmp_path,doc,intents=3)
    # Valid individual bounded files; their total exceeds 8 MiB before any
    # receipt exists. Missing-receipt branches must never bypass accounting.
    for name in ('recipe.json','execution.json','result.json'):
        path=root/name; path.write_bytes(path.read_bytes()+b' '*(2<<20))
    for sequence in range(3):
        path=root/'trials'/f'{sequence:04d}'/'intent.json'
        path.write_bytes(path.read_bytes()+b' '*(1<<20))
    with pytest.raises(ValueError,match='at most|8 MiB'):
        E.collect(doc,[str(root)])


def test_durable_committed_declaration_survives_missing_trial_directory(tmp_path):
    import shutil
    doc=design(); root=staged_evidence(tmp_path,doc,receipt=True)
    committed_id=E.prepare(doc)['recipe']['cases'][0]['id']
    shutil.rmtree(root/'trials/0000')
    result=E.collect(doc,[str(root)])
    assert result['uncertain']==[committed_id]
    assert result['remaining']==15 and result['recovery_recipe'] is None


def test_extreme_finite_values_keep_bounded_finite_estimates():
    data=observed(design())
    for observation in data['observations']: observation['value']*=1e97
    result=E.analyze(data)
    json.dumps(result,allow_nan=False)
    assert result['fit']['effects']


def test_published_design_and_result_schemas_accept_actual_compiler_inputs():
    import referencing
    from jsonschema import Draft202012Validator
    directory=Path('docs/schemas'); names=['factorial-design.v1.schema.json','factorial-results.v1.schema.json','investigation-diagnostic-campaign.schema.json']
    documents=[json.loads((directory/name).read_text()) for name in names]
    registry=referencing.Registry().with_resources(('https://raven.local/schemas/'+name,referencing.Resource.from_contents(doc)) for name,doc in zip(names,documents))
    Draft202012Validator(documents[0],registry=registry).validate(design())
    Draft202012Validator(documents[1],registry=registry).validate(observed(design()))


def test_real_worker_executes_readonly_factorial_plan_and_analysis(tmp_path):
    import time
    from raven.raven_worker_client import WorkerClient
    from raven import raven_execution as R
    config={'site_kind':'desktop','desktop_explicit':True,'context':{},'resources':{'memory_mib':512,'cpus':1,'max_tasks':1,'wall_seconds':10}}
    with WorkerClient(config) as worker:
        deadline=time.monotonic()+5
        while not worker.ready and time.monotonic()<deadline: worker.poll(); time.sleep(.01)
        assert worker.ready,worker.error
        router=R.ExecutionRouter({'managed':True,'mode':'heavy','site':'desktop'},lambda:worker)
        ctx=O.Context(execution=router,scope={'cluster':'fixture','user':'owner'})
        for feature,document in [('factorial-design',design()),('factorial-analysis',observed(design()))]:
            path=tmp_path/(feature+'.json');path.write_text(json.dumps(document))
            result=O.run(feature,{'manifest':str(path)},ctx)
            assert result['execution_receipt']['operation']=='scientific-inspection'
            assert result['feature']==feature
            assert 'plan' not in result


def test_unsupported_or_changed_semantic_controls_cannot_define_bitwise_reference():
    data=observed(design())
    controls={row['id'] for row in E.prepare(data['design'])['schedule'] if row['control']}
    for row in data['observations']:
        if row['id'] in controls: row['controls']={}
    report=E.analyze(data)
    assert report['control_bitwise_variants']==0
    assert all(row['bitwise_change_from_controls'] is None for row in report['checks'])
