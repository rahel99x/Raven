"""Observe exact existing loop without interpreting wall delay as blocking."""
import ast
import cProfile
import importlib.util
import json
from pathlib import Path
import platform
import pstats
import sys
import tempfile
import time
import pytest

spec=importlib.util.spec_from_file_location('remote_ui_fixture','tests/test_remote_log_snapshots.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
source=Path('tests/test_remote_log_snapshots.py').read_text()
tree=ast.parse(source)
node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='test_real_controller_frames_and_keyboard_never_wait_for_remote_io')
class Observe(ast.NodeTransformer):
 def visit_Assign(self,n):
  if any(isinstance(x,ast.Name) and x.id=='before' for x in n.targets):
   return [n,*ast.parse('before_cpu = time.thread_time(); before_process = time.process_time(); _profile.enable() if _profile else None').body]
  return n
 def visit_Assert(self,n):
  if isinstance(n.test,ast.Compare) and isinstance(n.test.left,ast.BinOp) and isinstance(n.test.left.right,ast.Name) and n.test.left.right.id=='before':
   return ast.parse('''_profile.disable() if _profile else None
_record({"wall_seconds": time.perf_counter()-before, "thread_cpu_seconds": time.thread_time()-before_cpu, "process_cpu_seconds": time.process_time()-before_process, "remote_future_pending": not future.done(), "remote_calls_during_pending": len(files.calls)})''').body
  return n
node=Observe().visit(node)
if '--bound' in sys.argv:
 class BindWriter(ast.NodeTransformer):
  def visit_Assign(self,n):
   if any(isinstance(x,ast.Attribute) and isinstance(x.value,ast.Name) and x.value.id=='app' and x.attr=='research' for x in n.targets):
    return ast.parse('from raven.worker_scheduler import WorkerScheduler; _scheduler = WorkerScheduler(lanes=("source", "research")); app.research = ResearchHub(cfg, files, worker_scheduler=_scheduler); from raven.ui_persistence import bind as _bind_preferences; _bind_preferences(app, _scheduler)').body
   return n
  def visit_Expr(self,n):
   call=n.value
   if isinstance(call,ast.Call) and isinstance(call.func,ast.Attribute) and isinstance(call.func.value,ast.Name) and call.func.value.id=='app' and call.func.attr=='handle' and call.args and isinstance(call.args[0],ast.Constant) and call.args[0].value=='y':
    return [n,*ast.parse('for _copy_future in getattr(app, "clipboard_pending", ()):\n    _copy_future.result(timeout=5)\nclipboard.poll(app)').body]
   return n
  def visit_Try(self,n):
   self.generic_visit(n)
   if n.finalbody:
    n.finalbody[-1:-1]=ast.parse('from raven.ui_persistence import close as _close_preferences; _close_preferences(app)').body
    n.finalbody+=ast.parse('_scheduler.shutdown(wait=True)').body
   return n
 node=BindWriter().visit(node)
namespace=dict(vars(module))
records=[]
namespace['_record']=records.append
namespace['_profile']=None
exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),'<observed-existing-test>','exec'),namespace)
fn=namespace[node.name]
for i in range(7):
 with tempfile.TemporaryDirectory() as directory,pytest.MonkeyPatch.context() as monkeypatch:
  fn(Path(directory),monkeypatch)
prof=cProfile.Profile()
namespace['_profile']=prof
with tempfile.TemporaryDirectory() as directory,pytest.MonkeyPatch.context() as monkeypatch:
 fn(Path(directory),monkeypatch)
stats=pstats.Stats(prof)
top=[]
for (file,line,name),(primitive,total,selfcpu,cumulative,callers) in sorted(stats.stats.items(),key=lambda kv:kv[1][3],reverse=True)[:18]:
 top.append({'function':f'{file}:{line}:{name}','calls':total,'self_seconds':selfcpu,'cumulative_seconds':cumulative})
result={'schema':'raven.remote-ui-profile/v1','python':platform.python_version(),'fixture':'Unchanged50handle/tick/composeiterations, samependingRemote Future, source/copy/selectionassertions retained. In-memory profiling transform replaces only oldwallassert with wall/thread/processCPUmeasurements.','fixture_writer_bound':('--bound' in sys.argv),'observational_rounds':records[:7],'profiled_round':records[7],'profile_top_cumulative':top,'limits':'LocalLinuxsharedhost; observationnotwhole-releasebenchmark. Profileoverhead excluded fromunprofiledrounds. HostedwallmeasurementhasnoCPUbreakdownsoitscauseisnotdirectlyobservable.'}
Path(sys.argv[1]).write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({'python':result['python'],'rounds':result['observational_rounds'],'top':top[:8]},indent=2))
