# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""Classify the actual checkout's one-live Consent feature, failing closed.

This only conditions tests for independently versioned application releases.
Partial or unreadable feature evidence is an error, never a reason to skip.
"""

import ast
import json
from pathlib import Path
import re


class ConsentFeatureError(ValueError):
    """The checkout cannot demonstrate a complete present or absent feature."""


def _read(path):
    return path.read_text(encoding="utf-8")


def _expressions(value, label):
    if (not isinstance(value, dict) or not {"deleted", "optout"} <= set(value)
            or any(not isinstance(k, str) or not isinstance(v, str) or not v.strip()
                   for k, v in value.items())
            or "Consent__live" not in value["deleted"]
            or "{archive}" not in value["deleted"]):
        raise ConsentFeatureError(label + ": incomplete Consent ranking expressions")
    return value


def _overlay(root):
    path = root / "scripts/migration/o19/overrides_schema.py"
    module = ast.parse(_read(path), filename=str(path))
    assignments = []
    for node in ast.walk(module):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                base = target
                while isinstance(base, (ast.Subscript, ast.Attribute)):
                    base = base.value
                if isinstance(base, ast.Name) and base.id == "VALUE_EXPRS":
                    if not isinstance(target, ast.Name) or not isinstance(node, ast.Assign):
                        raise ConsentFeatureError("overlay: dynamic VALUE_EXPRS assignment")
                    assignments.append(node)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "VALUE_EXPRS"):
            raise ConsentFeatureError("overlay: dynamic VALUE_EXPRS mutation")
    if (len(assignments) != 1 or assignments[0] not in module.body
            or not isinstance(assignments[0].value, ast.Dict)):
        raise ConsentFeatureError("overlay: one static VALUE_EXPRS dictionary is required")
    names = set()
    consent = None
    found = False
    for key, value in zip(assignments[0].value.keys, assignments[0].value.values):
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            raise ConsentFeatureError("overlay: invalid VALUE_EXPRS key")
        if key.value in names or not isinstance(value, ast.Dict):
            raise ConsentFeatureError("overlay: duplicate key or invalid table expressions")
        names.add(key.value)
        if key.value == "Consent":
            found = True
            consent = ast.literal_eval(value)
    return _expressions(consent, "overlay") if found else None


def _string_list(value, label):
    if (not isinstance(value, list) or not value
            or any(not isinstance(item, str) or not item for item in value)
            or len(value) != len(set(value))):
        raise ConsentFeatureError(label + ": invalid column list")
    return value


def _unique_json_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ConsentFeatureError("manifest: duplicate JSON key " + repr(key))
        value[key] = item
    return value


def _manifest(root, overlay):
    path = root / "debian/assets/o19-manifest/o19map_schema.json"
    model = json.loads(_read(path), object_pairs_hook=_unique_json_keys)
    if (not isinstance(model, dict) or type(model.get("format")) is not int
            or model["format"] != 1 or model.get("kind") != "o19map-schema"):
        raise ConsentFeatureError("manifest: expected format 1 o19map-schema envelope")
    provinces = [model, model["PROFILES"]["bc"]]
    present = []
    for province, data in zip(("ON", "BC"), provinces):
        entry = data["TABLES"]["Consent"]
        if not isinstance(entry, dict) or entry.get("class") != "copy":
            raise ConsentFeatureError(province + ": invalid Consent copy entry")
        columns = _string_list(entry["cols"], province)
        target = _string_list(data["CARLOS_COLUMNS"]["Consent"], province + " target")
        # CARLOS_COLUMNS.deleted alone is a pre-feature baseline column.
        hinted = ("deleted" in columns or "value_exprs" in entry
                  or "live_demographic_no" in target)
        if hinted:
            expressions = _expressions(entry.get("value_exprs"), province)
            if ("deleted" not in columns or "live_demographic_no" not in target
                    or overlay is None or expressions != overlay):
                raise ConsentFeatureError(province + ": partial or inconsistent Consent feature")
        present.append(hinted)
    if present[0] != present[1]:
        raise ConsentFeatureError("ON and BC disagree about the Consent feature")
    return present[0]


def _migration(root):
    directory = root / "database/mysql/migration/common"
    found = sorted(p for p in directory.iterdir()
                   if p.name.endswith("one_live_consent_per_type.sql"))
    if not found:
        return False
    if len(found) != 1:
        raise ConsentFeatureError("migration: expected exactly one one-live Consent migration")
    sql = re.sub(r"/\*.*?\*/|--[^\n]*|#[^\n]*", " ", _read(found[0]), flags=re.S)
    sql = re.sub(r"\s+", " ", sql.replace("`", "")).lower()
    statements = sql.split(";")
    alters = [s for s in statements
              if re.search(r"\balter\s+table\s+(?:\w+\.)?consent\b", s)]
    generated = []
    unique = []
    cols = r"\(\s*live_demographic_no\s*,\s*consent_type_id\s*\)"
    for statement in alters:
        generated.extend(re.findall(
            r"\badd\s+(?:column\s+)?live_demographic_no\s+(?:bigint|int)"
            r"(?:\s*\(\s*\d+\s*\))?(?:\s+unsigned)?\s+"
            r"(?:generated\s+always\s+)?as\s*\((.+?)\)\s*(?:stored|persistent|virtual)\b",
            statement))
        unique.extend(re.findall(
            r"\b(?:unique\s+(?:key|index)\s+uq_consent_live_type|"
            r"constraint\s+uq_consent_live_type\s+unique)\s*" + cols, statement))
    for statement in statements:
        unique.extend(re.findall(
            r"\bcreate\s+unique\s+index\s+uq_consent_live_type\s+on\s+"
            r"(?:\w+\.)?consent\s*" + cols, statement))
    expression = re.sub(r"\s+", "", generated[0]) if len(generated) == 1 else ""
    if (expression not in ("if(deleted=0,demographic_no,null)",
                           "casewhendeleted=0thendemographic_noelsenullend")
            or len(unique) != 1):
        raise ConsentFeatureError("migration: invalid generated live-patient column or unique key")
    return True


def classify(app_checkout):
    """Return feature presence for a checkout or None; reject partial evidence.

    None is the existing unavailable-checkout case. An explicitly supplied bad
    checkout path, unreadable file or malformed marker must keep CI red.
    """
    if app_checkout is None:
        return False
    try:
        root = Path(app_checkout)
        if not root.is_dir():
            raise ConsentFeatureError("application checkout is not a directory")
        overlay = _overlay(root)
        manifest = _manifest(root, overlay)
        migration = _migration(root)
        markers = (overlay is not None, manifest, migration)
        if any(markers) and not all(markers):
            raise ConsentFeatureError("partial one-live Consent feature across checkout artifacts")
        return all(markers)
    except ConsentFeatureError:
        raise
    except (OSError, UnicodeError, SyntaxError, ValueError, TypeError, KeyError,
            AttributeError) as error:
        raise ConsentFeatureError("cannot classify Consent feature: " + str(error)) from error
