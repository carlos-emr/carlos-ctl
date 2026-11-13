# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""Imported unassigned-signature ownership is source-derived and immutable."""

import re
import unittest

from carlos_ctl import o19signatures


class SourceAndTarget:
    """Model staged OSCAR rows and the target's frozen-owner receipt."""

    def __init__(self):
        """Seed source rows with unique, conflicting and absent references."""
        self.source_signatures = [("42", None), ("43", "CONSULTATION"),
                                  ("44", None), ("45", None)]
        self.source_eforms = [
            ("signatureValue", "/imageRenderingServlet?source=signature_stored&amp;digitalSignatureId=42", "7"),
            ("signatureValue", "/imageRenderingServlet?digitalSignatureId=420", "8"),
            ("signatureValue", "/imageRenderingServlet?digitalSignatureId=44", "7"),
            ("signatureValue", "/imageRenderingServlet?digitalSignatureId=44", "8"),
        ]
        self.source_consults = [("43", "9"), ("45", "9")]
        self.source_prescriptions = [("45", "9")]
        self.columns = {
            "DigitalSignature": {"id", "demographicId", "ModuleType"},
            "consultationRequests": {"requestId", "signature_img", "demographicNo"},
            "consultationResponse": {"responseId", "signatureImg", "demographicNo"},
            "prescription": {"script_no", "digital_signature_id", "demographic_no"},
            "eform_values": {"id", "var_name", "var_value", "demographic_no"},
        }
        self.recorded = {}

    def query(self, sql):
        """Respond to the ownership queries with this fixture's source rows."""
        if "information_schema.TABLES" in sql:
            return [(name,) for name in
                    self.columns]
        if "information_schema.COLUMNS" in sql:
            return [(table, column) for table, names in self.columns.items()
                    for column in names]
        if "FROM `o19_import`.`DigitalSignature`" in sql:
            if "IFNULL(`ModuleType`, '')" in sql:
                return [(sid, "" if module is None else module)
                        for sid, module in self.source_signatures]
            if "SELECT `id`, ''" in sql:
                return [(sid, "") for sid, _module in self.source_signatures]
            raise AssertionError(sql)
        if "FROM `o19_import`.`consultationRequests`" in sql:
            return self.source_consults
        if "FROM `o19_import`.`consultationResponse`" in sql:
            return [("43", "9")]
        if "FROM `o19_import`.`prescription`" in sql:
            return self.source_prescriptions
        if "FROM `o19_import`.`eform_values`" in sql:
            assert "('signatureValue', 'Letter')" in sql
            return [(value, patient) for name, value, patient in self.source_eforms
                    if name in {"signatureValue", "Letter"}
                    and "digitalSignatureId=" in value]
        if sql.startswith("DELETE FROM `carlos`.`o19_imported_signature_owner`"):
            self.recorded.clear()
            return []
        if sql.startswith("INSERT INTO `carlos`.`o19_imported_signature_owner`"):
            for sid, patient, module in re.findall(
                    r"\(([0-9]+), ([0-9]+), '([A-Z_]+)'\)", sql):
                self.recorded[int(sid)] = (int(patient), module)
            return []
        if "FROM `carlos`.`o19_imported_signature_owner`" in sql:
            return [(sid, patient, module) for sid, (patient, module)
                    in self.recorded.items()]
        raise AssertionError(sql)


class TestImportedSignatureOwnership(unittest.TestCase):
    """Check source-only ownership inference and P7 receipt verification."""

    def test_only_unique_source_references_become_owners(self):
        """Freeze only unique patient/module pairs with exact signature IDs."""
        fixture = SourceAndTarget()
        owners, unresolved = o19signatures.expected_owners(
            fixture.query, "o19_import")
        self.assertEqual(owners, {42: (7, "E_FORM"),
                                  43: (9, "CONSULTATION")})
        # 44 has two patients; 45 has two modules. A URL for 420 does
        # not claim signature 42 by prefix.
        self.assertEqual(unresolved, 2)

    def test_receipt_is_replaced_on_resume_and_verified_against_source(self):
        """Replace a partial P4 receipt and reject a later altered owner."""
        fixture = SourceAndTarget()
        fixture.recorded[999] = (3, "E_FORM")
        self.assertEqual(o19signatures.install_owners(
            fixture.query, "o19_import", "carlos"), (2, 2))
        self.assertEqual(fixture.recorded, {42: (7, "E_FORM"),
                                            43: (9, "CONSULTATION")})
        self.assertIsNone(o19signatures.verification_problem(
            fixture.query, "o19_import", "carlos"))
        fixture.recorded[42] = (8, "E_FORM")
        self.assertIn("differs from the staged source",
                      o19signatures.verification_problem(
                          fixture.query, "o19_import", "carlos"))

    def test_later_live_form_cannot_change_imported_owner(self):
        """Keep target form submissions out of the source ownership decision."""
        fixture = SourceAndTarget()
        o19signatures.install_owners(fixture.query, "o19_import", "carlos")
        # A user can submit a new target eFormValue, but the receipt
        # is computed only from the staged source and stays patient 7.
        fixture.target_eform = (
            "/imageRenderingServlet?digitalSignatureId=42", "99")
        self.assertEqual(fixture.recorded[42], (7, "E_FORM"))
        self.assertIsNone(o19signatures.verification_problem(
            fixture.query, "o19_import", "carlos"))

    def test_saved_letter_reference_can_establish_one_source_owner(self):
        """Accept exact saved Letter URLs but ignore unrelated eForm fields."""
        fixture = SourceAndTarget()
        fixture.source_signatures = [("42", None), ("420", None)]
        fixture.source_eforms = [
            ("Letter", '<img src="/imageRenderingServlet?digitalSignatureId=42">', "7"),
            ("Letter", '<img src="/imageRenderingServlet?digitalSignatureId=420">', "8"),
            ("other", "/imageRenderingServlet?digitalSignatureId=42", "99"),
        ]

        owners, unresolved = o19signatures.expected_owners(fixture.query, "o19_import")

        self.assertEqual(owners, {42: (7, "E_FORM"), 420: (8, "E_FORM")})
        self.assertEqual(unresolved, 0)

    def test_older_prescription_without_signature_column_is_supported(self):
        """Handle an older source schema without a prescription signature ID."""
        fixture = SourceAndTarget()
        fixture.columns["prescription"].remove("digital_signature_id")
        owners, unresolved = o19signatures.expected_owners(
            fixture.query, "o19_import")
        self.assertEqual(owners, {42: (7, "E_FORM"),
                                  43: (9, "CONSULTATION"),
                                  45: (9, "CONSULTATION")})
        self.assertEqual(unresolved, 1)

    def test_sql_null_module_allows_unique_source_owner(self):
        """Permit a NULL source module when one source reference proves it."""
        fixture = SourceAndTarget()
        owners, _ = o19signatures.expected_owners(fixture.query, "o19_import")
        self.assertEqual(owners[42], (7, "E_FORM"))

    def test_missing_module_column_allows_unique_source_owner(self):
        """Infer the module when the source predates the module column."""
        fixture = SourceAndTarget()
        fixture.columns["DigitalSignature"].remove("ModuleType")
        owners, _ = o19signatures.expected_owners(fixture.query, "o19_import")
        self.assertEqual(owners[42], (7, "E_FORM"))

    def test_literal_null_module_does_not_claim_unique_source_owner(self):
        """Keep a literal NULL string distinct from a SQL NULL module."""
        fixture = SourceAndTarget()
        fixture.source_signatures[0] = ("42", "NULL")
        owners, unresolved = o19signatures.expected_owners(
            fixture.query, "o19_import")
        self.assertNotIn(42, owners)
        self.assertEqual(unresolved, 3)


if __name__ == "__main__":
    unittest.main()
