from pathlib import Path
import hashlib,io,json,subprocess,tarfile,zipfile
root=Path('/workspace/Raven');rev=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip();short=rev[:7];tested='003f3a5c515d70b48982d6c856c57a1d6607d613'
def source_at(revision):
    with tarfile.open(fileobj=io.BytesIO(subprocess.check_output(['git','archive',revision,'raven'],cwd=root))) as archive:
        return {member.name:archive.extractfile(member).read() for member in archive if member.isfile()}
def digest(raw):return hashlib.sha256(raw).hexdigest()
blobs=source_at(rev);base=source_at(tested);dist=Path('/workspace/review-final-dist-'+short);wheel,=dist.glob('*.whl');sdist,=dist.glob('*.tar.gz')
with zipfile.ZipFile(wheel) as archive:
    names={name for name in archive.namelist() if name.startswith('raven/') and not name.endswith('/')}
    assert names==set(blobs),(names-set(blobs),set(blobs)-names)
    for name in names:assert archive.read(name)==blobs[name],name
seen=set()
with tarfile.open(sdist) as archive:
    for member in archive:
        name=member.name.partition('/')[2]
        if name in blobs:
            assert archive.extractfile(member).read()==blobs[name],name
            seen.add(name)
assert seen==set(blobs),set(blobs)-seen
mapping={name:digest(raw) for name,raw in sorted(blobs.items())}
backend=json.loads(Path('/workspace/raven-final-slurm-qualification-artifacts-003f3a5c/qualified-backend-manifest.json').read_text())
for name,sha in backend['files'].items():assert mapping[name]==sha,name
changed={name:{'tested_sha256':digest(base[name]),'delivered_sha256':mapping[name]} for name in blobs if name in base and blobs[name]!=base[name]}
proof={'schema':'raven.delivered-source-proof/v1','code_revision':rev,'runtime_files':len(blobs),'python_files':sum(name.endswith('.py') for name in blobs),'runtime_sha256':mapping,'mapping_digest_method':'SHA256 of sorted compact JSON UTF8 without newline','mapping_sha256':digest(json.dumps(mapping,sort_keys=True,separators=(',',':')).encode()),'wheel_sha256':digest(wheel.read_bytes()),'sdist_sha256':digest(sdist.read_bytes()),'wheel_all_runtime_members_equal_git_archive':True,'sdist_runtime_members_equal_git_archive':True,'live_slurm_tested_revision':tested,'qualified_backend_files_unchanged':len(backend['files']),'qualified_backend_mapping_sha256':backend['sha256'],'changed_runtime_files_after_live_checkpoint':changed,'added_runtime_files_after_live_checkpoint':sorted(set(blobs)-set(base)),'removed_runtime_files_after_live_checkpoint':sorted(set(base)-set(blobs)),'scope_limit':'The five-version live Slurm matrix tested its exact earlier checkpoint. Unchanged backend proof does not claim the entire delivered source was live-qualified. New UI, plugin and update-policy paths require their final focused/full/installed/CI checks.'}
Path('/workspace/review-final-source-proof-'+short+'.json').write_text(json.dumps(proof,indent=2)+'\n');print(len(blobs),'runtime members match wheel and sdist;',len(backend['files']),'qualified backend files unchanged;',len(changed),'later runtime files changed')
