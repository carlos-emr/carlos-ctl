# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""The OSCAR 19 manifests carlos-emr ships, as this CLI reads them.

The manifests are generated in carlos-emr/carlos beside the schema they
describe and installed by the carlos-emr package as JSON; this package
only loads them (o19manifest), hands them to the ETL under the names it
always used (o19map_schema, o19map_props) and to the standalone
assessment script (o19_preflight.load_manifest). These tests pin the
envelope contract -- format and kind checked before any key is trusted,
one clear message per failure -- and the standalone writer, which must
re-inline the installed manifest into a file that still imports nothing.

Run (from the repository root):
    python3 -m unittest discover -v -s tests -t .
"""

import ast
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from carlos_ctl import o19_preflight, o19manifest, o19map_props, o19map_schema


def _write(directory, file_kind, **overrides):
    """A minimal manifest file of `kind` under `directory`, with the
    envelope overridable (a wrong format, a wrong kind)."""
    body = {"format": o19manifest.MANIFEST_FORMAT, "kind": file_kind,
            "generator": "test", "SCHEMA_MAP_VERSION": "o19map-0+test"}
    body.update(overrides)
    path = os.path.join(directory, o19manifest.MANIFEST_FILES[file_kind])
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(body, fh)
    return path


class TestEnvelope(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {
            o19manifest.MANIFEST_DIR_ENV: self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_a_good_file_returns_the_data_keys_only(self):
        _write(self.tmp.name, "o19map-props", KEYS={"a": 1})
        data = o19manifest.load("o19map-props")
        self.assertEqual(data, {"SCHEMA_MAP_VERSION": "o19map-0+test",
                                "KEYS": {"a": 1}})
        for envelope_key in ("format", "kind", "generator"):
            self.assertNotIn(envelope_key, data)

    def test_a_missing_file_names_carlos_emr_as_the_provider(self):
        with self.assertRaises(o19manifest.ManifestError) as caught:
            o19manifest.load("o19map-schema")
        message = str(caught.exception)
        self.assertIn("carlos-emr", message)
        self.assertIn(os.path.join(self.tmp.name, "o19map_schema.json"),
                      message)

    def test_an_unknown_format_is_refused_before_any_key_is_read(self):
        _write(self.tmp.name, "o19map-schema",
               format=o19manifest.MANIFEST_FORMAT + 1)
        with self.assertRaises(o19manifest.ManifestError) as caught:
            o19manifest.load("o19map-schema")
        self.assertIn("format", str(caught.exception))
        self.assertIn("carlos-ctl release that matches", str(caught.exception))

    def test_a_file_of_another_kind_is_refused(self):
        _write(self.tmp.name, "o19map-schema", kind="o19map-props")
        with self.assertRaises(o19manifest.ManifestError) as caught:
            o19manifest.load("o19map-schema")
        self.assertIn("kind", str(caught.exception))

    def test_malformed_json_is_one_message_not_a_traceback(self):
        with open(os.path.join(self.tmp.name, "o19map_props.json"), "w") as fh:
            fh.write("{not json")
        with self.assertRaises(o19manifest.ManifestError) as caught:
            o19manifest.load("o19map-props")
        self.assertIn("cannot read", str(caught.exception))

    def test_a_non_object_document_is_refused(self):
        with open(os.path.join(self.tmp.name, "o19map_props.json"), "w") as fh:
            fh.write("[1, 2]")
        with self.assertRaises(o19manifest.ManifestError):
            o19manifest.load("o19map-props")

    def test_an_unknown_kind_is_a_programming_error(self):
        with self.assertRaises(o19manifest.ManifestError):
            o19manifest.load("no-such-kind")

    def test_the_default_directory_is_carlos_emrs_share_tree(self):
        self.env.stop()
        try:
            with mock.patch.dict(os.environ, clear=False):
                os.environ.pop(o19manifest.MANIFEST_DIR_ENV, None)
                self.assertEqual(o19manifest.manifest_dir(),
                                 "/usr/share/carlos-emr/o19-manifest")
        finally:
            self.env.start()


class TestTheLoadedModules(unittest.TestCase):

    """What the ETL sees: the same names the generated modules carried."""

    def test_schema_loader_carries_every_name_the_etl_uses(self):
        for name in ("SCHEMA_MAP_VERSION", "O19_PROFILE", "TABLES",
                     "CARLOS_COLUMNS", "SEED_ROW_COUNTS",
                     "PRISTINE_TOLERATED_TABLES", "SUPPORTED_PROVINCES",
                     "REQUIRED_TABLES", "CARLOSDOC_SEED_DELETES",
                     "SEED_PROVIDER_NO", "SEED_USER_NAME",
                     "CREDENTIAL_TABLES", "CLAIM_HEADER_TABLE",
                     "STARTUP_CREATED_ROWS", "STOCK_ROLE_NAMES",
                     "ROLE_TEMPLATE_MIN_JACCARD", "PREVENTION_TYPE_MAP",
                     "KNOWN_PREVENTION_TYPES", "PRIMITIVE_COLUMNS",
                     "PROFILES"):
            self.assertTrue(hasattr(o19map_schema, name), name)
        self.assertNotIn("format", vars(o19map_schema))
        self.assertNotIn("kind", vars(o19map_schema))

    def test_props_loader_carries_every_name_the_props_phase_uses(self):
        for name in ("PROPS_MAP_VERSION", "O19_DEFAULTS",
                     "SECRET_DEFAULT_KEYS", "CARLOS_DEFAULTS",
                     "BUNDLE_KEY_RENAMES", "KEYS", "PREFIX_RULES"):
            self.assertTrue(hasattr(o19map_props, name), name)
        # [prefix, spec] pairs unpack exactly as the tuples did
        for prefix, spec in o19map_props.PREFIX_RULES:
            self.assertIsInstance(prefix, str)
            self.assertIsInstance(spec, dict)

    def test_the_snapshot_names_are_the_per_province_names(self):
        # bind() rebinds exactly the names the manifest repeats per
        # province; a PROFILES entry with another shape would be bound on
        # the way out and never restored
        for province, profile in o19map_schema.PROFILES.items():
            self.assertEqual(sorted(profile),
                             sorted(o19map_schema._PROFILE_NAMES), province)

    def test_bind_round_trips(self):
        default = o19map_schema._DEFAULT_PROFILE["O19_PROFILE"]
        self.addCleanup(o19map_schema.bind, default)
        for province in sorted(o19map_schema.PROFILES):
            self.assertEqual(o19map_schema.bind(province), province)
            self.assertEqual(o19map_schema.O19_PROFILE, province)
        self.assertEqual(o19map_schema.bind(default), default)
        self.assertEqual(o19map_schema.TABLES,
                         o19map_schema._DEFAULT_PROFILE["TABLES"])
        # an unknown province leaves the module where it is
        self.assertEqual(o19map_schema.bind("zz"), default)


class TestPreflightManifest(unittest.TestCase):

    """The standalone script's inlined copy, and the installed manifest
    loaded over it in import mode."""

    def setUp(self):
        # load_manifest replaces module globals: restore the inlined data
        # afterwards so no other test sees this one's manifest
        self.saved = {n: getattr(o19_preflight, n)
                      for n in o19_preflight._MANIFEST_NAMES}
        self.saved_default = dict(o19_preflight._DEFAULT_PROFILE)

        def restore():
            for n, v in self.saved.items():
                setattr(o19_preflight, n, v)
            o19_preflight._DEFAULT_PROFILE = self.saved_default
        self.addCleanup(restore)

    def test_the_inlined_block_is_the_manifest_the_suite_runs_with(self):
        # the checked-in copy must be exactly what the manifest renders:
        # a stale block is the drift --write-standalone exists to cure,
        # and here it would mean the assessment and the import disagree
        data = o19manifest.load("o19-preflight")
        source = open(o19_preflight.__file__, encoding="utf-8").read()
        b = source.index(o19manifest.MARKER_BEGIN)
        e = source.index(o19manifest.MARKER_END) + len(o19manifest.MARKER_END)
        self.assertEqual(source[b:e], o19manifest.render_preflight_block(data))

    def test_load_manifest_replaces_the_data_and_retakes_the_snapshot(self):
        data = o19manifest.load("o19-preflight")
        data = json.loads(json.dumps(data))
        data["SCHEMA_MAP_VERSION"] = "o19map-0+loaded"
        data["KNOWN_TABLES"] = {"only_table": "copy"}
        self.assertEqual(o19_preflight.load_manifest(data), "o19map-0+loaded")
        self.assertEqual(o19_preflight.SCHEMA_MAP_VERSION, "o19map-0+loaded")
        self.assertEqual(o19_preflight._DEFAULT_PROFILE["KNOWN_TABLES"],
                         {"only_table": "copy"})
        # and bind() returns to THIS default, not the inlined one
        default = data["O19_PROFILE"]
        for province in sorted(data["PROFILES"]):
            o19_preflight.bind(province)
        o19_preflight.bind(default)
        self.assertEqual(o19_preflight.KNOWN_TABLES, {"only_table": "copy"})

    def test_load_manifest_refuses_unknown_and_missing_keys(self):
        data = o19manifest.load("o19-preflight")
        with self.assertRaises(ValueError) as caught:
            o19_preflight.load_manifest(dict(data, STRAY=1))
        self.assertIn("STRAY", str(caught.exception))
        short = dict(data)
        del short["CHARSET_SCAN"]
        with self.assertRaises(ValueError) as caught:
            o19_preflight.load_manifest(short)
        self.assertIn("CHARSET_SCAN", str(caught.exception))

    def test_write_standalone_inlines_the_manifest_into_a_self_contained_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "o19_preflight.py")
            version = o19manifest.write_standalone_preflight(dest)
            self.assertEqual(version, o19manifest.load(
                "o19-preflight")["SCHEMA_MAP_VERSION"])
            self.assertEqual(oct(os.stat(dest).st_mode & 0o777), "0o644")
            text = open(dest, encoding="utf-8").read()
            # still imports nothing from the package
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn("carlos_ctl", node.module or "")
                    self.assertNotEqual(node.level, 1)
            self.assertNotIn("from carlos_ctl", text)
            # and runs standalone, with the inlined data, under this python
            probe = subprocess.run(
                [sys.executable, dest, "--help"], capture_output=True,
                text=True)
            self.assertEqual(probe.returncode, 0, probe.stderr)
            # exact block: what the installed manifest renders to
            data = o19manifest.load("o19-preflight")
            self.assertIn(o19manifest.render_preflight_block(data), text)
            self.assertLessEqual(
                max(len(line) for line in text.splitlines()), 100)

    def test_write_standalone_reports_a_missing_manifest_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ,
                                {o19manifest.MANIFEST_DIR_ENV: tmp}):
            with self.assertRaises(o19manifest.ManifestError):
                o19manifest.write_standalone_preflight(
                    os.path.join(tmp, "out.py"))
            self.assertEqual(os.listdir(tmp), [])


class TestTheVerbFlag(unittest.TestCase):

    def test_o19_preflight_write_standalone_needs_no_root_and_no_inputs(self):
        from carlos_ctl import o19import
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "o19_preflight.py")
            out = io.StringIO()
            with mock.patch.object(o19import.os, "geteuid", return_value=1000), \
                    contextlib.redirect_stdout(out):
                rc = o19import.cmd_o19_preflight(["--write-standalone", dest])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.isfile(dest))
            self.assertIn("wrote " + dest, out.getvalue())
            self.assertIn("OSCAR 19 server", out.getvalue())

    def test_the_import_verb_has_no_such_flag(self):
        from carlos_ctl import o19import
        parser = o19import._parser("carlos-ctl import-o19", import_mode=True)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            parser.parse_args(["--write-standalone", "x"])


if __name__ == "__main__":
    unittest.main()
