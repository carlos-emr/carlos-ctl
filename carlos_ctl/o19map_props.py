# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""OSCAR 19 -> CARLOS properties manifest, loaded from the carlos-emr
package (/usr/share/carlos-emr/o19-manifest/o19map_props.json).

A loader with the same module-level names the generated module carried
before the package split; see o19map_schema for the reasoning. PREFIX_RULES
arrives as [prefix, spec] pairs (lists, not tuples).
"""

from . import o19manifest

_DATA = o19manifest.load("o19map-props")
globals().update(_DATA)
