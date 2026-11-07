# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""Installed root/service-user attestation check for disposable CI containers.

Run after installing the candidate package and creating its synthetic carlos
service identity. This is deliberately not a silently skipped unit test.
"""

import grp
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

from carlos_ctl import cdsimport, cdsexport, cdsruntime


def main():
    if os.geteuid() != 0:
        raise SystemExit("CDS delegation integration check requires the disposable container's root")
    if "--installed" in sys.argv and any(not module.__file__.startswith("/usr/lib/carlos-ctl/")
                                          for module in (cdsimport, cdsexport, cdsruntime)):
        raise AssertionError("CDS delegation check did not import the installed candidate")
    args = cdsimport.parse_args([
        "--input", "/approved/synthetic.xml", "--provider", "999998",
        "--source-namespace", "synthetic-source", "--source-profile", "omd-5.2", "--dry-run",
    ])
    arguments = cdsimport.engine_arguments(args)
    ticket = cdsimport._create_ticket("root", 0, args, arguments)
    check_ticket(ticket, args.provider, args.operation, arguments, cdsimport.TICKET_DIRECTORY)
    args = cdsexport.parse_args([
        "--provider", "999998", "--physicians", "999998", "--profile", "omd-5.2",
        "--recipient", "synthetic-recipient", "--audit-plan", "synthetic-audit", "--detach",
    ])
    arguments = cdsexport.engine_arguments(args)
    ticket = cdsruntime.create_ticket("root", 0, args.provider, args.operation, arguments,
                                      parent=cdsexport.TICKET_PARENT, directory=cdsexport.TICKET_DIRECTORY,
                                      service_user=cdsexport.SERVICE_USER, protocol=cdsexport.PROTOCOL,
                                      lifetime=300)
    check_ticket(ticket, args.provider, args.operation, arguments, cdsexport.TICKET_DIRECTORY)
    assert cdsimport.TICKET_DIRECTORY != cdsexport.TICKET_DIRECTORY
    print("CDS import/export delegation ownership, digest, service read/tamper denial and owned cleanup passed")


def check_ticket(ticket, provider, operation, arguments, directory):
    try:
        path = Path(ticket.path)
        actual = path.stat()
        assert path.parent.name == directory
        assert actual.st_uid == 0
        assert actual.st_gid == grp.getgrnam("carlos").gr_gid
        assert stat.S_IMODE(actual.st_mode) == 0o640
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o750
        body = json.loads(path.read_text())
        assert body["provider"] == provider
        assert body["operation"] == operation
        assert body["argvSha256"] == cdsruntime.arguments_digest(arguments)
        child = """import json,pathlib,sys
p=pathlib.Path(sys.argv[1])
assert json.loads(p.read_text())['provider']=='999998'
for target in (p,p.parent/'forged-integration-ticket.json'):
 try: target.write_text('forged')
 except PermissionError: continue
 raise AssertionError('service account modified a root attestation')
"""
        result = subprocess.run(["/usr/sbin/runuser", "-u", "carlos", "--",
                                 "/usr/bin/python3", "-c", child, str(path)], check=False)
        assert result.returncode == 0
    finally:
        assert ticket.remove()
    assert not path.exists()


if __name__ == "__main__":
    main()
