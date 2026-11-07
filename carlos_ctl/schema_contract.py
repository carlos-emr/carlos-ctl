# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""Physical CARLOS schema contract, independent of Flyway's history check.

Contracts are exported from a freshly migrated reference database and packaged
by carlos-emr. Runtime counters, statistics and archive extensions are excluded;
column/index/constraint definitions are compared, not just their names.
"""
import hashlib
import json
import re
from pathlib import Path

FORMAT = 2

# MariaDB information_schema.CHARACTER_SETS.MAXLEN, plus MySQL's gb18030.
# An unknown charset cannot prove a character-to-byte capacity transition.
CHARSET_MAX_BYTES = {
    **dict.fromkeys("armscii8 ascii binary cp1250 cp1251 cp1256 cp1257 cp850 cp852 cp866 "
                    "dec8 geostd8 greek hebrew hp8 keybcs2 koi8r koi8u latin1 latin2 latin5 "
                    "latin7 macce macroman swe7 tis620".split(), 1),
    **dict.fromkeys("big5 cp932 euckr gb2312 gbk sjis ucs2".split(), 2),
    **dict.fromkeys("eucjpms ujis utf8 utf8mb3".split(), 3),
    **dict.fromkeys("gb18030 utf16 utf16le utf32 utf8mb4".split(), 4),
}

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


def migration_digest(root, province, exclude=()):
    """Bind migration bytes deterministically, excluding only explicitly named recovery additions."""
    digest = hashlib.sha256()
    root = Path(root)
    for location in ("common", province):
        if not (root / location).is_dir():
            raise ValueError("missing packaged migration location: " + location)
        for path in sorted((root / location).glob("V*.sql")):
            if path.name in exclude:
                continue
            digest.update((location + "/" + path.name + "\0").encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def pre_billing_contract(schema_root, province):
    """The exact preceding contract for the additive billing audit migration.

    This candidate grants nothing by itself: recovery also requires its entire
    document hash to equal the old ledger and every physical definition to
    match the pristine target. Future unrelated changes therefore fail closed.
    """
    filename = "V1.0.59.33__audit_imported_billing_filenames.sql"
    root = Path(schema_root)
    if not (root / "migration" / "common" / filename).is_file():
        return None
    current = load(root, province)
    if not any(row[0] == "billing_filename_import" for row in current["schema"]["tables"]):
        return None
    prior = dict(current)
    prior["migration_sha256"] = migration_digest(root / "migration", province, exclude=(filename,))
    prior["schema"] = {kind: [row for row in rows if row[0] != "billing_filename_import"]
                       for kind, rows in current["schema"].items()}
    return prior


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


def widening_columns(expected, actual):
    """Columns explained entirely by a lossless text/binary widening.

    No values are scanned. Every other metadata attribute must match;
    this is used only to recover an import that has never copied rows.
    """
    text = {"tinytext": 255, "text": 65535,
            "mediumtext": 16777215, "longtext": 4294967295}
    binary = {k.replace("text", "blob"): v for k, v in text.items()}

    def capacity(decl):
        """Return the storage family, size and whether that size counts bytes."""
        sized = re.fullmatch(r"(var)?(char|binary)\((\d+)\)", decl.lower())
        if sized:
            return sized.group(2), int(sized.group(3)), False
        if decl.lower() in text:
            return "char", text[decl.lower()], True
        if decl.lower() in binary:
            return "binary", binary[decl.lower()], True
        return None

    adjusted = dict(actual, columns=[list(row) for row in actual["columns"]])
    wanted = {(row[0], row[1]): row for row in expected["columns"]}
    changed = []
    for row in adjusted["columns"]:
        want = wanted.get((row[0], row[1]))
        if not want or row[2] == want[2]:
            continue
        before, after = capacity(row[2]), capacity(want[2])
        if not before or not after or before[0] != after[0]:
            continue
        if (not before[2] and not after[2]
                and row[2].split("(", 1)[0] != want[2].split("(", 1)[0]):
            continue  # fixed/variable padding semantics are not a widening
        required = before[1]
        if after[2] and not before[2] and before[0] == "char":
            width = CHARSET_MAX_BYTES.get((row[6] or "").lower())
            if width is None:
                continue
            required *= width
        if not after[2] and before[2] or after[1] < required:
            continue
        row[2] = want[2]
        changed.append(row[0] + "." + row[1])
    return changed if not differences(expected, adjusted) else []


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
