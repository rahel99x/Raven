"""Aggregate Light worker admission across lanes and live mode changes."""
from concurrent.futures import wait
from threading import Event, Lock, get_ident
import time

import pytest

from tower.worker_scheduler import WorkerScheduler


def await_status(scheduler,predicate):
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        state=scheduler.status()
        if predicate(state): return state
        Event().wait(.001)
    pytest.fail('worker admission did not settle: '+str(scheduler.status()))


@pytest.mark.parametrize('bad',[0,-1,True,1.5,'2'])
def test_invalid_aggregate_limit_is_refused_before_executor(bad):
    with pytest.raises(ValueError,match='aggregate worker limit'):
        WorkerScheduler(aggregate_limit=bad)


def test_legacy_budget_is_unchanged_and_large_ceiling_cannot_raise_lane_total():
    for ceiling in (None,1000):
        scheduler=WorkerScheduler(workers=2,aggregate_limit=ceiling)
        try:
            assert scheduler.multi_limit==6
            assert scheduler._executor._max_workers==6
        finally:
            scheduler.shutdown()


def test_all_lanes_share_ceiling_while_single_multi_switch_drains_exactly_once():
    scheduler=WorkerScheduler(workers=4,aggregate_limit=2)
    lanes=[scheduler.lane(name) for name in ('source','gpu','research','notification')]
    starts=[Event() for _ in range(6)]
    releases=[Event() for _ in range(6)]
    lock=Lock(); active=0; peak=0; calls=[]
    def task(index):
        nonlocal active,peak
        with lock:
            active+=1; peak=max(peak,active); calls.append(index)
        starts[index].set()
        try:
            assert releases[index].wait(3)
            return index
        finally:
            with lock: active-=1
    try:
        futures=[lanes[index%4].submit(task,index) for index in range(6)]
        assert starts[0].wait(1) and starts[1].wait(1)
        state=scheduler.status()
        assert state['running']==state['limit']==2 and state['queued']==4
        assert not any(event.is_set() for event in starts[2:])
        state=scheduler.request_mode('single')
        assert state['pending'] and state['draining']==2 and state['target_limit']==1
        releases[0].set(); assert futures[0].result(1)==0
        await_status(scheduler,lambda s:s['running']==1)
        assert not any(event.is_set() for event in starts[2:])
        releases[1].set(); assert futures[1].result(1)==1
        await_status(scheduler,lambda s:s['mode']=='single' and s['running']==1 and any(event.is_set() for event in starts[2:]))
        assert sum(event.is_set() for event in starts[2:])==1
        scheduler.request_mode('multi')
        await_status(scheduler,lambda s:s['running']==2)
        # Admission may precede task entry by one executor turn.
        assert scheduler.status()['target_limit']==2
        for release in releases: release.set()
        assert not wait(futures,timeout=2).not_done
        assert [future.result() for future in futures]==list(range(6))
        assert sorted(calls)==list(range(6)) and peak==2 and active==0
        assert await_status(scheduler,lambda s:not s['running'])['queued']==0
    finally:
        for release in releases: release.set()
        scheduler.shutdown(wait=True,cancel_futures=True)


@pytest.mark.parametrize('ceiling',[1,2])
def test_nested_cross_lane_results_use_existing_aggregate_permits(ceiling):
    scheduler=WorkerScheduler(workers=4,aggregate_limit=ceiling)
    source,research=scheduler.lane('source'),scheduler.lane('research')
    seen=set(); lock=Lock()
    def child(value):
        with lock: seen.add(get_ident())
        assert scheduler.status()['running']<=ceiling
        return value
    def parent(lane,start):
        with lock: seen.add(get_ident())
        children=[lane.submit(child,start+index) for index in range(8)]
        return [future.result() for future in children]
    try:
        left=source.submit(parent,research,0)
        right=research.submit(parent,source,8)
        assert left.result(timeout=2)==list(range(8))
        assert right.result(timeout=2)==list(range(8,16))
        assert len(seen)<=ceiling
        assert scheduler._executor._max_workers==ceiling
    finally:
        scheduler.shutdown(wait=True,cancel_futures=True)
