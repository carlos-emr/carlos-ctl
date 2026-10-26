# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""Physical CARLOS schema contract, independent of Flyway's history check.

Contracts are exported from a freshly migrated reference database and packaged
by carlos-emr. Runtime counters, statistics and archive extensions are excluded;
column/index/constraint definitions are compared, not just their names.
"""
import hashlib
import json
from pathlib import Path

FORMAT = 2

FIELDS = {
    "defaults": ("SCHEMATA", ["SCHEMA_NAME", "DEFAULT_CHARACTER_SET_NAME", "DEFAULT_COLLATION_NAME"], "1=1"),
    "tables": ("TABLES", ["TABLE_NAME", "ENGINE", "ROW_FORMAT", "TABLE_COLLATION"], "TABLE_TYPE = 'BASE TABLE'"),
    "columns": ("COLUMNS", ["TABLE_NAME", "COLUMN_NAME", "COLUMN_TYPE", "IS_NULLABLE", "COLUMN_DEFAULT",
                            "EXTRA", "CHARACTER_SET_NAME", "COLLATION_NAME", "GENERATION_EXPRESSION",
                            "ORDINAL_POSITION"], "1=1"),
    "indexes": ("STATISTICS", ["TABLE_NAME", "INDEX_NAME", "NON_UNIQUE", "SEQ_IN_INDEX", "COLUMN_NAME",
                               "COLLATION", "SUB_PART", "INDEX_TYPE", "IGNORED"], "1=1"),
    "keys": ("KEY_COLUMN_USAGE", ["TABLE_NAME", "CONSTRAINT_NAME", "ORDINAL_POSITION", "COLUMN_NAME",
                                  "REFERENCED_TABLE_SCHEMA", "REFERENCED_TABLE_NAME", "REFERENCED_COLUMN_NAME"], "1=1"),
    "references": ("REFERENTIAL_CONSTRAINTS", ["TABLE_NAME", "CONSTRAINT_NAME", "REFERENCED_TABLE_NAME",
                                             "UPDATE_RULE", "DELETE_RULE"], "1=1"),
    "checks": ("CHECK_CONSTRAINTS", ["TABLE_NAME", "CONSTRAINT_NAME", "CHECK_CLAUSE"], "1=1"),
}


def _literal(value):
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def capture(query, schema):
    metadata = {}
    for kind, (source, columns, predicate) in FIELDS.items():
        schema_field = ("SCHEMA_NAME" if source == "SCHEMATA" else
                        "CONSTRAINT_SCHEMA" if source in ("REFERENTIAL_CONSTRAINTS", "CHECK_CONSTRAINTS") else "TABLE_SCHEMA")
        rows = query("SELECT JSON_ARRAY({0}) FROM information_schema.{1} WHERE {2} = {3} AND {4}".format(
            ", ".join(columns), source, schema_field, _literal(schema), predicate))
        values = []
        for row in rows:
            value = json.loads(row[0])
            if kind == "defaults":
                value[0] = "$self"
            if value[0] == "flyway_schema_history" or value[0].startswith("import_archived_"):
                continue
            if kind == "columns" and value[1].startswith("import_archived_"):
                continue
            if kind == "keys" and value[4] == schema:
                value[4] = "$self"
            values.append(value)
        metadata[kind] = sorted(values, key=lambda row: json.dumps(row, ensure_ascii=True))
    return metadata


def migration_digest(root, province):
    digest = hashlib.sha256()
    root = Path(root)
    for location in ("common", province):
        if not (root / location).is_dir():
            raise ValueError("missing packaged migration location: " + location)
        for path in sorted((root / location).glob("V*.sql")):
            digest.update((location + "/" + path.name + "\0").encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def load(schema_root, province):
    root = Path(schema_root)
    path = root / "contracts" / (province + ".json")
    document = json.loads(path.read_text())
    if type(document.get("format")) is not int or document["format"] != FORMAT or document.get("province") != province:
        raise ValueError("unsupported or wrong-province schema contract")
    metadata = document.get("schema")
    if not isinstance(metadata, dict) or set(metadata) != set(FIELDS):
        raise ValueError("incomplete physical schema contract")
    for kind, (_, fields, _) in FIELDS.items():
        if not isinstance(metadata[kind], list) or any(not isinstance(row, list) or len(row) != len(fields) for row in metadata[kind]):
            raise ValueError("invalid physical schema contract section: " + kind)
    if document.get("migration_sha256") != migration_digest(root / "migration", province):
        raise ValueError("schema contract does not describe the packaged migration files")
    return document


def differences(expected, actual):
    """List metadata differences without ever reading clinic values."""
    problems = []
    for kind in FIELDS:
        want = {json.dumps(row, ensure_ascii=True) for row in expected.get(kind, [])}
        have = {json.dumps(row, ensure_ascii=True) for row in actual.get(kind, [])}
        for row in sorted(want - have):
            value = json.loads(row)
            problems.append("{0}: expected definition differs or is missing for {1}{2}".format(
                kind, value[0], "." + str(value[1]) if len(value) > 1 and kind != "tables" else ""))
        for row in sorted(have - want):
            value = json.loads(row)
            problems.append("{0}: unexpected definition for {1}{2}".format(
                kind, value[0], "." + str(value[1]) if len(value) > 1 and kind != "tables" else ""))
    return problems


def foreign_key_violations(query, schema, metadata=None):
    """Validate existing rows; SET foreign_key_checks=1 does not scan them."""
    from .o19etl import ident
    metadata = metadata if metadata is not None else capture(query, schema)
    constraints = {}
    for row in metadata["keys"]:
        table, name, position, column, parent_schema, parent, parent_column = row
        if parent:
            constraints.setdefault((table, name, parent_schema, parent), []).append((position, column, parent_column))
    failures = []
    for (table, name, parent_schema, parent), columns in sorted(constraints.items()):
        columns.sort()
        parent_schema = schema if parent_schema == "$self" else parent_schema
        non_null = " AND ".join("c.{0} IS NOT NULL".format(ident(column)) for _, column, _ in columns)
        join = " AND ".join("p.{0} = c.{1}".format(ident(parent_column), ident(column)) for _, column, parent_column in columns)
        sql = "SELECT COUNT(*) FROM {0}.{1} c WHERE {2} AND NOT EXISTS (SELECT 1 FROM {3}.{4} p WHERE {5})".format(
            ident(schema), ident(table), non_null, ident(parent_schema), ident(parent), join)
        count = int(query(sql)[0][0])
        if count:
            failures.append("{0}.{1}: {2} row(s) violate foreign key {3}".format(schema, table, count, name))
    return failures
