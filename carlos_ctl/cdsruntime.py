# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""Shared installed CDS process transport; no clinical interpretation or saved credentials."""

from dataclasses import dataclass
import grp
import hashlib
import json
import os
import pwd
import re
import selectors
import signal
import stat
import struct
import subprocess
import sys
import time
from typing import List, Mapping, Sequence
import uuid
from . import dbops, util

class CdsCommandError(Exception):
    """An operator-safe message and process outcome; never raw source detail."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def operator() -> tuple[str, int]:
    """Resolve the invoking OS identity; Java still requires explicit delegation."""
    try:
        sudo_uid = os.environ.get("SUDO_UID")
        if sudo_uid is not None:
            if not re.fullmatch(r"[0-9]{1,10}", sudo_uid, flags=re.ASCII):
                raise ValueError("invalid sudo identity")
            uid = int(sudo_uid)
        else:
            uid = os.getuid()
        name = pwd.getpwuid(uid).pw_name
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\$?", name, flags=re.ASCII):
            raise ValueError("unsupported operator name")
        if sudo_uid is not None and os.environ.get("SUDO_USER") not in (None, name):
            raise ValueError("inconsistent sudo identity")
        return name, uid
    except (KeyError, ValueError, OverflowError):
        raise CdsCommandError("CDS_OPERATOR: cannot verify the invoking OS identity", 3) from None


def environment(operator: str, uid: int) -> dict[str, str]:
    # Deliberately omit inherited JVM options, classpath and arbitrary CARLOS
    # overrides. The selected production file and approved operator are explicit.
    # No database password is parsed or copied into command-line arguments.
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
           "CARLOS_CDS_PROPERTIES": util.PROPERTIES, "CARLOS_CDS_OPERATOR": operator,
           "CARLOS_CDS_OPERATOR_UID": str(uid)}
    # Preserve the platform's network/CA path. These are not JVM executable
    # options; the engine must still disable external XML/schema resolution.
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
                "SSL_CERT_FILE", "SSL_CERT_DIR"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def arguments_digest(arguments: Sequence[str]) -> str:
    """Wire protocol: SHA-256 of four-byte BE length + UTF-8 bytes per argument."""
    digest = hashlib.sha256()
    for argument in arguments:
        encoded = argument.encode("utf-8")
        digest.update(struct.pack(">I", len(encoded)))
        digest.update(encoded)
    return digest.hexdigest()


@dataclass(frozen=True)
class Ticket:
    path: str
    directory_fd: int
    name: str
    inode: int
    device: int

    def remove(self) -> bool:
        try:
            try:
                entry = os.stat(self.name, dir_fd=self.directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                # The root-owned expiry cleanup may already have retired it.
                return True
            if not stat.S_ISREG(entry.st_mode) or entry.st_uid != 0 \
                    or (entry.st_dev, entry.st_ino) != (self.device, self.inode):
                return False
            os.unlink(self.name, dir_fd=self.directory_fd)
            return True
        except OSError:
            return False
        finally:
            os.close(self.directory_fd)


#: Seconds past a ticket's lifetime before the sweep removes it; the Java verifier refuses it
#: long before.
TICKET_SWEEP_GRACE_SECONDS = 300
#: Attestations are issued by root; the sweep never removes a file another user owns.
TICKET_OWNER_UID = 0
_TICKET_NAME = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.json")


def sweep_expired_tickets(directory_fd: int, older_than: int) -> int:
    """Remove root-owned attestation files older than ``older_than`` seconds.

    Only regular, root-owned files named like a ticket are considered; anything else in the
    directory is left alone. Failures are ignored: the sweep is housekeeping, and the Java
    verifier independently refuses expired tickets.
    """
    removed = 0
    cutoff = time.time() - older_than
    try:
        names = os.listdir(directory_fd)
    except OSError:
        return 0
    for name in names:
        if not _TICKET_NAME.fullmatch(name):
            continue
        try:
            entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISREG(entry.st_mode) or entry.st_uid != TICKET_OWNER_UID \
                    or entry.st_mtime >= cutoff:
                continue
            os.unlink(name, dir_fd=directory_fd)
            removed += 1
        except OSError:
            continue
    return removed


def create_ticket(operator: str, uid: int, provider: str, operation: str,
                  arguments: Sequence[str], *, parent: str, directory: str, service_user: str,
                  protocol: int, lifetime: int) -> Ticket:
    """Attest the exact request in a root-controlled, service-readable file.

    The Java entry point verifies ownership, containment, expiry and argv hash
    independently. Merely exporting an operator/provider is never sufficient.
    """
    directory_name = directory
    parent_fd = directory_fd = file_fd = None
    name = None
    created_file = False
    try:
        gid = grp.getgrnam(service_user).gr_gid
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        parent_metadata = os.fstat(parent_fd)
        if parent_metadata.st_uid != 0 or parent_metadata.st_mode & 0o022:
            raise CdsCommandError("CDS_DELEGATION: unsafe delegation parent directory", 2)
        created = False
        try:
            os.mkdir(directory_name, mode=0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            # Concurrent/earlier invocation: validate the existing directory.
            pass
        directory_fd = os.open(directory_name,
                               os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                               dir_fd=parent_fd)
        if created:
            os.fchown(directory_fd, 0, gid)
            os.fchmod(directory_fd, 0o750)
        directory = os.fstat(directory_fd)
        if directory.st_uid != 0 or directory.st_gid != gid \
                or stat.S_IMODE(directory.st_mode) != 0o750:
            raise CdsCommandError("CDS_DELEGATION: unsafe delegation directory ownership or permissions", 2)
        # Retire attestations left by a killed supervisor before issuing a new one, rather than
        # relying only on the tmpfiles timer: an expired ticket authorizes nothing, but it should
        # not accumulate.
        sweep_expired_tickets(directory_fd, max(lifetime, 0) + TICKET_SWEEP_GRACE_SECONDS)
        nonce = str(uuid.uuid4())
        name = nonce + ".json"
        file_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                          0o600, dir_fd=directory_fd)
        created_file = True
        issued = int(time.time())
        payload = json.dumps({
            "protocol": protocol, "operator": operator, "operatorUid": uid,
            "provider": provider, "operation": operation,
            "argvSha256": arguments_digest(arguments), "nonce": nonce,
            "issuedEpochSeconds": issued, "expiresEpochSeconds": issued + lifetime,
        }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        os.fchown(file_fd, 0, gid)
        # write(2) may be short even for this small bounded attestation.
        view = memoryview(payload)
        while view:
            written = os.write(file_fd, view)
            if written <= 0:
                raise OSError("short delegation write")
            view = view[written:]
        os.fchmod(file_fd, 0o640)
        os.fsync(file_fd)
        entry = os.fstat(file_fd)
        os.close(file_fd)
        file_fd = None
        ticket = Ticket(os.path.join(parent, directory_name, name),
                         directory_fd, name, entry.st_ino, entry.st_dev)
        directory_fd = None  # ownership passes to the returned ticket
        return ticket
    except (OSError, KeyError, ValueError):
        raise CdsCommandError("CDS_DELEGATION: cannot create a trusted operator attestation", 2) from None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            if name is not None and created_file:
                try:
                    os.unlink(name, dir_fd=directory_fd)
                except FileNotFoundError:
                    # Failed creation may not have published a file at all.
                    pass
                except OSError:
                    # Preserve the primary creation error; root-owned TTL
                    # cleanup can retire this failed, expiring attestation.
                    print("carlos-ctl: CDS_DELEGATION_CLEANUP: failed attestation retained for expiry cleanup",
                          file=sys.stderr)
            os.close(directory_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def java_command(main_class: str, runuser: str, service_user: str) -> List[str]:
    if not os.path.isdir(os.path.join(util.WEBAPP, "WEB-INF", "classes")) or not os.path.isdir(
            os.path.join(util.WEBAPP, "WEB-INF", "lib")):
        raise CdsCommandError("CDS_DEPLOYMENT: install a compatible CARLOS EMR package first", 2)
    if not os.path.isfile(util.PROPERTIES):
        raise CdsCommandError("CDS_CONFIGURATION: production carlos.properties is required", 2)
    try:
        java = dbops._find_java()
    except SystemExit:
        raise CdsCommandError("CDS_JAVA: install the supported Java 25 runtime", 2) from None
    classpath = (f"{util.WEBAPP}/WEB-INF/classes:{util.WEBAPP}/WEB-INF/lib/*:"
                 "/usr/share/tomcat11/lib/*")
    return [runuser, "-u", service_user, "--", java, "-Xmx128m",
            "-Djava.awt.headless=true", "-cp", classpath, main_class]


def stop_probe(process: subprocess.Popen) -> bool:
    """Reap the probe and terminate its entire private process group."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        # The whole group is already gone; wait still reaps our direct child.
        try:
            process.wait(timeout=2)
            return True
        except subprocess.TimeoutExpired:
            return False
    except OSError:
        return False
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        # Escalate below, including children which ignored the first signal.
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        # The group exited before escalation; this is the normal completed case.
        pass
    except OSError:
        return False
    try:
        process.wait(timeout=2)
        return True
    except subprocess.TimeoutExpired:
        return False


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate capability field")
        result[key] = value
    return result


def probe(command: Sequence[str], env: Mapping[str, str], *, operations,
          protocol: int, minimum_heap: int, maximum_heap: int, maximum_bytes: int,
          timeout: float, engine: str = None, stop=None) -> dict:
    """Read bounded handshake pipes with a deadline; never capture an import."""
    stop = stop_probe if stop is None else stop
    process = None
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(list(command) + ["--capabilities"], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=dict(env), start_new_session=True)
        with selectors.DefaultSelector() as selector:
            for name in ("stdout", "stderr"):
                selector.register(getattr(process, name), selectors.EVENT_READ, name)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CdsCommandError("CDS_HANDSHAKE: engine capability probe timed out", 2)
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    data = buffers[key.data]
                    if len(data) + len(chunk) > maximum_bytes:
                        raise CdsCommandError("CDS_HANDSHAKE: engine capability response exceeded its limit", 2)
                    data.extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or process.wait(timeout=remaining) != 0:
            raise CdsCommandError("CDS_HANDSHAKE: compatible CDS engine is unavailable", 2)
        try:
            result = json.loads(buffers["stdout"].decode("utf-8"), object_pairs_hook=unique_object)
        except (ValueError, UnicodeError, RecursionError):
            raise CdsCommandError("CDS_HANDSHAKE: engine capability response is invalid", 2) from None
        advertised_operations = result.get("operations") if isinstance(result, dict) else None
        if not isinstance(result, dict) or type(result.get("protocol")) is not int \
                or result["protocol"] != protocol or type(result.get("java")) is not int \
                or result["java"] != 25 or not isinstance(advertised_operations, list) \
                or not all(isinstance(item, str) for item in advertised_operations) \
                or not operations.issubset(advertised_operations) \
                or type(result.get("cliHeapMiB")) is not int \
                or not minimum_heap <= result["cliHeapMiB"] <= maximum_heap:
            raise CdsCommandError("CDS_HANDSHAKE: install matching CARLOS and carlos-ctl releases", 2)
        if engine is not None and result.get("engine") != engine:
            raise CdsCommandError("CDS_HANDSHAKE: engine identity does not match the requested command", 2)
        return result
    except (OSError, subprocess.TimeoutExpired):
        raise CdsCommandError("CDS_HANDSHAKE: cannot start or finish the engine capability probe", 2) from None
    finally:
        if process is not None:
            primary_error = sys.exc_info()[0] is not None
            cleanup_ok = stop(process)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            if not cleanup_ok:
                if primary_error:
                    print("carlos-ctl: CDS_HANDSHAKE_CLEANUP: probe termination could not be confirmed",
                          file=sys.stderr)
                else:
                    raise CdsCommandError("CDS_HANDSHAKE_CLEANUP: probe termination could not be confirmed", 2)


def run_engine(command: Sequence[str], arguments: Sequence[str], env: Mapping[str, str], *, stop=None) -> int:
    """Supervise without capturing import output; retain root ticket cleanup."""
    stop = stop_probe if stop is None else stop
    process = None
    requested_signal = None
    old_handlers = {}

    def forward(signum, frame):
        nonlocal requested_signal
        repeat = requested_signal is not None
        requested_signal = signum
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL if repeat else signum)
            except ProcessLookupError:
                # The engine finished concurrently; its durable result remains authoritative.
                pass

    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
            old_handlers[signum] = signal.signal(signum, forward)
        if requested_signal is not None:
            return 128 + requested_signal
        process = subprocess.Popen(list(command) + list(arguments), env=dict(env),
                                   start_new_session=True)
        if requested_signal is not None:
            try:
                os.killpg(process.pid, requested_signal)
            except ProcessLookupError:
                # Cancellation raced process completion; preserve its recorded outcome.
                pass
        rc = process.wait()
        if rc < 0:
            return 128 - rc
        return 128 + requested_signal if requested_signal is not None and rc == 0 else rc
    finally:
        if process is not None and process.poll() is None:
            stop(process)
        for signum, old_handler in old_handlers.items():
            signal.signal(signum, old_handler)
