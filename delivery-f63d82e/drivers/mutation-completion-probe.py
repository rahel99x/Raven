"""Compare old/current late-completion ownership with a real ResearchHub worker.

No scheduler is contacted: the controlled transport waits after dispatch and
returns one successful cancellation. Old control flow uses current contracts.
"""
import ast
import hashlib
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic,sleep
from types import MethodType
from raven import controller
from raven.actions import Actions
from raven.config import Config
from raven.model import Store
from raven.slurm import Slurm,FakeBackend

BASELINE = '3fd4d97c5f4d1a0e7bb5fad8322ab32912691e52'
source = subprocess.check_output(['git','show',BASELINE+':raven/controller.py'],text=True)
klass = next(node for node in ast.parse(source).body if isinstance(node,ast.ClassDef) and node.name=='App')
function = next(node for node in klass.body if isinstance(node,ast.FunctionDef) and node.name=='finish_confirm')
namespace = dict(vars(controller))
exec(compile(ast.fix_missing_locations(ast.Module(body=[function],type_ignores=[])),'<baseline-finish-confirm>','exec'),namespace)

def probe(baseline):
    with TemporaryDirectory(prefix='raven-stale-mutation-') as temporary:
        old = Store(str(Path(temporary)/'old'),persist=False)
        slurm = Slurm(FakeBackend('alex'),'alex')
        old.apply_jobs(slurm.jobs())
        app = controller.App(old,None,Actions(slurm,old),Config(),'alex',ascii_=True,interactive=True)
        if baseline:
            app.finish_confirm = MethodType(namespace['finish_confirm'],app)
        app.visible_ids = [job.id for job in old.jobs]
        app.selected_id = app.visible_ids[0]
        job_id = app.selected_id
        started,release = Event(),Event()
        def cancel(ids):
            started.set()
            assert release.wait(5)
            return True,''
        slurm.cancel = cancel
        try:
            app.run_command('cancel '+job_id)
            app.finish_confirm(True)
            assert started.wait(3)
            new = Store(str(Path(temporary)/'new'),persist=False)
            new_slurm = Slurm(FakeBackend('bob'),'bob')
            new.apply_jobs(new_slurm.jobs())
            app.store,app.actions,app.user = new,Actions(new_slurm,new),'bob'
            app.marks = {job_id}
            app.message = 'Current connection is ready'
            release.set()
            deadline = monotonic()+5
            while app.research.pending and monotonic()<deadline:
                app.research.poll_task();sleep(.005)
            assert app.research.pending is None
            return {'new_connection_mark_preserved':job_id in app.marks,'new_connection_message_preserved':app.message=='Current connection is ready','old_audit_records':len([event for event in old.events if event.get('action')=='cancel']),'reviewed_user':'alex','current_user':app.user}
        finally:
            release.set()
            if app.research is not None:
                app.research.close()

result = {'schema':'raven.mutation-completion-ownership-probe/v1','baseline_revision':BASELINE,'baseline':probe(True),'fixed':probe(False),'current_controller_sha256':hashlib.sha256(Path(controller.__file__).read_bytes()).hexdigest(),'limits':'Actual local worker and audit with controlled fake cancellation transport; no live scheduler, remote host or performance claim.'}
assert not result['baseline']['new_connection_mark_preserved']
assert not result['baseline']['new_connection_message_preserved']
assert result['fixed']['new_connection_mark_preserved']
assert result['fixed']['new_connection_message_preserved']
assert result['baseline']['old_audit_records']==result['fixed']['old_audit_records']==1
print(json.dumps(result,indent=2))
