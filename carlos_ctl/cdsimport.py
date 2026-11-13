# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""CDS command adapter for the installed CARLOS import engine.

Only argument validation, production runtime selection and the bounded protocol
handshake live here. Clinical interpretation, authorization, source containment,
durable jobs and recovery belong to CARLOS, shared with its browser workflow.
"""

import argparse
import os
import re
import sys
from typing import List, Sequence
from . import cdsruntime, dbops as dbops, util as util

# Retain the established internal testing/integration patch points while sharing the
# actual transport implementation with export. These modules hold no user identity.
grp = cdsruntime.grp
pwd = cdsruntime.pwd
uuid = cdsruntime.uuid
subprocess = cdsruntime.subprocess
signal = cdsruntime.signal
time = cdsruntime.time
CdsCommandError = cdsruntime.CdsCommandError
_Ticket = cdsruntime.Ticket
arguments_digest = cdsruntime.arguments_digest
_stop_probe = cdsruntime.stop_probe
_unique_object = cdsruntime.unique_object

MAIN_CLASS = "io.github.carlos_emr.carlos.demographic.cdsimport.CdsImport"
RUNUSER = "/usr/sbin/runuser"
SERVICE_USER = "carlos"
PROTOCOL = 1
CAPABILITY_BYTES = 64 * 1024
CAPABILITY_TIMEOUT = 30.0
OPERATIONS = frozenset({"import", "status", "resume", "cancel", "report"})
# Advertised by engines that offer the audited abandon. It is required only for an abandon
# request, so this release keeps working with engines that predate it.
ABANDON = "abandon"
MAX_REASON = 1000
MIN_HEAP_MIB = 128
MAX_HEAP_MIB = 16 * 1024
TICKET_PARENT = "/run"
TICKET_DIRECTORY = "carlos-cds-delegations"
TICKET_LIFETIME = 300


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


def _reason(value: str) -> str:
    # The stated reason travels only to the engine's protected record. It is a single
    # bounded line; control and line-separator characters are refused.
    if not value.strip() or len(value) > MAX_REASON or any(
            ord(char) < 32 or 127 <= ord(char) < 160 or char in "\u2028\u2029" for char in value):
        raise argparse.ArgumentTypeError("invalid reason")
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
                "committed clinical data; resume/reconcile by run ID. An abandon always "
                "exits 4: an abandoned import is never complete."),
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
    modes.add_argument("--abandon", action=_Once, nargs=0, const=True, default=False,
                       help=("close a stopped, already cancelled import that cannot complete: its "
                             "held charts are released incomplete, no clinical data changes, and "
                             "the EMR reopens once no other import holds a chart; requires --run, "
                             "--confirm and --reason; always exits 4"))
    ap.add_argument("--run", action=_Once, type=_identifier, metavar="RUN_ID",
                    help="the run to abandon")
    ap.add_argument("--confirm", action=_Once, type=_identifier, metavar="RUN_ID",
                    help="the same run ID again, confirming the abandon")
    ap.add_argument("--reason", action=_Once, type=_reason, metavar="TEXT",
                    help=("why the import is abandoned, one line of at most 1000 characters, "
                          "kept in the run's protected record; never name a patient"))
    return ap


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    ap = parser()
    args = ap.parse_args(list(argv))
    provided = getattr(args, "_provided", set())
    if args.abandon:
        if provided - {"provider", "abandon", "run", "confirm", "reason"} \
                or not (args.run and args.confirm and args.reason):
            ap.error("an abandon takes only --provider, --run, --confirm and --reason")
        if args.confirm != args.run:
            ap.error("the confirmation must repeat the run ID")
        args.operation = ABANDON
        args.run_id = args.run
        return args
    if provided & {"run", "confirm", "reason"}:
        ap.error("--run, --confirm and --reason belong to --abandon")
    selected = [name for name in OPERATIONS - {"import"} if getattr(args, name)]
    args.operation = "status" if args.status_submission else (selected[0] if selected else "import")
    args.run_id = getattr(args, selected[0]) if selected else None
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
    if args.operation == ABANDON:
        return result + ["--run", args.run_id, "--confirm", args.confirm, "--reason", args.reason]
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
    return cdsruntime.operator()


def _environment(operator: str, uid: int) -> dict[str, str]:
    return cdsruntime.environment(operator, uid)


def _create_ticket(operator: str, uid: int, args: argparse.Namespace,
                   arguments: Sequence[str]) -> _Ticket:
    return cdsruntime.create_ticket(operator, uid, args.provider, args.operation, arguments,
                                    parent=TICKET_PARENT, directory=TICKET_DIRECTORY,
                                    service_user=SERVICE_USER, protocol=PROTOCOL, lifetime=TICKET_LIFETIME)


def _java_command() -> List[str]:
    return cdsruntime.java_command(MAIN_CLASS, RUNUSER, SERVICE_USER)


def _probe(command, env) -> dict:
    return cdsruntime.probe(command, env, operations=OPERATIONS, protocol=PROTOCOL,
                            minimum_heap=MIN_HEAP_MIB, maximum_heap=MAX_HEAP_MIB,
                            maximum_bytes=CAPABILITY_BYTES, timeout=CAPABILITY_TIMEOUT, stop=_stop_probe)


def _run_engine(command, arguments, env) -> int:
    return cdsruntime.run_engine(command, arguments, env, stop=_stop_probe)


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
        if args.operation == ABANDON and ABANDON not in capabilities.get("operations", []):
            raise CdsCommandError("CDS_HANDSHAKE: deploy a CARLOS release whose import engine offers --abandon", 2)
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
