# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""Export boundary tests; these do not claim installed clinical-engine qualification."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock
from carlos_ctl import cdsexport, cdsruntime

EXPORT = ["--provider", "007", "--physicians", "007,T099", "--profile", "omd-5.2",
          "--recipient", "recipient-fixture", "--audit-plan", "retained-fixture"]
CAPS = {"engine": "cds-export", "protocol": 1, "java": 25, "cliHeapMiB": 512,
        "operations": sorted(cdsexport.OPERATIONS), "qualifiedProfiles": ["omd-5.2"],
        "submissionLookup": True, "detachedSubmission": True}


class TestExportArguments(unittest.TestCase):
    def test_explicit_roster_and_literal_provider_keys_reach_the_engine(self):
        args = cdsexport.parse_args(EXPORT)
        argv = cdsexport.engine_arguments(args)
        self.assertEqual(argv[:4], ["--operation", "export", "--provider", "007"])
        self.assertEqual(argv[argv.index("--physicians") + 1], "007,T099")
        self.assertEqual(argv[argv.index("--categories") + 1], "all")
        self.assertFalse(args.detach)

    def test_selected_patient_file_is_absolute_without_resolving_links(self):
        argv = ["--patients-file", "./selection-fixture.json", "--provider", "T099", "--profile", "omd-5.2",
                "--recipient", "fixture", "--audit-plan", "fixture"]
        with mock.patch.object(os.path, "realpath", side_effect=AssertionError):
            args = cdsexport.parse_args(argv)
        self.assertEqual(args.patients_file, os.path.abspath("./selection-fixture.json"))
        self.assertNotIn("--physicians", cdsexport.engine_arguments(args))

    def test_categories_must_explicitly_include_demographics(self):
        args = cdsexport.parse_args(EXPORT + ["--categories", "Reports,Demographics"])
        self.assertEqual(args.categories, "Demographics,Reports")
        for categories in ("Reports", "Demographics,unknown", "Demographics,Demographics", "demographics", "all,Reports"):
            with self.subTest(categories=categories), self.assertRaises(cdsexport.CdsCommandError):
                cdsexport.parse_args(EXPORT + ["--categories", categories])

    def test_existing_jobs_cannot_change_their_accepted_intent(self):
        for operation in ("status", "resume", "cancel", "report"):
            args = cdsexport.parse_args(["--provider", "007", "--" + operation, "run-fixture"])
            self.assertEqual(cdsexport.engine_arguments(args), ["--operation", operation, "--provider", "007", "--run", "run-fixture"])
            for extra in (["--profile", "omd-5.2"], ["--physicians", "007"], ["--facility", "1"], ["--submission-key", "other"]):
                with self.subTest(operation=operation, extra=extra), self.assertRaises(cdsexport.CdsCommandError):
                    cdsexport.parse_args(["--provider", "007", "--" + operation, "run-fixture", *extra])
        resumed = cdsexport.parse_args(["--provider", "007", "--resume", "run-fixture", "--detach"])
        self.assertEqual(cdsexport.engine_arguments(resumed)[-1], "--detach")
        with self.assertRaises(cdsexport.CdsCommandError):
            cdsexport.parse_args(["--provider", "007", "--status", "run-fixture", "--detach"])

    def test_submission_lookup_does_not_resubmit_or_change_options(self):
        args = cdsexport.parse_args(["--provider", "007", "--status-submission", "request-fixture"])
        self.assertEqual(cdsexport.engine_arguments(args), ["--operation", "status", "--provider", "007", "--submission-key", "request-fixture"])

    def test_download_requires_an_explicit_artifact_and_destination(self):
        args = cdsexport.parse_args(["--provider", "007", "--download", "run-fixture", "--artifact", "artifact-fixture", "--output", "./fixture.cms"])
        argv = cdsexport.engine_arguments(args)
        self.assertEqual(argv[-4:], ["--artifact", "artifact-fixture", "--output", os.path.abspath("./fixture.cms")])
        for extra in ([], ["--output", "./fixture.cms"], ["--artifact", "fixture"]):
            with self.subTest(extra=extra), self.assertRaises(cdsexport.CdsCommandError):
                cdsexport.parse_args(["--provider", "007", "--download", "run-fixture", *extra])

    def test_duplicate_ambiguous_and_invalid_selection_options_refuse(self):
        for extra in (["--provider", "4"], ["--profile", "omd-5.2"], ["--detach", "--detach"],
                      ["--pro", "4"], ["--password", "SECRET_SENTINEL"], ["--patients-file", "/private/SENTINEL"],
                      ["--facility", "2147483648"]):
            with self.subTest(extra=extra), self.assertRaises(cdsexport.CdsCommandError) as error:
                cdsexport.parse_args(EXPORT + extra)
            self.assertNotIn("SENTINEL", str(error.exception))
        for physicians in ("007,007", "007,", "007,٣"):
            with self.subTest(physicians=physicians), self.assertRaises(cdsexport.CdsCommandError):
                cdsexport.parse_args([*EXPORT[:3], physicians, *EXPORT[4:]])

    def test_help_works_without_root_or_an_installed_war(self):
        cp = subprocess.run([sys.executable, "-m", "carlos_ctl.cli", "cds-export", "--help"], capture_output=True, text=True)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        for flag in ("--physicians", "--patients-file", "--detach", "--status-submission", "--download"):
            self.assertIn(flag, cp.stdout)


class TestExportEngineBoundary(unittest.TestCase):
    def test_nonroot_refuses_before_probing_or_creating_a_delegation(self):
        with mock.patch.object(os, "geteuid", return_value=1000), mock.patch.object(cdsexport, "_probe") as probe, \
                mock.patch.object(cdsruntime, "create_ticket") as ticket, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cdsexport.cmd_cds_export(EXPORT), 3)
        probe.assert_not_called()
        ticket.assert_not_called()

    def invoke(self, argv, capabilities=CAPS, outcome=0):
        ticket = SimpleNamespace(path="/trusted/fixture.json", remove=mock.Mock(return_value=True))
        with mock.patch.object(os, "geteuid", return_value=0), \
                mock.patch.object(cdsruntime, "operator", return_value=("fixture", 1001)), \
                mock.patch.object(cdsruntime, "java_command", return_value=["java", "-Xmx128m"]), \
                mock.patch.object(cdsexport, "_probe", return_value=capabilities), \
                mock.patch.object(cdsruntime, "create_ticket", return_value=ticket) as created, \
                mock.patch.object(cdsruntime, "run_engine", return_value=outcome) as executed, \
                contextlib.redirect_stderr(io.StringIO()):
            result = cdsexport.cmd_cds_export(argv)
        return result, ticket, created, executed

    def test_unqualified_profiles_never_start_a_clinical_engine(self):
        for profiles in (None, [], ["pcon-2024-02"], "omd-5.2"):
            result, ticket, created, executed = self.invoke(EXPORT, {**CAPS, "qualifiedProfiles": profiles})
            self.assertEqual(result, 2)
            created.assert_not_called()
            executed.assert_not_called()

    def test_export_delegations_and_heap_are_bound_to_the_explicit_request(self):
        result, ticket, created, executed = self.invoke(EXPORT + ["--detach"], outcome=4)
        self.assertEqual(result, 4)
        self.assertEqual(created.call_args.args[:4], ("fixture", 1001, "007", "export"))
        self.assertEqual(created.call_args.kwargs["directory"], "carlos-cds-export-delegations")
        self.assertEqual(executed.call_args.args[0], ["java", "-Xmx512m"])
        self.assertEqual(executed.call_args.args[1], created.call_args.args[4])
        ticket.remove.assert_called_once()

    def test_detached_and_submission_lookup_features_are_checked_independently(self):
        for field, argv in (("detachedSubmission", EXPORT + ["--detach"]),
                            ("submissionLookup", ["--provider", "007", "--status-submission", "fixture"])):
            for value in (None, False, 1):
                with self.subTest(field=field, value=value):
                    result, ticket, created, executed = self.invoke(argv, {**CAPS, field: value})
                    self.assertEqual(result, 2)
                    created.assert_not_called()
                    executed.assert_not_called()

    def test_status_remains_available_without_advertising_new_exports_as_qualified(self):
        result, ticket, created, executed = self.invoke(["--provider", "007", "--status", "run-fixture"],
                                                       {**CAPS, "qualifiedProfiles": []})
        self.assertEqual(result, 0)
        executed.assert_called_once()

    def test_real_probe_refuses_an_import_engine_or_missing_export_identity(self):
        for engine in (None, "cds-import", "cds-export"):
            caps = {**CAPS, "engine": engine}
            command = [sys.executable, "-c", "print(" + repr(json.dumps(caps)) + ")"]
            if engine == "cds-export":
                self.assertEqual(cdsexport._probe(command, dict(os.environ)), caps)
            else:
                with self.assertRaises(cdsexport.CdsCommandError):
                    cdsexport._probe(command, dict(os.environ))


if __name__ == "__main__":
    unittest.main()
