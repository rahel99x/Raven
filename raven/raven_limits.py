"""Managed Light retention budgets; legacy sessions keep their existing limits."""
from __future__ import annotations

from collections import OrderedDict, deque
from itertools import islice
import json
import os
import stat
import sys
import threading


def retained_size(value, limit=32 << 20):
    """Bounded conservative Python-container accounting, never a serialization."""
    pending, seen, total = [value], set(), 0
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if len(seen) > 100000:
            return limit + 1
        kind = type(item)
        if isinstance(item, dict):
            total += dict.__sizeof__(item)
            pending.extend(dict.keys(item)); pending.extend(dict.values(item))
        elif isinstance(item, (list, tuple, deque, set, frozenset)):
            total += sys.getsizeof(item)
            pending.extend(item)
        elif kind in (str, bytes, bytearray, int, float, bool, type(None), object, type):
            total += sys.getsizeof(item)
        elif kind.__module__.startswith('raven.') and hasattr(item, '__dict__'):
            total += sys.getsizeof(item)
            pending.append(vars(item))
        else:
            return limit + 1  # Unknown extension storage is never admitted.
        if total > limit:
            return total
    return total


class CacheBudget:
    """One aggregate LRU across participating UI preparation caches."""
    def __init__(self, limit):
        self.limit = max(1, int(limit))
        self.used = 0
        self.evictions = 0
        self.entries = OrderedDict()
        self.lock = threading.RLock()

    def forget(self, owner, key):
        with self.lock:
            old = self.entries.pop((id(owner),key), None)
            if old:
                self.used -= old[0]

    def touch(self, owner, key):
        with self.lock:
            ident = id(owner),key
            if ident in self.entries:
                self.entries.move_to_end(ident)

    def admit(self, owner, key, value):
        cost = retained_size((key,value), self.limit)
        expired=[]
        with self.lock:
            self.forget(owner,key)
            self.entries[(id(owner),key)] = cost,owner,value
            self.used += cost
            while self.used > self.limit:
                (_,old_key),(size,old_owner,old_value) = self.entries.popitem(last=False)
                self.used -= size
                self.evictions += 1
                expired.append((old_owner,old_key,old_value))
        # No foreign callback runs under the ledger lock. The value identity
        # also prevents a delayed eviction from removing a newer publication.
        for old_owner,old_key,old_value in expired:
            old_owner.expire(old_key,old_value)


class BudgetCache(OrderedDict):
    """Drop-in bounded cache. Owners keep their existing point/count counters."""
    def __init__(self, budget, expired=None):
        super().__init__()
        self.budget,self.expired = budget,expired

    def __setitem__(self, key, value):
        super().__setitem__(key,value)
        self.budget.admit(self,key,value)

    def get(self,key,default=None):
        value=super().get(key,default)
        if key in self:
            self.budget.touch(self,key)
        return value

    def move_to_end(self,key,last=True):
        if key in self:
            super().move_to_end(key,last)
            self.budget.touch(self,key)

    def pop(self,key,*default):
        value=super().pop(key,*default)
        self.budget.forget(self,key)
        return value

    def popitem(self,last=True):
        key,value=super().popitem(last)
        self.budget.forget(self,key)
        return key,value

    def __delitem__(self,key):
        super().__delitem__(key)
        self.budget.forget(self,key)

    def clear(self):
        for key in tuple(self):
            self.budget.forget(self,key)
        super().clear()

    def expire(self,key,expected):
        if super().get(key) is not expected:
            return
        value=super().pop(key,None)
        if value is not None and self.expired:
            self.expired(value)


def cache(app, name):
    owner=getattr(app,'_chart_owner',app)
    budget=getattr(owner,'raven_cache_budget',None)
    return BudgetCache(budget) if budget else OrderedDict()


class LightLimits:
    def __init__(self, values, state_dir=None):
        defaults=dict(max_job_rows=5000,max_history_rows=5000,max_series_jobs=128,
                      series_bytes=16<<20,persistence_queue_bytes=8<<20,
                      storage_bytes=256<<20,storage_files=512)
        self.values={key:max(1,int(values.get(key,default))) for key,default in defaults.items()}
        self.coverage={}
        self.series=OrderedDict()
        self.series_bytes=0
        self.pending_bytes=0
        self.state_dir=state_dir
        self.disk=None
        self.disk_bytes=0
        self.lock=threading.RLock()
        self.disk_lock=threading.RLock()

    def rows(self,rows,key):
        limit=self.values['max_job_rows' if key=='jobs' else 'max_history_rows']
        records=list(rows[:limit]) if hasattr(rows,'__getitem__') else list(islice(rows,limit+1))
        total=len(rows) if hasattr(rows,'__len__') else len(records)
        self.coverage[key]={'retained':len(records),'observed':total,'limited':total>limit}
        return records[:limit],total>limit

    def retain_series(self,store,jid,cost=None):
        """Call under Store.lock after append, or after a bounded archive merge."""
        source=store.series[jid]
        old=self.series.pop(jid,deque())
        if not source:
            self.series_bytes-=sum(old)
            old=deque()
            cost=None
        if cost is None:
            self.series_bytes-=sum(old)
            old=deque(retained_size(sample,self.values['series_bytes']) for sample in source)
            self.series_bytes+=sum(old)
        else:
            while len(old)>=len(source) and old:
                self.series_bytes-=old.popleft()
            old.append(cost); self.series_bytes+=cost
        self.series[jid]=old
        trimmed=0
        while (len(self.series)>self.values['max_series_jobs'] or self.series_bytes>self.values['series_bytes']):
            oldest,costs=next(iter(self.series.items()))
            if oldest==jid and len(self.series)==1:
                self.series_bytes-=costs.popleft()
                source.popleft(); trimmed+=1
                if not costs:
                    break
            else:
                self.series.pop(oldest)
                self.series_bytes-=sum(costs)
                store.series.pop(oldest,None)
                publication=store._publications.pop(oldest,None)
                if publication: publication[0].release_publication()
                store._series_loaded.discard(oldest)
                store._series_archive.limited.discard(oldest)
                store.hist_cpu.pop(oldest,None); store.hist_gpu.pop(oldest,None)
                trimmed+=1
        if trimmed:
            previous=self.coverage.get('series',{}).get('evicted',0)
            self.coverage['series']={'limited':True,'evicted':previous+trimmed,
                                     'retained_jobs':len(self.series),'bytes':self.series_bytes}

    def enqueue(self,queue,path,sample):
        # Bound work before JSON materialization as well as queued bytes.
        # Unknown extension values are refused instead of invoking encoders.
        preparation_limit=max(1024,self.values['persistence_queue_bytes'])
        if retained_size(sample,preparation_limit)>preparation_limit:
            self.coverage['persistence']={'limited':True,'reason':'record exceeds queue preparation budget'}
            return
        try:
            payload=(json.dumps(sample,separators=(',',':'))+'\n').encode('utf-8')
        except (ValueError,TypeError,RecursionError):
            self.coverage['persistence']={'limited':True,'reason':'unsupported record'}
            return
        with self.lock:
            if self.pending_bytes+len(payload)>self.values['persistence_queue_bytes']:
                self.coverage['persistence']={'limited':True,'reason':'queue byte budget reached'}
                return
            self.pending_bytes+=len(payload)
            queue.append((path,payload))

    def _inventory(self):
        if self.disk is not None:
            return
        self.disk={}
        roots=[(self.state_dir,False),(os.path.join(self.state_dir,'series'),True)] if self.state_dir else []
        for directory,series in roots:
            try:
                with os.scandir(directory) as entries:
                    for index,entry in enumerate(entries):
                        if index>=self.values['storage_files']*2:
                            self.disk_bytes=self.values['storage_bytes']+1
                            self.coverage['storage']={'limited':True,'reason':'owned inventory scan limit reached'}
                            return
                        if not (entry.name.endswith(('.jsonl','.rvb','.rvt')) if series else entry.name=='events.jsonl'):
                            continue
                        if len(self.disk)>=self.values['storage_files']:
                            self.disk_bytes=self.values['storage_bytes']+1
                            return
                        info=entry.stat(follow_symlinks=False)
                        if not stat.S_ISREG(info.st_mode):
                            continue
                        self.disk[entry.path]=info.st_size
                        self.disk_bytes+=info.st_size
            except FileNotFoundError:
                continue
            except OSError:
                self.disk_bytes=self.values['storage_bytes']+1
                raise

    def write(self,path,payload):
        """One owned append; never delete external or prior evidence to fit."""
        with self.disk_lock:
            self._inventory()
            new=path not in self.disk
            if (self.disk_bytes+len(payload)>self.values['storage_bytes'] or
                    new and len(self.disk)>=self.values['storage_files']):
                self.coverage['storage']={'limited':True,'reason':'owned history quota reached'}
                return False
            os.makedirs(os.path.dirname(path),exist_ok=True)
            fd=os.open(path,os.O_WRONLY|os.O_APPEND|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
            try:
                info=os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise OSError('owned history must be a regular file')
                previous=self.disk.get(path,0)
                if self.disk_bytes-previous+info.st_size+len(payload)>self.values['storage_bytes']:
                    self.coverage['storage']={'limited':True,'reason':'owned history grew beyond quota'}
                    return False
                view=memoryview(payload)
                try:
                    while view:
                        count=os.write(fd,view)
                        if count<=0: raise OSError('short history write')
                        view=view[count:]
                finally:
                    actual=os.fstat(fd).st_size
                    self.disk_bytes+=actual-previous
                    self.disk[path]=actual
            finally:
                os.close(fd)
            return True

    def drain(self,store,queue,io_lock):
        with io_lock:
            while True:
                with store._persist_queue_lock:
                    if not queue: return
                    batch=list(queue); queue.clear()
                paths = {}
                for path, payload in batch:
                    paths.setdefault(path, []).append(payload)
                for path, payloads in paths.items():
                    try:
                        compact = False
                        if path != store._events_path:
                            from .series_storage import append as append_recordings, compact_path, uses_blocks
                            with self.disk_lock:
                                self._inventory()
                                packed = compact_path(path)
                                available = self.values['storage_bytes'] - self.disk_bytes
                                if packed not in self.disk and len(self.disk) >= self.values['storage_files']:
                                    available = 0
                                def write_block(target, payload):
                                    if not self.write(target, payload):
                                        raise OSError('owned history quota reached')
                                if uses_blocks(path, store._series_format_cache):
                                    compact = append_recordings(path, [json.loads(payload) for payload in payloads],
                                        writer=write_block, quota=available, cache=store._series_format_cache)
                        if not compact:
                            for payload in payloads:
                                self.write(path, payload)
                    except (OSError, ValueError) as exc:
                        self.coverage['storage']={'limited':True,'reason':str(exc)[:256]}
                        store._state_health("state recordings", str(exc))
                    finally:
                        with self.lock:
                            self.pending_bytes -= sum(map(len, payloads))


def bind(app, store=None, views=None, logs=None):
    """Attach one persistent Light budget; compute mode never resets it."""
    config=getattr(app,'cfg',{}).get('raven')
    if not isinstance(config,dict): return None
    values=config.get('resources',{}).get('light',{})
    store=store or getattr(app,'store',None)
    limits=getattr(app,'raven_limits',None) or getattr(store,'raven_limits',None)
    if limits is None:
        limits=LightLimits(values,getattr(store,'state_dir',None))
    app.raven_limits=limits
    if getattr(app,'raven_cache_budget',None) is None:
        app.raven_cache_budget=CacheBudget(values.get('prepared_cache_bytes',32<<20))
    if store is not None: store.raven_limits=limits
    logs=logs or getattr(app,'logs',None)
    if logs is not None:
        logs.max_bytes=min(logs.max_bytes,int(values.get('log_window_bytes',2<<20)))
        logs.max_buffers=int(values.get('max_log_buffers',4))
        while len(logs.buffers)>logs.max_buffers: logs.buffers.pop(next(iter(logs.buffers)))
    views=views or getattr(app,'views_ref',None)
    if views is not None:
        from .native_series_cache import NativeSeriesCache
        from .metric_raster import MetricRasterCache
        if getattr(views._native_sources,'budget',None) is not app.raven_cache_budget:
            views._native_sources=NativeSeriesCache(budget=app.raven_cache_budget)
            views._metric_rasters=MetricRasterCache(budget=app.raven_cache_budget)
    return limits
