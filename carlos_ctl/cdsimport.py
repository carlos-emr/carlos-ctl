# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""CDS command adapter for the installed CARLOS import engine.

Only argument validation, production runtime selection and the bounded protocol
handshake live here. Clinical interpretation, authorization, source containment,
durable jobs and recovery belong to CARLOS, shared with its browser workflow.
"""

import argparse
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

MAIN_CLASS = "io.github.carlos_emr.carlos.demographic.cdsimport.CdsImport"
RUNUSER = "/usr/sbin/runuser"
SERVICE_USER = "carlos"
PROTOCOL = 1
CAPABILITY_BYTES = 64 * 1024
CAPABILITY_TIMEOUT = 30.0
OPERATIONS = frozenset({"import", "status", "resume", "cancel", "report"})
MIN_HEAP_MIB = 128
MAX_HEAP_MIB = 16 * 1024
TICKET_PARENT = "/run"
TICKET_DIRECTORY = "carlos-cds-delegations"
TICKET_LIFETIME = 300


class CdsCommandError(Exception):
    """An operator-safe message and process outcome; never raw source detail."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default message echoes values, including PHI-bearing source
        # paths and unknown arguments. The engine's protected diagnostics carry
        # source context; the console only names the safe argument contract.
        raise CdsCommandError("CDS_ARGUMENTS: invalid, duplicate or conflicting arguments; "
                              "run 'carlos-ctl cds-import --help'")


class _Once(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        provided = getattr(namespace, "_provided", set())
        if self.dest in provided:
            parser.error("duplicate option")
        provided.add(self.dest)
        namespace._provided = provided
        setattr(namespace, self.dest, self.const if self.nargs == 0 else values)


def _identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value, flags=re.ASCII):
        raise argparse.ArgumentTypeError("invalid identifier")
    return value


def _number(value: str) -> str:
    if not re.fullmatch(r"[0-9]{1,20}", value, flags=re.ASCII):
        raise argparse.ArgumentTypeError("invalid number")
    return value


def _provider(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,16}", value, flags=re.ASCII):
        raise argparse.ArgumentTypeError("invalid provider key")
    return value


def _shift(value: str) -> int:
    if not re.fullmatch(r"-?[0-9]{1,10}", value, flags=re.ASCII):
        raise argparse.ArgumentTypeError("invalid shift")
    result = int(value)
    if not -(2**31) <= result < 2**31:
        raise argparse.ArgumentTypeError("shift outside supported integer range")
    return result


def parser() -> argparse.ArgumentParser:
    ap = _Parser(
        prog="carlos-ctl cds-import",
        description=("Import OntarioMD CDS data through the same durable CARLOS engine "
                     "as the browser. Merge is off by default. Existing job operations "
                     "use their recorded source/options; they never resubmit the input."),
        allow_abbrev=False,
        epilog=("Exit: 0 clean import/no-op/dry-run (or successful status/report command); "
                "1 arguments; 2 configuration/runtime; 3 denied; 4 refused/busy/failed/"
                "partial/uncertain; 130 interrupted. Status/report success does not mean "
                "the recorded import is complete. A nonzero result can coexist with "
                "committed clinical data; resume/reconcile by run ID."),
    )
    ap.add_argument("--input", action=_Once, metavar="PATH",
                    help="source XML, ZIP or directory within the configured allowed root")
    ap.add_argument("--provider", action=_Once, type=_provider, required=True, metavar="ID",
                    help="active CARLOS provider approved for this operator's delegation")
    ap.add_argument("--program", action=_Once, type=_number, metavar="ID")
    ap.add_argument("--facility", action=_Once, type=_number, metavar="ID")
    ap.add_argument("--source-namespace", action=_Once, type=_identifier, metavar="KEY",
                    help="registered source instance; required for a new import")
    ap.add_argument("--source-profile", action=_Once, type=_identifier, metavar="KEY",
                    help="approved source contract profile; required for a new import")
    for flag, help_text in (
        ("--merge-existing", "opt in to a unique eligible HIN + exact DOB chart merge"),
        ("--dry-run", "preflight without clinical writes"),
        ("--no-match-providers", "retain source provider identities without name matching"),
        ("--synthetic", "authorized synthetic/anonymized mode, never real-chart merge"),
    ):
        ap.add_argument(flag, action=_Once, nargs=0, const=True, default=False, help=help_text)
    ap.add_argument("--timeshift", action=_Once, type=_shift, default=0, metavar="DAYS",
                    help="synthetic-mode date shift; default zero")
    ap.add_argument("--course", action=_Once, type=_number, metavar="ID",
                    help="authorized synthetic student/program replication")
    modes = ap.add_mutually_exclusive_group()
    for operation in ("status", "resume", "cancel", "report"):
        modes.add_argument("--" + operation, action=_Once, type=_identifier,
                           metavar="RUN_ID", help=operation + " an existing durable run")
    modes.add_argument("--status-submission", action=_Once, type=_identifier, metavar="KEY",
                       help="recover a run handle from its printed submission key without resubmitting")
    return ap


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    ap = parser()
    args = ap.parse_args(list(argv))
    selected = [name for name in OPERATIONS - {"import"} if getattr(args, name)]
    args.operation = "status" if args.status_submission else (selected[0] if selected else "import")
    args.run_id = getattr(args, selected[0]) if selected else None
    provided = getattr(args, "_provided", set())
    if args.operation != "import":
        if provided - {"provider", "status_submission" if args.status_submission else args.operation}:
            ap.error("existing jobs cannot change input or options")
        return args
    if not args.input or not args.source_namespace or not args.source_profile:
        ap.error("input, namespace and profile are required")
    if any(ord(char) < 32 or ord(char) == 127 for char in args.input):
        ap.error("invalid source path")
    # Make the caller's relative path explicit before runuser changes identity.
    # This does not dereference symlinks or replace Java's allowed-root checks.
    args.input = os.path.abspath(args.input)
    if (args.course is not None or args.timeshift != 0) and not args.synthetic:
        ap.error("course and nonzero shift require synthetic mode")
    if args.merge_existing and args.synthetic:
        ap.error("synthetic import cannot merge real charts")
    return args


def engine_arguments(args: argparse.Namespace) -> List[str]:
    result = ["--operation", args.operation, "--provider", args.provider]
    if args.status_submission:
        return result + ["--submission-key", args.status_submission]
    if args.operation != "import":
        return result + ["--run", args.run_id]
    result += ["--input", args.input, "--source-namespace", args.source_namespace,
               "--source-profile", args.source_profile]
    for name in ("program", "facility", "course"):
        value = getattr(args, name)
        if value is not None:
            result += ["--" + name, value]
    for name in ("merge_existing", "dry_run", "no_match_providers", "synthetic"):
        if getattr(args, name):
            result.append("--" + name.replace("_", "-"))
    if args.timeshift != 0:
        result += ["--timeshift", str(args.timeshift)]
    return result


def _operator() -> tuple[str, int]:
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


def _environment(operator: str, uid: int) -> dict[str, str]:
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
class _Ticket:
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


def _create_ticket(operator: str, uid: int, args: argparse.Namespace,
                   arguments: Sequence[str]) -> _Ticket:
    """Attest the exact request in a root-controlled, service-readable file.

    The Java entry point verifies ownership, containment, expiry and argv hash
    independently. Merely exporting an operator/provider is never sufficient.
    """
    parent_fd = directory_fd = file_fd = None
    name = None
    created_file = False
    try:
        gid = grp.getgrnam(SERVICE_USER).gr_gid
        parent_fd = os.open(TICKET_PARENT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        parent = os.fstat(parent_fd)
        if parent.st_uid != 0 or parent.st_mode & 0o022:
            raise CdsCommandError("CDS_DELEGATION: unsafe delegation parent directory", 2)
        created = False
        try:
            os.mkdir(TICKET_DIRECTORY, mode=0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            # Concurrent/earlier invocation: validate the existing directory.
            pass
        directory_fd = os.open(TICKET_DIRECTORY,
                               os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                               dir_fd=parent_fd)
        if created:
            os.fchown(directory_fd, 0, gid)
            os.fchmod(directory_fd, 0o750)
        directory = os.fstat(directory_fd)
        if directory.st_uid != 0 or directory.st_gid != gid \
                or stat.S_IMODE(directory.st_mode) != 0o750:
            raise CdsCommandError("CDS_DELEGATION: unsafe delegation directory ownership or permissions", 2)
        nonce = str(uuid.uuid4())
        name = nonce + ".json"
        file_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                          0o600, dir_fd=directory_fd)
        created_file = True
        issued = int(time.time())
        payload = json.dumps({
            "protocol": PROTOCOL, "operator": operator, "operatorUid": uid,
            "provider": args.provider, "operation": args.operation,
            "argvSha256": arguments_digest(arguments), "nonce": nonce,
            "issuedEpochSeconds": issued, "expiresEpochSeconds": issued + TICKET_LIFETIME,
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
        ticket = _Ticket(os.path.join(TICKET_PARENT, TICKET_DIRECTORY, name),
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


def _java_command() -> List[str]:
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
    return [RUNUSER, "-u", SERVICE_USER, "--", java, "-Xmx128m",
            "-Djava.awt.headless=true", "-cp", classpath, MAIN_CLASS]


def _stop_probe(process: subprocess.Popen) -> bool:
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


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate capability field")
        result[key] = value
    return result


def _probe(command: Sequence[str], env: Mapping[str, str]) -> dict:
    """Read bounded handshake pipes with a deadline; never capture an import."""
    process = None
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + CAPABILITY_TIMEOUT
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
                    if len(data) + len(chunk) > CAPABILITY_BYTES:
                        raise CdsCommandError("CDS_HANDSHAKE: engine capability response exceeded its limit", 2)
                    data.extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or process.wait(timeout=remaining) != 0:
            raise CdsCommandError("CDS_HANDSHAKE: compatible CDS engine is unavailable", 2)
        try:
            result = json.loads(buffers["stdout"].decode("utf-8"), object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError, RecursionError):
            raise CdsCommandError("CDS_HANDSHAKE: engine capability response is invalid", 2) from None
        operations = result.get("operations") if isinstance(result, dict) else None
        if not isinstance(result, dict) or type(result.get("protocol")) is not int \
                or result["protocol"] != PROTOCOL or type(result.get("java")) is not int \
                or result["java"] != 25 or not isinstance(operations, list) \
                or not all(isinstance(item, str) for item in operations) \
                or not OPERATIONS.issubset(operations) \
                or type(result.get("cliHeapMiB")) is not int \
                or not MIN_HEAP_MIB <= result["cliHeapMiB"] <= MAX_HEAP_MIB:
            raise CdsCommandError("CDS_HANDSHAKE: install matching CARLOS and carlos-ctl releases", 2)
        return result
    except (OSError, subprocess.TimeoutExpired):
        raise CdsCommandError("CDS_HANDSHAKE: cannot start or finish the engine capability probe", 2) from None
    finally:
        if process is not None:
            primary_error = sys.exc_info()[0] is not None
            cleanup_ok = _stop_probe(process)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            if not cleanup_ok:
                if primary_error:
                    print("carlos-ctl: CDS_HANDSHAKE_CLEANUP: probe termination could not be confirmed",
                          file=sys.stderr)
                else:
                    raise CdsCommandError("CDS_HANDSHAKE_CLEANUP: probe termination could not be confirmed", 2)


def _run_engine(command: Sequence[str], arguments: Sequence[str], env: Mapping[str, str]) -> int:
    """Supervise without capturing import output; retain root ticket cleanup."""
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
            _stop_probe(process)
        for signum, old_handler in old_handlers.items():
            signal.signal(signum, old_handler)


def cmd_cds_import(argv: Sequence[str]) -> int:
    ticket = None
    result = 2
    try:
        args = parse_args(argv)
        if os.geteuid() != 0:
            raise CdsCommandError("CDS_AUTHORIZATION: use sudo carlos-ctl cds-import", 3)
        operator, uid = _operator()
        command = _java_command()
        env = _environment(operator, uid)
        capabilities = _probe(command, env)
        if args.status_submission and capabilities.get("submissionLookup") is not True:
            raise CdsCommandError("CDS_HANDSHAKE: deploy a matching CARLOS release with submission lookup", 2)
        # Java's real Properties loader selects the server launch budget. Do not
        # duplicate its escaping/separator/continuation semantics in Python.
        command = ["-Xmx" + str(capabilities["cliHeapMiB"]) + "m"
                   if part == "-Xmx128m" else part for part in command]
        arguments = engine_arguments(args)
        ticket = _create_ticket(operator, uid, args, arguments)
        env["CARLOS_CDS_DELEGATION_TICKET"] = ticket.path
        result = _run_engine(command, arguments, env)
    except CdsCommandError as exc:
        print("carlos-ctl: " + str(exc), file=sys.stderr)
        result = exc.code
    except OSError:
        print("carlos-ctl: CDS_RUNTIME: engine supervision failed; inspect the durable run before retrying",
              file=sys.stderr)
        result = 2
    except KeyboardInterrupt:
        result = 130
    finally:
        if ticket is not None and not ticket.remove():
            print("carlos-ctl: CDS_DELEGATION_CLEANUP: attestation cleanup failed; "
                  "clinical commits, if any, remain recorded in the durable run", file=sys.stderr)
            if result == 0:
                result = 2
    return result
