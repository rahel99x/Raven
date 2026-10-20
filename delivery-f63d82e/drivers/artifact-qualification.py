from pathlib import Path
import io,json,subprocess,sys,tarfile
root=Path('/workspace/Raven'); rev=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(); short=rev[:7]
source=Path('/workspace/review-final-source-'+short); dist=Path('/workspace/review-final-dist-'+short)
source.mkdir(exist_ok=False); dist.mkdir(exist_ok=False)
with tarfile.open(fileobj=io.BytesIO(subprocess.check_output(['git','archive',rev],cwd=root))) as archive:
    archive.extractall(source,filter='data')
python=str(root/'.venv/bin/python')
def run(command,label,cwd=root):
    with open('/workspace/review-final-'+label+'-'+short+'.log','w') as log:
        subprocess.run(command,cwd=cwd,stdout=log,stderr=subprocess.STDOUT,check=True)
run([python,'-m','build',str(source),'--outdir',str(dist)],'build')
wheel,=dist.glob('*.whl'); sdist,=dist.glob('*.tar.gz')
run([python,str(source/'scripts/release_inventory.py'),'create','--output',str(dist/'release-inventory.json'),'--spdx',str(dist/'raven.spdx.json'),'--checksums',str(dist/'SHA256SUMS'),'--source-commit',rev,str(wheel),str(sdist)],'inventory-create')
run([python,str(source/'scripts/release_inventory.py'),'verify',str(dist/'release-inventory.json'),str(wheel),str(sdist)],'inventory-verify')
run([python,str(source/'scripts/release_inventory.py'),'policy',str(dist/'release-inventory.json'),'--expected-version','7.0.0','--expected-source',rev,str(wheel),str(sdist)],'inventory-policy')
run([python,str(source/'scripts/qualify_flagship.py'),'--wheel',str(wheel),'--output','/workspace/review-final-flagship-'+short+'.json'],'flagship')
smoke=Path('/workspace/qualify-final-installed-smoke.py').read_text().replace('3fd4d97c5f4d1a0e7bb5fad8322ab32912691e52',rev).replace('3fd4d97',short)
smoke_path=Path('/workspace/qualify-final-installed-smoke-'+short+'.py');smoke_path.write_text(smoke)
run([python,str(smoke_path)],'installed-smoke')
run([python,str(source/'scripts/setup.py'),'--mode','demo','--venv','/workspace/review-final-offline-venv-'+short,'--report','/workspace/review-final-offline-'+short+'.txt'],'offline',cwd=source)
with tarfile.open(sdist) as archive:
    destination=Path('/workspace/review-final-sdist-'+short); destination.mkdir(exist_ok=False);archive.extractall(destination,filter='data')
extracted,=destination.iterdir()
assert not (extracted/'docs/implementation').exists()
fixtures=extracted/'tests/fixtures/review-temporal-holdout'
assert sorted(str(path.relative_to(fixtures)) for path in fixtures.rglob('*.json'))==['forecast/model.json','forecast/result.json','queue/model.json','queue/result.json']
run([python,'-c','import pathlib,raven; assert pathlib.Path(raven.__file__).resolve().is_relative_to(pathlib.Path.cwd())'],'sdist-source',cwd=extracted)
run([python,'-m','pytest','-q','tests/test_slurm_json.py','tests/test_slurm_qualification.py','tests/test_completed_series_identity.py','tests/test_trace_lab_example.py','tests/test_trace_lab_accelerators.py','tests/test_forecast_scenarios.py','tests/test_slurm_simulator.py','tests/test_review_environment.py'],'sdist',cwd=extracted)
Path('/workspace/review-final-artifacts-'+short+'.json').write_text(json.dumps({'code_revision':rev,'source':str(source),'dist':str(dist),'status':'passed'},indent=2)+'\n')
print('Qualified final artifacts',rev)
