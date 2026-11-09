# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""Installed command adapter for the shared durable CDS export engine.

No clinical data mapping, database credentials, filesystem publication or permission
substitution belongs here. The Java engine freezes the request and owns execution.
"""

import argparse
import os
import re
import sys
from typing import Sequence
from . import cdsruntime

MAIN_CLASS = "io.github.carlos_emr.carlos.demographic.cdsexport.CdsExport"
PROTOCOL = 1
OPERATIONS = frozenset({"export", "status", "resume", "cancel", "report", "download"})
PROFILES = ("omd-5.2", "pcon-2024-02")
CdsCommandError = cdsruntime.CdsCommandError
TICKET_PARENT = "/run"
TICKET_DIRECTORY = "carlos-cds-export-delegations"
SERVICE_USER = "carlos"


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CdsCommandError("CDS_ARGUMENTS: invalid, duplicate or conflicting export arguments; "
                              "run 'carlos-ctl cds-export --help'")


class _Once(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        provided = getattr(namespace, "_provided", set())
        if self.dest in provided:
            parser.error("duplicate option")
        provided.add(self.dest)
        namespace._provided = provided
        setattr(namespace, self.dest, self.const if self.nargs == 0 else values)


def _identifier(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value, flags=re.ASCII):
        raise argparse.ArgumentTypeError("invalid identifier")
    return value


def _provider(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,16}", value, flags=re.ASCII):
        raise argparse.ArgumentTypeError("invalid provider")
    return value


def _registry_key(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value, flags=re.ASCII):
        raise argparse.ArgumentTypeError("invalid registered key")
    return value


def _physicians(value):
    items = value.split(",")
    if not 1 <= len(items) <= 100 or len(set(items)) != len(items):
        raise argparse.ArgumentTypeError("invalid physician cohort")
    return tuple(sorted(_provider(item) for item in items))


def _categories(value):
    known = {"Demographics", "PersonalHistory", "FamilyHistory", "PastHealth", "ProblemList", "RiskFactors",
             "AllergiesAndAdverseReactions", "MedicationsAndTreatments", "Immunizations", "LaboratoryResults",
             "Appointments", "ClinicalNotes", "Reports", "CareElements", "AlertsAndSpecialNeeds", "NewCategory"}
    if value == "all":
        return "all"
    items = value.split(",")
    if not set(items) <= known or len(set(items)) != len(items) or "Demographics" not in items:
        raise argparse.ArgumentTypeError("invalid categories")
    return ",".join(sorted(items))


def _facility(value):
    if not re.fullmatch(r"[0-9]{1,10}", value, flags=re.ASCII) or not 1 <= int(value) <= 2147483647:
        raise argparse.ArgumentTypeError("invalid facility")
    return str(int(value))


def parser():
    ap = _Parser(prog="carlos-ctl cds-export", allow_abbrev=False,
                 description="Export OntarioMD CDS through the same durable CARLOS jobs as the browser.",
                 epilog="Exit: 0 completed export or successful observation/control; 1 arguments; "
                        "2 configuration/runtime; 3 denied; 4 review/failed/incomplete; 130 interrupted. "
                        "Stopping a foreground wait does not cancel a job. Detached acceptance and status "
                        "success do not mean that an export package is complete.")
    ap.add_argument("--provider", action=_Once, type=_provider, required=True, metavar="ID",
                    help="active provider approved for the invoking operator")
    ap.add_argument("--facility", action=_Once, type=_facility, metavar="ID")
    selection = ap.add_mutually_exclusive_group()
    selection.add_argument("--physicians", action=_Once, type=_physicians, metavar="ID[,ID]",
                           help="discover the primary-provider (MRP) roster, including every patient status")
    selection.add_argument("--patients-file", action=_Once, metavar="PATH",
                           help="protected UTF-8 JSON patient selection file within the configured selection root")
    ap.add_argument("--profile", action=_Once, choices=PROFILES,
                    help="explicit profile; the installed engine must advertise it as qualified")
    ap.add_argument("--categories", action=_Once, type=_categories, default="all", metavar="all|LIST",
                    help="schema category names, including Demographics; default all")
    ap.add_argument("--recipient", action=_Once, type=_registry_key, metavar="KEY",
                    help="registered encryption recipient; no private key or password on the command line")
    ap.add_argument("--audit-plan", action=_Once, type=_registry_key, metavar="KEY",
                    help="registered, verified access arrangement for complete historical audit information")
    ap.add_argument("--submission-key", action=_Once, type=_identifier, metavar="KEY",
                    help="replay-safe identity for a new request; Java prints one before acceptance if absent")
    ap.add_argument("--detach", action=_Once, nargs=0, const=True, default=False,
                    help="durably submit or resume and return; status truthfully reports waiting when no worker is available")
    modes = ap.add_mutually_exclusive_group()
    for operation in ("status", "resume", "cancel", "report", "download"):
        modes.add_argument("--" + operation, action=_Once, type=_identifier, metavar="RUN_ID")
    modes.add_argument("--status-submission", action=_Once, type=_identifier, metavar="KEY",
                       help="recover the accepted handle after a lost acknowledgement")
    ap.add_argument("--artifact", action=_Once, type=_identifier, metavar="ID",
                    help="encrypted artifact identifier (use 'package' with the current engine), for download only")
    ap.add_argument("--output", action=_Once, metavar="PATH",
                    help="new private destination within the configured delivery root, for download only")
    return ap


def parse_args(argv: Sequence[str]):
    ap = parser()
    args = ap.parse_args(list(argv))
    existing = [name for name in OPERATIONS - {"export"} if getattr(args, name)]
    args.operation = "status" if args.status_submission else (existing[0] if existing else "export")
    args.run_id = getattr(args, existing[0]) if existing else None
    provided = getattr(args, "_provided", set())
    if args.operation == "export":
        if not (args.physicians or args.patients_file) or not args.profile or not args.recipient or not args.audit_plan:
            ap.error("new exports require selection, profile, recipient and audit plan")
        if args.artifact or args.output:
            ap.error("download-only options")
    else:
        allowed = {"provider", "status_submission" if args.status_submission else args.operation}
        if args.operation == "resume":
            allowed.add("detach")
        if args.operation == "download":
            allowed.update({"artifact", "output"})
            if not args.artifact or not args.output:
                ap.error("download requires artifact and output")
        if provided - allowed:
            ap.error("existing jobs cannot change accepted intent")
    for field in ("patients_file", "output"):
        value = getattr(args, field)
        if value is not None:
            if not value or len(value) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in value):
                ap.error("invalid path")
            setattr(args, field, os.path.abspath(value))
    return args


def engine_arguments(args):
    argv = ["--operation", args.operation, "--provider", args.provider]
    if args.status_submission:
        return argv + ["--submission-key", args.status_submission]
    if args.operation != "export":
        argv += ["--run", args.run_id]
        if args.operation == "download":
            argv += ["--artifact", args.artifact, "--output", args.output]
    else:
        argv += ["--profile", args.profile, "--categories", args.categories,
                 "--recipient", args.recipient, "--audit-plan", args.audit_plan]
        if args.physicians:
            argv += ["--physicians", ",".join(args.physicians)]
        else:
            argv += ["--patients-file", args.patients_file]
        if args.facility:
            argv += ["--facility", args.facility]
        if args.submission_key:
            argv += ["--submission-key", args.submission_key]
    if args.detach:
        argv.append("--detach")
    return argv


def _probe(command, env):
    return cdsruntime.probe(command, env, operations=OPERATIONS, protocol=PROTOCOL,
                            minimum_heap=128, maximum_heap=16384, maximum_bytes=65536, timeout=30,
                            engine="cds-export")


def cmd_cds_export(argv: Sequence[str]) -> int:
    ticket = None
    result = 2
    try:
        args = parse_args(argv)
        if os.geteuid() != 0:
            raise CdsCommandError("CDS_AUTHORIZATION: use sudo carlos-ctl cds-export", 3)
        operator, uid = cdsruntime.operator()
        command = cdsruntime.java_command(MAIN_CLASS, "/usr/sbin/runuser", SERVICE_USER)
        env = cdsruntime.environment(operator, uid)
        capabilities = _probe(command, env)
        if args.operation == "export" and (not isinstance(capabilities.get("qualifiedProfiles"), list)
                                          or args.profile not in capabilities["qualifiedProfiles"]):
            raise CdsCommandError("CDS_HANDSHAKE: the requested export profile is not qualified by the installed engine", 2)
        if args.status_submission and capabilities.get("submissionLookup") is not True:
            raise CdsCommandError("CDS_HANDSHAKE: submission lookup requires a matching CARLOS release", 2)
        if args.detach and capabilities.get("detachedSubmission") is not True:
            raise CdsCommandError("CDS_HANDSHAKE: durable detached submission requires a matching CARLOS release", 2)
        command = ["-Xmx" + str(capabilities["cliHeapMiB"]) + "m" if part == "-Xmx128m" else part for part in command]
        arguments = engine_arguments(args)
        ticket = cdsruntime.create_ticket(operator, uid, args.provider, args.operation, arguments,
                                          parent=TICKET_PARENT, directory=TICKET_DIRECTORY,
                                          service_user=SERVICE_USER, protocol=PROTOCOL, lifetime=300)
        env["CARLOS_CDS_DELEGATION_TICKET"] = ticket.path
        result = cdsruntime.run_engine(command, arguments, env)
    except CdsCommandError as exc:
        print("carlos-ctl: " + str(exc), file=sys.stderr)
        result = exc.code
    except OSError:
        print("carlos-ctl: CDS_RUNTIME: export supervision failed; inspect the durable run before retrying", file=sys.stderr)
        result = 2
    except KeyboardInterrupt:
        result = 130
    finally:
        if ticket is not None and not ticket.remove():
            print("carlos-ctl: CDS_DELEGATION_CLEANUP: export attestation cleanup failed; inspect the recorded run", file=sys.stderr)
            if result == 0:
                result = 2
    return result
