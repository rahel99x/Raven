"""Verify the authorized merge against completed CI and exact runtime artifacts."""
from pathlib import Path
import argparse,hashlib,io,json,subprocess,tarfile
ROOT=Path('/workspace/Raven')
CODE='f63d82ecd40c8b3ad4bdb42c5f92a492d9b0cdc0'
def git(*args):
    return subprocess.check_output(['git',*args],cwd=ROOT,text=True).strip()
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--ci-receipt',type=Path,required=True)
args=parser.parse_args()
ci=json.loads(args.ci_receipt.read_bytes())
assert ci['conclusion']=='success' and ci['event']=='pull_request'
assert {x['python'] for x in ci['python_jobs']}=={'3.10','3.11','3.12','3.13','3.14'}
assert all(x['conclusion']=='success' and x['status']=='completed' and x['full_suite']['passed']>19000 and not x['full_suite'].get('failed',0) and not x['full_suite'].get('errors',0) for x in ci['python_jobs'])
head,qualified_merge=ci['pull_request_head'],ci['tested_merge']
tree=git('rev-parse',head+'^{tree}')
assert git('rev-parse',qualified_merge+'^{tree}')==tree
raw=subprocess.check_output(['gh','pr','view','5','--json','url,state,headRefOid,mergeCommit,mergedAt,baseRefName'],cwd=ROOT)
pr=json.loads(raw)
assert pr['state']=='MERGED' and pr['headRefOid']==head and pr['baseRefName']=='main',pr
Path('/workspace/review-main-merged-pr.json').write_bytes(raw)
subprocess.run(['git','fetch','origin','main'],cwd=ROOT,check=True)
main=git('rev-parse','origin/main')
assert main==pr['mergeCommit']['oid'],(main,pr)
assert git('rev-parse',main+'^{tree}')==tree
proof=json.loads(Path('/workspace/review-final-source-proof-f63d82e.json').read_text())
with tarfile.open(fileobj=io.BytesIO(subprocess.check_output(['git','archive',main,'raven'],cwd=ROOT))) as archive:
    mapping={x.name:hashlib.sha256(archive.extractfile(x).read()).hexdigest() for x in archive if x.isfile()}
assert mapping==proof['runtime_sha256']
report={
 'schema':'raven.main-delivery-verification/v1','pr':pr,
 'main_commit':main,'qualified_head':head,'qualified_pr_merge':qualified_merge,
 'identical_qualified_tree':tree,'runtime_code_revision':CODE,
 'runtime_files_identical':len(mapping),
 'all_main_runtime_bytes_match_qualified_wheel_and_sdist':True,
 'successful_ci_receipt':str(args.ci_receipt),'successful_ci_receipt_sha256':hashlib.sha256(args.ci_receipt.read_bytes()).hexdigest(),
 'main_push_workflow_note':'PR CI qualified this exact Git tree. The independently triggered main push workflow has its own status and is not inferred from tree equality.'
}
Path('/workspace/review-main-delivery-proof.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
