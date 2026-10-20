"""Observe the unchanged regression body and prove its blocking guards."""
import ast
import cProfile
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import pstats
import sys
import tempfile
import threading
import time
import pytest

path = Path('tests/test_remote_log_snapshots.py')
spec = importlib.util.spec_from_file_location('remote_ui_final_fixture', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
source = path.read_text()
original = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == 'test_real_controller_frames_and_keyboard_never_wait_for_remote_io')
records = []


def make_function(probe=None):
    node = ast.parse(ast.unparse(original)).body[0]
    class Observe(ast.NodeTransformer):
        def visit_Assign(self, item):
            names = [target.id for target in item.targets if isinstance(target, ast.Name)]
            if 'before_cpu' in names:
                added = ast.parse('before_process = time.process_time(); _profile.enable() if _profile else None').body
                if probe:
                    added += ast.parse(probe).body
                return [item, *added]
            if 'wall_elapsed' in names:
                return [item, *ast.parse('''_profile.disable() if _profile else None
_record({"wall_seconds": wall_elapsed, "ui_thread_cpu_seconds": cpu_elapsed, "process_cpu_seconds": time.process_time() - before_process, "remote_future_pending": not future.done(), "remote_calls_during_pending": len(files.calls)})''').body]
            return item
    node = Observe().visit(node)
    namespace = dict(vars(module), _record=records.append, _profile=None)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), '<observed-final-fixture>', 'exec'), namespace)
    return namespace[node.name], namespace


def run(function):
    with tempfile.TemporaryDirectory() as directory, pytest.MonkeyPatch.context() as monkeypatch:
        function(Path(directory), monkeypatch)


def fds():
    return len(list(Path('/proc/self/fd').iterdir()))

before_threads = {thread.ident for thread in threading.enumerate()}
before_fds = fds()
function, namespace = make_function()
for _ in range(10):
    run(function)
profile = cProfile.Profile()
namespace['_profile'] = profile
run(function)
stats = pstats.Stats(profile)
top = []
for (file, line, name), (_, calls, self_seconds, cumulative, _) in sorted(stats.stats.items(), key=lambda row: row[1][3], reverse=True)[:20]:
    top.append({'function': f'{file}:{line}:{name}', 'calls': calls, 'self_seconds': self_seconds, 'cumulative_seconds': cumulative})
probes = {}
for name, code, expected in [
    ('pending_result', 'future.result(timeout=0)', 'result'),
    ('pending_exception', 'future.exception(timeout=0)', 'exception'),
    ('pending_condition_wait', 'future._condition.wait(0)', 'pending future condition'),
    ('ui_sleep', 'time.sleep(.001)', 'sleep'),
    ('synchronous_preferences', 'store.save_ui({})', 'preference I/O'),
    ('ui_remote_io', 'files.snapshot_stat("/remote/project/77.out")', 'SSH cannot run'),
]:
    function, _ = make_function(code)
    try:
        run(function)
    except AssertionError as error:
        assert expected in str(error), (name, error)
        probes[name] = {'rejected': True, 'message': str(error)}
    else:
        raise AssertionError('Blocking probe was accepted: ' + name)
after_threads = [thread.name for thread in threading.enumerate() if thread.ident not in before_threads]
assert not after_threads, after_threads
assert fds() == before_fds, (before_fds, fds())
result = {
    'schema': 'raven.remote-ui-final-profile/v1',
    'python': platform.python_version(),
    'test_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
    'rounds': records[:10],
    'profiled_round': records[10],
    'profile_top_cumulative': top,
    'negative_guard_probes': probes,
    'resource_ownership': {'file_descriptors_before': before_fds, 'file_descriptors_after': fds(), 'new_live_threads_after_owned_shutdown': after_threads},
    'limits': 'Observational shared-host fixture measurements; not a controlled release-performance benchmark. Ten original 50-iteration bodies and all original behavior assertions retained; cProfile excluded from unprofiled rounds. Negative mutations inject one forbidden call at the guarded UI-loop boundary, then verify rejection and owned cleanup.'
}
Path(sys.argv[1]).write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
