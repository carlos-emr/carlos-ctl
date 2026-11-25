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
import tempfile

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
    check_expiry_cleanup("--installed" in sys.argv)
    print("CDS import/export delegation identity, ownership, digest, tamper denial and cleanup passed")


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
        assert body["operator"] == "root"
        assert type(body["operatorUid"]) is int and body["operatorUid"] == 0
        assert type(body["protocol"]) is int and body["protocol"] == 1
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


def check_expiry_cleanup(installed):
    source = (Path("/usr/lib/tmpfiles.d/carlos-ctl.conf") if installed else
              Path(__file__).resolve().parents[1] / "debian/carlos-ctl.tmpfiles")
    configuration = source.read_text()
    directories = (cdsimport.TICKET_DIRECTORY, cdsexport.TICKET_DIRECTORY)
    with tempfile.TemporaryDirectory(prefix="cds-delegation-expiry-") as temporary:
        root = Path(temporary)
        config = root / "delegations.conf"
        config.write_text(configuration)

        def run_tmpfiles(operation):
            subprocess.run(["/usr/bin/systemd-tmpfiles", operation, "--root", str(root), str(config)], check=True)

        run_tmpfiles("--create")
        assert all(not (root / "run" / directory).exists() for directory in directories)
        files = []
        for directory in directories:
            parent = root / "run" / directory
            parent.mkdir(parents=True, mode=0o750)
            ticket = parent / "synthetic-orphan.json"
            ticket.write_text("{}")
            ticket.chmod(0o640)
            files.append(ticket)
        unrelated = root / "run" / "unrelated.json"
        unrelated.write_text("retain")
        run_tmpfiles("--clean")
        assert all(path.exists() for path in files), "Fresh tickets must survive ordinary cleanup"

        # Changing only the test copy's age avoids waiting for the filesystem ctime
        # to age. The installed paths/directives must actually remove old files.
        expired = []
        for line in configuration.splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[1] in {"/run/" + directory for directory in directories}:
                assert len(fields) == 6 and fields[0] == "e" and fields[5] == "10m"
                fields[5] = "0s"
                line = " ".join(fields)
            expired.append(line)
        config.write_text("\n".join(expired) + "\n")
        run_tmpfiles("--clean")
        assert all(not path.exists() and path.parent.is_dir() for path in files), "Orphans must retire, keeping directories"
        assert unrelated.read_text() == "retain", "Expiry cleanup must stay inside delegation directories"
    print("Import/export expiry cleanup retains fresh tickets and removes orphan files without creating identities")


if __name__ == "__main__":
    main()
