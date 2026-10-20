#!/usr/bin/env python3
"""Prepare held hosted evidence inputs outside Git; never snapshot or publish."""
import hashlib
import json
from pathlib import Path

ROOT = Path('/workspace')
TEMPLATE = ROOT / 'review-hosted-evidence-inputs.template.json'
PREPARED = ROOT / 'review-hosted-historical-preparation'
PREPARED.mkdir(mode=0o700, exist_ok=True)
contract = json.loads(TEMPLATE.read_text())
assert contract['ready'] is False
contract['python_run_id'] = 'PENDING final candidate successful Python workflow'
contract['slurm_run_id'] = 'PENDING final candidate successful Slurm workflow'
attempt = contract['historical_failed_ci']['additional_attempts'][0]
summary_path = ROOT / 'review-hosted-ffda87d-python-summary.json'
summary = json.loads(summary_path.read_text())
assert summary['head'] == attempt['head'] and summary['run_id'] == attempt['tests_run_id']
assert summary['conclusion'] == 'failure'
complete = json.loads((ROOT / 'review-hosted-ffda87d-complete.json').read_text())
assert complete['headSha'] == attempt['head'] and complete['conclusion'] == 'failure'
attempt['status'] = ('Completed workflow failed: Python3.10 helper ownership fixture and Python3.11 remote-log wall-time fixture failed one test each. Python3.12 full regression passed before experimental packaging backend isolation failed. Python3.13 and3.14 complete jobs passed. All per-interpreter outcomes remain separate and non-additive.')
attempt['python_outcomes'] = []
for item in summary['python']:
    raw = Path(item['log']).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == item['log_sha256']
    attempt['python_outcomes'].append({k: item[k] for k in ('python', 'job', 'conclusion', 'full_suite_summary', 'log_sha256')})
attempt['helper_failure_diagnosis'] = {
    'historical_cause': 'Unresolved: original hosted log lacks PID/group/session/birth and signal-state diagnostics.',
    'verified_local_ambiguity': 'A concurrent unrelated fake Git invocation can overwrite the global shared helper PID file; the original assertion can observe that other live group after the owned group was correctly killed.',
    'production_update_source_changed': False,
    'preserved_assertions': 'Original0.1second command timeout,2second helper-death assertion and unrelated sentinel survival; no skip or budget relaxation.',
    'local_validation': 'Python3.10 and3.12 whole file32passed each; Python3.14 focused1passed;20actual3.10repeats FD4->4; forced readiness failure cleanup verified.',
    'report': 'historical/fixes/update-helper-fixture.json'
}
attempt['correction_qualification_status'] = 'Local correction reports passed; next committed-candidate hosted workflow remains pending.'
records = contract['records']
by_source = {entry['source']: entry for entry in records}
def add(source, relative, role, gzip=False):
    source = Path(source)
    assert source.is_file() and not source.is_symlink(), source
    record = {'source': str(source), 'relative': relative, 'role': role,
              'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
    if gzip:
        record['gzip'] = True
    if str(source) in by_source:
        original = by_source[str(source)]
        original.update(record)
    else:
        records.append(record)
        by_source[str(source)] = record

add(ROOT / 'review-hosted-ffda87d-complete.json', 'historical/ffda87d-complete-workflow.json', 'Completed failed second-candidate workflow with all actual job and step outcomes; not final acceptance')
add(summary_path, 'historical/ffda87d-python-summary.json', 'Exact second-candidate per-interpreter full regression outcomes and original complete-log hashes; Python3.12 full success precedes packaging failure')
for version in ('310', '311', '312', '313', '314'):
    add(ROOT / f'review-hosted-py{version}-ffda87d.log', f'historical/py{version}-ffda87d.log.gz', 'Complete unchanged second-candidate hosted job log, including passing and failing steps; counts overlap and do not imply workflow success', True)
for name in ('review-update-helper-fixture-final.json', 'review-update-helper-probe-310.json', 'review-update-helper-interference-310.json', 'review-update-helper-scoped-repeat-310.json', 'review-update-helper-readiness-failure-310.json'):
    target = 'update-helper-fixture.json' if name == 'review-update-helper-fixture-final.json' else name
    add(ROOT / name, 'historical/fixes/' + target, 'Local helper-fixture diagnosis/probe evidence; exact historical hosted cause remains unresolved and production update_source.py is unchanged')
for name in ('review-update-helper-probe.py', 'review-update-helper-interference.py', 'review-update-helper-scoped-repeat.py', 'review-update-helper-readiness-failure.py'):
    add(ROOT / name, 'drivers/' + name, 'Actual local helper-ownership diagnostic driver; not an additional hosted or controlled performance qualification')
for name in ('review-update-helper-whole-310.log', 'review-update-helper-whole-312.log', 'review-update-helper-scoped-314.log', 'review-update-helper-scoped-repeat-310.log', 'review-update-helper-readiness-failure-310.log'):
    add(ROOT / name, 'historical/fixes/' + name, 'Actual local focused/repeated/fault-injected fixture validation; counts overlap and historical hosted cause remains unresolved')

add(ROOT / 'review-prototype-build-isolation-final.json', 'historical/fixes/prototype-build-isolation.json', 'Verified hosted and independent backend mismatch, isolated-build correction, source hashes and six complete installed experimental journeys; final hosted replacement still pending')
for source, relative, role in (
    (ROOT / 'review-hosted-ffda87d-distributions/tmp/raven-distribution-prototype/manifest.json', 'historical/prototype/hosted-ffda87d-failure.json', 'Actual hosted failed prototype manifest including verbatim backend mismatch stdout/stderr and exact source manifest'),
    (ROOT / 'review-prototype-clean-baseline/manifest.json', 'historical/prototype/clean-outer-baseline-failure.json', 'Independent actual same-backend mismatch reproduction in clean outer setuptools84.2/no-wheel environment'),
    (ROOT / 'review-prototype-isolation-final/manifest.json', 'historical/prototype/local-isolated-passing-six-journeys.json', 'Actual local five isolated builds, six installed add/remove/upgrade/downgrade journeys and mismatch refusal with626package source hashes'),
):
    add(source, relative, role)
    if 'failure.json' in relative:
        raw = source.read_bytes()
        value = json.loads(raw)
        text_path = PREPARED / (Path(relative).stem + '.stderr.txt')
        text_path.write_text(value['stderr'])
        add(text_path, relative.removesuffix('.json') + '.stderr.txt', 'Verbatim stderr field extracted from source manifest SHA256 ' + hashlib.sha256(raw).hexdigest() + '; derived view, original full manifest also retained')
for name in ('full', 'core', 'lab', 'next-core', 'next-lab'):
    add(ROOT / f'review-prototype-isolation-final/{name}-build.log', f'historical/prototype/local-isolated-{name}-build.log.gz', 'Actual isolated PEP517 build log from local six-journey experiment; no production split publication or final hosted acceptance claim', True)
for source, relative in (
    ('review-prototype-clean-baseline.log', 'historical/prototype/clean-outer-baseline.log.gz'),
    ('review-prototype-isolation-final.log', 'historical/prototype/local-isolated-qualification.log.gz'),
    ('review-prototype-isolation-focused.log', 'historical/prototype/local-focused-tests.log.gz'),
):
    add(ROOT / source, relative, 'Exact local prototype baseline/correction validation output; not a new hosted run', True)

add(ROOT / 'capture-final-python-ci.py', 'drivers/capture-final-python-ci.py', 'Completed-job capture driver; retains complete raw logs, exact full-regression step counts and explicitly labelled excerpts with source hashes')
add(ROOT / 'capture-final-python-ci-parser-validation.json', 'historical/capture-driver-parser-validation.json', 'Offline capture/parser validation; eleven cases are driver tests and not hosted application qualification')
# Preserve the historical object proof even if the parent generates the next
# candidate proof at the original conventional path.
proof_source = ROOT / 'review-replacement-pr-tree-proof.json'
proof_frozen = PREPARED / 'ffda87d-pr-tree-proof.json'
if not proof_frozen.exists():
    proof_frozen.write_bytes(proof_source.read_bytes())
records[:] = [entry for entry in records if entry['source'] != str(proof_source)]
by_source.pop(str(proof_source), None)
add(proof_frozen, 'historical/ffda87d-pr-tree-proof.json', 'Historical exact Git-object tree/parent proof for the failed second candidate, not the pending final replacement')

# Freeze all current retained payloads against later accidental path reuse.
for record in records:
    if not record.get('sha256'):
        record['sha256'] = hashlib.sha256(Path(record['source']).read_bytes()).hexdigest()
ui_path = ROOT / 'review-remote-log-fixture-final.json'
if ui_path.is_file():
    ui = json.loads(ui_path.read_text())
    assert ui['runtime_unchanged'] is True and ui['base_revision'] == attempt['head']
    add(ui_path, 'historical/fixes/remote-log-fixture.json', 'Actual shared-scheduler fixture omission, deterministic nonblocking guards and unchanged0.5second thread-CPU work bound; original hosted wall-delay attribution remains unavailable')
    ui_sources = {}
    for item in ui['drivers']:
        ui_sources[item['path']] = ('drivers/' + Path(item['path']).name, item['sha256'])
    for item in ui['validation']:
        if 'log' in item:
            proof = item['log']
            ui_sources[proof['path']] = ('historical/fixes/' + Path(proof['path']).name, proof['sha256'])
    for measured in ui['measurements']:
        for phase in ('before', 'after'):
            proof = measured[phase]['artifact']
            ui_sources[proof['path']] = ('historical/fixes/' + Path(proof['path']).name, proof['sha256'])
    for source, (relative, expected) in sorted(ui_sources.items()):
        assert hashlib.sha256(Path(source).read_bytes()).hexdigest() == expected
        add(source, relative, 'Actual local remote-log fixture profile/guard/cleanup evidence; descriptive shared-host observations, no controlled product speedup or exact historical hosted wall-cause claim')
    attempt['remote_log_correction'] = {'runtime_changed': False, 'historical_wall_seconds': ui['hosted_observation']['wall_seconds'], 'hosted_wall_cause': 'Exact CPU/storage/descheduling contributions unavailable', 'fixture_issue': 'Original test omitted production worker_ui/UIWriter bindings, synchronously saving preferences on each v key.', 'validation': '21whole-file checks passed on3.11 and3.12;10actual50frame repeats and6negative blocking probes per interpreter; FD4->4 and no new live threads.'}
contract['pending_requirements'] = [
 'Final all-green Python3.10-3.14 workflow receipt, complete five raw job logs, raw run/jobs/commit receipts and exact head/synthetic-merge/tree proof.',
 'Final successful Slurm and installer receipts/artifacts for the final candidate, preserving earlier attempt identity separately.',
 'Actual merged PR receipt and origin/main commit/tree equality, followed by explicit parent release; ready remains false until then.'
]
limits = contract['limits']
extra = 'The secondffda87d workflow also failed; its two single-test failures and Python3.12 packaging failure remain historical. Local fixture ambiguity does not prove the original hosted helper failure cause; no production cleanup fix is claimed.'
if extra not in limits:
    limits.append(extra)
TEMPLATE.write_text(json.dumps(contract, indent=2) + '\n')
print(json.dumps({'held_ready': contract['ready'], 'records': len(records),
                  'raw_record_bytes': sum(Path(r['source']).stat().st_size for r in records),
                  'template_sha256': hashlib.sha256(TEMPLATE.read_bytes()).hexdigest(),
                  'git_paths_written': 0}, indent=2))
