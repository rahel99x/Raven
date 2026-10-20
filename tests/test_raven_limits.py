"""Light retention admission, cross-cache eviction, and durable-write quotas."""
from pathlib import Path
from types import SimpleNamespace
import threading

from raven import layout as L
from raven.raven_limits import CacheBudget, BudgetCache, LightLimits, bind, retained_size
from raven.model import Store, Job, Finished
from raven.native_series_cache import NativeSeriesCache
from raven.metric_raster import MetricRasterCache
from raven.logs import LogSession


def test_shared_cache_budget_evicts_across_owners_and_never_calls_back_under_lock():
    budget=CacheBudget(1200)
    expired=[]
    def release(value):
        assert not budget.lock._is_owned()
        expired.append(value)
    left=BudgetCache(budget,release); right=BudgetCache(budget,release)
    left['old']='a'*600
    right['new']='b'*600
    assert budget.used<=1200 and not left and right and expired==['a'*600]
    right.clear()
    assert budget.used==0 and not budget.entries


def test_delayed_cache_eviction_never_removes_new_publication():
    budget=CacheBudget(2000); cache=BudgetCache(budget)
    old={'v':'old'}; new={'v':'new'}
    cache['same']=old; cache['same']=new
    cache.expire('same',old)
    assert cache['same'] is new


def test_oversized_native_preparation_returns_value_without_retaining_it():
    budget=CacheBudget(200)
    cache=NativeSeriesCache(budget=budget)
    value=cache.remember('source',[{'t':1,'v':2}],lambda samples:list(samples)*100)
    assert len(value)==100 and not cache.entries and cache.bytes==0
    assert budget.used==0


def test_raster_counters_survive_cross_cache_eviction():
    budget=CacheBudget(5000)
    raster=MetricRasterCache(budget=budget)
    rows,_=raster.render(L.Glyphs(True),[1,2,3],20,5)
    assert rows
    other=BudgetCache(budget)
    other['large']='x'*4500
    assert raster.points>=0 and raster.cells>=0
    assert raster.points==sum(value['points'] for value in raster.entries.values())
    assert raster.cells==sum(value['cells'] for value in raster.entries.values())
    assert budget.used<=budget.limit


def job(jid):
    return Job(str(jid),'job','compute','RUNNING')


def test_truncated_queue_never_infers_omitted_job_finished():
    limits=LightLimits({'max_job_rows':2})
    store=Store(persist=False,raven_limits=limits)
    store.apply_jobs([job(1),job(2)])
    events=store.apply_jobs([job(3),job(4),job(1),job(2)])
    assert [j.id for j in store.jobs]==['3','4']
    assert not any(event.get('kind') in ('finished','left') for event in events)
    assert not store.departed_jobs
    assert store.snapshot()['coverage']['jobs']=={'retained':2,'observed':4,'limited':True}


def test_finished_publication_and_accounting_are_capped():
    store=Store(persist=False,raven_limits=LightLimits({'max_history_rows':2}))
    store.apply_finished([Finished(str(i),state='COMPLETED') for i in range(10)])
    assert len(store.finished)==2 and len(store._accounting_jobs)==2
    assert store.snapshot()['coverage']['history']['limited']


def test_series_jobs_and_bytes_bound_archive_and_live_sources():
    limits=LightLimits({'max_series_jobs':2,'series_bytes':3000})
    store=Store(persist=False,raven_limits=limits)
    for jid in range(20):
        store.record(str(jid),{'t':jid,'k':'cpu','value':'x'*200},_persist=False)
    assert len(store.series)<=2 and limits.series_bytes<=3000
    for i in range(30):
        store.record('last',{'t':i,'value':'x'*200},_persist=False)
    assert limits.series_bytes<=3000 and len(store.series['last'])<30
    for jid in range(30): store.series_view('empty'+str(jid))
    assert len(store.series)<=2 and len(store._series_loaded)<=2
    assert limits.coverage['series']['limited']


def test_single_oversized_metric_is_not_retained():
    limits=LightLimits({'series_bytes':500})
    store=Store(persist=False,raven_limits=limits)
    store.record('1',{'payload':'x'*2000},_persist=False)
    assert not store.series['1'] and limits.series_bytes==0


def test_persistence_inflight_bytes_remain_charged(tmp_path):
    limits=LightLimits({'persistence_queue_bytes':100},str(tmp_path))
    store=Store(str(tmp_path),raven_limits=limits)
    entered=threading.Event(); release=threading.Event()
    original=limits.write
    def blocked(path,payload):
        entered.set(); assert release.wait(2); original(path,payload)
    limits.write=blocked
    store.record('1',{'v':'x'*60},_persist=False)
    worker=threading.Thread(target=lambda:store.flush_persistence(_series_only=True))
    worker.start(); assert entered.wait(2)
    assert limits.pending_bytes>60 and not store._persist_queue
    store.record('2',{'v':'y'*60},_persist=False)
    assert not store._persist_queue and limits.coverage['persistence']['limited']
    release.set(); worker.join(2)
    assert not worker.is_alive() and limits.pending_bytes==0


def test_owned_storage_quota_protects_active_evidence(tmp_path):
    limits=LightLimits({'storage_bytes':100,'storage_files':1},str(tmp_path))
    store=Store(str(tmp_path),raven_limits=limits)
    store.apply_jobs([job(1), job(2)])
    store.record('1',{'value':'x'*40})
    existing=Path(store._series_path('1')).read_bytes()
    store.record('2',{'value':'y'*40})
    assert not Path(store._series_path('2')).exists()
    store.record('1',{'value':'z'*90})
    assert Path(store._series_path('1')).read_bytes()==existing
    assert limits.coverage['storage']['limited'] and limits.pending_bytes==0


def test_owned_history_symlink_is_never_followed(tmp_path):
    target=tmp_path/'outside'; target.write_text('private')
    (tmp_path/'series').mkdir()
    limits=LightLimits({},str(tmp_path)); store=Store(str(tmp_path),raven_limits=limits)
    Path(store._series_path('1')).symlink_to(target)
    store.record('1',{'t':1})
    assert target.read_text()=='private' and limits.coverage['storage']['limited']


def test_bind_caps_log_count_and_each_raw_window_without_changing_legacy(tmp_path):
    logs=LogSession()
    store=Store(persist=False)
    config={'raven':{'resources':{'light':{'max_log_buffers':2,'log_window_bytes':32}}}}
    owner=SimpleNamespace(cfg=config,store=store,logs=logs)
    bind(owner)
    for index in range(5):
        path=tmp_path/str(index); path.write_text('long-line\n'*20)
        logs.buffer(str(path))
    assert len(logs.buffers)==2 and logs.max_bytes==32
    assert all(buffer._retained_bytes<=32 for buffer in logs.buffers.values())
    legacy=SimpleNamespace(cfg={},logs=LogSession())
    assert bind(legacy) is None and legacy.logs.max_bytes==32<<20


def test_archive_restore_obeys_series_budget(tmp_path):
    directory=tmp_path/'series'; directory.mkdir()
    limits=LightLimits({'series_bytes':2000},str(tmp_path))
    store=Store(str(tmp_path),raven_limits=limits)
    Path(store._series_path('1')).write_text(''.join('{"t":'+str(i)+',"v":"'+('x'*100)+'"}\n' for i in range(100)))
    data=store.series_of('1')
    assert data and len(data)<100 and limits.series_bytes<=2000
    assert store.series_status('1')['status']=='limited'


def test_store_supplies_owned_inventory_root_to_limits(tmp_path):
    limits=LightLimits({})
    store=Store(str(tmp_path),raven_limits=limits)
    assert limits.state_dir==store.state_dir


def test_gpu_device_histories_survive_queue_poll_then_expire_with_job():
    store=Store(persist=False,raven_limits=LightLimits({}))
    store.apply_jobs([job(1)])
    store.gpu_mean['1:GPU-uuid']=[60.0,2]
    store.hist_gpu['1:GPU-uuid'].append(.3)
    store.gpu_mean['orphan:GPU-uuid']=[90.0,1]
    store.hist_gpu['orphan:GPU-uuid'].append(.9)
    store.apply_jobs([job(1)])
    assert store.gpu_mean_of('1:GPU-uuid')==30.0
    assert list(store.hist_gpu['1:GPU-uuid'])==[.3]
    assert 'orphan:GPU-uuid' not in store.gpu_mean
    assert 'orphan:GPU-uuid' not in store.hist_gpu


def test_archive_inventory_bounds_non_history_entries(tmp_path,monkeypatch):
    limits=LightLimits({'storage_files':2})
    store=Store(str(tmp_path),raven_limits=limits)
    visited=[]
    class Entries:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def __iter__(self):
            for index in range(1000):
                visited.append(index)
                yield SimpleNamespace(name='unrelated-'+str(index))
    monkeypatch.setattr('raven.series_archive.os.scandir',lambda path:Entries())
    assert store.series_jobs()==[]
    assert len(visited)==5 and limits.coverage['archive_inventory']['limited']


def test_oversized_persistence_record_refused_before_serialization(monkeypatch):
    limits=LightLimits({'persistence_queue_bytes':100})
    monkeypatch.setattr('raven.raven_limits.json.dumps',lambda *a,**kw:(_ for _ in ()).throw(AssertionError('serialized')))
    queue=[]
    limits.enqueue(queue,'owned.jsonl',{'payload':'x'*2000})
    assert not queue and limits.coverage['persistence']['limited']
