"""Adversarial recording, ownership, ordering and resource regressions."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import subprocess
import sys
from types import SimpleNamespace

import pytest

from raven import data_export as D, evidence_index as I, export as E, reporting
from raven import series_blocks as B, series_storage as S, series_retention as R
from raven import retained_telemetry as T
from raven.model import Store, Job, Finished
from raven.model import Live
from raven.sampler import Sampler
from raven.raven_limits import LightLimits


def path(root, suffix='.jsonl', jid='7'):
    return root / ('v2-' + jid.encode().hex() + '-' + 'a'*64 + suffix)


def test_compact_newest_tail_after_old_forward_frame_limit(tmp_path):
    source = path(tmp_path, '.rvb')
    with source.open('wb') as stream:
        for stamp in range(70000):
            stream.write(B.encode([{'t':stamp,'k':'live','cpu':.5}], B._identity(source)))
    rows, status = B.read(source, 5, max_bytes=4096)
    assert [row['t'] for row in rows] == list(range(69995,70000))
    assert status['kept_end'] == 'newest' and status['earlier_bytes_unread'] > 0


def test_every_torn_tail_can_resume_with_quarantined_evidence(tmp_path):
    source=path(tmp_path,'.rvb')
    frames=[B.encode([{'t':stamp}],B._identity(source)) for stamp in range(3)]
    complete=b''.join(frames)
    for offset in range(len(complete)+1):
        source.write_bytes(complete[:offset])
        B.append(source,[{'t':1000}])
        rows,status=B.read(source,100)
        assert rows[-1]=={'t':1000} and status['status']=='ready', offset
        valid=sum(end<=offset for end in (len(frames[0]),len(frames[0])+len(frames[1]),len(complete)))
        assert [row['t'] for row in rows if row.get('k')!='gap']==list(range(valid))+[1000]
        for damaged in tmp_path.glob('*.damaged-*'): damaged.unlink()


def test_short_light_append_rolls_back_and_retry_retains_order(tmp_path,monkeypatch):
    limits=LightLimits({},str(tmp_path)); source=path(tmp_path)
    assert limits.write(str(source),b'{"t":1}\n')
    before=source.read_bytes(); original=os.write; first=True
    def short(fd,data):
        nonlocal first
        if first:
            first=False
            return original(fd,data[:3])
        raise OSError(errno.ENOSPC,'fixture full disk')
    monkeypatch.setattr(os,'write',short)
    with pytest.raises(OSError): limits.write(str(source),b'{"t":2}\n')
    assert source.read_bytes()==before
    monkeypatch.setattr(os,'write',original)
    assert limits.write(str(source),b'{"t":2}\n')
    assert [row['t'] for row in S.json_rows(source)]==[1,2]


def test_recovery_gap_survives_failed_append_and_fresh_process(tmp_path):
    source=path(tmp_path,'.rvb');B.append(source,[{'t':1}])
    with source.open('ab') as stream:stream.write(b'torn append')
    def full(*args):raise OSError(errno.ENOSPC,'fixture full after recovery')
    with pytest.raises(OSError):B.append(source,[{'t':2}],writer=full,cache={})
    marker=Path(str(source)+'.recovery.json');descriptor=json.loads(marker.read_text())
    assert B.read(source,10)[0]==[{'t':1}] and descriptor['damaged_bytes']==len(b'torn append')
    child=subprocess.run([sys.executable,'-c','from raven import series_blocks as B; import sys; B.append(sys.argv[1],[{"t":3}],cache={})',str(source)],capture_output=True,text=True)
    assert child.returncode==0,child.stderr
    rows,status=B.read(source,10)
    assert rows[0]=={'t':1} and rows[-1]=={'t':3}
    assert rows[1]['k']=='gap' and rows[1]['recovery_id']==descriptor['recovery_id']
    assert not marker.exists() and status['status']=='ready'


def test_json_torn_boundary_finite_values_and_accounted_migration(tmp_path):
    source=path(tmp_path); source.write_bytes(b'{"t":0}\n{"t":')
    S.append_jsonl(str(source),[{'t':1}])
    with pytest.raises(ValueError): S.append_jsonl(str(source),[{'t':math.nan}])
    result=S.migrate(source,apply=True)
    assert result['ignored_records']==1 and result['source_coverage']=='partial'
    assert [row['t'] for row in S.block_rows(result['target'])]==[0,1]
    assert b'{"t":\n' in source.read_bytes()


def test_quota_evicts_oldest_inactive_and_protects_running_and_pin(tmp_path):
    limits=LightLimits({'storage_files':3,'storage_bytes':4096},str(tmp_path)); store=Store(str(tmp_path),raven_limits=limits)
    try:
        store.apply_jobs([Job('active','train','local','RUNNING')])
        store.record('active',{'t':1})
        store.record('old',{'t':1}); old=Path(store._series_path('old')); os.utime(old,ns=(1,1))
        store.record('pin',{'t':1}); store.pin('pin',True)
        limits.pinned_jobs={'pin'}
        store.record('new',{'t':2})
        assert not old.exists()
        assert all(Path(store._series_path(jid)).exists() for jid in ('active','pin','new'))
        assert limits.coverage['storage']['evicted_files']==1
    finally: store.close()


def test_large_old_inventory_does_not_latch_over_quota_forever(tmp_path):
    folder=tmp_path/'series';folder.mkdir()
    for number in range(100):
        item=path(folder,jid=str(number)); item.write_text('{"t":1}\n'); os.utime(item,ns=(number+1,number+1))
    limits=LightLimits({'storage_files':2,'storage_bytes':1000},str(tmp_path))
    target=path(folder,jid='new')
    assert limits.write(str(target),b'{"t":2}\n')
    assert len(list(folder.glob('*.jsonl')))==2
    assert path(folder,jid='99').exists() and target.exists()


def test_converted_redundant_copy_can_leave_quota_without_losing_raw(tmp_path):
    source=path(tmp_path);source.write_text('{"t":1}\n{"t":2}\n')
    result=S.migrate(source,apply=True)
    limits=LightLimits({'storage_files':2,'storage_bytes':4096},str(tmp_path.parent))
    # Use the real owned series layout for inventory discovery.
    root=tmp_path/'owned'; folder=root/'series';folder.mkdir(parents=True)
    old_json=folder/source.name; old_block=folder/Path(result['target']).name
    source.rename(old_json);Path(result['target']).rename(old_block)
    limits.state_dir=str(root)
    assert limits.write(str(path(folder,jid='next')),b'{"t":3}\n')
    assert not old_json.exists() and list(S.block_rows(old_block))==[{'t':1},{'t':2}]


def test_quarantined_tails_count_toward_quota_and_retire_before_raw(tmp_path):
    folder=tmp_path/'series';folder.mkdir()
    original=path(folder,'.rvb',jid='old');original.write_bytes(B.encode([{'t':1}],B._identity(original)));os.utime(original,ns=(1,1))
    damaged=Path(str(original)+'.damaged-0-12345678');damaged.write_bytes(b'partial suffix');os.utime(damaged,ns=(2,2))
    limits=LightLimits({'storage_files':2,'storage_bytes':4096},str(tmp_path))
    fresh=path(folder,jid='new')
    assert limits.write(str(fresh),b'{"t":2}\n')
    assert not damaged.exists() and list(S.block_rows(original))==[{'t':1}]
    assert limits.disk_count==2 and limits.disk_bytes==original.stat().st_size+fresh.stat().st_size


def test_known_stale_session_precedes_older_finished_recording(tmp_path):
    folder=tmp_path/'series';folder.mkdir()
    finished=path(folder,jid='finished');finished.write_bytes(b'{"t":1}\n');os.utime(finished,ns=(1,1))
    session=path(folder,jid='session');session.write_bytes(b'{"t":2}\n');os.utime(session,ns=(2,2))
    limits=LightLimits({'storage_files':2,'storage_bytes':4096},str(tmp_path));limits.note_session_path(str(session))
    assert limits.write(str(path(folder,jid='new')),b'{"t":3}\n')
    assert not session.exists() and finished.exists()


def test_pin_protection_updates_without_waiting_for_queue_poll(tmp_path):
    limits=LightLimits({'storage_files':2,'storage_bytes':4096},str(tmp_path));store=Store(str(tmp_path),raven_limits=limits)
    try:
        store.record('pin',{'t':1});pinned=Path(store._series_path('pin'));os.utime(pinned,ns=(1,1))
        store.record('old',{'t':2});old=Path(store._series_path('old'));os.utime(old,ns=(2,2))
        store.pin_many(['pin'],True)
        store.record('new',{'t':3})
        assert pinned.exists() and not old.exists() and 'pin' in limits.pinned_jobs
    finally:store.close()


@pytest.mark.parametrize('file_budget',[1,4])
def test_full_event_journal_rotates_and_resumes_at_quota(tmp_path,file_budget):
    journal=tmp_path/'events.jsonl'
    journal.write_bytes(b'{"kind":"old","text":"bounded journal"}\n'*100)
    limits=LightLimits({'storage_bytes':journal.stat().st_size,'storage_files':file_budget},str(tmp_path))
    assert limits.write(str(journal),b'{"kind":"new"}\n')
    assert list(S.json_rows(journal))==[{'kind':'new'}]
    assert limits.disk_bytes<=limits.values['storage_bytes'] and limits.disk_count<=file_budget
    backup=tmp_path/'events.jsonl.previous'
    assert not backup.exists() or backup.stat().st_size<=limits.values['storage_bytes']//8


def test_default_retention_streams_more_than_fifty_thousand_samples(tmp_path):
    source=path(tmp_path)
    with source.open('wb') as stream:
        for number in range(60001): stream.write(S._canonical({'t':number*5,'k':'live','cpu':.5,'rss':1234}))
    result=R.build(source,as_of=300000,apply=True)
    assert result['raw_records']==60001 and result['source']['records']==60001
    assert result['observed_gaps']==[] and result['gap_threshold_seconds']==15
    assert 'SQLite' in result['working_memory']


def test_compact_append_cache_invalidates_on_external_change(tmp_path,monkeypatch):
    source=path(tmp_path,'.rvb'); cache={}; original=B.decode; calls=[]
    monkeypatch.setattr(B,'decode',lambda *args: (calls.append(1),original(*args))[1])
    B.append(source,[{'t':1}],cache=cache)
    B.append(source,[{'t':2}],cache=cache)
    assert not calls
    data=source.read_bytes(); source.write_bytes(data)
    B.append(source,[{'t':3}],cache=cache)
    assert len(calls)==1 and B.read(source,3)[0]==[{'t':1},{'t':2},{'t':3}]


def test_deferred_publication_batches_until_deadline_and_close(tmp_path):
    store=Store(str(tmp_path),series_storage='blocks-v1')
    for number in range(32):
        store.record('7',{'t':number},_persist=False)
        store.flush_persistence(_defer_series=True)
    target=Path(S.compact_path(store._series_path('7')))
    assert not target.exists()
    store._series_last_flush-=2
    store.flush_persistence(_defer_series=True)
    assert B.read(target,100)[1]['blocks']==1
    store.record('7',{'t':32},_persist=False); store.close()
    assert len(B.read(target,100)[0])==33


def test_browsing_empty_charts_preserves_running_history_and_bounds_caches(tmp_path):
    limits=LightLimits({'max_series_jobs':2});store=Store(persist=False,raven_limits=limits)
    store.apply_jobs([Job('7','train','local','RUNNING')])
    for number in range(50):store.record('7',{'t':number},_persist=False)
    for number in range(300):assert store.series_of('empty-'+str(number))==[]
    assert len(store.series['7'])==50 and len(store.series)==1
    assert len(store._recording_identities)<=4 and len(store._series_archive.empty)<=2


def test_record_context_index_is_reused_across_sample_publications():
    class Counted(list):
        traversals=0
        def __iter__(self): self.traversals+=1;return super().__iter__()
    store=Store(persist=False)
    store.apply_jobs([Job(str(i),'train','local','RUNNING') for i in range(1000)])
    store.jobs=Counted(store.jobs); store._record_lookup_signature=None
    for job in list(store.jobs):store.record(job.id,{'t':1},_persist=False)
    assert store.jobs.traversals<=3


def test_record_context_rechecks_replaced_public_row_without_full_scan():
    from dataclasses import replace
    store=Store(persist=False)
    store.apply_jobs([Job('7','train','local','RUNNING',start='old'),Job('8','other','local','RUNNING')])
    assert store.job('7').start=='old'
    changed=replace(store.jobs[0],start='new')
    store.jobs[0]=changed
    assert store.record_context('7')[0] is changed and store.job('7') is changed
    finished=Finished('9',state='COMPLETED',start='first')
    store.finished=[finished]
    assert store.record_context('9')[0] is finished
    corrected=replace(finished,start='corrected');store.finished[0]=corrected
    assert store.record_context('9')[0] is corrected
    assert store.record_context('10')[0] is None
    other=replace(changed,id='10');store.jobs[0]=other
    assert store.record_context('10')[0] is other and store.job('10') is other
    assert store.record_context('7')[0] is None


def test_tag_and_uniform_pin_use_one_shared_background_publication(tmp_path,monkeypatch):
    store=Store(str(tmp_path)); entered=threading.Event(); release=threading.Event(); actual=store._save_tags
    def slow(): entered.set();assert release.wait(2);actual()
    monkeypatch.setattr(store,'_save_tags',slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        store.configure_persistence_writer(pool.submit)
        store.tag_many(['1','2'],'lab')
        assert entered.wait(1)
        store.pin_many(['1'],True)
        assert store.pin_many(['1','2']) is True
        assert all(store.pinned(jid) for jid in ('1','2'))
        assert store.lock.acquire(timeout=.1);store.lock.release()
        release.set(); store.configure_persistence_writer(None)
    assert json.loads((tmp_path/'tags.json').read_text())['2']['pinned']
    store.close()


def test_restart_barrier_drains_new_tags_pins_and_notes_after_a_paused_old_snapshot(tmp_path, monkeypatch):
    from raven import ui_state

    store = Store(str(tmp_path))
    store.tag('7', 'urgent', 'paper')
    entered, release, waited = threading.Event(), threading.Event(), threading.Event()
    actual_atomic = ui_state._atomic
    held = False

    def paused_atomic(path, payload):
        nonlocal held
        if str(path) == str(tmp_path / 'tags.json') and not held:
            held = True
            assert json.loads(payload)['7']['tags'] == ['urgent', 'paper']
            entered.set()
            assert release.wait(10)
        return actual_atomic(path, payload)

    monkeypatch.setattr(ui_state, '_atomic', paused_atomic)
    reader = None
    with ThreadPoolExecutor(max_workers=1) as writer, ThreadPoolExecutor(max_workers=1) as shutdown:
        try:
            store.configure_persistence_writer(writer.submit)
            store.note('7', 'old note')
            assert entered.wait(5)
            store.tag('7', 'urgent', remove=True)
            store.note('7', 'latest note')
            store.pin('8', True)
            pending = store._persist_writer_future
            actual_result = pending.result

            def observed_wait(*args, **kwargs):
                waited.set()
                return actual_result(*args, **kwargs)

            monkeypatch.setattr(pending, 'result', observed_wait)
            drained = shutdown.submit(store.configure_persistence_writer, None)
            assert waited.wait(5)
            assert not drained.done(), 'Shutdown must wait for the paused older publication'
            assert store.tags_of('7') == ['paper'] and store.pinned('8')
            release.set()
            drained.result(timeout=10)
            reader = Store(str(tmp_path))
            assert reader.tags_of('7') == ['paper']
            assert reader.tags['7']['note'] == 'latest note'
            assert reader.pinned('8')
            assert not store._tags_dirty
        finally:
            release.set()
            store.close()
            if reader is not None:
                reader.close()


def test_started_sequence_outlives_bounded_event_deque():
    store=Store(persist=False,max_events=2)
    for number in range(5):store.event('started','job '+str(number))
    assert len(store.events)==2 and store.render_snapshot()['started_seq']==5


def test_stale_legacy_details_cannot_authorize_new_verified_sample(tmp_path):
    job=Job('7','train','local','RUNNING',submit='2026-10-20T01:00:00',start='2026-10-20T01:00:01',cluster='lab')
    store=Store(str(tmp_path));store.apply_jobs([job])
    details={'JobId':'7','SubmitTime':job.submit,'StartTime':job.start,'Restarts':'0','Cluster':'lab'}
    store.details['7']=details; assert store.bind_series_scheduler('7',details)
    store.record('7',{'t':1});verified=Path(store._series_path('7'));original=verified.read_bytes()
    # A same-second requeue is indistinguishable in this legacy queue row.
    store.apply_jobs([replace(job)])
    store.record('7',{'t':2})
    assert verified.read_bytes()==original and [row['t'] for row in store.series['7']]==[1,2]
    isolated=store._isolated_recording_paths['7'];isolated_path=Path(store._series_identity_path(isolated))
    assert list(S.json_rows(isolated_path))==[{'t':2}] and not isolated.verified
    fresh=dict(details,Restarts='1');store.details['7']=fresh;store.bind_series_scheduler('7',fresh)
    store.record('7',{'t':3})
    assert list(S.json_rows(store._series_path('7')))==[{'t':3}]
    assert verified.read_bytes()==original and isolated_path.exists()
    store.close()


def test_unselected_legacy_jobs_receive_fresh_attempt_proofs(tmp_path):
    jobs=[Job(str(i),'train','local','RUNNING',submit='2026-10-20T01:00:00',start='2026-10-20T01:00:01',cluster='lab') for i in range(4)]
    calls=[]
    def details(job):
        calls.append(job.id)
        return {'JobId':job.id,'SubmitTime':job.submit,'StartTime':job.start,'Restarts':'0','Cluster':'lab'}
    slurm=SimpleNamespace(record_details=details,live=lambda job,previous,now:(Live(t=now,cpu_time=1,rss=1234),None,[]))
    store=Store(str(tmp_path));store.apply_jobs(jobs);sampler=Sampler(slurm,store,{},[],workers=1)
    try:
        sampler.src_live();store.flush_persistence()
        assert set(calls)=={job.id for job in jobs} and sampler.want_detail is None
        assert all(store.capture_series_identity(job.id).verified for job in jobs)
        assert all(list(S.json_rows(store._series_path(job.id))) for job in jobs)
    finally:sampler.shutdown();store.close()


def test_explicit_retained_binding_authorizes_only_its_current_publication(tmp_path):
    from dataclasses import replace
    job=Job('7','train','local','RUNNING',submit='2026-10-20T01:00:00',start='2026-10-20T01:00:01',cluster='lab')
    binding=dict(run_root='/project',run_id='run',attempt='a1',job_id='7',submission_token='token',cluster='lab')
    store=Store(str(tmp_path));store.apply_jobs([job])
    try:
        identity=store.bind_series_attempt('7',binding)
        assert store.sample_recording_identity('7')==identity
        store.record('7',{'t':1})
        path=Path(store._series_identity_path(identity));assert list(S.json_rows(path))==[{'t':1}]
        store.apply_jobs([replace(job)])
        assert not store.sample_recording_identity('7').verified
        store.record('7',{'t':2})
        assert list(S.json_rows(path))==[{'t':1}]
        store.bind_series_attempt('7',binding)
        store.record('7',{'t':3})
        assert list(S.json_rows(path))==[{'t':1},{'t':3}]
    finally:store.close()


def test_rotation_creation_enospc_retries_without_retiring_family(tmp_path,monkeypatch):
    (tmp_path/'job.sh').write_text('#!/bin/sh\ntrue\n')
    run=reporting.begin_run(tmp_path,'telemetry-run',name='fixture',script='job.sh')
    monkeypatch.setattr(threading.Thread,'start',lambda self:None)
    recording=T.Recording(run,{'runtime_pid':os.getpid()},environ={}); source=recording.sources[0]
    try:
        recording.segment_bytes=1
        recording._append(source,dict(recording._common(source,1),rss_bytes=1))
        original=os.open;failed=False
        def full(name,flags,*args,**kwargs):
            nonlocal failed
            if name=='cpu-memory.jsonl' and flags&os.O_CREAT and not failed:
                failed=True;raise OSError(errno.ENOSPC,'fixture rotation full')
            return original(name,flags,*args,**kwargs)
        monkeypatch.setattr(os,'open',full)
        with pytest.raises(OSError):recording._append(source,dict(recording._common(source,2),rss_bytes=2))
        assert 'cpu-memory' in recording.streams and recording.streams['cpu-memory'] is None
        monkeypatch.setattr(os,'open',original)
        recording._append(source,dict(recording._common(source,3),rss_bytes=3))
        recording._publish()
        assert source['coverage']['samples']==2 and source['segments']
    finally:recording._release()


def test_full_prefix_watermark_rejects_middle_edit_followed_by_append(tmp_path):
    source=tmp_path/'metrics.jsonl';source.write_bytes(b'a'*(256<<10))
    captured=D._source('application','metrics',source,append=True)
    with source.open('r+b') as stream:stream.seek(128<<10);stream.write(b'changed')
    with source.open('ab') as stream:stream.write(b'new records\n')
    with pytest.raises(D.ExportError,match='changed'):D._source_open(captured)


def test_private_durable_export_refuses_symlink_and_existing_file(tmp_path,monkeypatch):
    root=tmp_path/'data';monkeypatch.setenv('XDG_DATA_HOME',str(root));monkeypatch.delenv('RAVEN_EXPORT_DIR',raising=False)
    folder=Path(E.export_dir(str(tmp_path/'ephemeral-state')))
    victim=tmp_path/'notes';victim.write_text('preserve')
    (folder/'unsafe.csv').symlink_to(victim)
    with pytest.raises(FileExistsError):E.write(None,'unsafe.csv','replace')
    target=Path(E.write(None,'safe.csv','content'))
    with pytest.raises(FileExistsError):E.write(None,'safe.csv','later')
    assert victim.read_text()=='preserve' and target.read_text()=='content'
    assert target.stat().st_mode&0o777==0o600 and root in target.parents


def test_default_telemetry_budget_scales_for_48h_eight_gpu_job():
    budget=T._storage_budget({'effective_options':{'time':'2-00:00:00','gpus':'8'}},{},{},{},5.)
    assert 64<<20 < budget <= 256<<20


def test_sqlite_without_extension_loading_still_applies_limits():
    class Embedded:
        def __init__(self):self.commands=[]
        def execute(self,command):self.commands.append(command)
    connection=Embedded();I._limits(connection)
    assert connection.commands==['PRAGMA trusted_schema=OFF']


def test_compensated_window_recovers_small_values_after_large_spike():
    selected={'samples':[(0,1e16,1),(1,1.,2),(2,1.,3),(3,1.,4)],'unit':'count'}
    result=I.transform(selected,operation='window',max_gap=10,window_seconds=2)
    assert [row['value'] for row in result['samples']][2:]==[1.,1.]


@pytest.fixture
def indexed(tmp_path):
    root=tmp_path/'project';root.mkdir();(root/'job.sh').write_text('#!/bin/sh\ntrue\n')
    run=reporting.begin_run(root,'run-one',name='experiment',script='job.sh')
    reporting.write_metric(run,{'loss':.5})
    exported=D.export_data({'project':str(root),'runs':[run.name],'include':['application']})
    bundle=Path(exported['outputs'][0]);index=tmp_path/'index';I.ingest(index,bundle,str(root))
    return root,bundle,index


@pytest.mark.parametrize('value',[1,None,{},[]])
def test_invalid_manifest_path_has_controlled_error(indexed,value):
    project,bundle,_=indexed;manifest=json.loads((bundle/'manifest.json').read_text());manifest['files'][0]['path']=value
    raw=json.dumps(manifest,separators=(',',':')).encode();(bundle/'manifest.json').write_bytes(raw)
    marker=json.loads((bundle/'COMPLETE.json').read_text());marker['manifest_sha256']=hashlib.sha256(raw).hexdigest();(bundle/'COMPLETE.json').write_text(json.dumps(marker))
    with pytest.raises(ValueError,match='paths'):I.inspect_bundle(bundle,str(project))


def test_evidence_view_uses_ordering_index_and_oversized_row_keeps_progress(indexed):
    _,_,index=indexed
    connection=sqlite3.connect(index/I.DATABASE)
    try:
        plan=connection.execute('EXPLAIN QUERY PLAN '+I.VIEWS['application']).fetchall()
        assert any('measurement_order' in row[-1] for row in plan)
        assert all('TEMP B-TREE' not in row[-1] for row in plan)
    finally:connection.close()
    result=I.query(index,"SELECT '" + 'λ'*1000 + "' AS long_text",max_bytes=512)
    assert result['reason']=='oversized row truncated' and len(result['rows'])==1
    assert result['oversized_rows'][0]['original_bytes']>512 and len(result['oversized_rows'][0]['sha256'])==64
