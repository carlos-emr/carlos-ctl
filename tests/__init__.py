# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""Test package for carlos_ctl.

Run from the repository root:
    python3 -m unittest discover -s tests -t .

Two environment variables shape a run:

* CARLOS_SRC -- a checkout of carlos-emr/carlos. Tests that pin this CLI
  to the application it administers (its Flyway migrations, its login
  rule, the generator's overlays, the packaging that ships the o19
  fixups) run when it is set and skip otherwise; carlos's CI sets it.
* CARLOS_CTL_O19_MANIFEST_DIR -- where the OSCAR 19 import manifests are
  read from. Defaulted here to the checkout's manifests when CARLOS_SRC
  is set, else to the snapshot under tests/fixtures/o19-manifest/, so no
  installed carlos-emr is needed.
"""

import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.environ.get("CARLOS_SRC")
if "CARLOS_CTL_O19_MANIFEST_DIR" not in os.environ:
    if _SRC and os.path.isdir(os.path.join(_SRC, "debian", "assets",
                                           "o19-manifest")):
        os.environ["CARLOS_CTL_O19_MANIFEST_DIR"] = os.path.join(
            _SRC, "debian", "assets", "o19-manifest")
    else:
        os.environ["CARLOS_CTL_O19_MANIFEST_DIR"] = os.path.join(
            _HERE, "fixtures", "o19-manifest")
