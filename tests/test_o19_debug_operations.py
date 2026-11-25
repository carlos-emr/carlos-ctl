# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from carlos_ctl import o19billing, o19debug, o19etl
from tests.test_etl_sql import col


class TestOperationMetadata(unittest.TestCase):
    def setUp(self):
        self.events = []
        def append(_path, text):
            self.events.append(json.loads(text))
        self.trace = o19debug.Diagnostics("/unused", append, verbose=True, mode="import",
                                          resume=False, version="test", manifest="test", timeout=0)

    def completed(self, operation):
        return next(e for e in self.events if e.get("operation") == operation and e["event"] == "completed")

    def test_computed_inputs_and_archive_reads_preserve_only_referenced_identifiers(self):
        entry = {"cols": ["htmlfilename", "other", "type"], "value_exprs": {
            "htmlfilename": o19billing.expression("billing_on_filename"),
            "other": "REPLACE(CONCAT(s.`One`, 'private payload s.decoy'), s.Two, s.Three)"},
            "fk_remap": {"type": "Category"}}
        src = {x: col() for x in ["id", "Type", "One", "Two", "Three", "decoy"]}
        dst = {c: col() for c in entry["cols"]}
        fields = o19etl._table_diagnostic("Stage", "billing_on_filename", "Carlos", "billing_on_filename",
                                          entry, src, dst, entry["cols"], "Archive")
        self.assertEqual(fields["expression_source_columns"], {"htmlfilename": ["id"], "other": ["One", "Three", "Two"]})
        self.assertEqual(fields["archive_reads_by_target"], {
            "htmlfilename": [{"schema": "Archive", "table": "billing_on_filename__filenames",
                              "columns": ["imported_filename", "row_id"]}],
            "type": [{"schema": "Archive", "table": "Category__idmap", "columns": ["new_id", "old_id"]}]})
        self.assertEqual(fields["source_column_by_target"]["type"], "Type")
        self.assertNotIn("private payload", json.dumps(fields))
        self.assertNotIn("decoy", json.dumps(fields))
        self.assertNotIn("archive_reads_by_target", o19etl._width_diagnostic(fields, "other", src, dst["other"]))

    def test_unindexed_width_read_names_the_whole_table_and_its_discovery_decision(self):
        src = {"data": dict(col("longtext", char_len=4294967295), charset="latin1"),
               "id": col("int", nullable=False)}
        dst = {"data": dict(col("text", char_len=65535), charset="utf8mb4")}
        query = Mock(side_effect=[[], [["0"]]])
        problems = o19etl.width_problems(query, "Log", {"chunk_by": "id", "cols": ["data"]}, "Stage",
                                         dst, src, diagnostics=self.trace, phase="P2", target_schema="Carlos")
        self.assertEqual(problems, [])
        discovery = self.completed("find width check ranges")
        self.assertEqual((discovery["candidate_range_key"], discovery["scope"], discovery["selected_scope"]),
                         ("id", "range discovery", "whole table"))
        read = self.completed("measure column width")
        self.assertEqual(read["scope"], "whole table")
        self.assertNotIn("range_key", read)
        self.assertNotIn(" AND s.`id`", read["width_count_sql"])

    def test_keyless_copy_tracks_global_target_counts_and_insert_only_columns(self):
        entry = {"class": "copy", "cols": ["id", "status"], "chunk_by": "id", "replace_seed": True}
        src = {"id": col("int", nullable=False), "status": col("varchar")}
        dst = dict(src, destinationFacilityId=col("int", primitive=True))
        plain = Mock(side_effect=[[["1", "3"]], [["0"]], [["1"]], [["1"]]])
        run = SimpleNamespace(query=Mock(), plain=plain, src="Stage", dst="Carlos", arch="Archive",
                              state_dir="/unused", progress={}, counts={"copy": 0}, seed_group=set(),
                              admin_user=None, admin_pn=None, copy_primary_keys={}, src_info={"records": src},
                              diagnostics=self.trace)
        state = {}
        with patch.object(o19etl, "save_progress"), patch.object(o19etl, "copy_multiset_parity", return_value=[]):
            o19etl.etl_copy_table(run, "records", entry, state, dst, None)
        inserted = self.completed("insert copied rows")
        self.assertEqual((inserted["supplied_target_columns"], inserted["scope"], inserted["first_id"], inserted["last_id"]),
                         (["destinationFacilityId"], "ID range", 1, 3))
        self.assertNotIn("supplied_target_columns", self.completed("verify copied values before checkpoint"))
        for name in ("count target rows before copy", "count target rows after copy"):
            event = self.completed(name)
            self.assertEqual(event["scope"], "whole table")
            self.assertNotIn("range_key", event)
            self.assertNotIn("first_id", event)
            self.assertNotIn("source_table", event)
        self.assertEqual(self.completed("count source rows for copy verification")["range_key"], "id")
        self.assertEqual(state["done_through"], 3)

    def test_failed_keyless_copy_reports_mismatches_before_its_checkpoint(self):
        src = {"status": col("varchar")}
        run = SimpleNamespace(query=Mock(), plain=Mock(side_effect=[[["0"]], [["2"]], [["2"]]]),
                              src="Stage", dst="Carlos", arch="Archive", state_dir="/unused", progress={},
                              counts={"copy": 0}, seed_group=set(), admin_user=None, admin_pn=None,
                              copy_primary_keys={}, src_info={"records": src}, diagnostics=self.trace)
        state = {}
        entry = {"class": "copy", "cols": ["status"], "replace_seed": True}
        with patch.object(o19etl, "save_progress"), patch.object(o19etl, "copy_multiset_parity",
                 return_value=["private mismatch"]), self.assertRaises(o19etl.QueryError):
            o19etl.etl_copy_table(run, "records", entry, state, src, None)
        event = self.events[-1]
        self.assertEqual((event["event"], event["inserted_rows"], event["expected_rows"], event["mismatch_checks"]),
                         ("failed", 2, 2, 1))
        self.assertNotIn("private mismatch", json.dumps(event))
        self.assertEqual(state, {"started": True})

    def test_keyless_parity_records_discrepancies_without_diagnostic_text(self):
        info = {"sample": {"note": col()}}
        entry = {"sample": {"class": "copy", "cols": ["note"]}}
        with patch.dict(o19etl.o19map_schema.TABLES, entry, clear=True), \
                patch.object(o19etl, "schema_tables", return_value={"sample"}), \
                patch.object(o19etl, "primary_key_columns", return_value={}), \
                patch.object(o19etl, "copy_multiset_parity", return_value=["private comparison details"]):
            ok, bad = o19etl.copy_content_parity(Mock(), "Stage", "Carlos", info, info, diagnostics=self.trace)
        self.assertEqual((ok, bad), ([], ["private comparison details"]))
        self.assertEqual(self.completed("compare keyless copied values")["mismatch_checks"], 1)
        self.assertNotIn("private comparison details", json.dumps(self.events))

    def test_seed_row_retained_on_keyless_target_identifies_count_only_verification(self):
        cols = {"user_name": col()}
        entry = {"class": "copy", "cols": ["user_name"]}
        state = {}
        run = SimpleNamespace(query=Mock(), plain=Mock(side_effect=[[["1"]], [["2"]], [["3"]]]),
                              src="Stage", dst="Carlos", arch="Archive", src_info={"secUserRole": cols},
                              state_dir="/unused", progress={}, counts={"copy": 0}, copy_primary_keys={},
                              seed_group={"secUserRole"}, admin_user="test_admin", admin_pn="1", diagnostics=self.trace)
        with patch.object(o19etl, "save_progress"), patch.object(o19etl, "copy_multiset_parity") as compare:
            o19etl.etl_copy_table(run, "secUserRole", entry, state, cols, None)
        compare.assert_not_called()
        checked = self.completed("verify copied row count before checkpoint")
        self.assertEqual((checked["expected_rows"], checked["inserted_rows"]), (2, 2))
        self.assertFalse(any(e.get("operation") == "verify copied values before checkpoint" for e in self.events))
        self.assertTrue(state["done"])
