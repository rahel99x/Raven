#!/usr/bin/env python3
"""Build checksum-pinned legacy Bash and execute Raven's bootstrap regressions.

Linux/GCC qualification only. Downloads are bounded and the built interpreters
are used only by tests. Existing shells and installations are never replaced.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import urllib.request

from qualify_updates import supervise

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    '3.2': '26c99025b59e30779300b68adb764f824974d267a4d7cc1b347d14a2393f9fb4',
    '4.2': 'a27a1179ec9c0830c65c6aa5d7dab60f7ce1a2a608618570f96bfa72e95ab3d8',
}


def extract(archive, destination):
    with tarfile.open(archive) as source:
        members = source.getmembers()
        if len(members) > 10000 or sum(member.size for member in members) > 128 << 20:
            raise ValueError('Bash source exceeds its extraction bound')
        consumed = 0
        for member in members:
            relative = Path(member.name)
            if relative.is_absolute() or '..' in relative.parts or not (member.isdir() or member.isfile() or member.issym()):
                raise ValueError('Unsafe Bash source member')
            payload = member
            if member.issym():
                linked = relative.parent / member.linkname
                if linked.is_absolute() or '..' in linked.parts:
                    raise ValueError('Unsafe Bash source link')
                payload = source.getmember(str(linked))
                if not payload.isfile():
                    raise ValueError('Bash source links must target regular archive files')
            consumed += payload.size
            if consumed > 128 << 20:
                raise ValueError('Materialized Bash source exceeds its byte bound')
            target = destination / relative
            if member.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
            else:
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                # Materialize GNU's internal ChangeLog link as ordinary bytes;
                # no filesystem symlink is created or traversed during extraction.
                with source.extractfile(payload) as incoming, target.open('xb') as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
                target.chmod(0o700 if payload.mode & 0o111 else 0o600)
                os.utime(target, (member.mtime, member.mtime))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', choices=tuple(SOURCES), required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--archive', type=Path, help='Use a previously downloaded, checksum-verified GNU archive')
    args = parser.parse_args(argv)
    args.output = args.output.absolute()
    if not sys.platform.startswith('linux') or not all(shutil.which(tool) for tool in ('gcc', 'make')):
        parser.error('this qualification needs Linux, GCC and make')
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = {'schema':'raven.bash-qualification/v1', 'version':args.version, 'passed':False,
              'sha256':SOURCES[args.version], 'commands':[],
              'limits':'Legacy Bash on Linux/GCC; not macOS or RHEL kernel qualification.'}
    try:
        archive = args.archive
        if archive is None:
            archive = args.output / ('bash-' + args.version + '.tar.gz')
            with urllib.request.urlopen('https://ftp.gnu.org/gnu/bash/' + archive.name, timeout=60) as incoming:
                data = incoming.read((16 << 20) + 1)
            if len(data) > 16 << 20:
                raise ValueError('Bash download exceeds its byte bound')
            archive.write_bytes(data)
        if archive.stat().st_size > 16 << 20 or hashlib.sha256(archive.read_bytes()).hexdigest() != SOURCES[args.version]:
            raise ValueError('Bash source checksum differs from the pinned GNU archive')
        extract(archive, args.output)
        source = args.output / ('bash-' + args.version)
        # GNU's shipped parser avoids a dependency on an obsolete yacc grammar.
        for name in ('y.tab.c', 'y.tab.h'):
            (source / name).touch()
        env = dict(os.environ, CC='gcc -std=gnu89 -Wno-error=implicit-function-declaration', ac_cv_func_working_mktime='yes')
        commands = [
            ('configure', ['./configure', '--without-bash-malloc', '--disable-nls'], source),
            ('build', ['make', '-j2'], source),
            ('tests', [sys.executable, '-m', 'pytest', 'tests/test_install_bootstrap.py', '-q'], ROOT),
        ]
        for label, command, cwd in commands:
            if label == 'tests':
                env['RAVEN_TEST_BASH'] = str(source / 'bash')
            result = supervise(command, args.output / (label + '.log'), cwd=cwd, env=env, timeout=600)
            report['commands'].append({'label':label, 'command':command, **result})
            if result.get('exit') != 0 or result.get('failure'):
                raise RuntimeError(label + ' failed; inspect its qualification log')
        report['interpreter'] = subprocess.check_output([source / 'bash', '--version'], text=True).splitlines()[0]
        report['passed'] = True
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        report['failure'] = str(exc)
        print('Bash qualification failed: ' + str(exc), file=sys.stderr)
        return 2
    finally:
        (args.output / 'result.json').write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
