"""Shared-cluster installation, source integrity, and explicit trust regressions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from raven import config_trust as T, path_trust_cli, update_source as S, update_verification as V
from raven.install_paths import worker_python
from raven.state_ownership import StateOwner
from test_user_install import I
from test_update_source import checkout, advance, run as source_git


def git(root, *args):
    return subprocess.check_output(['git', '-c', 'user.name=Raven test', '-c', 'user.email=test@example.invalid',
        '-C', str(root), *args], text=True).strip()


def source(root):
    root.mkdir()
    (root / 'raven').mkdir()
    (root / 'raven/__init__.py').write_text('VERSION = "test"\n')
    (root / 'raven/gone.py').write_text('OLD = True\n')
    (root / '.gitignore').write_text('build/\n')
    (root / 'pyproject.toml').write_text('[project]\nname="raven"\nversion="0.0.0"\n')
    git(root, 'init', '-b', 'main')
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'initial')
    return root


@pytest.mark.parametrize('group_name,members,primary,expected', [
    ('student', [], 123, True), ('lab', [], 123, False),
    ('student', ['labmate'], 123, False), ('student', [], 124, False),
])
def test_private_group_is_an_identity_rule_not_a_mode_bit_exception(monkeypatch, group_name, members, primary, expected):
    T._private_group.cache_clear()
    monkeypatch.setattr(T.pwd, 'getpwuid', lambda uid: SimpleNamespace(pw_name='student', pw_gid=primary))
    monkeypatch.setattr(T.grp, 'getgrgid', lambda gid: SimpleNamespace(gr_name=group_name, gr_mem=members))
    assert T._private_group(54321, 123) is expected
    info = SimpleNamespace(st_mode=stat.S_IFREG | 0o664, st_gid=123)
    monkeypatch.setattr(T.os, 'getuid', lambda: 54321)
    assert T.unsafe_write(info) is not expected
    info.st_mode = stat.S_IFREG | 0o666
    assert T.unsafe_write(info)
    T._private_group.cache_clear()


def test_umask_002_config_and_legacy_marker_are_readable_in_private_locations(tmp_path):
    previous = os.umask(0o002)
    try:
        folder = tmp_path / 'private'
        folder.mkdir(mode=0o700)
        config = folder / 'config.json'
        config.write_text('{"account":"lab"}\n')
        marker = folder / I.MARKER
        marker.write_text(json.dumps({'schema':'raven.user-install/v1', 'source':str(tmp_path)}))
    finally:
        os.umask(previous)
    assert json.loads(T.read(config)[0]) == {'account':'lab'}
    assert I.read_marker(folder)['source'] == str(tmp_path)


def test_explicit_shared_grant_is_private_bounded_and_bound_to_the_chosen_inode(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    shared = tmp_path / 'lab'
    shared.mkdir(mode=0o770)
    assert path_trust_cli.main([str(shared), '--yes']) == 0
    registry = T.trust_store()
    assert registry.stat().st_mode & 0o777 == 0o600
    assert registry.parent.stat().st_mode & 0o777 == 0o700
    assert T._trusted_roots() == (shared,)
    retained = tmp_path / 'previous-lab'
    shared.rename(retained)
    shared.mkdir(mode=0o770)
    assert T._trusted_roots() == ()
    assert path_trust_cli.main([str(shared), '--yes']) == 0
    assert T._trusted_roots() == (shared,)
    assert path_trust_cli.main([str(shared), '--remove', '--yes']) == 0
    assert T._trusted_roots() == ()


def test_shared_grant_accepts_a_pi_owner_but_never_world_write():
    info = SimpleNamespace(st_mode=stat.S_IFDIR | 0o770, st_uid=54321, st_gid=123)
    with pytest.raises(T.TrustError, match='owner'):
        T._directory(info, '/project/pi')
    T._directory(info, '/project/pi', explicitly_trusted=True)
    info.st_mode |= 0o002
    with pytest.raises(T.TrustError, match='writable'):
        T._directory(info, '/project/pi', explicitly_trusted=True)


def test_shared_grant_never_licenses_a_symlink_below_the_root(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    shared = tmp_path / 'lab'; shared.mkdir()
    other = tmp_path / 'elsewhere'; other.mkdir()
    (other / 'plugin.py').write_text('not trusted through a link')
    assert path_trust_cli.main([str(shared), '--yes']) == 0
    (shared / 'redirect').symlink_to(other, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        T.read(shared / 'redirect/plugin.py')


def test_selected_xdg_root_is_resolved_once_but_internal_links_are_rejected(tmp_path, monkeypatch):
    actual = tmp_path / 'actual'; (actual / 'raven').mkdir(parents=True)
    selected = tmp_path / 'selected'; selected.symlink_to(actual, target_is_directory=True)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(selected))
    (actual / 'raven/config.json').write_text('{}')
    assert T.read(selected / 'raven/config.json')[0] == b'{}'
    other = tmp_path / 'other'; other.mkdir(); (other / 'config.json').write_text('{}')
    (actual / 'raven/redirect').symlink_to(other, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        T.read(selected / 'raven/redirect/config.json')


@pytest.mark.parametrize('name', ['update', 'update_source', 'update_revert', 'update_verification', 'install_paths', 'release_verification'])
def test_installation_package_preserves_exact_legacy_module_identity(name, monkeypatch):
    import importlib
    legacy = importlib.import_module('raven.' + name)
    canonical = importlib.import_module('raven.installation.' + name)
    assert legacy is canonical
    marker = object()
    monkeypatch.setattr(legacy, '_identity_regression_marker', marker, raising=False)
    assert canonical._identity_regression_marker is marker


def test_pristine_build_does_not_reuse_deleted_modules_or_stale_package_directories(tmp_path, monkeypatch):
    checkout = source(tmp_path / 'checkout')
    (checkout / 'raven/gone.py').unlink()
    (checkout / 'raven/probe.py').write_text('NEW = True\n')
    git(checkout, 'add', '-A'); git(checkout, 'commit', '-m', 'remove obsolete module')
    stale = checkout / 'build/lib/raven'
    stale.mkdir(parents=True)
    (stale / 'gone.py').write_text('OLD = True\n')
    (stale / 'probe').mkdir()
    (stale / 'probe/__init__.py').write_text('STALE_PACKAGE = True\n')
    monkeypatch.setattr(I, 'ROOT', checkout)
    stage = tmp_path / 'release'; stage.mkdir()
    built = I._stage_source(stage, I.source_metadata())
    assert not (built / 'raven/gone.py').exists()
    assert not (built / 'raven/probe').exists()
    assert (built / 'raven/probe.py').read_text() == 'NEW = True\n'
    assert not (built / 'build').exists()
    assert (stale / 'gone.py').is_file()  # Unrelated user build data was not erased.


def test_pristine_export_cannot_silently_drop_a_tracked_package_input(tmp_path, monkeypatch):
    checkout = source(tmp_path / 'checkout')
    (checkout / '.gitattributes').write_text('raven/gone.py export-ignore\n')
    git(checkout, 'add', '.gitattributes'); git(checkout, 'commit', '-m', 'incorrect export exclusion')
    monkeypatch.setattr(I, 'ROOT', checkout)
    stage = tmp_path / 'release'; stage.mkdir()
    with pytest.raises(I.InstallError, match='omits tracked package inputs'):
        I._stage_source(stage, I.source_metadata())


def test_stable_worker_path_survives_release_pruning(tmp_path):
    release = tmp_path / 'release-one'
    (release / 'bin').mkdir(parents=True)
    (tmp_path / 'current').symlink_to(release.name)
    assert worker_python(release / 'bin/python') == str(tmp_path / 'current/bin/python')
    assert worker_python(tmp_path / 'custom/bin/python') == str(tmp_path / 'custom/bin/python')


def test_state_preflight_names_unsafe_path_without_creating_a_store(tmp_path):
    parent = tmp_path / 'unsafe'; parent.mkdir(); parent.chmod(0o777)
    state = parent / 'state'
    report = StateOwner.preflight(state)
    assert report['state'] == 'read-only'
    assert str(parent) in report['message']
    assert not state.exists()
    assert 'chmod go-w -- ' in report['message']


def test_private_group_exception_does_not_relax_mandatory_site_policy(monkeypatch):
    monkeypatch.setattr(T, '_private_group', lambda *args: True)
    info = SimpleNamespace(st_uid=0, st_gid=123, st_mode=stat.S_IFDIR | 0o775)
    with pytest.raises(T.TrustError, match='writable'):
        T._directory(info, '/etc/site', site=True)


def test_ignored_executable_inputs_are_visible_and_overwrite_removes_only_them(checkout):
    seed, root = checkout
    (root / 'raven').mkdir()
    (root / '.git/info/exclude').write_text('raven/helper.py\nignored-data.txt\n')
    (root / 'raven/helper.py').write_text('LOCAL = True\n')
    (root / 'ignored-data.txt').write_text('scientific data\n')
    assert S.inspect_source(root)['ignored_build_inputs'] == ['raven/helper.py']
    advance(seed)
    with pytest.raises(S.SourceUpdateError, match='Local source changes'):
        S.update_source(root)
    S.update_source(root, overwrite=True)
    assert not (root / 'raven/helper.py').exists()
    assert (root / 'ignored-data.txt').read_text() == 'scientific data\n'


def test_failed_build_restores_source_while_preserving_current_config(checkout, monkeypatch):
    from raven import update as U
    seed, root = checkout
    original = source_git(root, 'rev-parse', 'HEAD')
    advance(seed)
    prefix = root.parent / 'installation'
    release = prefix / 'release-original'
    release.mkdir(parents=True)
    (prefix / 'current').symlink_to(release.name)
    commands = root.parent / 'commands'; commands.mkdir()
    installation = U.Installation('managed', root, prefix, commands, release, False, original)
    monkeypatch.setattr(U, 'discover', lambda *args: installation)
    def fail(*args, **kwargs):
        (root / 'settings.json').write_text('{"user":"changed while building"}\n')
        raise U.UpdateError('controlled build failure')
    monkeypatch.setattr(U, 'install_updated', fail)
    assert U.cli(['update', '--allow-fork', '--config', str(root / 'settings.json')]) == 2
    assert source_git(root, 'rev-parse', 'HEAD') == original
    assert json.loads((root / 'settings.json').read_text()) == {'user':'changed while building'}
    assert (installation.prefix / 'current').resolve() == installation.release


def test_retention_prunes_matching_refs_and_preserves_three_complete_backups(checkout, tmp_path):
    from raven import update as U
    _, root = checkout
    control = tmp_path / 'pipx/raven-updates'
    installation = U.Installation('pipx', root, tmp_path / 'pipx/venvs/raven', tmp_path / 'bin', tmp_path / 'pipx/venvs/raven', False)
    backups = [S.create_backup(root, backup_root=control / 'backups') for _ in range(5)]
    U._prune_backups(installation)
    retained = list((control / 'backups').glob('raven-source-*'))
    refs = source_git(root, 'for-each-ref', '--format=%(refname)', 'refs/raven/update-backups/').splitlines()
    assert len(retained) == len(refs) == 3
    assert {path.name for path in retained} == {ref.rsplit('/', 1)[-1] for ref in refs}


def test_abandoned_staging_sweep_requires_private_marker_and_dead_process(tmp_path, monkeypatch):
    import time
    stale, unknown, active = (tmp_path / ('release-' + name) for name in ('stale','unknown','active'))
    for candidate in (stale, unknown, active):
        candidate.mkdir(mode=0o700)
    for candidate, pid in ((stale, 987654), (active, os.getpid())):
        marker = candidate / '.raven-staging.json'
        marker.write_text(json.dumps({'schema':'raven.install-stage/v1', 'path':str(candidate), 'pid':pid, 'created':time.time() - 7200}))
        marker.chmod(0o600)
    real_kill = I.os.kill
    def kill(pid, sig):
        if pid == 987654:
            raise ProcessLookupError()
        return real_kill(pid, sig)
    monkeypatch.setattr(I.os, 'kill', kill)
    I._sweep_staging(tmp_path)
    assert not stale.exists()
    assert unknown.is_dir() and active.is_dir()


def test_attestation_requirement_fails_before_any_git_mutation(tmp_path, monkeypatch):
    info = {'source':str(tmp_path), 'tracking_remote':'origin', 'target_head':'a' * 40}
    commands = []
    monkeypatch.setattr(S, 'git', lambda *args, **kwargs: commands.append(args) or b'https://github.com/rahel99x/Raven.git\n')
    with pytest.raises(S.SourceUpdateError, match='attested-artifact'):
        V.verify(info, require_attestation=True)
    assert [args[1:] for args in commands] == [
        ('config', '--get-all', 'remote.origin.url'),
        ('remote', 'get-url', '--all', 'origin'),
    ]


def test_failed_authorization_keeps_checkout_and_creates_no_backup(checkout):
    seed, root = checkout
    original = source_git(root, 'rev-parse', 'HEAD')
    advance(seed)
    backup_root = root.parent / 'authorization-backups'
    def reject(info):
        raise S.SourceUpdateError('controlled provenance rejection')
    with pytest.raises(S.SourceUpdateError, match='provenance rejection'):
        S.update_source(root, backup_root=backup_root, authorize=reject)
    assert source_git(root, 'rev-parse', 'HEAD') == original
    assert not backup_root.exists()


def test_nonzero_attestation_verifier_fails_closed(tmp_path, monkeypatch):
    info = {'source':str(tmp_path), 'tracking_remote':'origin', 'target_head':'c' * 40}
    monkeypatch.setattr(S, 'git', lambda *args: b'https://github.com/rahel99x/Raven.git\n')
    artifact = tmp_path / 'raven.whl'
    artifact.write_bytes(b'fixture')
    monkeypatch.setattr(V.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=1))
    with pytest.raises(S.SourceUpdateError, match='verification failed'):
        V.verify(info, require_attestation=True, artifact=artifact)


def test_fork_updates_require_explicit_authorization(tmp_path, monkeypatch):
    info = {'source':str(tmp_path), 'tracking_remote':'origin', 'target_head':'b' * 40}
    monkeypatch.setattr(S, 'git', lambda *args: b'https://example.invalid/lab/fork.git\n')
    with pytest.raises(S.SourceUpdateError, match='allow-fork'):
        V.verify(info)
    assert V.verify(info, allow_fork=True)['source_channel'] == 'explicit-fork'


@pytest.mark.parametrize('rewrite', [
    'git@github.com:rahel99x/Raven.git',
    'ssh://git@github.com/rahel99x/Raven.git',
])
def test_official_https_to_ssh_rewrites_remain_official(checkout, rewrite):
    _, root = checkout
    declared = 'https://github.com/rahel99x/Raven.git'
    source_git(root, 'remote', 'set-url', 'origin', declared)
    source_git(root, 'config', 'url.' + rewrite + '.insteadOf', declared)
    info = {'source':str(root), 'tracking_remote':'origin', 'target_head':'b' * 40}
    assert source_git(root, 'remote', 'get-url', '--all', 'origin') == rewrite
    assert V.verify(info)['source_channel'] == 'official-repository'


def test_official_declared_url_cannot_hide_a_rewritten_fork(checkout, tmp_path):
    _, root = checkout
    declared = 'https://github.com/rahel99x/Raven.git'
    rewritten = str(tmp_path / 'different-origin')
    source_git(root, 'remote', 'set-url', 'origin', declared)
    source_git(root, 'config', 'url.' + rewritten + '.insteadOf', declared)
    info = {'source':str(root), 'tracking_remote':'origin', 'target_head':'b' * 40}
    assert source_git(root, 'remote', 'get-url', '--all', 'origin') == rewritten
    with pytest.raises(S.SourceUpdateError, match='effective fetch URLs: custom'):
        V.verify(info)
    assert V.verify(info, allow_fork=True)['source_channel'] == 'explicit-fork'
    artifact = tmp_path / 'raven.whl'; artifact.write_bytes(b'fixture')
    with pytest.raises(S.SourceUpdateError, match='attestation policy pins'):
        V.verify(info, allow_fork=True, require_attestation=True, artifact=artifact)


@pytest.mark.parametrize('declared,effective', [
    ('https://github.com/rahel99x/Raven.git\n',
     'https://github.com/rahel99x/Raven.git\nhttps://unreviewed.invalid/fork.git\n'),
    ('https://github.com/rahel99x/Raven.git\nhttps://unreviewed.invalid/fork.git\n',
     'https://github.com/rahel99x/Raven.git\n'),
    ('https://github.com/rahel99x/Raven.git\n',
     'https://github.com/rahel99x/Raven.git \n'),
])
def test_every_declared_and_effective_url_must_be_official(tmp_path, monkeypatch, declared, effective):
    info = {'source':str(tmp_path), 'tracking_remote':'origin', 'target_head':'b' * 40}
    monkeypatch.setattr(S, 'git', lambda root, command, *args: (declared if command == 'config' else effective).encode())
    with pytest.raises(S.SourceUpdateError, match='allow-fork'):
        V.verify(info)
    assert V.verify(info, allow_fork=True)['source_channel'] == 'explicit-fork'


def test_multiple_equivalent_official_fetch_urls_are_supported(tmp_path, monkeypatch):
    info = {'source':str(tmp_path), 'tracking_remote':'origin', 'target_head':'b' * 40}
    urls = b'https://github.com/rahel99x/Raven.git\ngit@github.com:rahel99x/Raven.git\n'
    monkeypatch.setattr(S, 'git', lambda *args: urls)
    assert V.verify(info)['source_channel'] == 'official-repository'


def test_source_channel_refusal_never_discloses_endpoint_credentials(tmp_path, monkeypatch):
    info = {'source':str(tmp_path), 'tracking_remote':'origin', 'target_head':'b' * 40}
    secret_url = b'https://credential-user:do-not-print@example.invalid/private.git\n'
    monkeypatch.setattr(S, 'git', lambda *args: secret_url)
    with pytest.raises(S.SourceUpdateError) as error:
        V.verify(info)
    assert 'do-not-print' not in str(error.value)
    assert 'credential-user' not in str(error.value)
    assert 'example.invalid' not in str(error.value)
    assert 'declared: custom; effective fetch URLs: custom' in str(error.value)


def test_attestation_verifies_the_selected_commit_and_expected_workflow(tmp_path, monkeypatch):
    info = {'source':str(tmp_path), 'tracking_remote':'origin', 'target_head':'c' * 40}
    monkeypatch.setattr(S, 'git', lambda *args: b'https://github.com/rahel99x/Raven.git\n')
    artifact = tmp_path / 'raven.whl'; artifact.write_bytes(b'controlled fixture, not a real signed artifact')
    commands = []
    def verify(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(V.subprocess, 'run', verify)
    result = V.verify(info, require_attestation=True, artifact=artifact)
    assert result['artifact_attestation'] == 'verified'
    command, = commands
    assert command[:3] == ['gh', 'attestation', 'verify']
    assert command[command.index('--source-digest') + 1] == 'c' * 40
    assert command[command.index('--signer-digest') + 1] == 'c' * 40
    assert command[command.index('--signer-workflow') + 1] == 'rahel99x/Raven/.github/workflows/release-candidate.yml'
    assert '--deny-self-hosted-runners' in command


def test_attestation_plan_makes_no_network_request(tmp_path, monkeypatch):
    info = {'source':str(tmp_path), 'tracking_remote':'origin', 'target_head':'d' * 40}
    monkeypatch.setattr(S, 'git', lambda *args: b'https://github.com/rahel99x/Raven.git\n')
    artifact = tmp_path / 'raven.whl'; artifact.write_bytes(b'fixture')
    monkeypatch.setattr(V.subprocess, 'run', lambda *args, **kwargs: pytest.fail('plan contacted attestation service'))
    assert V.verify(info, require_attestation=True, artifact=artifact, plan=True)['artifact_attestation'] == 'required-at-apply'


def test_isolated_bootstrap_does_not_import_python_from_the_invocation_directory(tmp_path):
    checkout = Path(__file__).resolve().parents[1]
    marker = tmp_path / 'shadow-executed'
    (tmp_path / 'random.py').write_text('from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\n')
    result = subprocess.run(['bash', str(checkout / 'install.sh'), '--python', sys.executable, '--plan',
        '--prefix', str(tmp_path / 'releases'), '--bin-dir', str(tmp_path / 'bin')], cwd=tmp_path,
        text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert not marker.exists() and not (tmp_path / 'releases').exists()
