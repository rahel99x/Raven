"""User-private, host-local cross-process query cache with bounded SQLite leases.

The owner executes a bounded loader on its caller's worker. Other processes wait
cooperatively; a cancelled waiter never cancels another process's collection.
Only callers declaring read_only=True are admitted. No scheduler mutation is
cached or replayed. Shared network filesystems are not a supported deployment.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import os
from pathlib import Path
import socket
import re
import sqlite3
import stat
import threading
import time
import uuid

from . import evidence as E, site_policy as P


def _process_start(pid):
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
        return data[data.rfind(")") + 2:].split()[19]
    except (OSError, IndexError):
        return None


def _host():
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError as exc:
        raise ValueError("Shared collection needs Linux process/boot identity") from exc
    return E.digest({"hostname": socket.gethostname(), "boot": boot})


def _network_mount(path):
    """Reject known shared/network mounts; never exercise their SQLite locks."""
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as stream:
            raw = stream.read(2 << 20)
            if len(raw) >= 2 << 20:
                raise ValueError("Mount identity exceeds its inspection bound")
    except OSError as exc:
        raise ValueError("Cannot verify the shared cache filesystem on this host") from exc
    selected = (0, "")
    for line in raw.splitlines():
        fields = line.split()
        try:
            marker = fields.index("-")
            mount = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4])
            filesystem = fields[marker + 1]
        except (ValueError, IndexError):
            continue
        if path == mount or path.startswith(mount.rstrip("/") + "/"):
            selected = max(selected, (len(mount), filesystem))
    kind = selected[1].lower()
    if not kind:
        raise ValueError("Shared cache filesystem identity is unavailable")
    return kind in {"nfs", "nfs4", "lustre", "gpfs", "beegfs", "fhgfs", "pvfs2", "cifs", "smb3", "ceph", "afs", "glusterfs"} or kind.startswith("fuse.")


class SharedQueryCache:
    def __init__(self, directory, *, capacity=128, max_bytes=16 << 20):
        E.integer(capacity, "shared cache capacity", 1024)
        E.integer(max_bytes, "shared cache byte bound", 64 << 20)
        if min(capacity, max_bytes) < 1:
            raise ValueError("Shared cache bounds must be positive")
        root = Path(directory).expanduser().absolute()
        if any(path.is_symlink() for path in (root, *root.parents)):
            raise ValueError("Shared collection directory must not traverse symlinks")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if _network_mount(str(root)):
            raise ValueError("Shared collection cache requires host-local storage, not a network/shared filesystem")
        info = root.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Shared collection directory must be private (0700) and owned by this user")
        self.path = root / "queries.sqlite3"
        self.host, self.capacity, self.max_bytes = _host(), capacity, max_bytes
        self._owner = (os.getpid(), _process_start(os.getpid()))
        if self._owner[1] is None:
            raise ValueError("Shared collection requires process start identity")
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY, scope TEXT NOT NULL, policy TEXT NOT NULL,
                    throttle TEXT NOT NULL, started REAL NOT NULL, expires REAL NOT NULL,
                    owner TEXT, pid INTEGER, process_start TEXT, reserved INTEGER NOT NULL,
                    value TEXT, size INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS throttle (key TEXT PRIMARY KEY, started REAL NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            old = connection.execute("SELECT value FROM metadata WHERE key='host'").fetchone()
            if old and old[0] != self.host:
                # Refuse a cache belonging to another host/boot. No cross-host
                # wall-clock guesses, shared-NFS locks or old leases are reused.
                raise ValueError("Shared cache belongs to another host/boot; choose a fresh host-local directory")
            connection.execute("INSERT OR IGNORE INTO metadata VALUES ('host', ?)", (self.host,))
        os.chmod(self.path, 0o600)

    @contextmanager
    def _connection(self):
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        for path in (self.path, Path(str(self.path) + "-journal"), Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("Shared cache files must be private regular files owned by this user")
            if info.st_size > 2 * self.max_bytes + (8 << 20):
                raise ValueError("Shared cache storage exceeds its bound")
        # The parent is private before SQLite creates its file. No state or
        # cached scheduler output is exposed through a public parent directory.
        connection = sqlite3.connect(self.path, timeout=.1, isolation_level=None)
        try:
            os.chmod(self.path, 0o600)
            connection.execute("PRAGMA busy_timeout=100")
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            connection.execute("PRAGMA max_page_count=" + str((2 * self.max_bytes + (4 << 20)) // page_size))
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _dead(row):
        observed = _process_start(row[1])
        if observed is not None:
            return observed != row[2]
        try:
            os.kill(row[1], 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        # Inaccessible process metadata is not proof of death. Retain the
        # lease rather than overlapping a possibly live collector.
        return False

    def query(self, scope, query, loader, policy, observed, *, read_only=False, cancel=None, fresh=False):
        if os.getpid() != self._owner[0]:
            raise ValueError("Create a new shared cache client after forking")
        if read_only is not True:
            raise ValueError("Shared collection is restricted to explicitly read-only queries")
        if type(fresh) is not bool:
            raise ValueError("Shared collection freshness must be boolean")
        E.keys(scope, {"connection", "generation"}, {"user", "cluster"}, "query scope")
        for value in scope.values():
            E.text(value, "query scope", 512)
        E.keys(query, {"request", "parameters"}, label="shared query")
        if not isinstance(query["parameters"], dict) or not callable(loader):
            raise ValueError("Invalid shared query parameters/loader")
        admission = P.admit(policy.value if isinstance(policy, P.SitePolicy) else policy, query["request"], observed)
        if not admission["allowed"]:
            raise ValueError("Collection refused: " + "; ".join(admission["reasons"]))
        return self._query_admitted(scope, query, loader, admission, cancel=cancel, fresh=fresh)

    def _query_admitted(self, scope, query, loader, admission, *, cancel=None, fresh=False):
        """Internal broker path: reuse its validated, detached read admission."""
        if os.getpid() != self._owner[0]:
            raise ValueError("Create a new shared cache client after forking")
        request = admission["request"]
        if request["max_bytes"] > self.max_bytes:
            raise ValueError("Request exceeds shared cache byte capacity")
        key = E.digest({"host": self.host, "scope": scope, "query": query, "policy": admission["policy_digest"], "capabilities": admission["capabilities_digest"]})
        scope_key = E.digest(scope)
        throttle = E.digest({"host": self.host, "scope": scope, "source": request["source"], "operation": request["operation"]})
        token, begin = uuid.uuid4().hex, time.monotonic()
        deadline = begin + request["duration"]
        stop = cancel if cancel is not None else threading.Event()
        while True:
            if stop.is_set():
                raise ValueError("Shared collection wait cancelled")
            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError("Shared collection lease deadline exceeded")
            owner = False
            try:
                with self._connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        for row in connection.execute("SELECT key,pid,process_start FROM cache WHERE owner IS NOT NULL").fetchall():
                            if self._dead(row):
                                connection.execute("DELETE FROM cache WHERE key=?", (row[0],))
                        connection.execute("DELETE FROM cache WHERE owner IS NULL AND expires<=?", (now,))
                        connection.execute("DELETE FROM throttle WHERE expires<=?", (now,))
                        row = connection.execute("SELECT owner,value,started,expires,size FROM cache WHERE key=?", (key,)).fetchone()
                        if row and row[0] is None and fresh and row[2] < begin:
                            # Reviewed actions must validate observations made
                            # after this request. Preserve source throttling
                            # and any active owner, but do not reuse old state.
                            connection.execute("DELETE FROM cache WHERE key=?", (key,))
                            row = None
                        if row and row[0] is None and row[3] > now:
                            value = E.loads(row[1])
                            connection.execute("COMMIT")
                            return {"value": value, "cached": True, "age": max(0., now - row[2]), "scope_digest": scope_key, "bytes": row[4]}
                        if row is None:
                            last = connection.execute("SELECT expires FROM throttle WHERE key=?", (throttle,)).fetchone()
                            if last is not None and now < last[0]:
                                # Another distinct query used this source's
                                # rate budget. Wait for admission, never replay
                                # a different query's data as a substitute.
                                pass
                            else:
                                count, reserved = connection.execute("SELECT count(*), coalesce(sum(reserved),0) FROM cache").fetchone()
                                active = connection.execute("SELECT count(*) FROM cache WHERE owner IS NOT NULL AND policy=?", (admission["policy_digest"],)).fetchone()[0]
                                if count >= self.capacity or reserved + request["max_bytes"] > self.max_bytes or active >= admission["max_inflight"]:
                                    raise ValueError("Shared collection admission capacity reached")
                                if connection.execute("SELECT count(*) FROM throttle").fetchone()[0] >= self.capacity * 4:
                                    raise ValueError("Shared collection throttle capacity reached")
                                connection.execute("INSERT INTO cache(key,scope,policy,throttle,started,expires,owner,pid,process_start,reserved) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                                   (key, scope_key, admission["policy_digest"], throttle, now, deadline, token, self._owner[0], self._owner[1], request["max_bytes"]))
                                connection.execute("INSERT OR REPLACE INTO throttle VALUES (?,?,?)", (throttle, now, now + admission["effective_min_interval"]))
                                owner = True
                        connection.execute("COMMIT")
                    except BaseException:
                        if connection.in_transaction:
                            connection.execute("ROLLBACK")
                        raise
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise ValueError("Shared cache database operation failed") from exc
            if owner:
                break
            stop.wait(min(.02, max(0., deadline - time.monotonic())))
        try:
            value = loader(stop)
            raw = E.canonical(value)
            if len(raw) > request["max_bytes"]:
                raise ValueError("Shared collector exceeded its byte budget")
            count = len(value.get("records", [])) if isinstance(value, dict) and isinstance(value.get("records"), list) else len(value) if isinstance(value, list) else 1
            if count > request["max_records"]:
                raise ValueError("Shared collector exceeded its record budget")
            now = time.monotonic()
            if stop.is_set() or now >= deadline:
                raise ValueError("Shared collector cancelled or exceeded its deadline")
            with self._connection() as connection:
                changed = connection.execute("UPDATE cache SET owner=NULL,pid=NULL,process_start=NULL,value=?,size=?,reserved=?,expires=? WHERE key=? AND owner=? AND expires>0",
                                             (raw.decode(), len(raw), len(raw), now + request["interval"], key, token)).rowcount
                if changed != 1:
                    raise ValueError("Shared collection lease was invalidated before publication")
            return {"value": copy.deepcopy(value), "cached": False, "age": 0., "scope_digest": scope_key, "bytes": len(raw)}
        except BaseException:
            with self._connection() as connection:
                connection.execute("DELETE FROM cache WHERE key=? AND owner=?", (key, token))
            raise

    def invalidate(self, scope):
        with self._connection() as connection:
            identity = E.digest(scope)
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM cache WHERE scope=? AND owner IS NULL", (identity,))
                # Keep live owners and their reservations until they drain.
                # Deleting them permits an overlapping replacement collector
                # after the interval floor, and loses inflight byte accounting.
                connection.execute("UPDATE cache SET expires=0 WHERE scope=? AND owner IS NOT NULL", (identity,))
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
