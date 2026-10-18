import os
from pathlib import Path
import subprocess

import pytest

from raven.remote import SshBackend
from raven import ssh_control
from raven.slurm import CommandError


def backend(host='example.invalid', **kwargs):
    return SshBackend(host, opts=['-F', 'none', *kwargs.pop('opts', [])], **kwargs)


def test_equivalent_connections_share_private_option_scoped_paths():
    first, second = backend(), backend()
    first.argv(['squeue'])
    second.argv(['sacct'])
    assert first.control_path == second.control_path and first.control_path
    parent = Path(first.control_path).parent
    assert parent.stat().st_mode & 0o777 == 0o700 and parent.stat().st_uid == os.getuid()
    assert not list(parent.glob('config-*'))
    variants = [backend('other.invalid'), backend(user='other'),
                backend(opts=['-p', '2222']), backend(opts=['-J', 'bastion.invalid']),
                backend(opts=['-o', 'StrictHostKeyChecking=yes']),
                backend(opts=['-o', 'IdentityAgent=none'])]
    for other in variants:
        other.argv(['squeue'])
        assert other.control_path != first.control_path


@pytest.mark.parametrize('options', [['-o', 'ControlMaster=no'], ['-S', 'none'], ['-o', 'ControlPath=none']])
def test_explicit_opt_out_remains_effective(options):
    item = backend(opts=options)
    item.argv(['squeue'])
    assert item.control_path == ''


def test_ssh_config_include_and_host_opt_out_and_updates(tmp_path):
    config = tmp_path / 'config'
    included = tmp_path / 'included'
    config.write_text('Include ' + str(included) + '\n')
    included.write_text('Host example.invalid\n ControlMaster no\n')
    item = SshBackend('example.invalid', opts=['-F', str(config)])
    item.argv(['squeue'])
    assert not item.control_path
    included.write_text('Host example.invalid\n ControlMaster auto\n StrictHostKeyChecking yes\n')
    item.argv(['squeue'])
    old = item.control_path
    assert old
    included.write_text('Host example.invalid\n ControlMaster auto\n StrictHostKeyChecking ask\n')
    item.argv(['squeue'])
    assert item.control_path != old


def test_explicit_custom_socket_is_scoped_privately():
    item = backend(opts=['-S', '/tmp/widely-shared'])
    argv = item.argv(['squeue'])
    assert item.control_path and argv[argv.index('-S', argv.index('-S') + 1) + 1] == item.control_path


@pytest.mark.parametrize('method', ['run', 'call'])
def test_lost_ack_is_never_retried(method):
    calls = []
    def runner(argv, timeout):
        calls.append(argv)
        return 255, '', 'connection lost after command may have run'
    item = backend(runner=runner)
    if method == 'run':
        with pytest.raises(CommandError, match='connection lost'):
            item.run(['scancel', '123'])
    else:
        assert item.call(['sbatch', 'job.sh'])[0] is False
    assert len(calls) == 1


def test_unresolvable_scope_disables_reuse_without_changing_host_key_policy(monkeypatch):
    def unavailable(*args):
        raise ssh_control.MultiplexUnavailable('configuration resolution unavailable')
    monkeypatch.setattr(ssh_control, 'prepare', unavailable)
    item = backend()
    argv = item.argv(['squeue'])
    assert 'ControlMaster=no' in argv and 'ControlPath=none' in argv
    assert 'StrictHostKeyChecking=no' not in argv and item.control_diagnostic


def test_unsafe_control_directory_rejected_without_chmod(tmp_path, monkeypatch):
    directory = tmp_path / ('raven-ssh-' + str(os.getuid()))
    directory.mkdir()
    directory.chmod(0o777)
    monkeypatch.setattr(ssh_control.tempfile, 'gettempdir', lambda: str(tmp_path))
    with pytest.raises(ValueError, match='writable|0700'):
        backend().argv(['squeue'])
    assert directory.stat().st_mode & 0o777 == 0o777


def test_remote_pager_keeps_connection_security_and_proxy_options():
    from raven.remote import RemoteFiles
    item = backend(opts=['-p', '2222', '-J', 'bastion.invalid', '-o', 'StrictHostKeyChecking=yes'])
    argv = RemoteFiles(item).less_argv('/project/run log.txt')
    assert argv[:2] == ['ssh', '-t']
    assert '-J' in argv and 'bastion.invalid' in argv and 'StrictHostKeyChecking=yes' in argv
    assert argv[-1] == "less +G -- '/project/run log.txt'"


def test_client_hosts_do_not_share_socket_names_on_shared_tmp(monkeypatch):
    item = backend()
    monkeypatch.setattr(ssh_control.socket, 'gethostname', lambda: 'client-a')
    item.argv(['squeue'])
    first = item.control_path
    monkeypatch.setattr(ssh_control.socket, 'gethostname', lambda: 'client-b')
    item.argv(['squeue'])
    assert item.control_path != first
