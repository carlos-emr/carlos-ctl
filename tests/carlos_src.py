# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""Where the application this CLI administers is checked out, if anywhere.

Some tests pin carlos-ctl to carlos-emr/carlos itself: the login-name
rule the importer mirrors, the Flyway migrations db-baseline adopts, the
packaging that ships the o19 fixup scripts, the runbook the man page
must agree with. They read that checkout through CARLOS_SRC and skip
without it; carlos's CI runs this suite with CARLOS_SRC set, against the
pinned carlos-ctl release, which is what makes them contract tests
rather than a reason for the two repositories to share a tree.
"""

import os
import unittest

#: the carlos checkout, or None
CARLOS_SRC = os.environ.get("CARLOS_SRC") or None
if CARLOS_SRC and not os.path.isdir(CARLOS_SRC):
    raise RuntimeError("CARLOS_SRC is set but is not a directory: "
                       + CARLOS_SRC)

#: decorator: run only with a carlos checkout at hand
requires_carlos_src = unittest.skipUnless(
    CARLOS_SRC, "set CARLOS_SRC to a carlos-emr/carlos checkout")


def carlos_path(*parts):
    """A path inside the carlos checkout. Without CARLOS_SRC it is a path
    that does not exist, so `os.path.isdir` / `Path.is_file` guards read
    as "not in this checkout" exactly as they did when the tests lived
    in the carlos tree."""
    root = CARLOS_SRC or "/nonexistent/carlos-emr-carlos-checkout"
    return os.path.join(root, *parts)
