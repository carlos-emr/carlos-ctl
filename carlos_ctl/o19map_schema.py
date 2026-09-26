# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""OSCAR 19 -> CARLOS schema manifest, loaded from the carlos-emr package.

Until the package split this module WAS the data (generated into the
carlos-emr tree by scripts/migration/o19/generate_manifests.py). It is now
a loader: the same module-level names, read at import from
/usr/share/carlos-emr/o19-manifest/o19map_schema.json (see o19manifest),
so o19etl, o19roles and the importer are unchanged. Tuples the generator
used to emit arrive as lists; every consumer iterates or unpacks them.

Importing this module with no manifest available raises
o19manifest.ManifestError; the import verbs catch it and print the remedy
(cli._cmd_import_o19). The module is imported lazily by those verbs
only, so `carlos-ctl status` on a host without carlos-emr never trips it.
"""

from . import o19manifest

_DATA = o19manifest.load("o19map-schema")
globals().update({k: v for k, v in _DATA.items() if k != "PROFILES"})

#: every province beyond the default, selected by bind()
PROFILES = _DATA["PROFILES"]

#: the names bind() rebinds -- the PER-PROVINCE part of the manifest --
#: and their module-level values captured BEFORE any bind can run.
#: Without this snapshot bind() would be one-way: there would be no way
#: back to the default profile once another was selected, and a process
#: that binds twice (the test suite does) would carry the first selection
#: into the second. The generator's PROFILE_NAMES is the same list.
_PROFILE_NAMES = [
    'SCHEMA_MAP_VERSION',
    'O19_PROFILE',
    'TABLES',
    'CARLOS_COLUMNS',
    'SEED_ROW_COUNTS',
    'STOCK_ROLE_NAMES',
    'PRIMITIVE_COLUMNS',
    'PRISTINE_TOLERATED_TABLES',
]
_DEFAULT_PROFILE = dict((n, globals()[n]) for n in _PROFILE_NAMES)


def bind(province):
    """Point this module's per-province names at `province`'s profile.

    Returns the profile name now bound. A province this manifest does not
    carry leaves the module unchanged -- the caller's own gate refuses
    it, and refusing is what should happen.
    """
    data = PROFILES.get(province)
    if data is None and province == _DEFAULT_PROFILE['O19_PROFILE']:
        data = _DEFAULT_PROFILE
    if data is not None:
        globals().update(data)
    return O19_PROFILE  # noqa: F821 -- bound by globals().update above
