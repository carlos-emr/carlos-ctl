# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""Freeze imported -1 signature ownership from the staged, immutable export.

Runtime eForm fields are user-writable, so they must never establish the
patient for an imported signature whose stored demographicId is -1. P4
materializes only uniquely supported source references; P7 recomputes the
same decision from staging and refuses a missing or changed receipt.
"""

import re
from typing import Dict, Optional, Set, Tuple


OWNER_TABLE = "o19_imported_signature_owner"
SIGNATURE_URL_ID = re.compile(
    r"(?:\?|&(?:amp;)?)digitalSignatureId=([0-9]+)(?=$|[&#\"'\s])")
REFERENCE_TABLES = {
    "consultationRequests": ("signature_img", "demographicNo", "CONSULTATION"),
    "consultationRequestsArchive": ("signature_img", "demographicNo", "CONSULTATION"),
    "consultationResponse": ("signatureImg", "demographicNo", "CONSULTATION"),
    "prescription": ("digital_signature_id", "demographic_no", "PRESCRIPTION"),
}


def _positive_int(value) -> Optional[int]:
    """Accept only positive identifiers from the source query result."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _source_tables(query, source: str) -> Set[str]:
    """List the source tables that may contain a signature or its reference."""
    rows = query("SELECT TABLE_NAME FROM information_schema.TABLES WHERE "
                 "TABLE_SCHEMA = '{0}' AND TABLE_NAME IN "
                 "('DigitalSignature', 'consultationRequests', "
                 "'consultationRequestsArchive', 'consultationResponse', "
                 "'prescription', 'eform_values')".format(source))
    return {str(row[0]) for row in rows if row}


def _source_columns(query, source: str, tables: Set[str]) -> Dict[str, Set[str]]:
    """Require column metadata for every discovered reference table."""
    columns: Dict[str, Set[str]] = {table: set() for table in tables}
    for row in query("SELECT TABLE_NAME, COLUMN_NAME FROM "
                     "information_schema.COLUMNS WHERE TABLE_SCHEMA = '{0}' "
                     "AND TABLE_NAME IN ('DigitalSignature', "
                     "'consultationRequests', 'consultationRequestsArchive', "
                     "'consultationResponse', 'prescription', 'eform_values')"
                     .format(source)):
        if len(row) >= 2 and row[0] in columns:
            columns[row[0]].add(str(row[1]))
    for table, names in columns.items():
        if not names:
            raise ValueError("source column metadata unavailable for " + table)
    return columns


def expected_owners(query, source: str) -> Tuple[Dict[int, Tuple[int, str]], int]:
    """Return unique (patient, module) decisions and unresolved -1 count.

    Every candidate is read from the staged source, never live target forms.
    A row with conflicting patients/modules or a module that disagrees with
    the signature's source module is deliberately left unmapped.
    """
    tables = _source_tables(query, source)
    if "DigitalSignature" not in tables:
        return {}, 0
    columns = _source_columns(query, source, tables)
    if not {"id", "demographicId"} <= columns["DigitalSignature"]:
        raise ValueError("source DigitalSignature id/patient columns unavailable")
    module_sql = ("IFNULL(`ModuleType`, '')" if any(
        name.lower() == "moduletype" for name in columns["DigitalSignature"])
        else "''")
    signatures = {}
    for row in query("SELECT `id`, {1} FROM `{0}`.`DigitalSignature` WHERE "
                     "`demographicId` = -1".format(source, module_sql)):
        signature_id = _positive_int(row[0])
        if signature_id is None:
            raise ValueError("source DigitalSignature has an invalid unassigned id")
        module = row[1] if len(row) > 1 else None
        signatures[signature_id] = None if module in (None, "") else module
    if not signatures:
        return {}, 0

    candidates: Dict[int, Set[Tuple[int, str]]] = {}

    def add(signature_id, patient, module):
        """Record a positive patient claim for a signature in this source export."""
        sid = _positive_int(signature_id)
        demographic = _positive_int(patient)
        if sid in signatures and demographic is not None:
            candidates.setdefault(sid, set()).add((demographic, module))

    for table, (reference, patient, module) in REFERENCE_TABLES.items():
        if table not in tables:
            continue
        if reference not in columns[table]:
            continue  # this OSCAR patch predates the signature field
        if patient not in columns[table]:
            raise ValueError("source patient column unavailable for " + table)
        for row in query("SELECT `{0}`, `{1}` FROM `{2}`.`{3}` WHERE "
                         "`{0}` IS NOT NULL".format(
                             reference, patient, source, table)):
            if len(row) >= 2:
                add(row[0], row[1], module)

    if ("eform_values" in tables
            and {"var_name", "var_value"} <= columns["eform_values"]):
        if "demographic_no" not in columns["eform_values"]:
            raise ValueError("source eForm patient column unavailable")
        for row in query("SELECT `var_value`, `demographic_no` FROM "
                         "`{0}`.`eform_values` WHERE `var_name` IN "
                         "('signatureValue', 'Letter') AND `var_value` LIKE "
                         "'%digitalSignatureId=%'".format(source)):
            if len(row) < 2 or not row[0]:
                continue
            for match in SIGNATURE_URL_ID.finditer(row[0]):
                add(match.group(1), row[1], "E_FORM")

    owners = {}
    for sid, stored_module in signatures.items():
        refs = candidates.get(sid, set())
        if len(refs) == 1:
            patient, module = next(iter(refs))
            if stored_module in (None, "", module):
                owners[sid] = (patient, module)
    return owners, len(signatures) - len(owners)


def install_owners(query, source: str, target: str) -> Tuple[int, int]:
    """Replace any partial receipt before P4 is marked complete."""
    owners, unresolved = expected_owners(query, source)
    query("DELETE FROM `{0}`.`{1}`".format(target, OWNER_TABLE))
    items = sorted(owners.items())
    for offset in range(0, len(items), 500):
        chunk = items[offset:offset + 500]
        values = ", ".join("({0}, {1}, '{2}')".format(
            sid, patient, module) for sid, (patient, module) in chunk)
        query("INSERT INTO `{0}`.`{1}` "
              "(`signature_id`, `demographic_id`, `module_type`) VALUES {2}"
              .format(target, OWNER_TABLE, values))
    return len(owners), unresolved


def verification_problem(query, source: str, target: str) -> Optional[str]:
    """Read-only P7 comparison, including a resumed import that skipped P4."""
    expected, _unresolved = expected_owners(query, source)
    actual = {}
    for row in query("SELECT `signature_id`, `demographic_id`, `module_type` "
                     "FROM `{0}`.`{1}`".format(target, OWNER_TABLE)):
        if len(row) < 3:
            raise ValueError("imported signature ownership row is incomplete")
        actual[int(row[0])] = (int(row[1]), str(row[2]))
    if actual != expected:
        return ("imported unassigned-signature ownership differs from the "
                "staged source ({0} expected, {1} recorded); restore the "
                "source-derived receipt before go-live".format(
                    len(expected), len(actual)))
    return None
