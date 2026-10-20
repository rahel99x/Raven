import json
import os
from pathlib import Path

import pytest

from raven import config, config_trust as T, plugins, raven_config, setup_config


@pytest.mark.parametrize('loader', [config.load_file, raven_config.read_file, setup_config._read_snapshot])
@pytest.mark.parametrize('unsafe', ['file', 'parent', 'ancestor', 'link', 'parent_link', 'fifo'])
def test_loaders_reject_untrusted_paths_before_using_them(tmp_path, loader, unsafe):
    parent = tmp_path / 'a' / 'b'
    parent.mkdir(parents=True)
    path = parent / 'config.json'
    path.write_text('{"schema":"raven.config/v1"}')
    if unsafe == 'file':
        path.chmod(0o666)
    elif unsafe == 'parent':
        parent.chmod(0o777)
    elif unsafe == 'ancestor':
        parent.parent.chmod(0o777)
    elif unsafe == 'link':
        actual = path.with_suffix('.actual')
        path.rename(actual)
        path.symlink_to(actual)
    elif unsafe == 'parent_link':
        actual = parent.with_name('actual')
        parent.rename(actual)
        parent.symlink_to(actual, target_is_directory=True)
    else:
        path.unlink()
        os.mkfifo(path)
    with pytest.raises((ValueError, OSError)):
        loader(path if loader != config.load_file else str(path))


@pytest.mark.parametrize('loader', [config.load_file, raven_config.read_file, setup_config._read_snapshot])
def test_public_readable_but_owned_configuration_remains_supported(tmp_path, loader):
    path = tmp_path / 'config.json'
    path.write_text('{"schema":"raven.config/v1"}')
    path.chmod(0o644)
    assert loader(path if loader != config.load_file else str(path))


def test_private_secret_policy_is_separate_and_defaults_are_private(tmp_path):
    path = tmp_path / 'config.json'
    config.Config.write_default(str(path))
    assert path.stat().st_mode & 0o777 == 0o600
    T.read(path, private=True)
    path.chmod(0o644)
    assert T.read(path)
    with pytest.raises(T.TrustError, match='0600'):
        T.read(path, private=True)


def test_root_policy_requires_root_not_current_user(tmp_path):
    if os.getuid() == 0:
        pytest.skip('needs an unprivileged current user')
    path = tmp_path / 'policy.json'
    path.write_text('{}')
    with pytest.raises(T.TrustError):
        raven_config.read_file(path, trusted=True)


def test_plugin_unsafe_permissions_report_path_without_executing(tmp_path):
    path = tmp_path / 'unsafe.py'
    marker = tmp_path / 'executed'
    path.write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\ndef setup(api): pass\n')
    path.chmod(0o666)
    api = plugins.load([str(path)])
    assert not marker.exists() and not api.loaded
    assert str(path) in api.errors['unsafe'] and 'write permission' in api.errors['unsafe']


def test_plugin_executes_pinned_verified_bytes_not_replaced_path(tmp_path, monkeypatch):
    path = tmp_path / 'hello.py'
    path.write_text('def setup(api): api.command("verified", lambda *a: "yes")\n')
    original = T.read
    def replace_after_read(*args, **kwargs):
        result = original(*args, **kwargs)
        path.write_text('raise RuntimeError("unverified replacement executed")\n')
        return result
    monkeypatch.setattr(T, 'read', replace_after_read)
    api = plugins.load([str(path)])
    assert api.loaded == ['hello'] and 'verified' in api.commands and not api.errors


def test_explicit_plugin_directory_alias_pins_the_selected_root_once(tmp_path):
    actual = tmp_path / 'actual'
    actual.mkdir()
    (actual / 'hello.py').write_text('def setup(api): api.command("selected", lambda *a: "yes")\n')
    selected = tmp_path / 'selected'
    selected.symlink_to(actual, target_is_directory=True)
    paths = plugins.discover(str(tmp_path / 'config.json'), [str(selected)])
    assert paths == [str(actual / 'hello.py')]
    alternate = tmp_path / 'alternate'
    alternate.mkdir()
    (alternate / 'hello.py').write_text('raise RuntimeError("retargeted alias executed")\n')
    selected.unlink()
    selected.symlink_to(alternate, target_is_directory=True)
    api = plugins.load(paths)
    assert api.loaded == ['hello'] and 'selected' in api.commands and not api.errors


@pytest.mark.parametrize('unsafe', ['file_link', 'replaced_root', 'writable_root'])
def test_selected_plugin_root_never_licenses_internal_symlinks_or_unsafe_ownership(tmp_path, unsafe):
    actual = tmp_path / 'actual'
    actual.mkdir()
    marker = tmp_path / 'executed'
    plugin = actual / 'hello.py'
    payload = f'from pathlib import Path\nPath({str(marker)!r}).touch()\ndef setup(api): pass\n'
    plugin.write_text(payload)
    selected = tmp_path / 'selected'
    selected.symlink_to(actual, target_is_directory=True)
    paths = plugins.discover(str(tmp_path / 'config.json'), [str(selected)])
    if unsafe == 'file_link':
        outside = tmp_path / 'outside.py'
        outside.write_text(payload)
        plugin.unlink()
        plugin.symlink_to(outside)
    elif unsafe == 'replaced_root':
        actual.rename(tmp_path / 'retained')
        alternate = tmp_path / 'alternate'
        alternate.mkdir()
        (alternate / 'hello.py').write_text(payload)
        actual.symlink_to(alternate, target_is_directory=True)
    else:
        actual.chmod(0o777)
    api = plugins.load(paths)
    assert not marker.exists() and not api.loaded
    assert 'hello' in api.errors


def test_replaced_ancestor_cannot_redirect_openat_read(tmp_path, monkeypatch):
    parent = tmp_path / 'original'
    parent.mkdir()
    (parent / 'config.json').write_text('{"safe":true}')
    alternate = tmp_path / 'alternate'
    alternate.mkdir()
    (alternate / 'config.json').write_text('{"safe":false}')
    actual_open = T.os.open
    def raced_open(path, flags, *args, **kwargs):
        fd = actual_open(path, flags, *args, **kwargs)
        if path == 'original':
            parent.rename(tmp_path / 'pinned')
            parent.symlink_to(alternate, target_is_directory=True)
        return fd
    monkeypatch.setattr(T.os, 'open', raced_open)
    raw, _ = T.read(parent / 'config.json')
    assert json.loads(raw) == {'safe': True}


def test_setup_does_not_save_through_writable_parent(tmp_path):
    directory = tmp_path / 'unsafe'
    directory.mkdir(mode=0o777)
    directory.chmod(0o777)
    path = directory / 'config.json'
    with pytest.raises(T.TrustError, match='writable'):
        setup_config._atomic_write(path, {'schema': raven_config.SCHEMA}, None)
    assert not path.exists()


def test_runtime_mailbox_rejects_private_child_beneath_writable_ancestor(tmp_path):
    from raven import runtime_control
    parent = tmp_path / 'unsafe-parent'
    parent.mkdir(mode=0o777)
    parent.chmod(0o777)
    child = parent / 'private-mailbox'
    child.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='writable'):
        runtime_control._private_directory(child)
    assert parent.stat().st_mode & 0o777 == 0o777
    assert child.stat().st_mode & 0o777 == 0o700


def test_runtime_mailbox_creation_validates_before_writing(tmp_path):
    from raven import runtime_control
    parent = tmp_path / 'unsafe-parent'
    parent.mkdir()
    parent.chmod(0o777)
    config = {'schema': 'raven.runtime-control/v1', 'attempts': ['job1-attempt1'], 'commands': {},
              'io': {'capacity': 1, 'max_lease_seconds': 60}}
    with pytest.raises(ValueError, match='writable'):
        runtime_control.initialize(parent / 'new-mailbox', config)
    assert not (parent / 'new-mailbox').exists()
