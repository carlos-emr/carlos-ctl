# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""CDS CLI boundary: safe arguments, bounded handshake and supervised execution."""

import contextlib
import io
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from carlos_ctl import cdsimport, cli, util


CAPS = {"protocol": 1, "java": 25, "cliHeapMiB": 512,
        "operations": ["import", "status", "resume", "cancel", "report"]}
IMPORT = ["--input", "/allowed/patient.xml", "--provider", "999998",
          "--source-namespace", "vendor.instance", "--source-profile", "omd-5.2"]


def _account_entry(name):
    """An OS-account fixture, with no credential or authentication material."""
    return SimpleNamespace(pw_name=name)


class TestArguments(unittest.TestCase):
    def test_framework_provider_keys_remain_literal_strings(self):
        for provider in ("T099", "T002", "007", "test-provider_1"):
            request = list(IMPORT)
            request[request.index("--provider") + 1] = provider
            args = cdsimport.parse_args(request)
            argv = cdsimport.engine_arguments(args)
            self.assertEqual(argv[argv.index("--provider") + 1], provider)

    def test_new_import_keeps_merge_off_and_source_profile_explicit(self):
        args = cdsimport.parse_args(IMPORT)
        self.assertEqual(args.operation, "import")
        self.assertFalse(args.merge_existing)
        self.assertFalse(args.dry_run)
        self.assertEqual(args.source_namespace, "vendor.instance")
        self.assertEqual(args.source_profile, "omd-5.2")

    def test_all_nondefault_options_reach_the_engine_as_literal_arguments(self):
        path = "/allowed/space ' $() ; unicode-é.xml"
        args = cdsimport.parse_args([
            "--input", path, "--provider", "007", "--source-namespace", "vendor.instance",
            "--source-profile", "pcon-2024-02", "--program", "12", "--facility", "3",
            "--dry-run", "--no-match-providers", "--merge-existing",
        ])
        self.assertEqual(cdsimport.engine_arguments(args), [
            "--operation", "import", "--provider", "007", "--input", path,
            "--source-namespace", "vendor.instance", "--source-profile", "pcon-2024-02",
            "--program", "12", "--facility", "3", "--merge-existing", "--dry-run",
            "--no-match-providers",
        ])

    def test_relative_input_is_absolute_without_dereferencing_symlinks(self):
        with mock.patch.object(cdsimport.os.path, "realpath", side_effect=AssertionError):
            args = cdsimport.parse_args([*IMPORT[:1], "./batch/link.xml", *IMPORT[2:]])
        self.assertEqual(args.input, os.path.abspath("./batch/link.xml"))

    def test_status_resume_cancel_and_report_use_existing_run_options(self):
        for operation in ("status", "resume", "cancel", "report"):
            with self.subTest(operation=operation):
                args = cdsimport.parse_args(["--" + operation, "run-123", "--provider", "4"])
                self.assertEqual(cdsimport.engine_arguments(args), [
                    "--operation", operation, "--provider", "4", "--run", "run-123"])

    def test_submission_lookup_is_a_read_only_status_with_no_import_options(self):
        args = cdsimport.parse_args(["--status-submission", "submission-123", "--provider", "T099"])
        self.assertEqual(cdsimport.engine_arguments(args), [
            "--operation", "status", "--provider", "T099", "--submission-key", "submission-123"])
        for extra in (["--input", "/allowed/new.xml"], ["--resume", "run-1"],
                      ["--status", "run-1"], ["--status-submission", "different"]):
            with self.subTest(extra=extra), self.assertRaises(cdsimport.CdsCommandError):
                cdsimport.parse_args(["--status-submission", "submission-123", "--provider", "4", *extra])

    def test_synthetic_course_and_shift_are_explicit_and_repeatable(self):
        argv = IMPORT + ["--synthetic", "--course", "9", "--timeshift", "-365"]
        first = cdsimport.engine_arguments(cdsimport.parse_args(argv))
        second = cdsimport.engine_arguments(cdsimport.parse_args(argv))
        self.assertEqual(first, second)
        self.assertIn("--synthetic", first)
        self.assertEqual(first[first.index("--timeshift") + 1], "-365")
        self.assertEqual(first[first.index("--course") + 1], "9")

    def test_invalid_ambiguous_and_replay_changing_arguments_refuse(self):
        invalid = [
            [], IMPORT[:-2], IMPORT[2:], IMPORT + ["--force"],
            IMPORT + ["--merge-existing", "--merge-existing"],
            IMPORT + ["--provider", "4"], IMPORT + ["--program", "1", "--program", "2"],
            IMPORT + ["--pro", "3"], IMPORT + ["--provider", "٣"],
            IMPORT + ["--timeshift", "12"], IMPORT + ["--course", "4"],
            IMPORT + ["--synthetic", "--merge-existing"],
            IMPORT + ["--synthetic", "--timeshift", "2147483648"],
            ["--status", "run-1", "--resume", "run-1", "--provider", "4"],
            ["--status", "run-1", "--status", "run-2", "--provider", "4"],
            ["--resume", "run-1", "--provider", "4", "--timeshift", "0"],
            ["--status", "../run", "--provider", "4"],
            ["--input", "/allowed/patient\nname.xml", *IMPORT[2:]],
        ]
        for argv in invalid:
            with self.subTest(argv=argv):
                with self.assertRaises(cdsimport.CdsCommandError) as error:
                    cdsimport.parse_args(argv)
                self.assertEqual(error.exception.code, 1)

    def test_usage_errors_do_not_echo_patient_values_or_unknown_options(self):
        sentinel = "PATIENT_SENTINEL_1970-01-01"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cdsimport.cmd_cds_import(IMPORT + ["--" + sentinel])
        self.assertEqual(rc, 1)
        self.assertNotIn(sentinel, err.getvalue())
        self.assertIn("CDS_ARGUMENTS", err.getvalue())

    def test_help_is_full_and_has_no_runtime_or_root_dependency(self):
        stdout = io.StringIO()
        with mock.patch.object(util, "carlos_emr_installed", return_value=False), \
                mock.patch.object(cdsimport, "_java_command", side_effect=AssertionError), \
                mock.patch.object(cdsimport.os, "geteuid", return_value=1000), \
                contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as exit_:
                cli.main(["cds-import", "--help"])
        self.assertEqual(exit_.exception.code, 0)
        for item in ("--source-profile", "--merge-existing", "--resume", "130", "committed"):
            self.assertIn(item, stdout.getvalue())


class TestIdentityAndRuntime(unittest.TestCase):
    def test_nonroot_refuses_before_probe_or_execution(self):
        with mock.patch.object(cdsimport.os, "geteuid", return_value=1000), \
                mock.patch.object(cdsimport, "_probe") as probe, \
                mock.patch.object(cdsimport.os, "execve") as execute, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cdsimport.cmd_cds_import(IMPORT), 3)
        probe.assert_not_called()
        execute.assert_not_called()

    # OS-account lookup fixtures contain usernames, never authentication material.
    def test_sudo_identity_comes_from_uid_and_matches_the_name(self):
        with mock.patch.dict(os.environ, {"SUDO_UID": "1001", "SUDO_USER": "operator.name"}, clear=True), \
                mock.patch("carlos_ctl.cdsimport.pwd.getpwuid", return_value=_account_entry("operator.name")):  # ggignore
            self.assertEqual(cdsimport._operator(), ("operator.name", 1001))

    def test_unverifiable_or_conflicting_sudo_identity_refuses(self):
        for env in ({"SUDO_UID": "not-a-uid"}, {"SUDO_UID": "1001", "SUDO_USER": "other"}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch("carlos_ctl.cdsimport.pwd.getpwuid", return_value=_account_entry("operator")):  # ggignore
                with self.assertRaises(cdsimport.CdsCommandError) as error:
                    cdsimport._operator()
                self.assertEqual(error.exception.code, 3)

    def test_direct_root_uses_real_uid_without_inherited_operator_claim(self):
        with mock.patch.dict(os.environ, {"CARLOS_CDS_OPERATOR": "forged"}, clear=True), \
                mock.patch.object(cdsimport.os, "getuid", return_value=0), \
                mock.patch("carlos_ctl.cdsimport.pwd.getpwuid", return_value=_account_entry("root")):  # ggignore
            self.assertEqual(cdsimport._operator(), ("root", 0))

    def test_environment_cannot_override_operator_properties_or_jvm_options(self):
        inherited = {
            "CARLOS_CDS_OPERATOR": "forged", "CARLOS_CDS_PROPERTIES": "/untrusted/file",
            "JAVA_TOOL_OPTIONS": "-javaagent:/untrusted/agent", "JDK_JAVA_OPTIONS": "-Xmx99g",
            "_JAVA_OPTIONS": "-Dunsafe=true", "CLASSPATH": "/untrusted/classes",
            "HTTP_PROXY": "http://proxy:8080", "SSL_CERT_FILE": "/trusted/ca.pem",
            "DATABASE_PASSWORD": "SECRET_SENTINEL",
        }
        with mock.patch.dict(os.environ, inherited, clear=True):
            env = cdsimport._environment("operator", 1001)
        self.assertEqual(env["CARLOS_CDS_OPERATOR"], "operator")
        self.assertEqual(env["CARLOS_CDS_OPERATOR_UID"], "1001")
        self.assertEqual(env["CARLOS_CDS_PROPERTIES"], util.PROPERTIES)
        for key in ("JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS", "CLASSPATH", "DATABASE_PASSWORD"):
            self.assertNotIn(key, env)
        self.assertEqual(env["HTTP_PROXY"], inherited["HTTP_PROXY"])
        self.assertEqual(env["SSL_CERT_FILE"], inherited["SSL_CERT_FILE"])

    def test_java_classpath_service_account_and_bounded_probe_heap(self):
        with mock.patch.object(cdsimport.os.path, "isdir", return_value=True), \
                mock.patch.object(cdsimport.os.path, "isfile", return_value=True), \
                mock.patch.object(cdsimport.dbops, "_find_java", return_value="/jdk25/bin/java"), \
                mock.patch.object(cdsimport.util, "prop_get", side_effect=AssertionError):
            argv = cdsimport._java_command()
        self.assertEqual(argv[:5], ["/usr/sbin/runuser", "-u", "carlos", "--", "/jdk25/bin/java"])
        self.assertIn("-Xmx128m", argv)
        self.assertEqual(argv[-1], cdsimport.MAIN_CLASS)
        classpath = argv[argv.index("-cp") + 1]
        self.assertIn("/WEB-INF/classes:", classpath)
        self.assertIn("/usr/share/tomcat11/lib/*", classpath)
        self.assertNotIn("password", " ".join(argv).lower())

    def test_actual_engine_heap_comes_from_java_capabilities(self):
        command = ["runuser", "-u", "carlos", "--", "/jdk25/java", "-Xmx128m"]
        ticket = SimpleNamespace(path="/trusted/ticket", remove=lambda: True)
        with mock.patch.object(cdsimport.os, "geteuid", return_value=0), \
                mock.patch.object(cdsimport, "_operator", return_value=("root", 0)), \
                mock.patch.object(cdsimport, "_java_command", return_value=command), \
                mock.patch.object(cdsimport, "_probe", return_value={**CAPS, "cliHeapMiB": 768}), \
                mock.patch.object(cdsimport, "_create_ticket", return_value=ticket), \
                mock.patch.object(cdsimport, "_run_engine", return_value=4) as execute, \
                mock.patch.object(cdsimport.util, "prop_get", side_effect=AssertionError):
            self.assertEqual(cdsimport.cmd_cds_import(IMPORT), 4)
        self.assertEqual(execute.call_args.args[0][5], "-Xmx768m")

    def test_submission_lookup_requires_capability_before_ticket_or_execution(self):
        for feature, expected in ((None, 2), (False, 2), (1, 2), (True, 0)):
            with self.subTest(feature=feature), \
                    mock.patch.object(cdsimport.os, "geteuid", return_value=0), \
                    mock.patch.object(cdsimport, "_operator", return_value=("root", 0)), \
                    mock.patch.object(cdsimport, "_java_command", return_value=["/java"]), \
                    mock.patch.object(cdsimport, "_probe", return_value={**CAPS, "submissionLookup": feature}), \
                    mock.patch.object(cdsimport, "_create_ticket", return_value=SimpleNamespace(path="/ticket", remove=lambda: True)) as ticket, \
                    mock.patch.object(cdsimport, "_run_engine", return_value=0) as execute, \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cdsimport.cmd_cds_import([
                    "--status-submission", "submission-123", "--provider", "4"]), expected)
                self.assertEqual(ticket.called, feature is True)
                self.assertEqual(execute.called, feature is True)

    def test_missing_application_is_runtime_exit_two_from_dispatcher(self):
        with mock.patch.object(util, "carlos_emr_installed", return_value=False), \
                mock.patch.object(cdsimport.os, "geteuid", return_value=0), \
                mock.patch.object(cdsimport, "_operator", return_value=("root", 0)), \
                mock.patch.object(cdsimport.os.path, "isdir", return_value=False), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["cds-import", *IMPORT]), 2)

    def test_failed_handshake_never_executes_import(self):
        with mock.patch.object(cdsimport.os, "geteuid", return_value=0), \
                mock.patch.object(cdsimport, "_operator", return_value=("root", 0)), \
                mock.patch.object(cdsimport, "_java_command", return_value=["/java"]), \
                mock.patch.object(cdsimport, "_probe", side_effect=cdsimport.CdsCommandError("CDS_HANDSHAKE", 2)), \
                mock.patch.object(cdsimport, "_create_ticket") as ticket, \
                mock.patch.object(cdsimport, "_run_engine") as execute, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cdsimport.cmd_cds_import(IMPORT), 2)
        execute.assert_not_called()
        ticket.assert_not_called()


class TestTrustedTicket(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name)
        real_fstat, real_stat = os.fstat, os.stat

        def root_owned(result):
            fields = list(result)
            fields[4] = 0
            return os.stat_result(fields)

        self.patches = [
            mock.patch.object(cdsimport, "TICKET_PARENT", str(self.parent)),
            mock.patch.object(cdsimport.grp, "getgrnam", return_value=SimpleNamespace(gr_gid=os.getgid())),
            mock.patch.object(cdsimport.os, "fchown"),
            mock.patch.object(cdsimport.os, "fstat", side_effect=lambda fd: root_owned(real_fstat(fd))),
            mock.patch.object(cdsimport.os, "stat", side_effect=lambda *a, **kw: root_owned(real_stat(*a, **kw))),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def create(self):
        args = cdsimport.parse_args(IMPORT)
        return cdsimport._create_ticket("operator.name", 1001, args, cdsimport.engine_arguments(args))

    def test_ticket_attests_exact_actor_request_digest_and_expiry(self):
        ticket = self.create()
        try:
            path = Path(ticket.path)
            payload = json.loads(path.read_text())
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)
            self.assertEqual(os.stat(path.parent).st_mode & 0o777, 0o750)
            self.assertEqual(payload["protocol"], 1)
            self.assertEqual(payload["operator"], "operator.name")
            self.assertEqual(payload["operatorUid"], 1001)
            self.assertEqual(payload["provider"], "999998")
            self.assertEqual(payload["operation"], "import")
            self.assertEqual(payload["expiresEpochSeconds"] - payload["issuedEpochSeconds"], 300)
            self.assertEqual(path.name, payload["nonce"] + ".json")
            self.assertEqual(payload["argvSha256"], cdsimport.arguments_digest(
                cdsimport.engine_arguments(cdsimport.parse_args(IMPORT))))
            self.assertNotIn("/allowed/patient.xml", path.read_text())
        finally:
            self.assertTrue(ticket.remove())
        self.assertFalse(path.exists())

    def test_digest_is_length_prefixed_utf8_not_ambiguous_concatenation(self):
        self.assertNotEqual(cdsimport.arguments_digest(["ab", "c"]),
                            cdsimport.arguments_digest(["a", "bc"]))
        wire = b"\x00\x00\x00\x02ab\x00\x00\x00\x02\xc3\xa9"
        self.assertEqual(cdsimport.arguments_digest(["ab", "é"]), hashlib.sha256(wire).hexdigest())

    def test_unsafe_parent_or_existing_directory_permissions_refuse(self):
        self.parent.chmod(0o777)
        with self.assertRaises(cdsimport.CdsCommandError):
            self.create()
        self.parent.chmod(0o700)
        directory = self.parent / cdsimport.TICKET_DIRECTORY
        directory.mkdir(mode=0o777)
        directory.chmod(0o777)
        with self.assertRaises(cdsimport.CdsCommandError):
            self.create()

    def test_symlink_directory_is_not_followed(self):
        outside = self.parent / "outside"
        outside.mkdir(mode=0o750)
        (self.parent / cdsimport.TICKET_DIRECTORY).symlink_to(outside, target_is_directory=True)
        with self.assertRaises(cdsimport.CdsCommandError):
            self.create()
        self.assertEqual(list(outside.iterdir()), [])

    def test_exclusive_name_collision_does_not_delete_another_ticket(self):
        directory = self.parent / cdsimport.TICKET_DIRECTORY
        directory.mkdir(mode=0o750)
        nonce = "00000000-0000-4000-8000-000000000000"
        retained = directory / (nonce + ".json")
        retained.write_text("retained")
        with mock.patch.object(cdsimport.uuid, "uuid4", return_value=nonce):
            with self.assertRaises(cdsimport.CdsCommandError):
                self.create()
        self.assertEqual(retained.read_text(), "retained")

    def test_cleanup_refuses_replaced_inode(self):
        ticket = self.create()
        path = Path(ticket.path)
        replacement = path.with_suffix(".replacement")
        replacement.write_text("other request")
        replacement.replace(path)
        self.assertFalse(ticket.remove())
        self.assertEqual(path.read_text(), "other request")

    def test_failed_ticket_creation_never_starts_engine(self):
        with mock.patch.object(cdsimport.os, "geteuid", return_value=0), \
                mock.patch.object(cdsimport, "_operator", return_value=("root", 0)), \
                mock.patch.object(cdsimport, "_java_command", return_value=["/java"]), \
                mock.patch.object(cdsimport, "_probe", return_value=CAPS), \
                mock.patch.object(cdsimport, "_create_ticket", side_effect=cdsimport.CdsCommandError("CDS_DELEGATION", 2)), \
                mock.patch.object(cdsimport, "_run_engine") as execute, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cdsimport.cmd_cds_import(IMPORT), 2)
        execute.assert_not_called()

    def test_cleanup_failure_is_visible_without_erasing_the_engine_outcome(self):
        ticket = SimpleNamespace(path="/trusted/ticket", remove=lambda: False)
        for outcome, expected in ((0, 2), (3, 3), (4, 4), (130, 130)):
            err = io.StringIO()
            with self.subTest(outcome=outcome), \
                    mock.patch.object(cdsimport.os, "geteuid", return_value=0), \
                    mock.patch.object(cdsimport, "_operator", return_value=("root", 0)), \
                    mock.patch.object(cdsimport, "_java_command", return_value=["/java"]), \
                    mock.patch.object(cdsimport, "_probe", return_value=CAPS), \
                    mock.patch.object(cdsimport, "_create_ticket", return_value=ticket), \
                    mock.patch.object(cdsimport, "_run_engine", return_value=outcome), \
                    contextlib.redirect_stderr(err):
                self.assertEqual(cdsimport.cmd_cds_import(IMPORT), expected)
                self.assertIn("clinical commits", err.getvalue())


class TestBoundedRealProbe(unittest.TestCase):
    def probe(self, program):
        return cdsimport._probe([sys.executable, "-c", program], dict(os.environ))

    def test_valid_protocol_and_noisy_stderr_are_bounded_and_not_printed(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = self.probe("import sys; print('PATIENT_SENTINEL', file=sys.stderr); print(" + repr(json.dumps(CAPS)) + ")")
        self.assertEqual(result, CAPS)
        self.assertNotIn("PATIENT_SENTINEL", out.getvalue() + err.getvalue())

    def test_protocol_mismatch_and_invalid_shapes_are_not_accepted(self):
        invalid = [
            {**CAPS, "protocol": 2}, {**CAPS, "protocol": True}, {**CAPS, "java": 21},
            {**CAPS, "java": "25"}, {**CAPS, "operations": ["import"]},
            {**CAPS, "operations": [True]}, [], None,
            {**CAPS, "cliHeapMiB": 127}, {**CAPS, "cliHeapMiB": 16385},
            {**CAPS, "cliHeapMiB": "512"}, {**CAPS, "cliHeapMiB": True},
        ]
        for caps in invalid:
            with self.subTest(caps=caps):
                with self.assertRaises(cdsimport.CdsCommandError) as error:
                    self.probe("print(" + repr(json.dumps(caps)) + ")")
                self.assertEqual(error.exception.code, 2)

    def test_duplicate_fields_or_trailing_log_lines_refuse(self):
        for payload in ('{"protocol":2,"protocol":1,"java":25,"operations":[]}',
                        json.dumps(CAPS) + "\nPATIENT_SENTINEL"):
            with self.subTest(payload=payload):
                with self.assertRaises(cdsimport.CdsCommandError) as error:
                    self.probe("print(" + repr(payload) + ")")
                self.assertNotIn("PATIENT_SENTINEL", str(error.exception))

    def test_oversized_stdout_or_stderr_is_refused_without_unbounded_capture(self):
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream):
                with self.assertRaises(cdsimport.CdsCommandError) as error:
                    self.probe("import sys; sys." + stream + ".write('x'*200000)")
                self.assertIn("exceeded", str(error.exception))

    def test_a_hung_probe_is_terminated_at_its_deadline(self):
        start = time.monotonic()
        with mock.patch.object(cdsimport, "CAPABILITY_TIMEOUT", .1):
            with self.assertRaises(cdsimport.CdsCommandError) as error:
                self.probe("import time; time.sleep(30)")
        self.assertIn("timed out", str(error.exception))
        self.assertLess(time.monotonic() - start, 5)

    def test_nonzero_probe_exit_is_not_success_even_with_valid_json(self):
        with self.assertRaises(cdsimport.CdsCommandError):
            self.probe("import sys; print(" + repr(json.dumps(CAPS)) + "); sys.exit(4)")


class TestRealExecution(unittest.TestCase):
    def test_supervision_preserves_engine_outcomes_without_capturing_import_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "engine.py"
            for outcome in (0, 1, 2, 3, 4, 130):
                engine.write_text(
                    "import sys\n"
                    "if '--capabilities' in sys.argv:\n"
                    " print(" + repr(json.dumps(CAPS)) + ")\n"
                    "else:\n"
                    " print('run=opaque-123 outcome=recorded')\n"
                    " sys.exit(" + str(outcome) + ")\n", encoding="utf-8")
                wrapper = (
                    "from unittest import mock; from carlos_ctl import cdsimport; "
                    "import sys; "
                    "cdsimport._java_command=lambda: [sys.executable," + repr(str(engine)) + "]; "
                    "cdsimport._operator=lambda: ('root',0); "
                    "cdsimport._create_ticket=lambda *a: mock.Mock(path='/trusted/ticket',remove=lambda: True); "
                    "cdsimport.os.geteuid=lambda: 0; "
                    "sys.exit(cdsimport.cmd_cds_import(" + repr(IMPORT) + "))"
                )
                cp = subprocess.run([sys.executable, "-c", wrapper], text=True, capture_output=True)
                with self.subTest(outcome=outcome):
                    self.assertEqual(cp.returncode, outcome, cp.stderr)
                    self.assertEqual(cp.stdout, "run=opaque-123 outcome=recorded\n")

    def test_sigterm_reaches_the_executed_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "engine.py"
            ready = Path(tmp) / "ready"
            engine.write_text(
                "import sys,time,pathlib\n"
                "if '--capabilities' in sys.argv:\n"
                " print(" + repr(json.dumps(CAPS)) + ")\n"
                "else:\n"
                " pathlib.Path(" + repr(str(ready)) + ").touch()\n"
                " time.sleep(30)\n", encoding="utf-8")
            wrapper = (
                "from carlos_ctl import cdsimport; import sys; "
                "from unittest import mock; "
                "cdsimport._java_command=lambda: [sys.executable," + repr(str(engine)) + "]; "
                "cdsimport._operator=lambda: ('root',0); cdsimport.os.geteuid=lambda: 0; "
                "cdsimport._create_ticket=lambda *a: mock.Mock(path='/trusted/ticket',remove=lambda: True); "
                "sys.exit(cdsimport.cmd_cds_import(" + repr(IMPORT) + "))"
            )
            process = subprocess.Popen([sys.executable, "-c", wrapper])
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists(), "engine did not reach execution")
                process.send_signal(signal.SIGTERM)
                self.assertEqual(process.wait(timeout=5), 128 + signal.SIGTERM)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
