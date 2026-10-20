"""Private, durable, cooperative application control and I/O permits.

The application calls safe_point() itself. Raven never injects code, signals the
workload, or writes its parameters behind its back. SQLite lives in a private
local directory on one host; multi-node applications use their existing control
rank to gather arrivals and broadcast a single decision around a collective.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import time
import uuid

from .operation_support import canonical, digest
from .runtime_contracts import identifier, integer, number, object_, sequence as sequence, text, unique_names

MAX_REQUESTS = 10000


def _machine():
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError as exc:
        raise ValueError("Runtime mailboxes require a Linux boot identity") from exc
    return {"host": socket.gethostname(), "boot": boot}


def _private_directory(path):
    path = Path(path).absolute()
    from .config_trust import directory_fd
    # Share the configuration integrity policy, retaining the stricter private
    # mailbox requirement and the existing host-local SQLite restriction below.
    # A private child under a writable ancestor is not a trusted mailbox path.
    try:
        with directory_fd(path, private=True):
            pass
    except OSError as exc:
        raise ValueError("Runtime mailbox paths require real, trusted directories without symlinks") from exc
    # SQLite coordination on distributed filesystems is not a supported transport.
    # Refuse known network filesystems rather than advertise unreliable locking.
    try:
        mounts = []
        for line in Path("/proc/mounts").read_text().splitlines():
            fields = line.split()
            mount = fields[1].replace("\\040", " ")
            if str(path) == mount or str(path).startswith(mount.rstrip("/") + "/"):
                mounts.append((len(mount), fields[2]))
        kind = max(mounts)[1] if mounts else ""
        if kind not in {"ext2", "ext3", "ext4", "xfs", "btrfs", "tmpfs", "ramfs", "zfs", "overlay", "overlayfs"}:
            raise ValueError("Mailbox SQLite requires a supported node-local filesystem; distributed or unknown filesystem types are rejected")
    except OSError:
        raise ValueError("Cannot verify node-local runtime mailbox storage")
    return path


def validate_configuration(configuration):
    object_(configuration, {"schema", "attempts", "commands", "io"})
    if configuration["schema"] != "raven.runtime-control/v1":
        raise ValueError("Expected raven.runtime-control/v1")
    unique_names(configuration["attempts"], "exact attempt identities", 1024)
    if not configuration["attempts"]:
        raise ValueError("At least one exact attempt identity is required")
    commands = configuration["commands"]
    if not isinstance(commands, dict) or len(commands) > 64:
        raise ValueError("Declare up to 64 supported commands")
    for name, command in commands.items():
        identifier(name, "command")
        object_(command, {"safe_point", "parameters"})
        identifier(command["safe_point"], "safe point")
        if not isinstance(command["parameters"], dict) or len(command["parameters"]) > 32:
            raise ValueError("Command parameters must be a bounded mapping")
        for parameter, contract in command["parameters"].items():
            identifier(parameter, "parameter")
            object_(contract, {"type"}, {"minimum", "maximum", "choices"})
            if not isinstance(contract["type"], str):
                raise ValueError("Parameter type must be a string")
            if contract["type"] in {"number", "integer"}:
                if set(contract) != {"type", "minimum", "maximum"}:
                    raise ValueError("Numeric parameters require explicit minimum and maximum")
                number(contract["minimum"], "minimum", low=-1e100)
                number(contract["maximum"], "maximum", low=-1e100)
                if contract["minimum"] > contract["maximum"]:
                    raise ValueError("Parameter minimum exceeds maximum")
            elif contract["type"] == "enum":
                if set(contract) != {"type", "choices"}:
                    raise ValueError("Enum parameters require only choices")
                unique_names(contract["choices"], "enum choices", 64)
                if not contract["choices"]:
                    raise ValueError("Enum must declare choices")
            elif contract["type"] != "boolean" or set(contract) != {"type"}:
                raise ValueError("Supported parameter types: number, integer, enum, boolean")
    object_(configuration["io"], {"capacity", "max_lease_seconds"})
    integer(configuration["io"]["capacity"], "I/O permit capacity", 1, 1024)
    number(configuration["io"]["max_lease_seconds"], "maximum lease seconds", .01, 3600)
    canonical(configuration)
    return configuration


def initialize(directory, configuration, *, clock=time.monotonic):
    """Create a new private mailbox. Existing paths are never overwritten."""
    validate_configuration(configuration)
    path = Path(directory).absolute()
    from .config_trust import directory_fd
    with directory_fd(path.parent) as parent:
        os.mkdir(path.name, 0o700, dir_fd=parent)
    db = path / "control.sqlite3"
    try:
        fd = os.open(db, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        _private_directory(path)
        now = number(clock(), "monotonic clock")
        with sqlite3.connect(db) as connection:
            connection.executescript("""
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE epochs (attempt TEXT PRIMARY KEY, epoch INTEGER NOT NULL);
                CREATE TABLE requests (id TEXT PRIMARY KEY, attempt TEXT NOT NULL, body TEXT NOT NULL,
                    expected_epoch INTEGER NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
                    status TEXT NOT NULL, claim TEXT, result TEXT);
                CREATE INDEX request_order ON requests(attempt,status,created,id);
                CREATE TABLE permits (id TEXT PRIMARY KEY, attempt TEXT NOT NULL, body TEXT NOT NULL,
                    created REAL NOT NULL, deadline REAL NOT NULL, state TEXT NOT NULL,
                    arrivals TEXT NOT NULL, lease_until REAL, token TEXT, result TEXT);
            """)
            values = {"configuration": configuration, "machine": _machine(), "last_clock": now, "mailbox_id": uuid.uuid4().hex}
            connection.executemany("INSERT INTO metadata VALUES (?,?)", [(key, canonical(value).decode()) for key, value in values.items()])
            connection.executemany("INSERT INTO epochs VALUES (?,0)", [(attempt,) for attempt in configuration["attempts"]])
        for directory in (path, path.parent):
            fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return RuntimeControl(path, clock=clock).status()
    except BaseException:
        for child in path.iterdir():
            child.unlink()
        path.rmdir()
        raise


class RuntimeControl:
    def __init__(self, directory, attempt=None, *, clock=time.monotonic):
        self.directory = Path(directory).absolute()
        self.attempt = attempt
        self.clock = clock

    @contextmanager
    def _transaction(self):
        path = _private_directory(self.directory) / "control.sqlite3"
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ValueError("Mailbox database must be a private user-owned regular file")
        connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=1)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            meta = {row["key"]: json.loads(row["value"]) for row in connection.execute("SELECT * FROM metadata")}
            if meta["machine"] != _machine():
                raise ValueError("Mailbox host/boot changed; prepare a new mailbox and reconcile old receipts")
            now = number(self.clock(), "monotonic clock")
            if now < meta["last_clock"]:
                raise ValueError("Monotonic clock moved backwards; mailbox is paused")
            config = validate_configuration(meta["configuration"])
            if self.attempt is not None and self.attempt not in config["attempts"]:
                raise ValueError("Exact job attempt is not authorized by this mailbox")
            connection.execute("UPDATE metadata SET value=? WHERE key='last_clock'", (json.dumps(now),))
            connection.execute("UPDATE requests SET status='expired' WHERE status='pending' AND expires<=?", (now,))
            connection.execute("UPDATE requests SET status='uncertain' WHERE status='claimed' AND expires<=?", (now,))
            # An expired active writer may still be doing I/O. Keep its slot
            # quarantined until the application explicitly releases it.
            connection.execute("UPDATE permits SET state='uncertain' WHERE state='granted' AND lease_until<=?", (now,))
            self._grant(connection, config, now)
            yield connection, config, now
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _attempt(self):
        return identifier(self.attempt, "exact attempt")

    def inspect_configuration(self):
        with self._transaction() as (_, configuration, _):
            return configuration

    def inspect_request(self, request_id):
        self._attempt()
        identifier(request_id, "request ID")
        with self._transaction() as (connection, _, _):
            row = connection.execute("SELECT * FROM requests WHERE id=? AND attempt=?", (request_id, self.attempt)).fetchone()
            if row is None:
                raise ValueError("Request is absent from this attempt")
            return self._request(row)

    def inspect_permit(self, request_id):
        self._attempt()
        identifier(request_id, "permit ID")
        with self._transaction() as (connection, _, _):
            row = connection.execute("SELECT * FROM permits WHERE id=? AND attempt=?", (request_id, self.attempt)).fetchone()
            if row is None:
                raise ValueError("Permit is absent from this attempt")
            return self._permit(row)

    @staticmethod
    def _command(configuration, command, parameters):
        contract = configuration["commands"].get(command)
        if contract is None:
            raise ValueError("Command is not declared by the running application")
        if not isinstance(parameters, dict) or set(parameters) != set(contract["parameters"]):
            raise ValueError("Command parameters must exactly match the application contract")
        for key, rule in contract["parameters"].items():
            value, kind = parameters[key], rule["type"]
            if kind == "boolean":
                if not isinstance(value, bool):
                    raise ValueError(f"{key} must be boolean")
            elif kind == "enum":
                if value not in rule["choices"] or not isinstance(value, str):
                    raise ValueError(f"{key} is not a declared choice")
            else:
                number(value, key, rule["minimum"], rule["maximum"])
                if kind == "integer" and not isinstance(value, int):
                    raise ValueError(f"{key} must be an integer")
        return contract

    def submit(self, request_id, command, parameters, *, expected_epoch, ttl=60):
        identifier(request_id, "request ID")
        identifier(command, "command")
        integer(expected_epoch, "expected parameter epoch", 0, 2**53)
        number(ttl, "request TTL", .01, 3600)
        body = {"attempt": self._attempt(), "command": command, "parameters": parameters, "expected_epoch": expected_epoch, "ttl": ttl}
        encoded = canonical(body).decode()
        with self._transaction() as (connection, config, now):
            self._command(config, command, parameters)
            old = connection.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            if old:
                if old["body"] != encoded:
                    raise ValueError("Request ID already belongs to a different immutable command")
                return self._request(old)
            if connection.execute("SELECT count(*) FROM requests").fetchone()[0] >= MAX_REQUESTS:
                raise ValueError("Mailbox receipt capacity reached; archive and initialize a new mailbox")
            connection.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,'pending',NULL,NULL)",
                               (request_id, self.attempt, encoded, expected_epoch, now, now + ttl))
            return self._request(connection.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone())

    @staticmethod
    def _request(row):
        return {"id": row["id"], **json.loads(row["body"]), "status": row["status"], "claim": row["claim"],
                "result": json.loads(row["result"]) if row["result"] else None, "expires_monotonic": row["expires"]}

    def poll(self, safe_point):
        """Claim at most one command. Never reclaim an uncertain command."""
        identifier(safe_point, "safe point")
        self._attempt()
        with self._transaction() as (connection, config, _):
            if connection.execute("SELECT 1 FROM requests WHERE attempt=? AND status IN ('claimed','uncertain')", (self.attempt,)).fetchone():
                return None
            epoch = connection.execute("SELECT epoch FROM epochs WHERE attempt=?", (self.attempt,)).fetchone()[0]
            for row in connection.execute("SELECT * FROM requests WHERE attempt=? AND status='pending' ORDER BY created,id", (self.attempt,)).fetchall():
                body = json.loads(row["body"])
                if config["commands"][body["command"]]["safe_point"] != safe_point:
                    continue
                if row["expected_epoch"] != epoch:
                    connection.execute("UPDATE requests SET status='stale' WHERE id=?", (row["id"],))
                    continue
                claim = uuid.uuid4().hex
                connection.execute("UPDATE requests SET status='claimed',claim=? WHERE id=?", (claim, row["id"]))
                return self._request(connection.execute("SELECT * FROM requests WHERE id=?", (row["id"],)).fetchone())
            return None

    def acknowledge(self, request_id, claim, *, applied, result=None):
        self._attempt()
        identifier(request_id, "request ID")
        text(claim, "claim token", 64)
        if not isinstance(applied, bool):
            raise ValueError("Applied must be boolean")
        result = {} if result is None else result
        if len(canonical(result)) > 65536:
            raise ValueError("Acknowledgment result exceeds 64 KiB")
        receipt = canonical({"applied": applied, "result": result}).decode()
        with self._transaction() as (connection, _, _):
            row = connection.execute("SELECT * FROM requests WHERE id=? AND attempt=?", (request_id, self.attempt)).fetchone()
            if row is None or row["claim"] != claim:
                raise ValueError("Command claim does not belong to this attempt")
            if row["status"] in {"applied", "rejected"}:
                if row["result"] != receipt:
                    raise ValueError("Acknowledgment conflicts with the durable result")
                return self._request(row)
            if row["status"] not in {"claimed", "uncertain"}:
                raise ValueError("Command cannot be acknowledged in its current state")
            if applied:
                connection.execute("UPDATE epochs SET epoch=epoch+1 WHERE attempt=?", (self.attempt,))
            connection.execute("UPDATE requests SET status=?,result=? WHERE id=?", ("applied" if applied else "rejected", receipt, request_id))
            return self._request(connection.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone())

    def safe_point(self, name, handler):
        """Application SDK: run one declared handler and durably acknowledge it.

        If the handler raises, outcome is uncertain and is never retried by
        Raven. Applications can reconcile with acknowledge using the claim token.
        """
        request = self.poll(name)
        if request is None:
            return None
        try:
            result = handler(request["command"], request["parameters"], request["id"])
        except BaseException:
            # Leave claimed/uncertain. The callback might have mutated state.
            raise
        return self.acknowledge(request["id"], request["claim"], applied=True, result=result)

    def mark_uncertain(self, request_id, claim, result):
        """Persist a partial application report without asserting success or retry."""
        self._attempt()
        encoded = canonical({"uncertain": result}).decode()
        if len(encoded) > 65536:
            raise ValueError("Uncertain receipt exceeds 64 KiB")
        with self._transaction() as (connection, _, _):
            row = connection.execute("SELECT * FROM requests WHERE id=? AND attempt=?", (request_id, self.attempt)).fetchone()
            if row is None or row["claim"] != claim or row["status"] not in {"claimed", "uncertain"}:
                raise ValueError("Uncertain report requires an active owned command claim")
            connection.execute("UPDATE requests SET status='uncertain',result=? WHERE id=?", (encoded, request_id))
            return self._request(connection.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone())

    def quarantine_permit(self, request_id, token):
        self._attempt()
        with self._transaction() as (connection, _, _):
            row = connection.execute("SELECT * FROM permits WHERE id=? AND attempt=?", (request_id, self.attempt)).fetchone()
            if row is None or row["token"] != token or row["state"] not in {"granted", "uncertain"}:
                raise ValueError("Quarantine requires an owned active permit")
            connection.execute("UPDATE permits SET state='uncertain' WHERE id=?", (request_id,))
            return self._permit(connection.execute("SELECT * FROM permits WHERE id=?", (request_id,)).fetchone())

    @staticmethod
    def _grant(connection, configuration, now):
        capacity = configuration["io"]["capacity"]
        used = connection.execute("SELECT count(*) FROM permits WHERE state IN ('granted','uncertain')").fetchone()[0]
        rows = connection.execute("SELECT * FROM permits WHERE state='waiting' ORDER BY created,id").fetchall()
        for row in rows:
            body = json.loads(row["body"])
            arrived = set(json.loads(row["arrivals"])) == set(body["participants"])
            if now >= row["deadline"]:
                state = ("bypass" if body["fallback"] == "proceed" else "skipped") if arrived else "incomplete"
                connection.execute("UPDATE permits SET state=? WHERE id=?", (state, row["id"]))
            elif arrived and used < capacity:
                connection.execute("UPDATE permits SET state='granted',lease_until=?,token=? WHERE id=?", (now + body["lease_seconds"], uuid.uuid4().hex, row["id"]))
                used += 1

    @staticmethod
    def _permit(row):
        return {"id": row["id"], **json.loads(row["body"]), "state": row["state"], "arrivals": json.loads(row["arrivals"]),
                "lease_until_monotonic": row["lease_until"], "token": row["token"], "deadline_monotonic": row["deadline"]}

    def permit(self, request_id, *, participants, arrived, lease_seconds=30, wait_seconds=30, fallback="skip"):
        """Register cooperative arrivals; return a durable group decision immediately.

        The application leader gathers arrivals using its existing communicator,
        calls this method, and broadcasts ONE decision before collective I/O.
        This method does not implement or impersonate an MPI barrier.
        """
        self._attempt()
        identifier(request_id, "permit ID")
        unique_names(participants, "participants", 4096)
        unique_names(arrived, "arrived participants", 4096)
        if not participants or set(arrived) - set(participants):
            raise ValueError("Arrivals must refer to declared participants")
        number(lease_seconds, "lease duration", .01, 3600)
        number(wait_seconds, "maximum wait", 0, 3600)
        if not isinstance(fallback, str) or fallback not in {"skip", "proceed"}:
            raise ValueError("Fallback must be skip or explicit proceed for deadline-critical output")
        body = {"attempt": self.attempt, "participants": sorted(participants), "lease_seconds": lease_seconds, "wait_seconds": wait_seconds, "fallback": fallback}
        encoded = canonical(body).decode()
        with self._transaction() as (connection, config, now):
            if lease_seconds > config["io"]["max_lease_seconds"]:
                raise ValueError("Lease exceeds the configured policy")
            row = connection.execute("SELECT * FROM permits WHERE id=?", (request_id,)).fetchone()
            if row:
                if row["body"] != encoded:
                    raise ValueError("Permit ID already belongs to a different request")
                if row["state"] == "waiting":
                    arrivals = sorted(set(json.loads(row["arrivals"])) | set(arrived))
                    connection.execute("UPDATE permits SET arrivals=? WHERE id=?", (json.dumps(arrivals), request_id))
            else:
                if connection.execute("SELECT count(*) FROM permits").fetchone()[0] >= MAX_REQUESTS:
                    raise ValueError("Permit receipt capacity reached")
                connection.execute("INSERT INTO permits VALUES (?,?,?,?,?,'waiting',?,NULL,NULL,NULL)",
                                   (request_id, self.attempt, encoded, now, now + wait_seconds, json.dumps(sorted(arrived))))
            self._grant(connection, config, now)
            return self._permit(connection.execute("SELECT * FROM permits WHERE id=?", (request_id,)).fetchone())

    def renew(self, request_id, token, seconds):
        self._attempt()
        number(seconds, "renew duration", .01, 3600)
        with self._transaction() as (connection, config, now):
            row = connection.execute("SELECT * FROM permits WHERE id=? AND attempt=?", (request_id, self.attempt)).fetchone()
            if row is None or row["state"] != "granted" or row["token"] != token:
                raise ValueError("Only an unexpired owned permit can be renewed")
            if seconds > config["io"]["max_lease_seconds"]:
                raise ValueError("Renewal exceeds configured policy")
            connection.execute("UPDATE permits SET lease_until=? WHERE id=?", (now + seconds, request_id))
            return self._permit(connection.execute("SELECT * FROM permits WHERE id=?", (request_id,)).fetchone())

    def release(self, request_id, token):
        self._attempt()
        with self._transaction() as (connection, config, now):
            row = connection.execute("SELECT * FROM permits WHERE id=? AND attempt=?", (request_id, self.attempt)).fetchone()
            if row is None or row["token"] != token or row["state"] not in {"granted", "uncertain", "released"}:
                raise ValueError("Permit token does not identify an owned granted lease")
            connection.execute("UPDATE permits SET state='released' WHERE id=?", (request_id,))
            self._grant(connection, config, now)
            return self._permit(connection.execute("SELECT * FROM permits WHERE id=?", (request_id,)).fetchone())

    def status(self):
        with self._transaction() as (connection, config, now):
            clause, args = (" WHERE attempt=?", (self.attempt,)) if self.attempt else ("", ())
            mailbox_id = json.loads(connection.execute("SELECT value FROM metadata WHERE key='mailbox_id'").fetchone()[0])
            return {"schema": "raven.runtime-status/v1", "configuration_digest": digest(config), "mailbox_id": mailbox_id, "machine": _machine(),
                    "epochs": {row["attempt"]: row["epoch"] for row in connection.execute("SELECT * FROM epochs" + clause, args)},
                    "requests": [self._request(row) for row in connection.execute("SELECT * FROM requests" + clause + " ORDER BY created DESC,id DESC LIMIT 64", args)],
                    "permits": [self._permit(row) for row in connection.execute("SELECT * FROM permits" + clause + " ORDER BY created DESC,id DESC LIMIT 64", args)],
                    "visible_limit": 64, "clock_monotonic": now,
                    "limitations": ["Same-user node-local mailbox; remote hosts and changed boots are rejected.",
                                    "Application controls its safe points and collective barriers; Raven never interrupts it.",
                                    "Expired claimed commands and I/O leases stay uncertain until explicit application reconciliation."]}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="raven runtime", description=__doc__)
    parser.add_argument("--mailbox", required=True)
    parser.add_argument("--attempt")
    sub = parser.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init")
    init.add_argument("manifest")
    sub.add_parser("status")
    get = sub.add_parser("get")
    get.add_argument("request_id")
    permit_status = sub.add_parser("permit-status")
    permit_status.add_argument("request_id")
    submit = sub.add_parser("submit")
    submit.add_argument("request_id")
    submit.add_argument("command")
    submit.add_argument("parameters", help="JSON object")
    submit.add_argument("--epoch", type=int, required=True)
    submit.add_argument("--ttl", type=float, default=60)
    poll = sub.add_parser("poll")
    poll.add_argument("safe_point")
    ack = sub.add_parser("ack")
    ack.add_argument("request_id")
    ack.add_argument("claim")
    ack.add_argument("--applied", action="store_true")
    ack.add_argument("--result", default="{}")
    permit = sub.add_parser("permit")
    permit.add_argument("request_id")
    permit.add_argument("--participants", required=True, help="Comma-separated participant identities")
    permit.add_argument("--arrived", default="", help="Arrivals already gathered by the application's coordinator")
    permit.add_argument("--lease", type=float, default=30)
    permit.add_argument("--wait", type=float, default=30)
    permit.add_argument("--fallback", choices=("skip", "proceed"), default="skip")
    release = sub.add_parser("release")
    release.add_argument("request_id")
    release.add_argument("token")
    renew = sub.add_parser("renew")
    renew.add_argument("request_id")
    renew.add_argument("token")
    renew.add_argument("seconds", type=float)
    args = parser.parse_args(argv)
    control = RuntimeControl(args.mailbox, args.attempt)
    try:
        if args.action == "init":
            from .science_files import json_file
            result = initialize(args.mailbox, json_file(args.manifest)[0])
        elif args.action == "status":
            result = control.status()
        elif args.action == "get":
            result = control.inspect_request(args.request_id)
        elif args.action == "permit-status":
            result = control.inspect_permit(args.request_id)
        elif args.action == "submit":
            result = control.submit(args.request_id, args.command, _json_argument(args.parameters), expected_epoch=args.epoch, ttl=args.ttl)
        elif args.action == "poll":
            result = control.poll(args.safe_point)
        elif args.action == "ack":
            result = control.acknowledge(args.request_id, args.claim, applied=args.applied, result=_json_argument(args.result))
        elif args.action == "permit":
            result = control.permit(args.request_id, participants=args.participants.split(","), arrived=args.arrived.split(",") if args.arrived else [], lease_seconds=args.lease, wait_seconds=args.wait, fallback=args.fallback)
        elif args.action == "renew":
            result = control.renew(args.request_id, args.token, args.seconds)
        else:
            result = control.release(args.request_id, args.token)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f"raven runtime: {exc}\n")


def _json_argument(value):
    if len(value.encode("utf-8")) > 65536:
        raise ValueError("Runtime JSON argument exceeds 64 KiB")
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = item
        return result
    try:
        result = json.loads(value, object_pairs_hook=pairs,
                            parse_constant=lambda value: (_ for _ in ()).throw(ValueError("JSON numbers must be finite")))
    except (RecursionError, UnicodeError) as exc:
        raise ValueError("Runtime JSON must be bounded and shallow") from exc
    from .planning_io import _bounded
    _bounded(result, 32)
    canonical(result)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
