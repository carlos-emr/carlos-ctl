# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""Regression boundaries for actual-checkout Consent feature conditioning."""

from copy import deepcopy
import itertools
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from .consent_feature import classify, ConsentFeatureError


EXPRESSIONS = {
    "optout": "IFNULL(s.`optout`, 1)",
    "deleted": "IFNULL((SELECT r.`deleted` FROM {archive}.`Consent__live` r "
               "WHERE r.`id` = s.`id`), 1)",
}
SQL = """ALTER TABLE `Consent`
ADD COLUMN `live_demographic_no` INT GENERATED ALWAYS AS
(IF(`deleted` = 0, `demographic_no`, NULL)) STORED,
ADD UNIQUE KEY `uq_consent_live_type` (`live_demographic_no`, `consent_type_id`);
"""


class TestConsentFeature(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.overlay = self.root / "scripts/migration/o19/overrides_schema.py"
        self.manifest = self.root / "debian/assets/o19-manifest/o19map_schema.json"
        self.migrations = self.root / "database/mysql/migration/common"
        self.overlay.parent.mkdir(parents=True)
        self.manifest.parent.mkdir(parents=True)
        self.migrations.mkdir(parents=True)

    def fixture(self, overlay=False, manifest=False, migration=False):
        expressions = {"Consent": EXPRESSIONS} if overlay else {}
        self.overlay.write_text("VALUE_EXPRS = " + repr(expressions) + "\n", encoding="utf-8")
        profile = {"TABLES": {"Consent": {"class": "copy", "cols": ["id", "optout"]}},
                   "CARLOS_COLUMNS": {"Consent": ["id", "deleted"]}}
        if manifest:
            profile["TABLES"]["Consent"].update(
                cols=["id", "optout", "deleted"], value_exprs=deepcopy(EXPRESSIONS))
            profile["CARLOS_COLUMNS"]["Consent"].append("live_demographic_no")
        model = deepcopy(profile)
        model.update(format=1, kind="o19map-schema")
        model["PROFILES"] = {"bc": deepcopy(profile)}
        self.manifest.write_text(json.dumps(model), encoding="utf-8")
        for path in self.migrations.iterdir():
            path.unlink()
        if migration:
            (self.migrations / "V99__one_live_consent_per_type.sql").write_text(SQL, encoding="utf-8")
        return model

    def change_manifest(self, edit):
        model = json.loads(self.manifest.read_text(encoding="utf-8"))
        edit(model)
        self.manifest.write_text(json.dumps(model), encoding="utf-8")

    def test_no_checkout_retains_existing_unavailable_case(self):
        self.assertFalse(classify(None))

    def test_explicit_missing_or_non_directory_checkout_is_an_error(self):
        for path in (self.root / "missing", self.root / "file"):
            if path.name == "file":
                path.write_text("not a checkout", encoding="utf-8")
            with self.subTest(path=path), self.assertRaises(ConsentFeatureError):
                classify(path)

    def test_all_absent_is_false_even_with_baseline_target_deleted_column(self):
        self.fixture()
        self.assertFalse(classify(self.root))

    def test_all_present_is_true(self):
        self.fixture(True, True, True)
        self.assertTrue(classify(self.root))

    def test_all_six_partial_artifact_combinations_raise(self):
        for markers in itertools.product((False, True), repeat=3):
            if len(set(markers)) == 1:
                continue
            with self.subTest(markers=markers):
                self.fixture(*markers)
                with self.assertRaises(ConsentFeatureError):
                    classify(self.root)

    def test_overlay_dynamic_unrelated_expression_need_not_be_executed(self):
        self.fixture()
        self.overlay.write_text("VALUE_EXPRS = {'tickler': {'date': 'x' + UNKNOWN_SENTINEL}}\n",
                                encoding="utf-8")
        self.assertFalse(classify(self.root))

    def test_deleted_manifest_column_alone_cannot_be_absent(self):
        self.fixture()
        self.change_manifest(lambda m: m["TABLES"]["Consent"]["cols"].append("deleted"))
        with self.assertRaises(ConsentFeatureError):
            classify(self.root)

    def test_empty_or_non_mapping_manifest_expressions_cannot_be_absent(self):
        for value in ({}, None, []):
            with self.subTest(value=value):
                self.fixture()
                self.change_manifest(lambda m: m["TABLES"]["Consent"].update(value_exprs=value))
                with self.assertRaises(ConsentFeatureError):
                    classify(self.root)

    def test_live_target_column_alone_cannot_be_absent(self):
        self.fixture()
        self.change_manifest(lambda m: m["CARLOS_COLUMNS"]["Consent"].append("live_demographic_no"))
        with self.assertRaises(ConsentFeatureError):
            classify(self.root)

    def test_both_provinces_require_full_columns_and_matching_expressions(self):
        for province in ("on", "bc"):
            for defect in ("deleted", "live", "ranking", "mismatch"):
                with self.subTest(province=province, defect=defect):
                    self.fixture(True, True, True)

                    def edit(model):
                        p = model if province == "on" else model["PROFILES"]["bc"]
                        entry = p["TABLES"]["Consent"]
                        if defect == "deleted":
                            entry["cols"].remove("deleted")
                        elif defect == "live":
                            p["CARLOS_COLUMNS"]["Consent"].remove("live_demographic_no")
                        elif defect == "ranking":
                            entry["value_exprs"]["deleted"] = "0"
                        else:
                            entry["value_exprs"]["optout"] = "IFNULL(s.`optout`, 0)"
                    self.change_manifest(edit)
                    with self.assertRaises(ConsentFeatureError):
                        classify(self.root)

    def test_overlay_internal_partial_and_dynamic_shapes_are_errors(self):
        for source in ("VALUE_EXPRS = {'Consent': {}}", "VALUE_EXPRS = {'Consent': {'deleted': '0'}}",
                       "VALUE_EXPRS = []", "VALUE_EXPRS = {}\nVALUE_EXPRS['Consent'] = {}",
                       "VALUE_EXPRS = {}\nVALUE_EXPRS.update({})", "VALUE_EXPRS = {}\nVALUE_EXPRS = {}",
                       "VALUE_EXPRS = {'Consent': {}, 'Consent': {}}"):
            with self.subTest(source=source):
                self.fixture()
                self.overlay.write_text(source, encoding="utf-8")
                with self.assertRaises(ConsentFeatureError):
                    classify(self.root)

    def test_malformed_overlay_and_json_fail_even_when_other_markers_absent(self):
        for target, text in (("overlay", "VALUE_EXPRS = {"), ("overlay", "# no dictionary"),
                             ("manifest", "{"), ("manifest", "[]"), ("manifest", "{}")):
            with self.subTest(target=target, text=text):
                self.fixture()
                getattr(self, target).write_text(text, encoding="utf-8")
                with self.assertRaises(ConsentFeatureError):
                    classify(self.root)

    def test_duplicate_json_keys_cannot_hide_partial_feature(self):
        for hidden in ("columns", "format", "kind"):
            with self.subTest(hidden=hidden):
                self.fixture()
                text = self.manifest.read_text(encoding="utf-8")
                if hidden == "columns":
                    text = text.replace('"cols": ["id", "optout"]',
                                        '"cols": ["id", "deleted"], "cols": ["id", "optout"]', 1)
                elif hidden == "format":
                    text = text.replace('"format": 1', '"format": 0, "format": 1')
                else:
                    text = text.replace('"kind": "o19map-schema"',
                                        '"kind": "wrong", "kind": "o19map-schema"')
                self.manifest.write_text(text, encoding="utf-8")
                with self.assertRaises(ConsentFeatureError):
                    classify(self.root)

    def test_manifest_format_and_kind_must_match_the_actual_schema_contract(self):
        for field, value in (("format", True), ("format", "1"), ("format", 1.0),
                             ("format", 0), ("format", None), ("kind", "wrong"),
                             ("kind", None)):
            with self.subTest(field=field, value=value):
                self.fixture()
                self.change_manifest(lambda m: m.update({field: value}))
                with self.assertRaises(ConsentFeatureError):
                    classify(self.root)

    def test_missing_or_unreadable_evidence_is_not_absence(self):
        self.fixture()
        for path in (self.overlay, self.manifest):
            original = path.read_bytes()
            path.unlink()
            with self.subTest(path=path), self.assertRaises(ConsentFeatureError):
                classify(self.root)
            path.write_bytes(original)
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            with self.assertRaises(ConsentFeatureError):
                classify(self.root)
        self.migrations.rmdir()
        with self.assertRaises(ConsentFeatureError):
            classify(self.root)

    def test_migration_directory_read_failure_is_not_absence(self):
        self.fixture()
        with mock.patch.object(Path, "iterdir", side_effect=PermissionError("denied")):
            with self.assertRaises(ConsentFeatureError):
                classify(self.root)

    def test_duplicate_migration_is_an_error(self):
        self.fixture(True, True, True)
        (self.migrations / "V100__one_live_consent_per_type.sql").write_text(SQL, encoding="utf-8")
        with self.assertRaises(ConsentFeatureError):
            classify(self.root)

    def test_missing_generated_or_unique_contract_is_an_error(self):
        for sql in (SQL.replace("GENERATED ALWAYS AS", "DEFAULT"),
                    SQL.replace("UNIQUE KEY", "KEY"),
                    SQL.replace("(`live_demographic_no`, `consent_type_id`)",
                                "(`demographic_no`, `consent_type_id`)"),
                    SQL.replace("`Consent`", "`Other`"),
                    SQL.replace("`deleted` = 0", "`deleted` = 1"),
                    "/* " + SQL + " */", "-- " + SQL.replace("\n", "\n-- ")):
            with self.subTest(sql=sql):
                self.fixture(True, True, True)
                next(self.migrations.iterdir()).write_text(sql, encoding="utf-8")
                with self.assertRaises(ConsentFeatureError):
                    classify(self.root)

    def test_case_generated_expression_and_named_constraint_are_supported(self):
        self.fixture(True, True, True)
        sql = SQL.replace("IF(`deleted` = 0, `demographic_no`, NULL)",
                          "CASE WHEN `deleted` = 0 THEN `demographic_no` ELSE NULL END")
        sql = sql.replace("UNIQUE KEY `uq_consent_live_type`", "CONSTRAINT `uq_consent_live_type` UNIQUE")
        next(self.migrations.iterdir()).write_text(sql, encoding="utf-8")
        self.assertTrue(classify(self.root))

    def test_hash_commented_migration_cannot_satisfy_presence(self):
        self.fixture(True, True, True)
        next(self.migrations.iterdir()).write_text(
            "# " + SQL.replace("\n", "\n# "), encoding="utf-8")
        with self.assertRaises(ConsentFeatureError):
            classify(self.root)

    def test_separate_unique_index_is_supported(self):
        self.fixture(True, True, True)
        sql = SQL.replace(",\nADD UNIQUE KEY", ";\nCREATE UNIQUE INDEX")
        sql = sql.replace("`uq_consent_live_type` (", "`uq_consent_live_type` ON `Consent` (")
        next(self.migrations.iterdir()).write_text(sql, encoding="utf-8")
        self.assertTrue(classify(self.root))


if __name__ == "__main__":
    unittest.main()
