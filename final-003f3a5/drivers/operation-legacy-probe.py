"""Independent local probe; canonical import precedes released composition facade."""
from pathlib import Path
import importlib.util
import json
import sys
import tempfile

from raven.operation_adapters import investigation as canonical
assert 'raven.ops_investigation' not in sys.modules
from raven import ops_investigation as released
from raven import diagnostic_trials
assert canonical is released
spec = importlib.util.spec_from_file_location('workflow_fixture', 'tests/test_investigation_workflow.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
with tempfile.TemporaryDirectory(prefix='raven-legacy-independent-') as temporary:
    root = Path(temporary)
    document, params, context = fixture.setup(root)
    original = released._launch_local
    released._launch_local = lambda recipe, workspace, current: diagnostic_trials.run(recipe, workspace, cancel=current.cancel)
    try:
        inspected = released.run('investigation-workflow', params, context)
        reviewed = released.run('investigation-workflow', dict(params, action='review-diagnostic'), context)
        result = released.apply('investigation-workflow', reviewed['plan'], context)
        assert result['data']['diagnostic']['data']['result']['state'] == 'completed'
        assert result['receipt_path']
        try:
            released.apply('investigation-workflow', reviewed['plan'], context)
        except ValueError as error:
            assert 'already' in str(error)
        else:
            raise AssertionError('Legacy apply repeated a claimed plan')
        status = released.run('investigation-workflow', dict(params, action='status'), context)
        assert status['data']['updated_findings']
        print(json.dumps({'canonical_before_facade': True, 'inspection': inspected['schema'], 'result': 'completed', 'duplicate_refused': True, 'retained_status': True, 'outer_and_nested_receipts': len(list((root / 'state' / 'operations-receipts').glob('*.result.json')))}))
    finally:
        released._launch_local = original
