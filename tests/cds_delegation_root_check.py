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

from carlos_ctl import cdsimport


def main():
    if os.geteuid() != 0:
        raise SystemExit("CDS delegation integration check requires the disposable container's root")
    if "--installed" in sys.argv and not cdsimport.__file__.startswith("/usr/lib/carlos-ctl/"):
        raise AssertionError("CDS delegation check did not import the installed candidate")
    args = cdsimport.parse_args([
        "--input", "/approved/synthetic.xml", "--provider", "999998",
        "--source-namespace", "synthetic-source", "--source-profile", "omd-5.2", "--dry-run",
    ])
    ticket = cdsimport._create_ticket("root", 0, args, cdsimport.engine_arguments(args))
    try:
        path = Path(ticket.path)
        actual = path.stat()
        assert actual.st_uid == 0
        assert actual.st_gid == grp.getgrnam("carlos").gr_gid
        assert stat.S_IMODE(actual.st_mode) == 0o640
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o750
        body = json.loads(path.read_text())
        assert body["argvSha256"] == cdsimport.arguments_digest(cdsimport.engine_arguments(args))
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
    print("CDS delegation ownership, digest, service read/tamper denial and owned cleanup passed")


if __name__ == "__main__":
    main()
