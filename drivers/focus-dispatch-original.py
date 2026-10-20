import ast,json,resource,statistics,subprocess,time
from raven import controller, workbench
from raven.config import Config
from raven.model import Store

old=ast.parse(subprocess.check_output(['git','show','HEAD:raven/controller.py'],text=True))
klass=next(n for n in old.body if isinstance(n,ast.ClassDef) and n.name=='App')
method=next(n for n in klass.body if isinstance(n,ast.FunctionDef) and n.name=='_handle')
method.name='_baseline_handle'
namespace=dict(vars(controller))
exec(compile(ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[])),'<HEAD-controller-handle>','exec'),namespace)
app=controller.App(Store(persist=False),None,None,Config(),'alex',ascii_=True)
app.mode='notifications'
workbench.owners(); workbench.module_map()
results={}
for label,fn in [('original_HEAD_control_flow_current_feature_contracts',namespace['_baseline_handle']),('foreground_focus_stack',controller.App._handle)]:
    fn(app,'noop');samples=[]
    for _ in range(7):
        begin=time.perf_counter()
        for _ in range(2000):fn(app,'noop')
        samples.append((time.perf_counter()-begin)/2000*1e6)
    results[label]={'median_us_per_key':statistics.median(samples),'round_us_per_key':samples}
assert app.mode=='notifications' and not app.confirm
report={'schema':'raven.focus-latency/v1','fixture':'same notifications mode, same unknown consumed key, same current feature contracts,2000keys per round7rounds','limits':'original HEAD _handle control flow compiled against current unchanged owner callbacks; isolates dispatch overhead, not original whole-release performance; no I/O or live scheduler; controlled benchmark only','results':results,'max_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,'python_allocations':'not measured; RSS is process high-water RSS'}
open('/workspace/review-focus-benchmark.json','w').write(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
