"""Fixed stdlib SSH reader transport; independent of readers and UI imports."""

from __future__ import annotations

import hashlib
import json
import re
import shlex

CHUNK_BYTES = 24576
MAX_PACKED = 512 * 1024
OPERATIONS = frozenset(
    {
        "discover_project",
        "discover_job",
        "select_run",
        "read_bound_json",
        "read_bound_passport",
        "read_bound_artifacts",
        "read_bound_tail",
        "read_bound_metric_chunk",
        "read_bound_telemetry",
        "read_passport",
        "artifact_tree",
        "preview_artifact",
        "read_artifact_page",
    }
)

# Only these exact source bytes may pass the governed adapter. Uploaded source
# is data here: execution follows the digest-checked reader's fixed operation
# allowlist, never a project-supplied module or shell program.
CACHED_BOOTSTRAP = r"""
import base64, hashlib, importlib.abc, importlib.util, json, lzma, marshal, os, stat, sys, tempfile
from pathlib import Path
ACTION, DIGEST, ROOT, PAYLOAD, COUNT = sys.argv[1:6]
if ACTION not in ('read', 'upload') or len(DIGEST) != 64 or any(c not in '0123456789abcdef' for c in DIGEST):
    raise ValueError('Invalid reader transport')
COUNT = int(COUNT)
if not 1 <= COUNT <= 32:
    raise ValueError('Invalid reader chunk count')

def private(path):
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Reader cache must be private and owned by this user')
    return path

if ROOT:
    root = Path(ROOT)
    if not root.is_absolute() or len(ROOT) > 4096:
        raise ValueError('Invalid reader cache root')
    # Resolve only the nominated temporary parent; leaf symlinks are rejected.
    parent = Path(tempfile.gettempdir()).resolve()
    if root.parent != parent or not root.name.startswith('raven-readers-'):
        raise ValueError('Reader cache escaped its temporary parent')
    private(root)
    info = parent.stat()
    if info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
        raise ValueError('Reader cache parent permits replacement by another user')
else:
    parent = Path(tempfile.gettempdir()).resolve()
    info = parent.stat()
    if info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
        raise ValueError('Reader cache parent permits replacement by another user')
    root = parent / ('raven-readers-' + str(os.getuid()))
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        pass
    try:
        private(root)
    except (OSError, ValueError):
        root = Path(tempfile.mkdtemp(prefix='raven-readers-', dir=parent))
        private(root)

# Keep old installations and interrupted uploads from growing the cache
# indefinitely. Current-version entries are never evicted during a read.
entries = []
with os.scandir(root) as scan:
    for entry in scan:
        if len(entries) >= 512:
            raise ValueError('Reader cache exceeds its entry inspection bound')
        if entry.is_file(follow_symlinks=False):
            info = entry.stat(follow_symlinks=False)
            entries.append((info.st_mtime_ns, info.st_size, entry.name))
size = sum(item[1] for item in entries)
count = len(entries)
for _, length, name in sorted(entries):
    if size <= 64 * 1024 * 1024 and count <= 128:
        break
    if name.startswith(DIGEST + '.'):
        continue
    try:
        os.unlink(root / name)
    except FileNotFoundError:
        pass
    size -= length
    count -= 1
if size > 72 * 1024 * 1024:
    raise ValueError('Reader cache exceeds its storage bound')

flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
def read(name, maximum):
    fd = os.open(root / name, flags)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > maximum:
            raise ValueError('Unsafe or oversized reader cache entry')
        data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError('Oversized reader cache entry')
        return data

def publish(name, data):
    fd, path = tempfile.mkstemp(prefix='.reader-', dir=root)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.replace(path, root / name)
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

if ACTION == 'upload':
    index, chunk = PAYLOAD.split(':', 1)
    index = int(index)
    if not 0 <= index < COUNT or len(chunk) > 24576 or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=' for c in chunk):
        raise ValueError('Invalid reader chunk')
    publish(DIGEST + '.' + str(COUNT) + '.' + str(index) + '.part', chunk.encode('ascii'))
    print(json.dumps({'uploaded': len(chunk)}))
    raise SystemExit

try:
    packed = read(DIGEST + '.xz64', 512 * 1024)
except FileNotFoundError:
    packed = None
if packed is not None and hashlib.sha256(packed).hexdigest() != DIGEST:
    os.unlink(root / (DIGEST + '.xz64'))
    try:
        os.unlink(root / (DIGEST + '.' + sys.implementation.cache_tag + '.code'))
    except FileNotFoundError:
        pass
    packed = None
if packed is None:
    try:
        packed = b''.join(read(DIGEST + '.' + str(COUNT) + '.' + str(i) + '.part', 24576) for i in range(COUNT))
    except FileNotFoundError:
        print(json.dumps({'raven_cache_missing': str(root)}))
        raise SystemExit
    if len(packed) > 512 * 1024 or hashlib.sha256(packed).hexdigest() != DIGEST:
        raise ValueError('Reader bundle digest mismatch')
    publish(DIGEST + '.xz64', packed)
    for i in range(COUNT):
        try:
            os.unlink(root / (DIGEST + '.' + str(COUNT) + '.' + str(i) + '.part'))
        except FileNotFoundError:
            pass
if hashlib.sha256(packed).hexdigest() != DIGEST:
    raise ValueError('Reader bundle digest mismatch')
code_name = DIGEST + '.' + sys.implementation.cache_tag + '.code'
try:
    codes = marshal.loads(read(code_name, 8 * 1024 * 1024))
except (FileNotFoundError, EOFError, ValueError, TypeError):
    decoder = lzma.LZMADecompressor(memlimit=32 * 1024 * 1024)
    raw = decoder.decompress(base64.b64decode(packed, validate=True), max_length=2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024 or not decoder.eof or decoder.unused_data:
        raise ValueError('Invalid or oversized reader bundle')
    sources = json.loads(raw)
    codes = {name: compile(source, '<raven-project-reader>', 'exec') for name, source in sources.items()}
    publish(code_name, marshal.dumps(codes))
class ReaderLoader(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in codes:
            return importlib.util.spec_from_loader(fullname, self, is_package=any(name.startswith(fullname + '.') for name in codes))
    def create_module(self, spec):
        return None
    def exec_module(self, module):
        exec(codes[module.__name__], module.__dict__)
sys.meta_path.insert(0, ReaderLoader())
from raven import projects
try:
    request = json.loads(PAYLOAD)
    allowed = {'discover_project', 'discover_job', 'select_run', 'read_bound_json', 'read_bound_passport',
               'read_bound_artifacts', 'read_bound_tail', 'read_bound_metric_chunk', 'read_bound_telemetry',
               'read_passport', 'artifact_tree', 'preview_artifact', 'read_artifact_page'}
    if request['operation'] not in allowed:
        raise ValueError('unsupported project reader operation')
    value = getattr(projects, request['operation'])(*request['args'], **request['kwargs'])
    selected = value.get('selected') if request['operation'] == 'discover_job' else value if request['operation'] == 'select_run' else None
    if selected:
        selected.pop('array_manifest_record', None)
    if request['operation'] == 'read_bound_tail':
        value = [base64.b64encode(value[0]).decode('ascii'), value[1], value[2]]
    response = {'value': value}
except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
    response = {'error': str(exc)[:2048], 'missing': isinstance(exc, FileNotFoundError)}
print(json.dumps(response, ensure_ascii=True, allow_nan=False, separators=(',', ':')))
"""


def command(action, digest, root, payload, count):
    argv = [
        "python3",
        "-I",
        "-S",
        "-c",
        CACHED_BOOTSTRAP,
        action,
        digest,
        root,
        payload,
        str(count),
    ]
    classify(argv)
    return argv


def classify(argv):
    """Validate the exact fixed helper and return detached admission metadata."""
    if (
        not isinstance(argv, (list, tuple))
        or len(argv) != 10
        or list(argv[:5]) != ["python3", "-I", "-S", "-c", CACHED_BOOTSTRAP]
        or any(not isinstance(item, str) for item in argv)
    ):
        raise ValueError("Unclassified project reader helper")
    if len(shlex.join(argv).encode("utf-8")) > 120000:
        raise ValueError("Reader command exceeds the SSH argument budget")
    action, digest, root, payload, count = argv[5:]
    if (
        action not in {"read", "upload"}
        or not re.fullmatch("[0-9a-f]{64}", digest)
        or len(root) > 4096
        or not count.isdecimal()
        or not 1 <= int(count) <= 32
    ):
        raise ValueError("Invalid project reader envelope")
    if action == "read":
        if len(payload) > 65536:
            raise ValueError("Reader request exceeds metadata budget")
        request = json.loads(payload)
        if (
            not isinstance(request, dict)
            or set(request) != {"operation", "args", "kwargs"}
            or request["operation"] not in OPERATIONS
            or not isinstance(request["args"], list)
            or not isinstance(request["kwargs"], dict)
        ):
            raise ValueError("Invalid project reader request")
        uploaded = 0
    else:
        index, sep, chunk = payload.partition(":")
        if (
            not sep
            or not index.isdecimal()
            or not 0 <= int(index) < int(count)
            or len(chunk) > CHUNK_BYTES
            or not re.fullmatch("[A-Za-z0-9+/=]*", chunk)
        ):
            raise ValueError("Invalid reader chunk")
        uploaded = len(chunk)
    return {
        "action": action,
        "identity": digest
        + ":"
        + action
        + ":"
        + hashlib.sha256(payload.encode()).hexdigest(),
        "upload_bytes": uploaded,
    }
