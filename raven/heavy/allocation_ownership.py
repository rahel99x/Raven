"""Durable allocation receipts, shared claims, and host-local kernel ownership."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import tempfile
import time


def process_start(pid):
    try:
        return Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, ValueError, IndexError):
        return "unavailable"


def boot_identity():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unavailable"


class OwnershipRecord:
    """Per-user/context lock and private receipt; old uncertainty blocks replay."""

    def __init__(self, directory, plan, request_id, *, reconcile=False):
        from ..state_ownership import filesystem_kind, LOCAL_FILESYSTEMS
        from ..config_trust import directory_fd

        directory = Path(
            plan.get("receipt_directory")
            or Path(directory).expanduser() / "allocations"
        ).expanduser()
        with directory_fd(directory, create=True, private=True) as parent:
            self.directory_fd = os.dup(parent)
        self.previous = None
        self.fd = -1
        from .allocation_identity import receipt_key, connection_binding

        key = receipt_key(plan)
        self.path = directory / (key + ".json")
        self.lock_directory_fd = -1
        lock_directory = directory
        if filesystem_kind(directory) not in LOCAL_FILESYSTEMS:
            root = os.environ.get("XDG_RUNTIME_DIR", "")
            root = (
                Path(root)
                if root and Path(root).is_absolute()
                else Path(tempfile.gettempdir())
            )
            lock_directory = root / ("raven-allocation-locks-" + str(os.getuid()))
        try:
            with directory_fd(lock_directory, create=True, private=True) as parent:
                self.lock_directory_fd = os.dup(parent)
        except BaseException:
            self.close()
            raise
        try:
            self.fd = os.open(
                key + ".lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                0o600,
                dir_fd=self.lock_directory_fd,
            )
            info = os.fstat(self.fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise ValueError("Invalid allocation ownership lock")
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._migrate(plan)
            try:
                receipt_fd = os.open(
                    self.path.name,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=self.directory_fd,
                )
            except FileNotFoundError:
                receipt_fd = -1
            if receipt_fd >= 0:
                with os.fdopen(receipt_fd, "rb") as source:
                    meta = os.fstat(source.fileno())
                    if (
                        not stat.S_ISREG(meta.st_mode)
                        or meta.st_uid != os.getuid()
                        or meta.st_mode & 0o077
                        or meta.st_size > 65536
                    ):
                        raise ValueError("Invalid prior allocation receipt")
                    from ..framing import finite_constant, unique_object

                    previous = json.loads(
                        source.read(65537),
                        parse_constant=finite_constant,
                        object_pairs_hook=unique_object,
                    )
                    if (
                        not isinstance(previous, dict)
                        or previous.get("schema") != "raven.allocation-receipt/v1"
                    ):
                        raise ValueError(
                            "Unsupported prior allocation receipt schema; preserve it for the compatible Raven version"
                        )
                    if any(
                        previous.get(key) is not None
                        and not isinstance(previous[key], dict)
                        for key in ("identity", "recovery")
                    ):
                        raise ValueError(
                            "Invalid prior allocation receipt identity or recovery metadata"
                        )
                    self.previous = previous
                    if not reconcile and (
                        previous.get("state") != "light"
                        or previous.get("cleanup_uncertain")
                    ):
                        raise ValueError(
                            "A previous allocation request needs reconciliation; inspect its private receipt before starting another"
                        )
            if reconcile and self.previous is None:
                try:
                    claim_fd = os.open(
                        key + ".claim",
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                        dir_fd=self.directory_fd,
                    )
                except FileNotFoundError:
                    claim_fd = -1
                if claim_fd >= 0:
                    with os.fdopen(claim_fd, "rb") as source:
                        info = os.fstat(source.fileno())
                        if (
                            not stat.S_ISREG(info.st_mode)
                            or info.st_uid != os.getuid()
                            or info.st_mode & 0o077
                            or info.st_size > 65536
                        ):
                            raise ValueError("Invalid allocation ownership claim")
                        from ..framing import finite_constant, unique_object

                        self.previous = json.loads(
                            source.read(65537),
                            parse_constant=finite_constant,
                            object_pairs_hook=unique_object,
                        )
                        if (
                            not isinstance(self.previous, dict)
                            or self.previous.get("schema")
                            != "raven.allocation-receipt/v1"
                        ):
                            raise ValueError("Unsupported allocation ownership claim")
            self._claim = None
            if not reconcile:
                # Exclusive shared-directory publication prevents simultaneous
                # starts from different login hosts; the kernel lock is local.
                claim_name = key + ".claim"
                try:
                    claim = os.open(
                        claim_name,
                        os.O_CREAT
                        | os.O_EXCL
                        | os.O_WRONLY
                        | os.O_NOFOLLOW
                        | os.O_CLOEXEC,
                        0o600,
                        dir_fd=self.directory_fd,
                    )
                except FileExistsError:
                    raise ValueError(
                        "A previous allocation request owns this connection; reconcile its durable receipt before starting another"
                    ) from None
                with os.fdopen(claim, "wb") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "schema": "raven.allocation-receipt/v1",
                                "request_id": request_id,
                                "state": "preparing",
                                "cleanup_uncertain": True,
                                "created_at": time.time(),
                                "ownership": plan.get("ownership"),
                                "site_kind": plan.get("site_kind"),
                                "transport": plan.get("transport"),
                                "cluster_id": plan.get("cluster_id"),
                                "connection_binding": connection_binding(plan),
                                "request_name": (
                                    "raven-heavy-"
                                    if plan.get("ownership") == "dedicated-allocation"
                                    else "raven-step-"
                                )
                                + request_id,
                                "reuse": plan.get("reuse"),
                                "helper_host": socket.gethostname(),
                                "helper_pid": os.getpid(),
                                "helper_start": process_start(os.getpid()),
                                "helper_boot": boot_identity(),
                            }
                        ).encode()
                    )
                    stream.flush()
                    os.fsync(stream.fileno())
                os.fsync(self.directory_fd)
                self._claim = claim_name
            self.request_id = request_id
        except BaseException:
            self.close()
            raise

    def _migrate(self, plan):
        """Adopt only a legacy receipt whose complete connection binding matches."""
        try:
            os.stat(self.path.name, dir_fd=self.directory_fd, follow_symlinks=False)
            return
        except FileNotFoundError:
            pass
        from .allocation_identity import connection_binding

        matches = []
        names = os.listdir(self.directory_fd)
        if len(names) > 2048:
            raise ValueError(
                "Allocation receipt directory exceeds its inspection bound"
            )
        for name in names:
            if not name.endswith(".json") or len(name) != 37:
                continue
            fd = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                dir_fd=self.directory_fd,
            )
            try:
                meta = os.fstat(fd)
                if (
                    not stat.S_ISREG(meta.st_mode)
                    or meta.st_uid != os.getuid()
                    or meta.st_mode & 0o077
                    or meta.st_size > 65536
                ):
                    continue
                from ..framing import finite_constant, unique_object

                old = json.loads(
                    os.read(fd, 65537),
                    parse_constant=finite_constant,
                    object_pairs_hook=unique_object,
                )
            finally:
                os.close(fd)
            if not isinstance(old, dict):
                continue
            legacy_fields = {
                key: plan.get(key)
                for key in (
                    "site_kind",
                    "cluster_id",
                    "target",
                    "transport",
                    "ssh_target",
                    "ssh_options",
                    "owner_uid",
                )
            }
            legacy_fields["cluster_id"] = old.get("cluster_id", plan.get("cluster_id"))
            legacy_binding = hashlib.sha256(
                json.dumps(legacy_fields, sort_keys=True).encode()
            ).hexdigest()
            if old.get("connection_binding") in {
                connection_binding(plan),
                legacy_binding,
            }:
                matches.append(name)
        if len(matches) > 1:
            raise ValueError(
                "Several legacy receipts match this connection; reconcile each before requesting compute"
            )
        if matches:
            # A still-running old helper retains its old lock. Never migrate
            # around that lock and create a second owner.
            old_lock = os.open(
                matches[0][:-5] + ".lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=self.directory_fd,
            )
            try:
                fcntl.flock(old_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.rename(
                    matches[0],
                    self.path.name,
                    src_dir_fd=self.directory_fd,
                    dst_dir_fd=self.directory_fd,
                )
                os.fsync(self.directory_fd)
            finally:
                os.close(old_lock)

    def write(self, value):
        if (
            value.get("schema", "raven.allocation-receipt/v1")
            != "raven.allocation-receipt/v1"
        ):
            raise ValueError("Unsupported allocation receipt schema")
        data = (
            json.dumps(
                {**value, "schema": "raven.allocation-receipt/v1"},
                allow_nan=False,
                sort_keys=True,
            )
            + "\n"
        ).encode()
        if len(data) > 65536:
            raise ValueError("Allocation receipt exceeds its budget")
        temporary = self.path.name + "." + secrets.token_hex(8) + ".tmp"
        fd = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self.directory_fd,
        )
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(
                temporary,
                self.path.name,
                src_dir_fd=self.directory_fd,
                dst_dir_fd=self.directory_fd,
            )
            os.fsync(self.directory_fd)
            if value.get("state") == "light" and not value.get("cleanup_uncertain"):
                try:
                    os.unlink(self.path.stem + ".claim", dir_fd=self.directory_fd)
                    os.fsync(self.directory_fd)
                except FileNotFoundError:
                    pass
        finally:
            try:
                os.unlink(temporary, dir_fd=self.directory_fd)
            except FileNotFoundError:
                pass

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        if self.directory_fd >= 0:
            os.close(self.directory_fd)
            self.directory_fd = -1
        if self.lock_directory_fd >= 0:
            os.close(self.lock_directory_fd)
            self.lock_directory_fd = -1
