# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""finish-install and the two things that kept a beta2 fresh install down.

A 2026.08.0-beta2 install on an Ubuntu 26.04 VM (carlos-emr/carlos#5439) met an
apache2 left over from an earlier web-stack setup on port 80, and a MariaDB whose
root@localhost had been given a password by that same setup, so root could not
reach it over the unix socket. The package configure now offers to stop the web
server and names the MariaDB cause; this is the repair the install points at, so
it does the same, at a terminal:

  * a root password with no socket login is put back to the distribution
    default (socket only, no MariaDB root password) after the operator confirms;
    the old password is typed to the mariadb client itself, never to this
    process, and is not kept;
  * another service on 80/443 is stopped after the operator confirms, through
    carlos-emr's shipped helper, which decides what may be stopped;
  * at boot, or with nobody at a terminal, nothing is changed: the cause is
    named and the install stays pending.

Run (from the repository root):
    python3 -m unittest discover -v -s tests -t .
"""

import contextlib
import io
import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from carlos_ctl import provision as p

ACCESS_DENIED = ("ERROR 1045 (28000): Access denied for user 'root'@'localhost' "
                 "(using password: NO)")
LOCKED = "ERROR 4151 (HY000): Access denied, this account is locked"


def completed(rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], rc, stdout, stderr)


class FinishInstallCase(unittest.TestCase):

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.marker = os.path.join(self.tmp, "incomplete")
        with open(self.marker, "w") as fh:
            fh.write("reason=test\nreset_admin=false\ndemo_data=false\n")
        props = os.path.join(self.tmp, "properties")
        open(props, "w").close()
        for name, value in [("MARKER", self.marker),
                            ("SEED_SENTINEL", os.path.join(self.tmp, "seed")),
                            ("PROPERTIES", props),
                            ("LOCK", os.path.join(self.tmp, "lock")),
                            ("ROOT_MY_CNF", os.path.join(self.tmp, "my.cnf")),
                            ("START_VETO", p.Path(self.tmp, "veto"))]:
            self.stack.enter_context(mock.patch.object(p, name, value))
        self.addCleanup(self._release_lock)
        self.mocks = {}
        for owner, name, value in [
                (p, "need_root", None), (p, "_wait_for_db", True),
                (p.config, "load", types.SimpleNamespace(db_name="carlos", bind_ip="0.0.0.0")),
                (p.config, "cmd_init_config", 0), (p.config, "apply_nginx", 0),
                (p.dbops, "cmd_db_apply_settings", 0), (p.dbops, "cmd_db_users", 0),
                (p, "_table_count", 431), (p.dbops, "run_flyway", 0),
                (p.dbops, "cmd_db_migrate", 0), (p.dbops, "cmd_bootstrap_admin", 0),
                (p.dbops, "cmd_demo_data", 0), (p, "_report_drugref_seed", None),
                (p, "_o19_import_running", None), (p.util, "reset_emr_start_limit", None),
                (p.util, "which", "/usr/bin/logger"),
                (p, "run", completed()), (p, "_interactive", True),
                (p, "_host_conflicts", None),
                (p.dbops, "db_root", completed(1, stderr=ACCESS_DENIED)),
                (p.dbops, "db_root_ok", True)]:
            self.mocks[name] = self.stack.enter_context(
                mock.patch.object(owner, name, return_value=value))
        self.answers = []
        self.stack.enter_context(mock.patch("builtins.input", side_effect=self._input))

    def _input(self, prompt):
        self.prompts = getattr(self, "prompts", []) + [prompt]
        if not self.answers:
            raise AssertionError(f"unexpected prompt: {prompt}")
        return self.answers.pop(0)

    def _release_lock(self):
        if p._LOCK_HANDLE is not None:
            p._LOCK_HANDLE.close()
            p._LOCK_HANDLE = None

    def finish(self, argv=None):
        self._release_lock()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = p.cmd_finish_install(argv or [])
            except SystemExit as exc:
                rc = exc.code
        return rc, out.getvalue(), err.getvalue()

    def commands(self):
        return [c.args[0] for c in self.mocks["run"].call_args_list]

    def resets(self):
        return [c for c in self.commands() if c[:1] == ["mariadb"]]


class TestRootPasswordFromAnEarlierSetup(FinishInstallCase):

    def setUp(self):
        super().setUp()
        self.mocks["_wait_for_db"].return_value = False

    def test_confirmed_reset_restores_socket_only_root_and_finishes(self):
        self.answers = ["y"]
        rc, out, err = self.finish()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.resets(), [["mariadb", "--protocol=socket", "--user=root",
                                          "--password", "-e", p._SOCKET_ONLY_ROOT]])
        self.assertFalse(os.path.exists(self.marker))
        self.assertIn("distribution default", out)
        # Recorded where the configure records its own changes to the host.
        self.assertTrue(any(c[:3] == ["logger", "-t", "carlos-emr"] for c in self.commands()))

    def test_the_password_goes_to_the_client_prompt_never_through_carlos_ctl(self):
        self.answers = ["y"]
        self.finish()
        reset = self.resets()[0]
        # --password with no value: the mariadb client asks on the terminal.
        self.assertIn("--password", reset)
        self.assertFalse(any(a.startswith(("--password=", "-p")) for a in reset))
        call = [c for c in self.mocks["run"].call_args_list if c.args[0][:1] == ["mariadb"]][0]
        self.assertNotIn("input", call.kwargs)
        self.assertNotIn("env", call.kwargs)
        self.assertNotIn("capture_output", call.kwargs)  # the client owns the terminal

    def test_the_reset_is_the_distribution_default_not_a_kept_password(self):
        self.assertEqual(p._SOCKET_ONLY_ROOT,
                         "ALTER USER root@localhost IDENTIFIED VIA unix_socket "
                         "OR mysql_native_password USING 'invalid'")

    def test_declined_reset_changes_nothing_and_stays_pending(self):
        self.answers = ["n"]
        rc, out, err = self.finish()
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.resets(), [])
        self.assertTrue(os.path.exists(self.marker))
        self.mocks["cmd_db_users"].assert_not_called()

    def test_wrong_password_leaves_root_alone_and_fails(self):
        self.answers = ["y"]
        self.mocks["run"].side_effect = lambda cmd, **kw: completed(1 if cmd[:1] == ["mariadb"] else 0)
        rc, out, err = self.finish()
        self.assertNotEqual(rc, 0)
        self.assertIn("root was left as it is", err)
        self.mocks["cmd_db_users"].assert_not_called()

    def test_boot_never_asks_and_names_the_cause(self):
        self.mocks["_mariadb_cause"] = self.stack.enter_context(mock.patch.object(
            p, "_mariadb_cause", return_value="root@localhost was given a password"))
        rc, out, err = self.finish(["--boot"])
        self.assertNotEqual(rc, 0)
        self.assertFalse(getattr(self, "prompts", []))
        self.assertEqual(self.resets(), [])
        self.assertIn("root@localhost was given a password", err)

    def test_no_terminal_never_asks(self):
        self.mocks["_interactive"].return_value = False
        rc, out, err = self.finish()
        self.assertNotEqual(rc, 0)
        self.assertFalse(getattr(self, "prompts", []))
        self.assertEqual(self.resets(), [])

    def test_locked_root_is_not_offered_a_reset_it_cannot_take(self):
        self.mocks["db_root"].return_value = completed(1, stderr=LOCKED)
        rc, out, err = self.finish()
        self.assertNotEqual(rc, 0)
        self.assertFalse(getattr(self, "prompts", []))
        self.assertEqual(self.resets(), [])

    def test_a_password_still_stored_in_root_my_cnf_is_flagged(self):
        with open(p.ROOT_MY_CNF, "w") as fh:
            fh.write("[client]\nuser=root\npassword=old\n")
        self.answers = ["y"]
        rc, out, err = self.finish()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("still stores a MariaDB password", err)

    def test_without_a_known_cause_the_old_message_stands(self):
        self.mocks["_interactive"].return_value = False
        rc, out, err = self.finish()
        self.assertIn("Start it (systemctl status mariadb)", err)


class TestFrontDoorConflicts(FinishInstallCase):

    PLAN = ["will-stop: apache2.service (package apache2), on 0.0.0.0:80",
            "blocked: docker-proxy (pid 4, docker.service) is listening on 0.0.0.0:443"]
    STOPPED = ["stopped: apache2.service (package apache2) was listening on 0.0.0.0:80, "
               "which the CARLOS front door needs; it is stopped and disabled"]

    def setUp(self):
        super().setUp()
        self.helper_calls = []

        def helper(*args):
            self.helper_calls.append(list(args))
            return list(self.PLAN if "--plan" in args else self.STOPPED)
        self.mocks["_host_conflicts"].side_effect = helper

    def test_terminal_confirmation_stops_the_service_before_nginx_is_applied(self):
        # init-config applies the front door itself, and with nginx running
        # and the site enabled it fails outright on a port it cannot bind; so
        # the conflict is offered before init-config, not just before the
        # final nginx step (review finding).
        order = []
        helper = self.mocks["_host_conflicts"].side_effect
        self.mocks["_host_conflicts"].side_effect = lambda *a: order.append(" ".join(a)) or helper(*a)
        self.mocks["cmd_init_config"].side_effect = lambda *a, **k: order.append("init-config") or 0
        self.mocks["apply_nginx"].side_effect = lambda *a, **k: order.append("nginx") or 0
        self.answers = ["y"]
        rc, out, err = self.finish()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.helper_calls, [["front-door", "--plan", "0.0.0.0"],
                                             ["front-door", "0.0.0.0"]])
        self.assertEqual(order, ["front-door --plan 0.0.0.0", "front-door 0.0.0.0",
                                 "init-config", "nginx"])
        self.assertIn("apache2.service (package apache2), on 0.0.0.0:80", out)
        self.assertIn("apache2.service (package apache2) was listening", out)
        self.assertTrue(any(c[:3] == ["logger", "-t", "carlos-emr"] for c in self.commands()))

    def test_declined_leaves_the_service_running(self):
        self.answers = ["n"]
        self.finish()
        self.assertEqual(self.helper_calls, [["front-door", "--plan", "0.0.0.0"]])

    def test_boot_only_names_what_holds_the_ports(self):
        rc, out, err = self.finish(["--boot"])
        self.assertFalse(getattr(self, "prompts", []))
        self.assertEqual(self.helper_calls, [["front-door", "--plan", "0.0.0.0"]])
        self.assertIn("apache2.service (package apache2)", err)
        self.assertIn("was not stopped", err)

    def test_nothing_to_stop_asks_nothing(self):
        self.PLAN = ["blocked: docker-proxy (pid 4, docker.service) is listening on 0.0.0.0:443"]
        rc, out, err = self.finish()
        self.assertFalse(getattr(self, "prompts", []))
        self.assertEqual(len(self.helper_calls), 1)

    def test_an_older_carlos_emr_without_the_helper_behaves_as_before(self):
        self.mocks["_host_conflicts"].side_effect = None
        self.mocks["_host_conflicts"].return_value = None
        rc, out, err = self.finish()
        self.assertEqual(rc, 0, out + err)
        self.assertFalse(getattr(self, "prompts", []))
        self.mocks["apply_nginx"].assert_called_once()


class TestHelperAccess(unittest.TestCase):

    def test_missing_helper_is_none_not_an_error(self):
        with mock.patch.object(p, "HOST_CONFLICTS", "/nonexistent/carlos-emr-host-conflicts"):
            self.assertIsNone(p._host_conflicts("mariadb"))
            self.assertEqual(p._mariadb_cause(), "")

    def test_helper_lines_are_read_from_the_shipped_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            helper = os.path.join(tmp, "carlos-emr-host-conflicts")
            with open(helper, "w") as fh:
                fh.write("#!/bin/sh\necho \"cause: because $1\"\n")
            os.chmod(helper, 0o755)
            with mock.patch.object(p, "HOST_CONFLICTS", helper):
                self.assertEqual(p._mariadb_cause(), "because mariadb")

    def test_the_helper_path_is_carlos_emrs(self):
        self.assertEqual(p.HOST_CONFLICTS, "/usr/lib/carlos-emr/carlos-emr-host-conflicts")


if __name__ == "__main__":
    unittest.main()
