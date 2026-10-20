"""Behavioral planning tests: no scheduler or state writes are needed."""
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

from raven import launch as L, launch_features as F, launch_settings as S


@pytest.fixture
def script(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'configuration'))
    monkeypatch.delenv('RAVEN_CONFIG', raising=False)
    path = tmp_path / 'test.sh'
    path.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n')
    return path


def plan(script, *args, env=None):
    return L.resolve_plan([str(script), *args], environ={} if env is None else env)


@pytest.mark.parametrize('flag', ['--research', '--r', '--re'])
def test_every_explicit_research_alias_and_deduplication(script, flag):
    names = [alias for entry in F.FEATURES for alias in (entry['name'], *entry['aliases'])]
    result = plan(script, flag, *names)
    assert result['features'] == list(F.FEATURE_REGISTRY)
    assert len(result['features']) == 13
    assert result['gpu'] is False and result['trace'] is False


def test_empty_research_is_basic_not_expensive(script):
    result = plan(script, '--research')
    assert result['features'] == ['experiment', 'evidence']
    assert result['gpu'] is False
    assert result['trace'] is False
    assert 'forecast' not in result['features']


def test_all_requests_integrations_without_operations_or_collectors(script):
    result = plan(script, '--r', 'all')
    assert len(result['features']) == 13
    statuses = {item['feature']: item for item in result['readiness']}
    assert statuses['operations']['status'] == 'needs-input'
    assert statuses['forecast']['status'] == 'needs-input'
    assert 'cannot be backfilled' in statuses['forecast']['reason']
    assert result['gpu'] is False and result['trace'] is False


def test_repeated_research_is_stably_deduplicated(script):
    assert plan(script, '--r', 'sc', 'f', '--re', 'scaling', 're')['features'] == ['scaling', 'forecast', 'resources']


def test_unknown_research_suggests_known_name_without_writing(script):
    before = sorted(script.parent.rglob('*'))
    with pytest.raises(ValueError, match="Did you mean 'scaling'"):
        plan(script, '--r', 'scalig')
    assert sorted(script.parent.rglob('*')) == before


@pytest.mark.parametrize('option', ['--rese', '--trac', '--proj', '--sbat'])
def test_no_option_abbreviation(script, option):
    with pytest.raises(SystemExit) as exc:
        L.parse([str(script), option])
    assert exc.value.code == 2


def test_every_argument_after_boundary_preserved_exactly(script):
    arguments = ['', 'hello world', '--gpu', '--', '$(touch BAD)', '*', 'β_日本', 'quote"\'']
    result = plan(script, '--sbatch=--mem=8G', '--r', 'ex', '--', *arguments)
    assert result['script_args'] == arguments
    assert result['sbatch_options'][0] == '--mem=8G'
    assert not (script.parent / 'BAD').exists()


@pytest.mark.parametrize('argv, expected', [
    (['--host', 'launch', '--once'], None),
    (['--metrics-file', 'data.sh', '--once'], None),
    (['--host=launch', 'launch', 'test.sh'], (1, 'launch')),
    (['--run', 'launch', '--fake'], None),
    (['--research-view', 'experiment', 'test.sh'], (2, 'script')),
    (['--config', 'launch', 'test.sh'], (2, 'script')),
    (['run', 'test.sh'], (0, 'run')),
    (['report', 'test.sh'], (0, 'report')),
    (['trace', 'test.sh'], (0, 'trace')),
    (['--', './test.sh'], (1, 'script')),
    (['--report', 'test.sh'], None),
])
def test_command_boundaries(argv, expected):
    assert L.command_boundary(argv) == expected


def test_command_name_file_needs_explicit_launch(script):
    path = script.with_name('run')
    path.write_bytes(script.read_bytes())
    assert L.command_boundary(['run']) == (0, 'run')
    assert L.command_boundary(['./run']) == (0, 'script')
    assert plan(path)['script'] == str(path)


@pytest.mark.parametrize('entry', ['light_main', 'heavy_main'])
@pytest.mark.parametrize('command', ['launch', 'script'])
def test_bootstrap_layer_preview_never_allocates_or_creates_state(script, monkeypatch, capsys, entry, command):
    from raven import raven_launch as bootstrap, raven_config
    monkeypatch.setattr(raven_config, 'SYSTEM_POLICIES', ())
    monkeypatch.setattr(bootstrap, '_allocation', Mock(side_effect=AssertionError('no allocation')))
    arguments = ([command] if command == 'launch' else []) + [str(script), '--r', 'sc', 'f', '--check', '--json']
    before = sorted(script.parent.rglob('*'))
    assert getattr(bootstrap, entry)(arguments) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['features'] == ['scaling', 'forecast']
    assert result['mutations'] == 0
    assert sorted(script.parent.rglob('*')) == before


def test_core_cli_layer_uses_same_shorthand(script, monkeypatch, capsys):
    from raven import cli, raven_config
    monkeypatch.setattr(raven_config, 'SYSTEM_POLICIES', ())
    assert cli.main([str(script), '--re', 're', '--check', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['features'] == ['resources']


def test_exact_directives_stop_at_first_execution_and_preserve_bytes(script):
    content = '#!/bin/bash -eu\n# preamble\n\n#SBATCH --job-name="my run" --account=LabX\n#SBATCH --output=logs/%j.out\necho start\ncat <<EOF\n#SBATCH --account=WRONG\nEOF\n'
    script.write_text(content)
    result = plan(script)
    assert result['name'] == 'my run'
    assert result['account'] == 'LabX'
    assert result['sbatch_header'] == ['#SBATCH --job-name="my run" --account=LabX', '#SBATCH --output=logs/%j.out']
    assert result['interpreter'] == ['/bin/bash', '-eu']
    assert result['script_sha256'] == hashlib.sha256(content.encode()).hexdigest()


def test_scheduler_precedence_does_not_promote_defaults_over_script(script):
    script.write_text('#!/bin/sh\n#SBATCH --account=Script --mem=8G -J original\ntrue\n')
    S.save_settings(script.parent, {'launch': {'sbatch_defaults': {'account': 'Default', 'mem': '2G', 'qos': 'normal'}}})
    result = plan(script, '--account', 'CLI', '--sbatch=--mem=32G', env={'SBATCH_ACCOUNT': 'Environment', 'SBATCH_MEM_PER_NODE': '16G'})
    assert result['account'] == 'CLI'
    assert result['effective_options']['mem'] == '32G'
    assert result['option_sources']['account'] == 'command line'
    assert result['effective_options']['qos'] == 'normal'
    assert '--account=Default' not in result['sbatch_options']
    inherited = plan(script)
    assert inherited['effective_options']['mem'] == '8G'
    assert inherited['sbatch_options'] == ['--qos=normal']


def test_defaults_never_override_environment(script):
    S.save_settings(script.parent, {'launch': {'sbatch_defaults': {'account': 'Default', 'partition': 'slow'}}})
    result = plan(script, env={'SBATCH_ACCOUNT': 'Env', 'SBATCH_PARTITION': 'fast'})
    assert result['account'] == 'Env'
    assert result['effective_options']['partition'] == 'fast'
    assert not any(x.startswith('--partition=') for x in result['sbatch_options'])


def test_root_and_original_workdir_remain_separate(script):
    root = script.parent
    (root / '.git').mkdir()
    (root / '.git/HEAD').write_text('ref: refs/heads/main\n')
    nested = root / 'jobs'
    nested.mkdir()
    payload = nested / 'batch.sh'
    payload.write_text('#!/bin/sh\n#SBATCH --chdir=../scratch --output=out/%j\ntrue\n')
    result = L.resolve_plan(['jobs/batch.sh'], cwd=root, environ={})
    assert result['project_root'] == str(root)
    assert result['project_source'] == 'Git worktree root'
    assert result['submit_dir'] == str(root)
    assert result['workdir'] == str(root.parent / 'scratch')
    assert result['effective_options']['output'] == 'out/%j'
    assert '--chdir=' + str(root) not in result['sbatch_options']


def test_project_precedence_and_non_git_fallback(script):
    assert plan(script)['project_source'].startswith('script directory')
    root = script.parent
    (root / '.git').write_text('gitdir: /some/worktree\n')
    assert plan(script)['project_source'] == 'Git worktree root'
    (root / '.raven').mkdir()
    (root / '.raven/project.json').write_text('{"schema":"raven.project/v1"}')
    assert plan(script)['project_source'] == 'nearest Raven project'
    assert plan(script, '--project', str(root))['project_source'] == 'explicit --project'


def test_explicit_inconsistent_project_refused(script, tmp_path):
    other = tmp_path / 'other'
    other.mkdir()
    with pytest.raises(ValueError, match='inside'):
        plan(script, '--project', str(other))


def test_existing_association_wins_but_ambiguity_is_an_error(script):
    root = script.parent
    matching = {'script': str(script), 'project_root': str(root)}
    assert L.resolve_plan([str(script)], environ={}, associations=[matching])['project_source'] == 'existing Raven association'
    with pytest.raises(ValueError, match='ambiguous'):
        L.resolve_plan([str(script)], environ={}, associations=[matching, dict(matching, project_root=str(root.parent))])


@pytest.mark.parametrize('content', ['print(1)\n', '#!/usr/bin/python3\nprint(1)', '#!/bin/zsh\ntrue', '#!/usr/bin/env -S bash -eu\ntrue', '#!/bin/bash --login\ntrue', '#!/bin/bash -euo pipefail\ntrue'])
def test_unsupported_script_contract_fails_without_writes(script, content):
    script.write_text(content)
    before = sorted(script.parent.rglob('*'))
    with pytest.raises(ValueError):
        plan(script)
    assert sorted(script.parent.rglob('*')) == before


@pytest.mark.parametrize('policy', ['ALL', 'NONE', 'NIL', 'PATH,HOME', 'ALL,MYVAR=value'])
def test_export_policy_is_preserved_without_weakening(script, policy):
    result = plan(script, '--sbatch=--export=' + policy)
    assert result['export_policy'] == policy
    assert '--export=ALL' not in result['sbatch_options'] if policy != 'ALL' else True


@pytest.mark.parametrize('option', ['--wrap=true', '--wait', '--test-only', '--clusters=elsewhere', '--export-file=x', '--het-group=0', '--wr=true', 'other.sh', ':', '--account'])
def test_conflicting_submission_modes_fail_without_scheduler_call(script, option):
    with pytest.raises(ValueError):
        plan(script, '--sbatch=' + option)


def test_conflicting_choices_and_local_submission_flags(script):
    for args in [('--gpu', '--no-gpu'), ('--trace', '--no-trace')]:
        with pytest.raises(SystemExit):
            plan(script, *args)
    with pytest.raises(ValueError, match='conflicts'):
        plan(script, '--account', 'A', '--sbatch=--account=B')
    with pytest.raises(ValueError, match='cannot accept'):
        plan(script, '--local', '--sbatch=--mem=1G')


def test_no_slurm_never_executes_locally_or_materializes(script, monkeypatch, capsys):
    monkeypatch.setattr(L.shutil, 'which', lambda _: None)
    before = sorted(script.parent.rglob('*'))
    assert L.main([str(script)]) == 2
    assert '--local' in capsys.readouterr().err
    assert sorted(script.parent.rglob('*')) == before


def test_remote_refusal_retains_host_and_original_command(script):
    with pytest.raises(ValueError) as captured:
        L.resolve_plan([str(script), '--gpu', '--', 'two words'], environ={}, connection={'transport': 'ssh', 'host': 'hpc.example'})
    text = str(captured.value)
    assert 'hpc.example' in text and '--gpu' in text and "'two words'" in text


def test_declared_sources_context_and_artifacts_require_no_manifest(script):
    result = plan(script, '--research', '--metrics-file', 'measurements/native.jsonl', '--artifact', 'outputs/result.json', '--research-source', 'sc=reports/scaling.json', '--parameter', 'seed=7', '--parameter', 'label=measurement', '--workers', '4', '--problem-size', '100', '--repeat', '1')
    assert result['settings']['parameters'] == {'seed': 7, 'label': 'measurement'}
    assert result['settings']['scientific_context'] == {'workers': 4, 'problem_size': 100., 'repeat': 1}
    assert result['settings']['research_sources'] == {'scaling': 'reports/scaling.json'}
    assert result['features'] == ['experiment', 'evidence', 'scaling']
    assert not (script.parent / '.raven').exists()


def test_symlink_script_and_declared_source_refused(script):
    link = script.with_name('linked.sh')
    link.symlink_to(script)
    with pytest.raises((OSError, ValueError)):
        plan(link)
    outside = script.parent / 'linked'
    outside.symlink_to('/tmp', target_is_directory=True)
    with pytest.raises(ValueError, match='symlinks'):
        plan(script, '--metrics-file', 'linked/data.jsonl')


def test_read_only_preview_does_not_require_existing_future_workdir(script):
    result = plan(script, '--sbatch=--chdir=/future/compute/path', '--check')
    assert result['workdir'] == '/future/compute/path'
    assert result['mutations'] == 0


def test_local_json_requires_separate_receipt_before_any_writes(script, capsys):
    before = sorted(script.parent.rglob('*'))
    assert L.main([str(script), '--local', '--json']) == 2
    assert '--receipt-file' in capsys.readouterr().err
    assert sorted(script.parent.rglob('*')) == before


def test_receipt_check_is_read_only_and_existing_path_rejected(script):
    target = script.parent / 'receipt.json'
    result = plan(script, '--local', '--json', '--receipt-file', str(target), '--check')
    assert result['receipt_file'] == str(target)
    assert not target.exists()
    target.write_text('preserve')
    with pytest.raises(ValueError, match='already exists'):
        plan(script, '--receipt-file', str(target))
    assert target.read_text() == 'preserve'


def test_receipt_publication_is_exclusive_and_private(script):
    target = script.parent / 'receipt.json'
    L._write_receipt(target, {'job_id': '12'})
    with pytest.raises(FileExistsError):
        L._write_receipt(target, {'job_id': '99'})
    assert json.loads(target.read_text())['job_id'] == '12'
    assert target.stat().st_mode & 0o777 == 0o600
    assert not list(script.parent.glob('.raven-receipt-*'))


def test_export_saved_remote_connection_cannot_fall_back_to_same_local_path(script, monkeypatch):
    from raven import data_export_cli
    submit = Mock(return_value=0)
    monkeypatch.setattr(data_export_cli, 'main', submit)
    remote = {'connection': {'transport': 'ssh', 'host': 'cluster.example'}}
    argv = ['export-data', '--project', str(script.parent), '--run', 'one', '--check']
    with pytest.raises(ValueError, match='remote host cluster.example'):
        L.dispatch(argv, resolved=remote)
    submit.assert_not_called()
    assert L.dispatch([*argv, '--host', 'local'], resolved=remote) == 0
    assert submit.call_args.args[0][-2:] == ['--host', 'local']


def test_global_version_never_submits_workload(script, capsys):
    assert L.dispatch(['--version', 'launch', str(script)]) == 0
    assert capsys.readouterr().out.startswith('Raven ')
    assert not (script.parent / '.raven').exists()


def test_invalid_array_manifest_fails_check_without_creating_launch_state(script):
    S.save_settings(script.parent, {'launch': {'array_manifest': 'array.json'}})
    declaration = script.parent / 'array.json'
    declaration.write_text('{"schema":"wrong"}')
    before = sorted(script.parent.rglob('*'))
    with pytest.raises(ValueError):
        plan(script, '--sbatch=--array=2,8', '--check')
    assert sorted(script.parent.rglob('*')) == before
    with pytest.raises(ValueError, match='requires a Slurm --array'):
        plan(script, '--check')


def test_scientific_readiness_separates_disabled_or_undeclared_sources(script):
    S.save_settings(script.parent, {'launch': {'cpu_memory': False}})
    result = plan(script, '--local', '--r', 'workflow', 'submit', 'scaling')
    rows = {row['feature']: row for row in result['readiness']}
    assert 'cpu-memory' not in rows
    assert rows['workflow']['status'] == 'needs-input'
    assert rows['submit']['status'] == 'unsupported'
    assert rows['scaling']['status'] == 'needs-input'


def test_builtin_completion_preset_means_only_available_evidence(script):
    assert plan(script, '--export-on-finish')['auto_export']['include'] == ['all']


def test_tilde_paths_resolve_before_project_selection(script):
    result = plan(Path('~/test.sh'), '--project', '~')
    assert result['script'] == str(script)
    assert result['project_root'] == str(script.parent)


def test_scheduler_budget_fails_without_materialization(script):
    with pytest.raises(ValueError, match='256 tokens'):
        plan(script, *(['--sbatch=--hold'] * 257))
    assert not (script.parent / '.raven').exists()


@pytest.mark.parametrize('application_status', [0, 7])
@pytest.mark.parametrize('entry', ['module', 'cli', 'heavy'])
def test_local_machine_receipt_preserves_application_bytes_and_status(script, application_status, entry):
    import subprocess
    script.write_text('#!/bin/sh\nprintf "application output\\n"\nprintf "application diagnostic\\n" >&2\nexit ' + str(application_status) + '\n')
    target = script.parent / 'receipt.json'
    command = [sys.executable, '-m', 'raven'] if entry == 'module' else [sys.executable, '-c',
        'import sys; from raven.' + ('cli import main' if entry == 'cli' else 'raven_launch import heavy_main as main') + '; sys.exit(main(sys.argv[1:]))']
    result = subprocess.run([*command, 'launch', str(script), '--project', str(script.parent), '--local', '--json', '--receipt-file', str(target)], cwd=Path(__file__).parents[1], capture_output=True, text=True, timeout=20)
    assert result.returncode == application_status, result.stderr
    assert result.stdout == 'application output\n'
    assert json.loads(target.read_text())['exit_code'] == application_status
    assert result.stderr.startswith('application diagnostic\n') and 'Raven receipt:' in result.stderr


def test_receipt_publication_failure_after_local_execution_keeps_workload_contract(script, monkeypatch, capsys):
    from raven import cli, launch, launch_runtime
    def execute(plan):
        print('application output')
        print('application diagnostic', file=sys.stderr)
        return {'execution': 'local', 'exit_code': 7}
    def fail(*args):
        raise OSError('disk unavailable')
    monkeypatch.setattr(launch_runtime, 'execute_plan', execute)
    monkeypatch.setattr(launch, '_write_receipt', fail)
    code = cli.main(['launch', str(script), '--local', '--json', '--receipt-file', str(script.parent / 'receipt.json')])
    output = capsys.readouterr()
    assert code == 7 and output.out == 'application output\n'
    assert output.err.startswith('application diagnostic\n')
    assert 'workload already executed' in output.err and 'do not resubmit' in output.err


@pytest.mark.parametrize('arguments,status', [([], 'needs-input'), (['--sbatch=--array=2,8'], 'will-verify'), (['--local'], 'unsupported')])
def test_arrays_readiness_uses_actual_submission_options(script, arguments, status):
    resolved = plan(script, '--research', 'arrays', *arguments)
    row = next(item for item in resolved['readiness'] if item['feature'] == 'arrays')
    assert row['status'] == status
    if status == 'needs-input':
        assert '--sbatch=--array=' in row['reason'] and 'manifest' in row['reason']
    elif status == 'will-verify':
        assert 'manifest' in row['reason']
