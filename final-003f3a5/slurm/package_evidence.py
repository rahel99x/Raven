from pathlib import Path
import hashlib,json,shutil,tarfile
root=Path('/workspace/raven-final-slurm-003f3a5c')
out=Path('/workspace/raven-final-slurm-qualification-artifacts-003f3a5c')
out.mkdir(mode=0o700,exist_ok=False)
source=json.loads((root/'source-manifest.json').read_text())
versions=('23.02.8','23.11.11','24.05.8','24.11.7','26.05.5')
summary={'schema':'raven.final-live-slurm-review/v1','tested_git_revision':source['base_revision'],
 'source_package_sha256':source['sha256'],'source_package_files':len(source['files']),
 'source_python_files':625,'source_python_sha256':'828b8787952dac6077aa8152c25a606670891a303d928df2531cdf8386dbb81e',
 'qualification_scope':'Owned disposable local single-node Slurm services; actual scheduler/accounting responses and actual Heavy processes.',
 'production_qualified':False,'versions':{},'checks_passed':0,
 'limitations':['No physical GPU, remote SSH, multinode or production-site qualification.',
 'No performance measurements: qualification overlapped the independent regression suite.',
 'Only26.05.5 uses the qualified JSON data-parser path. Its warning case deliberately augments real scheduler JSON warning metadata without changing job rows; older-version native text rows are unmodified.',
 'Exact tested checkpoint is003f3a5c; later pointer_routing/workbench UI corrections are outside this source snapshot.'],
 'commands':[],'archives':{}}
def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def archive(name,entries):
 path=out/name
 with tarfile.open(path,'w:gz') as t:
  for source_path,arcname in entries:t.add(source_path,arcname=arcname,recursive=False)
 summary['archives'][name]={'sha256':digest(path),'bytes':path.stat().st_size}
def files_below(base):
 # Private runtime cache/receipt directories are not exported. Recorded commands,
 # resulting manager snapshots, source maps and daemon logs are all retained.
 files=[]
 for folder in (base,base/'daemon-logs'):
  if folder.is_dir():
   files.extend((p,str(p.relative_to(base))) for p in sorted(folder.iterdir()) if p.is_file())
 return files
for version in versions:
 base=root/'evidence'/version
 m=json.loads((base/'manifest.json').read_text())
 assert m['status']=='passed-recorded-checks' and all(c['passed'] for c in m['checks'])
 manifest=(base/'source-manifest.json').read_bytes()
 assert hashlib.sha256(manifest).hexdigest()==digest(root/'source-manifest.json')
 archive(version+'.tar.gz',files_below(base))
 checks=[]
 for c in m['checks']:
  checks.append({k:v for k,v in c.items() if k in ('name','passed','records','physical_lines')})
 summary['versions'][version]={'status':m['status'],'checks':checks,'check_count':len(checks),
  'image_id':(base/'image-id.txt').read_text().strip(),'response_augmentation':m['response_augmentation'],
  'reader_status':m.get('reader_status',{}),'command_count':len(m['commands'])}
 summary['checks_passed']+=len(checks)
 summary['commands'].append(['env','RAVEN_QUALIFICATION_REVISION='+source['base_revision'],'python3',str(root/'scripts/qualify_slurm.py'),'--version',version,'--skip-build','--scenario','review-findings','--output',str(base)])
base=root/'evidence/orphan-26.05.5';m=json.loads((base/'manifest.json').read_text())
assert m['status']=='passed' and m['unrelated_sentinel_preserved']
archive('orphan-26.05.5.tar.gz',files_below(base))
summary['orphan_recovery']={'version':'26.05.5','status':m['status'],'unrelated_sentinel_preserved':m['unrelated_sentinel_preserved'],
 'actual_owner_uid':m['recovery']['actual_owner_uid'],'ledger_counts':m['recovery']['ledger_counts'],
 'first_phase':m['recovery']['first']['recovery']['phase'],'confirmed_cleanup':not m['recovery']['second']['cleanup_uncertain']}
summary['commands'].append(['env','RAVEN_QUALIFICATION_REVISION='+source['base_revision'],'python3',str(root/'scripts/qualify_slurm.py'),'--version','26.05.5','--skip-build','--scenario','allocation-recovery','--output',str(base)])
entries=[]
for folder in ('raven','scripts'):
 for p in sorted((root/folder).rglob('*')):
  if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc':entries.append((p,str(p.relative_to(root))))
entries.append((root/'source-manifest.json','source-manifest.json'))
archive('qualified-source.tar.gz',entries)
for name in ('prepare_snapshot.py','review_findings_driver.py'):
 shutil.copyfile(Path('/workspace/raven-review-slurm-qualification-artifacts')/name,out/name)
shutil.copyfile(Path(__file__),out/'package_evidence.py')
(out/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
(out/'README.md').write_text('''# Final real Slurm qualification\n\nExact tested Git checkpoint: `003f3a5c515d70b48982d6c856c57a1d6607d613`. The source archive recursively includes all canonical families,625 Python files plus the tracked `transport/py.typed` marker. The complete per-file map is `source-manifest.json` inside `qualified-source.tar.gz`.\n\nAll82 checks passed across23.02.8,23.11.11,24.05.8,24.11.7 and26.05.5. The26.05.5 actual governed orphan-recovery scenario passed separately and preserved an unrelated owned sentinel. Six result archives contain all recorded commands and scheduler responses, manager snapshots, image identities and daemon logs. Private runtime cache/receipt directories are excluded; their relevant resulting ownership snapshots are retained in the manifests.\n\nThe26.05.5 warning case augments real scheduler JSON with declared warning metadata while retaining genuine job rows. It is not evidence of a naturally emitted Slurm warning. No physicalGPU, remoteSSH, multinode, production-site or performance qualification is claimed. Later UI-only pointer-routing/workbench corrections are outside this exact tested checkpoint.\n\nTo reproduce, run `python3 prepare_snapshot.py /path/to/checkout /path/to/private/snapshot`, then the printed command for each pinned version and the `allocation-recovery` scenario on26.05.5. Cached images are used with `--skip-build`; the repository harness owns and removes only its private disposable container/network and preserves evidence on failure.\n''')
manifest={'schema':'raven.qualification-artifact-manifest/v1','tested_git_revision':source['base_revision'],
 'files':{p.name:{'sha256':digest(p),'bytes':p.stat().st_size} for p in sorted(out.iterdir()) if p.is_file()}}
(out/'artifact-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
print(json.dumps({'directory':str(out),'checks_passed':summary['checks_passed'],'orphan':summary['orphan_recovery'],
 'artifact_manifest_sha256':digest(out/'artifact-manifest.json'),'summary_sha256':digest(out/'summary.json')},indent=2))
