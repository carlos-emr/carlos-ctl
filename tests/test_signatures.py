# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""Imported unassigned-signature ownership is source-derived and immutable."""

import re
import unittest

from carlos_ctl import o19signatures


class SourceAndTarget:
    def __init__(self):
        self.source_signatures = [("42", None), ("43", "CONSULTATION"),
                                  ("44", None), ("45", None)]
        self.source_eforms = [
            ("/imageRenderingServlet?source=signature_stored&amp;digitalSignatureId=42", "7"),
            ("/imageRenderingServlet?digitalSignatureId=420", "8"),
            ("/imageRenderingServlet?digitalSignatureId=44", "7"),
            ("/imageRenderingServlet?digitalSignatureId=44", "8"),
        ]
        self.source_consults = [("43", "9"), ("45", "9")]
        self.source_prescriptions = [("45", "9")]
        self.recorded = {}

    def query(self, sql):
        if "information_schema.TABLES" in sql:
            return [(name,) for name in
                    ("DigitalSignature", "consultationRequests", "prescription",
                     "eform_values")]
        if "information_schema.COLUMNS" in sql:
            return [("1",)]
        if "FROM `o19_import`.`DigitalSignature`" in sql:
            return self.source_signatures
        if "FROM `o19_import`.`consultationRequests`" in sql:
            return self.source_consults
        if "FROM `o19_import`.`prescription`" in sql:
            return self.source_prescriptions
        if "FROM `o19_import`.`eform_values`" in sql:
            return self.source_eforms
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
    def test_only_unique_source_references_become_owners(self):
        fixture = SourceAndTarget()
        owners, unresolved = o19signatures.expected_owners(
            fixture.query, "o19_import")
        self.assertEqual(owners, {42: (7, "E_FORM"),
                                  43: (9, "CONSULTATION")})
        # 44 has two patients; 45 has two modules. A URL for 420 does
        # not claim signature 42 by prefix.
        self.assertEqual(unresolved, 2)

    def test_receipt_is_replaced_on_resume_and_verified_against_source(self):
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
        fixture = SourceAndTarget()
        o19signatures.install_owners(fixture.query, "o19_import", "carlos")
        # A user can submit a new target eFormValue, but the receipt
        # is computed only from the staged source and stays patient 7.
        fixture.target_eform = (
            "/imageRenderingServlet?digitalSignatureId=42", "99")
        self.assertEqual(fixture.recorded[42], (7, "E_FORM"))
        self.assertIsNone(o19signatures.verification_problem(
            fixture.query, "o19_import", "carlos"))


if __name__ == "__main__":
    unittest.main()
