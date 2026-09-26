# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""The OSCAR 19 import manifests: shipped by carlos-emr, loaded here.

The manifests describe carlos-emr's schema (which O19 table maps to which
CARLOS table and columns, what the Flyway set seeds, which property keys
CARLOS dropped), so they are generated in the carlos-emr repository beside
the schema -- scripts/migration/o19/generate_manifests.py -- and the
carlos-emr package installs them as JSON under MANIFEST_DIR. This module
reads them; `o19map_schema` and `o19map_props` are the loaders that hand
the data to the ETL under the module-level names it always used, and
`o19_preflight.load_manifest` does the same for the assessment.

Why JSON and not the modules carlos-emr used to ship: the CLI is its own
package now (carlos-emr depends on it), and a schema migration must not
require a new CLI release. Every file carries `"format"`; this loader
refuses a format it does not know, so a CLI too old for a new manifest
shape fails at the first key rather than half-way through a copy.

The standalone assessment script (o19_preflight.py, copied ALONE to the
clinic's server) keeps an inlined copy of the preflight manifest between
its GENERATED DATA markers. `write_standalone_preflight` re-inlines the
installed manifest, so the copy an operator carries matches the schema
the CARLOS host will import into; `render_preflight_block` is a copy of
the generator's renderer and must stay byte-for-byte with it.
"""

import json
import os
from typing import Any, Dict, List, Optional

from .util import SHARE

#: where carlos-emr installs the manifests; CARLOS_CTL_O19_MANIFEST_DIR
#: overrides it (the test suite, and a developer with a source checkout:
#: <carlos>/debian/assets/o19-manifest)
MANIFEST_DIR = os.path.join(SHARE, "o19-manifest")
MANIFEST_DIR_ENV = "CARLOS_CTL_O19_MANIFEST_DIR"

#: the manifest format this CLI reads; carlos-emr's generator writes
#: MANIFEST_FORMAT into every file and bumps it only when a consumer could
#: misread the previous shape
MANIFEST_FORMAT = 1

#: manifest kind -> file name (the kind is written into the file too)
MANIFEST_FILES = {
    "o19map-schema": "o19map_schema.json",
    "o19map-props": "o19map_props.json",
    "o19-preflight": "o19_preflight.json",
}

MARKER_BEGIN = "# === BEGIN GENERATED DATA (generate_manifests.py) ==="
MARKER_END = "# === END GENERATED DATA ==="

#: The names of the preflight block, in emission order, before PROFILES.
#: Same list as the generator's PREFLIGHT_BLOCK_NAMES.
PREFLIGHT_BLOCK_NAMES = (
    "SCHEMA_MAP_VERSION", "O19_PROFILE", "SUPPORTED_PROVINCES",
    "REQUIRED_TABLES", "PATIENT_DATA_TABLES", "KNOWN_TABLES",
    "B3_FLAGGED_COLUMNS", "CHARSET_SCAN", "DROPPED_PROP_PREFIXES",
    "DROPPED_PROP_KEYS", "STOCK_ROLE_NAMES", "LEGACY_PREVENTION_TYPES",
)
#: the preflight names that are PER-PROVINCE (repeated under PROFILES)
PREFLIGHT_PROFILE_NAMES = (
    "SCHEMA_MAP_VERSION", "O19_PROFILE", "PATIENT_DATA_TABLES",
    "KNOWN_TABLES", "B3_FLAGGED_COLUMNS", "CHARSET_SCAN",
    "STOCK_ROLE_NAMES",
)


class ManifestError(Exception):
    """A manifest could not be read: carlos-emr is not installed, the
    file is missing or unreadable, or its format is not this CLI's."""


def manifest_dir() -> str:
    return os.environ.get(MANIFEST_DIR_ENV) or MANIFEST_DIR


def manifest_path(kind: str) -> str:
    return os.path.join(manifest_dir(), MANIFEST_FILES[kind])


def load(kind: str) -> Dict[str, Any]:
    """The manifest of `kind`, checked: the envelope's kind and format
    must be the ones this CLI expects. Returns the data keys only.

    Raises ManifestError with the remedy in the message; callers turn it
    into util.die so the operator sees one line, not a traceback.
    """
    if kind not in MANIFEST_FILES:
        raise ManifestError("unknown manifest kind: {0}".format(kind))
    path = manifest_path(kind)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise ManifestError(
            "OSCAR 19 import manifest not found: {0}. The manifests are "
            "installed by the carlos-emr package (from 2026.09.0~snapshot25); "
            "is carlos-emr installed, and is it at least that version? "
            "(dpkg -l carlos-emr)".format(path))
    except (OSError, ValueError) as exc:
        raise ManifestError("cannot read the OSCAR 19 import manifest "
                            "{0}: {1}".format(path, exc))
    if not isinstance(data, dict):
        raise ManifestError("{0}: not a manifest object".format(path))
    if data.get("kind") != kind:
        raise ManifestError("{0}: kind {1!r}, expected {2!r}".format(
            path, data.get("kind"), kind))
    fmt = data.get("format")
    if fmt != MANIFEST_FORMAT:
        raise ManifestError(
            "{0}: manifest format {1!r}, but this carlos-ctl reads format "
            "{2}. Install the carlos-ctl release that matches the installed "
            "carlos-emr (dpkg -l carlos-emr carlos-ctl).".format(
                path, fmt, MANIFEST_FORMAT))
    return {k: v for k, v in data.items()
            if k not in ("format", "kind", "generator")}


# --- rendering the standalone preflight's data block -----------------------

def _fmt(obj, indent: int = 0, pair_width: Optional[int] = None) -> str:
    """Deterministic, diff-friendly repr wrapped for the 100-col house
    style. `pair_width` additionally puts a dict value on its own line
    when `key: value` would exceed it -- the generated block inside
    o19_preflight.py lives in a hand-written 79-column file.

    A copy of the generator's `_fmt` (scripts/migration/o19/
    generate_manifests.py in carlos-emr/carlos); keep the two identical."""
    pad = "    " * indent
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        lines = ["{"]
        for k in obj:
            value = _fmt(obj[k], indent + 1, pair_width)
            one = "{}    {!r}: {},".format(pad, k, value)
            if (pair_width and len(one) > pair_width
                    and "\n" not in value):
                lines.append("{}    {!r}:".format(pad, k))
                lines.append("{}        {},".format(pad, value))
            else:
                lines.append(one)
        lines.append(pad + "}")
        return "\n".join(lines)
    if isinstance(obj, list):
        if not obj:
            return "[]"
        one = "[" + ", ".join(repr(x) for x in obj) + "]"
        if len(one) + len(pad) <= 96:
            return one
        lines = ["["]
        for x in obj:
            lines.append("{}    {!r},".format(pad, x))
        lines.append(pad + "]")
        return "\n".join(lines)
    return repr(obj)


def render_preflight_block(data: Dict[str, Any]) -> str:
    """Render the marker-delimited data block of o19_preflight.py from
    the preflight manifest data. A copy of the generator's
    `render_preflight_block`; the two must stay byte-for-byte (the
    contract test compares them when a carlos checkout is at hand)."""
    lines: List[str] = [MARKER_BEGIN]
    for name in PREFLIGHT_BLOCK_NAMES:
        width = 79 if name == "B3_FLAGGED_COLUMNS" else None
        lines.append("{0} = {1}".format(
            name, _fmt(data[name], pair_width=width)))
    others = sorted(data.get("PROFILES") or {})
    for province in others:
        body = data["PROFILES"][province]
        lines.append("PROFILE_{0} = {{".format(province.upper()))
        for name in PREFLIGHT_PROFILE_NAMES:
            width = 75 if name == "B3_FLAGGED_COLUMNS" else None
            rendered = _fmt(body[name], indent=1, pair_width=width)
            lines.append("    {0!r}: {1},".format(name, rendered))
        lines.append("}")
    lines.append("PROFILES = {"
                 + ", ".join("{0!r}: PROFILE_{1}".format(p, p.upper())
                             for p in others)
                 + "}")
    lines.append(MARKER_END)
    block = "\n".join(lines)
    for line in block.split("\n"):
        if len(line) > 79:
            raise ManifestError(
                "generated preflight line exceeds 79 columns ({0}): "
                "{1}".format(len(line), line))
    return block


def inline_preflight(source: str, data: Dict[str, Any]) -> str:
    """`source` (the text of o19_preflight.py) with its generated-data
    block replaced by `data` rendered. Missing or malformed markers are
    an error: writing the block anywhere else would corrupt the file."""
    b = source.find(MARKER_BEGIN)
    e = source.find(MARKER_END)
    if b == -1 or e == -1 or e < b:
        raise ManifestError(
            "o19_preflight.py: generated-data markers missing or malformed")
    return (source[:b] + render_preflight_block(data)
            + source[e + len(MARKER_END):])


def write_standalone_preflight(dest: str) -> str:
    """Write a standalone copy of o19_preflight.py to `dest`, carrying the
    INSTALLED carlos-emr's preflight manifest inlined -- the file to copy
    to the clinic's OSCAR 19 server. Returns the manifest version
    inlined. 0644: the file holds schema rulings, no credentials."""
    from . import o19_preflight
    data = load("o19-preflight")
    with open(o19_preflight.__file__, encoding="utf-8") as fh:
        text = inline_preflight(fh.read(), data)
    tmp = dest + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, dest)
    return str(data["SCHEMA_MAP_VERSION"])
