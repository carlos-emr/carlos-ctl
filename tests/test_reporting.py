# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""The Query by Example reporting account (carlos-emr/carlos#3372).

carlos-emr runs Query by Example as `carlos_report` and ships, with the
WAR, the grant list that decides what that account may read. These tests
pin what carlos-ctl does with it: create the account with no grants,
apply the deployed list after db-users and after every migration (and
never fail the verb when it cannot), report drift from `check`, and drop
the account in destroy-data. The CARLOS_SRC tests read the real list from
a carlos checkout; the live test runs it against a disposable MariaDB.
"""

import argparse
import contextlib
import errno
import fcntl
import io
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from unittest import mock

from carlos_ctl import dbops, reporting, util

from .carlos_src import carlos_path, requires_carlos_src

ROWS_HEADER = ("INSERT INTO carlos_report_tables (table_name, access, "
               "excluded_columns, reason) VALUES")
CARLOS_GRANTS = ("src", "main", "resources", "db", "reporting",
                 "carlos_report_grants.sql")


class _Cp(object):
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _write_list(directory, rows):
    path = os.path.join(directory, "carlos_report_grants.sql")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("-- header\nSET SESSION sql_log_bin = 0;\n" + ROWS_HEADER + "\n")
        fh.write("\n".join(rows) + "\n\nDELIMITER //\nBEGIN NOT ATOMIC\nEND//\n")
    return path


class TestAccountStatements(unittest.TestCase):

    def test_the_account_is_created_on_every_host_without_a_grant(self):
        sql = reporting.account_sql("pw")
        for host in ("localhost", "127.0.0.1"):
            self.assertIn(f"CREATE USER IF NOT EXISTS 'carlos_report'@'{host}' IDENTIFIED BY 'pw';", sql)
            self.assertIn(f"ALTER USER 'carlos_report'@'{host}' IDENTIFIED BY 'pw';", sql)
        # what it may read comes only from the WAR's list, never from here
        self.assertNotIn("GRANT", sql.upper())

    def test_the_password_is_escaped_for_the_string_literal(self):
        self.assertIn("IDENTIFIED BY 'a\\'b'", reporting.account_sql("a'b"))

    def test_destroy_data_drops_every_host(self):
        sql = reporting.drop_sql()
        self.assertIn("DROP USER IF EXISTS 'carlos_report'@'localhost';", sql)
        self.assertIn("DROP USER IF EXISTS 'carlos_report'@'127.0.0.1';", sql)


class TestReadingTheGrantList(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="reporting-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_rows_are_read_until_the_closing_semicolon(self):
        path = _write_list(self.dir, [
            "('demographic', 'reportable', NULL, NULL),",
            "('casemgmt_note', 'reportable', 'password', 'Legacy note-lock password'),",
            "('security', 'restricted', NULL, 'Password hashes'),",
            "('PageMonitor', 'restricted', NULL, 'Session identifiers');",
        ])
        grant_list = reporting.read_grant_list(path)
        self.assertEqual(grant_list.reportable,
                         {"demographic": frozenset(), "casemgmt_note": frozenset({"password"})})
        self.assertEqual(grant_list.restricted, frozenset({"security", "PageMonitor"}))

    def test_a_missing_file_is_not_a_list(self):
        self.assertIsNone(reporting.read_grant_list(os.path.join(self.dir, "absent.sql")))

    def test_a_malformed_row_is_not_trusted(self):
        path = _write_list(self.dir, [
            "('demographic', 'reportable', NULL, NULL),",
            "('security', 'maybe', NULL, NULL);",
        ])
        self.assertIsNone(reporting.read_grant_list(path))

    def test_an_undecodable_file_is_not_a_list(self):
        path = _write_list(self.dir, ["('demographic', 'reportable', NULL, NULL);"])
        with open(path, "ab") as fh:
            fh.write(b"-- \xff\xfe is not UTF-8\n")
        self.assertIsNone(reporting.read_grant_list(path))

    def test_rows_that_never_end_are_not_a_list(self):
        path = os.path.join(self.dir, "open.sql")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(ROWS_HEADER + "\n('demographic', 'reportable', NULL, NULL),\n")
        self.assertIsNone(reporting.read_grant_list(path))


@requires_carlos_src
class TestTheShippedGrantList(unittest.TestCase):
    """Contract with the carlos checkout: the file this CLI applies is where
    the WAR puts it and in the format this module reads."""

    def test_the_war_path_mirrors_the_source_path(self):
        self.assertTrue(reporting.GRANTS_SQL.endswith(
            os.path.join("WEB-INF", "classes", "db", "reporting", "carlos_report_grants.sql")))
        self.assertTrue(os.path.isfile(carlos_path(*CARLOS_GRANTS)))

    def test_the_shipped_list_parses_and_restricts_the_credential_tables(self):
        grant_list = reporting.read_grant_list(carlos_path(*CARLOS_GRANTS))
        self.assertIsNotNone(grant_list)
        for table in ("security", "SecurityArchive", "ServiceClient", "ServiceAccessToken",
                      "oscarKeys", "publicKeys", "PageMonitor", "casemgmt_note_lock", "studylogin"):
            self.assertIn(table, grant_list.restricted)
            self.assertNotIn(table, grant_list.reportable)
        self.assertIn("demographic", grant_list.reportable)
        self.assertIn("password", grant_list.reportable["casemgmt_note"])

    def test_the_shipped_list_selects_no_database_of_its_own(self):
        # sync_grants passes the schema as the default database; a USE in the
        # file would silently point the grants somewhere else.
        with open(carlos_path(*CARLOS_GRANTS), encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotRegex(text, r"(?im)^\s*USE\s")


class TestSyncingTheGrants(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="reporting-sync-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = _write_list(self.dir, ["('demographic', 'reportable', NULL, NULL);"])
        self.calls = []

    def _sync(self, account_answer=_Cp(0, "2\n"), script_answer=_Cp(0)):
        def fake_db_root(args, **kw):
            self.calls.append((list(args), kw.get("input")))
            if "-e" in args:
                return account_answer
            return script_answer
        err = io.StringIO()
        out = io.StringIO()
        with mock.patch.object(reporting, "GRANTS_SQL", self.path), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            applied = reporting.sync_grants("carlos", fake_db_root)
        return applied, out.getvalue(), err.getvalue()

    def test_the_deployed_list_runs_against_the_clinical_schema(self):
        applied, _out, err = self._sync()
        self.assertTrue(applied)
        self.assertEqual(err, "")
        args, script = self.calls[-1]
        self.assertEqual(args, ["carlos"])
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(script, fh.read())

    def test_no_list_in_the_deployed_war_changes_nothing(self):
        os.unlink(self.path)
        applied, out, _err = self._sync()
        self.assertFalse(applied)
        self.assertEqual(self.calls, [])
        self.assertIn("ships no Query by Example grant list", out)

    def test_an_account_that_does_not_exist_yet_is_left_for_db_users(self):
        applied, out, _err = self._sync(account_answer=_Cp(0, "0\n"))
        self.assertFalse(applied)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("db-users", out)

    def test_an_unanswered_account_probe_is_a_warning_not_a_guess(self):
        applied, _out, err = self._sync(account_answer=_Cp(1, "", "ERROR 2002"))
        self.assertFalse(applied)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("WARNING", err)

    def test_an_undecodable_list_is_reported_and_never_raised(self):
        with open(self.path, "ab") as fh:
            fh.write(b"-- \xff\n")
        applied, _out, err = self._sync()
        self.assertFalse(applied)
        self.assertIn("not valid UTF-8", err)
        self.assertEqual([args for args, _script in self.calls if "-e" not in args], [])

    def test_the_refusal_is_reported_rather_than_the_echoed_script(self):
        # The client echoes the failing BEGIN NOT ATOMIC block before its ERROR
        # line; the warning must carry MariaDB's message, not the SQL.
        echoed = ("--------------\nBEGIN NOT ATOMIC\n" + "    DECLARE v INT;\n" * 60
                  + "END\n--------------\n\nERROR 1644 (45000) at line 545: carlos_report_grants.sql: "
                    "PUBLIC holds privileges that reach the CARLOS schema\n")
        applied, _out, err = self._sync(script_answer=_Cp(1, "", echoed))
        self.assertFalse(applied)
        self.assertIn("ERROR 1644 (45000) at line 545: carlos_report_grants.sql: PUBLIC holds", err)
        self.assertNotIn("BEGIN NOT ATOMIC", err)

    def test_a_list_that_fails_is_reported_and_never_raised(self):
        refused = _Cp(1, "", "ERROR 1644 (45000) at line 545: carlos_report_grants.sql: PUBLIC "
                             "holds privileges that reach the CARLOS schema")
        applied, _out, err = self._sync(script_answer=refused)
        self.assertFalse(applied)
        self.assertIn("PUBLIC holds privileges", err)
        self.assertIn("carlos-ctl db-users", err)


class _DbUsersHarness(unittest.TestCase):
    """cmd_db_users with every external effect replaced: properties files in a
    temp dir, MariaDB answered by a fake that records the batch."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="reporting-users-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.properties = os.path.join(self.dir, "carlos.properties")
        # Generated per run: fixtures carry no credential literal for secret scanners.
        self.app_password = util.genpw()
        with open(self.properties, "w", encoding="latin-1") as fh:
            fh.write(f"db_username = carlos\ndb_password = {self.app_password}\n"
                     "db_reporting_username =\ndb_reporting_password =\n")
        self.batches = []
        self.synced = []
        self.count_answer = _Cp(0, "412\n")
        self.service_active = False
        self.err = ""

    def _db_users(self, argv=()):
        def fake_db_root(args, **kw):
            if "-e" in args:
                return self.count_answer
            self.batches.append(kw.get("input", ""))
            return _Cp(0)

        settings = argparse.Namespace(db_name="carlos")
        patches = [
            # db-users takes the database-ownership lock; a temp file stands in
            # for /var/lib/carlos-emr, which a test runner neither owns nor needs
            mock.patch.object(util, "DB_OWNERSHIP_LOCK", os.path.join(self.dir, ".finish-install.lock")),
            mock.patch.object(dbops, "need_root", lambda verb: None),
            mock.patch.object(dbops, "require_db_root", lambda: None),
            mock.patch.object(dbops.config, "load", lambda: settings),
            mock.patch.object(dbops, "db_root", fake_db_root),
            mock.patch.object(dbops, "run", lambda *a, **k: _Cp(0 if self.service_active else 3)),
            mock.patch.object(dbops, "PROPERTIES", self.properties),
            mock.patch.object(dbops, "DRUGREF_PROPERTIES", os.path.join(self.dir, "absent-drugref")),
            mock.patch.object(dbops, "BACKUP_ENV", os.path.join(self.dir, "absent-backup.env")),
            mock.patch.object(reporting, "sync_grants",
                              lambda db_name, db_root: self.synced.append(db_name) or True),
        ]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                rc = dbops.cmd_db_users(list(argv))
            self.err = err.getvalue()
            return rc

    def prop(self, key):
        return util.prop_unescape(util.prop_get(self.properties, key) or "")


class TestDbUsersProvisionsTheReportingAccount(_DbUsersHarness):

    def test_the_account_is_created_and_recorded_for_the_application(self):
        self.assertEqual(self._db_users(), 0)
        batch = self.batches[0]
        password = self.prop("db_reporting_password")
        self.assertEqual(self.prop("db_reporting_username"), "carlos_report")
        self.assertEqual(len(password), 32)
        self.assertIn(reporting.account_sql(password), batch)
        # the application keeps its own, different credential
        self.assertEqual(self.prop("db_password"), self.app_password)
        self.assertNotEqual(password, self.app_password)

    def test_the_grants_are_refreshed_when_the_schema_already_has_tables(self):
        self._db_users()
        self.assertEqual(self.synced, ["carlos"])

    def test_an_empty_schema_waits_for_the_migration_to_grant_anything(self):
        self.count_answer = _Cp(0, "0\n")
        self._db_users()
        self.assertEqual(self.synced, [])

    def test_an_unanswered_table_count_still_refreshes_the_grants(self):
        self.count_answer = _Cp(1, "", "ERROR 2013 (HY000): Lost connection")
        self.assertEqual(self._db_users(), 0)
        self.assertEqual(self.synced, ["carlos"])
        self.assertIn("could not count the tables of carlos", self.err)

    def test_an_existing_reporting_password_is_kept(self):
        kept = util.genpw()
        util.prop_set(self.properties, "db_reporting_password", kept)
        self._db_users()
        self.assertEqual(self.prop("db_reporting_password"), kept)
        self.assertIn(f"IDENTIFIED BY '{kept}'", self.batches[0])

    def test_a_corrected_reporting_username_asks_for_a_restart(self):
        util.prop_set(self.properties, "db_password", self.app_password)
        util.prop_set(self.properties, "db_reporting_username", "carlos")
        util.prop_set(self.properties, "db_reporting_password", util.genpw())
        self.service_active = True
        self._db_users()
        self.assertEqual(self.prop("db_reporting_username"), "carlos_report")
        self.assertIn("carlos-ctl restart", self.err)

    def test_unchanged_credentials_ask_for_no_restart(self):
        util.prop_set(self.properties, "db_reporting_username", "carlos_report")
        util.prop_set(self.properties, "db_reporting_password", util.genpw())
        self.service_active = True
        self._db_users()
        self.assertNotIn("carlos-ctl restart", self.err)

    def test_a_published_placeholder_is_never_adopted(self):
        util.prop_set(self.properties, "db_reporting_password", "password")
        self._db_users()
        self.assertNotEqual(self.prop("db_reporting_password"), "password")

    def test_rotation_issues_a_new_reporting_password(self):
        old = util.genpw()
        util.prop_set(self.properties, "db_reporting_password", old)
        self._db_users(["--new-passwords"])
        self.assertNotEqual(self.prop("db_reporting_password"), old)


class TestAnInterruptedRunIsRepairedByTheNext(_DbUsersHarness):
    """The recovery contract for a db-users stopped between its ALTER USERs
    and its writes (power loss, a full disk, a kill). MariaDB is left
    accepting a password carlos.properties does not record, and the next
    db-users sets MariaDB back to what the file records."""

    def accepted(self, user):
        """The password the fake MariaDB was last set to for user@localhost."""
        prefix = f"ALTER USER '{user}'@'localhost' IDENTIFIED BY '"
        found = [line[len(prefix):-len("';")] for batch in self.batches
                 for line in batch.splitlines() if line.startswith(prefix)]
        return found[-1] if found else None

    def interrupted(self, fault, argv=()):
        """A db-users stopped by `fault` as it records the reporting password,
        after MariaDB has already been set."""
        real = util.prop_set

        def stopping(path, key, value):
            if key == reporting.PASSWORD_KEY:
                raise fault
            real(path, key, value)
        with mock.patch.object(dbops, "prop_set", stopping), self.assertRaises(type(fault)):
            self._db_users(argv)

    def test_a_first_run_stopped_by_a_full_disk_is_repaired_by_the_next(self):
        self.interrupted(OSError(errno.ENOSPC, "No space left on device"))
        stranded = self.accepted("carlos_report")
        self.assertIsNotNone(stranded)
        self.assertEqual(self.prop("db_reporting_password"), "")
        self.assertEqual(self._db_users(), 0)
        recorded = self.prop("db_reporting_password")
        self.assertEqual(self.accepted("carlos_report"), recorded)
        self.assertNotEqual(recorded, stranded)

    def test_a_rotation_killed_before_it_records_is_undone_by_the_next_run(self):
        recorded = util.genpw()
        util.prop_set(self.properties, "db_reporting_password", recorded)
        self.interrupted(KeyboardInterrupt(), ["--new-passwords"])
        self.assertNotEqual(self.accepted("carlos_report"), recorded)
        self.assertEqual(self.prop("db_reporting_password"), recorded)
        self.assertEqual(self._db_users(), 0)
        self.assertEqual(self.accepted("carlos_report"), recorded)
        self.assertEqual(self.prop("db_reporting_password"), recorded)
        # the application account, recorded before the stop, agrees too
        self.assertEqual(self.accepted("carlos"), self.prop("db_password"))

    def test_an_interrupted_run_gives_the_lock_back(self):
        # rotate and finish-install call db-users in-process; a stop must not
        # leave them, or the next run, facing a lock nobody will release
        self.interrupted(OSError(errno.EIO, "Input/output error"))
        lock = os.path.join(self.dir, ".finish-install.lock")
        fd = os.open(lock, os.O_WRONLY)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


class TestMigrationRefreshesTheGrants(unittest.TestCase):

    def _flyway(self, command, returncode):
        synced = []
        settings = argparse.Namespace(db_name="carlos", db_host="127.0.0.1", db_port="3306",
                                      flyway_locations="classpath:db/migration/common")
        webapp = tempfile.mkdtemp(prefix="reporting-webapp-")
        self.addCleanup(shutil.rmtree, webapp, True)
        os.makedirs(os.path.join(webapp, "WEB-INF", "lib"))
        properties = os.path.join(webapp, "carlos.properties")
        with open(properties, "w", encoding="latin-1") as fh:
            fh.write(f"db_username = carlos\ndb_password = {util.genpw()}\n")
        with mock.patch.object(dbops.config, "load", lambda: settings), \
                mock.patch.object(dbops, "WEBAPP", webapp), \
                mock.patch.object(dbops, "PROPERTIES", properties), \
                mock.patch.object(dbops, "_find_java", lambda: "/usr/bin/java"), \
                mock.patch.object(dbops, "run", lambda *a, **k: _Cp(returncode)), \
                mock.patch.object(reporting, "sync_grants",
                                  lambda db_name, db_root: synced.append(db_name) or True):
            rc = dbops.run_flyway(command)
        return rc, synced

    def test_a_successful_migration_refreshes_the_grants(self):
        self.assertEqual(self._flyway("migrate", 0), (0, ["carlos"]))

    def test_a_failed_migration_leaves_them_alone(self):
        self.assertEqual(self._flyway("migrate", 1), (1, []))

    def test_validate_and_info_change_nothing(self):
        self.assertEqual(self._flyway("validate", 0), (0, []))
        self.assertEqual(self._flyway("info", 0), (0, []))


class TestCheckReportsTheAccount(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="reporting-check-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = _write_list(self.dir, [
            "('demographic', 'reportable', NULL, NULL),",
            "('casemgmt_note', 'reportable', 'password', 'Legacy note-lock password'),",
            "('appointment', 'reportable', NULL, NULL),",
            "('security', 'restricted', NULL, 'Password hashes');",
        ])
        self.properties = os.path.join(self.dir, "carlos.properties")
        self.password = util.genpw()
        with open(self.properties, "w", encoding="latin-1") as fh:
            fh.write(f"db_reporting_username = carlos_report\ndb_reporting_password = {self.password}\n")
        # What the shipped list leaves behind: the same grants on both hosts,
        # casemgmt_note column by column without its hidden password.
        self.answers = {
            "hosts": "localhost\n127.0.0.1\n", "extra": "0", "public": "0",
            "tables_priv": self._both("carlos\tdemographic\tSelect|", "carlos\tcasemgmt_note\t|Select",
                                      "carlos\tappointment\tSelect|"),
            "columns_priv": self._both("carlos\tcasemgmt_note\tnote", "carlos\tcasemgmt_note\tnote_id"),
            "schema_columns": "casemgmt_note\tnote_id\ncasemgmt_note\tnote\ncasemgmt_note\tpassword\n",
            "existing": "demographic\ncasemgmt_note\nappointment\nsecurity\n",
        }
        self.queries = []
        self.logins = []
        self.login_answer = None

    @staticmethod
    def _both(*rows):
        return "".join(f"{host}\t{row}\n" for host in reporting.REPORT_HOSTS for row in rows)

    def _answer(self, args):
        sql = args[-1]
        self.queries.append(sql)
        if "SELECT Host FROM mysql.global_priv" in sql:
            return _Cp(0, self.answers["hosts"])
        if "User = 'PUBLIC'" in sql:
            return _Cp(0, self.answers["public"])
        if "roles_mapping" in sql:
            return _Cp(0, self.answers["extra"])
        if "FROM mysql.tables_priv" in sql:
            return _Cp(0, self.answers["tables_priv"])
        if "FROM mysql.columns_priv" in sql:
            return _Cp(0, self.answers["columns_priv"])
        if "information_schema.COLUMNS" in sql:
            return _Cp(0, self.answers["schema_columns"])
        if "information_schema.TABLES" in sql:
            return _Cp(0, self.answers["existing"])
        raise AssertionError(sql)

    def _check(self):
        def fake_db_root(args, **kw):
            return self._answer(args)

        def fake_login(password):
            self.logins.append(password)
            return self.login_answer
        with mock.patch.object(reporting, "GRANTS_SQL", self.path), \
                mock.patch.object(reporting, "PROPERTIES", self.properties):
            return reporting.check_lines("carlos", fake_db_root, login=fake_login)

    def verdicts(self):
        return [verdict for verdict, _message in self._check()]

    def bad(self):
        return " | ".join(m for v, m in self._check() if v == "bad")

    def test_an_account_in_step_with_the_list_is_ok(self):
        lines = self._check()
        self.assertEqual([v for v, _m in lines], ["ok"])
        self.assertIn("3 reportable table(s)", lines[0][1])
        self.assertEqual(self.logins, [self.password])

    def test_a_restricted_table_grant_is_bad(self):
        self.answers["tables_priv"] += "localhost\tcarlos\tsecurity\tSelect|\n"
        self.assertIn("security (restricted)", self.bad())

    def test_an_unclassified_table_grant_is_bad(self):
        self.answers["tables_priv"] += "127.0.0.1\tcarlos\tqbe_unclassified\tSelect|\n"
        self.assertIn("qbe_unclassified (not in the grant list)", self.bad())

    def test_a_hidden_column_grant_is_bad(self):
        self.answers["columns_priv"] += "localhost\tcarlos\tcasemgmt_note\tpassword\n"
        self.assertIn("casemgmt_note.password", self.bad())

    def test_a_table_wide_grant_on_a_table_with_hidden_columns_is_bad(self):
        self.answers["tables_priv"] = self.answers["tables_priv"].replace(
            "casemgmt_note\t|Select", "casemgmt_note\tSelect|")
        self.assertIn("casemgmt_note (every column", self.bad())

    def test_a_write_privilege_is_bad(self):
        self.answers["tables_priv"] = self.answers["tables_priv"].replace(
            "demographic\tSelect|", "demographic\tSelect,Insert|")
        self.assertIn("demographic (Select,Insert)", self.bad())

    def test_a_write_privilege_on_a_column_is_bad(self):
        # MariaDB records a column privilege in tables_priv.Column_priv too.
        self.answers["tables_priv"] = self.answers["tables_priv"].replace(
            "casemgmt_note\t|Select", "casemgmt_note\t|Select,Update")
        self.assertIn("casemgmt_note (Select,Update)", self.bad())

    def test_a_column_only_grant_on_a_table_without_hidden_columns_is_incomplete(self):
        self.answers["tables_priv"] = self.answers["tables_priv"].replace(
            "demographic\tSelect|", "demographic\t|Select")
        bad = self.bad()
        self.assertIn("demographic (only some columns)", bad)
        self.assertNotIn("must not read", bad)

    def test_a_permitted_column_missing_on_one_host_is_bad(self):
        self.answers["columns_priv"] = self.answers["columns_priv"].replace(
            "127.0.0.1\tcarlos\tcasemgmt_note\tnote\n", "")
        bad = self.bad()
        self.assertIn("casemgmt_note.note for 127.0.0.1", bad)
        self.assertNotIn("for localhost", bad)

    def test_a_missing_grant_after_a_migration_is_bad(self):
        self.answers["tables_priv"] = self._both("carlos\tdemographic\tSelect|",
                                                 "carlos\tcasemgmt_note\t|Select")
        self.assertIn("appointment", self.bad())

    def test_grants_on_one_host_do_not_cover_the_other(self):
        self.answers["tables_priv"] = self.answers["tables_priv"].replace(
            "127.0.0.1\tcarlos\tappointment\tSelect|\n", "")
        bad = self.bad()
        self.assertIn("carlos_report@127.0.0.1", bad)
        self.assertIn("appointment", bad)
        self.assertNotIn("carlos_report@localhost", bad)

    def test_a_stray_host_is_bad(self):
        self.answers["hosts"] += "%\n"
        self.assertIn("also exists for host(s) %", self.bad())

    def test_public_grants_and_extra_privileges_are_bad(self):
        self.answers["public"] = "1"
        self.answers["extra"] = "1"
        lines = self._check()
        self.assertEqual(sum(1 for v, _m in lines if v == "bad"), 2)

    def test_routine_and_proxy_grants_are_counted(self):
        self._check()
        public = next(q for q in self.queries if "User = 'PUBLIC'" in q)
        extra = next(q for q in self.queries if "roles_mapping" in q)
        self.assertIn("mysql.procs_priv WHERE User = 'PUBLIC'", public)
        self.assertIn("mysql.proxies_priv WHERE User = 'carlos_report'", extra)

    def test_the_configured_port_reaches_the_login_probe(self):
        seen = []
        with mock.patch.object(reporting, "GRANTS_SQL", self.path), \
                mock.patch.object(reporting, "PROPERTIES", self.properties), \
                mock.patch.object(reporting, "_login_problem",
                                  lambda password, port: seen.append((password, port))):
            reporting.check_lines("carlos", lambda args, **kw: self._answer(args), db_port="3307")
        self.assertEqual(seen, [(self.password, "3307")])

    def test_a_password_that_does_not_log_in_is_bad(self):
        self.login_answer = ("bad", "the carlos_report password does not log in")
        self.assertEqual(self.verdicts(), ["bad"])

    def test_a_busy_account_is_only_a_note(self):
        self.login_answer = ("note", "carlos_report is at its connection limit")
        self.assertEqual(self.verdicts(), ["note"])

    def test_a_login_that_cannot_be_tried_is_a_note_and_never_ok(self):
        # MariaDB not answering on CARLOS_DB_PORT says nothing about the
        # password, nor that Query by Example can connect.
        answers = iter([_Cp(0, "1\n"), _Cp(1, "", "ERROR 2003 (HY000): Can't connect to server "
                                                  "on '127.0.0.1' (111)")])
        with mock.patch.object(reporting, "GRANTS_SQL", self.path), \
                mock.patch.object(reporting, "PROPERTIES", self.properties), \
                mock.patch.object(reporting.util, "run", lambda cmd, **kw: next(answers)):
            lines = reporting.check_lines("carlos", lambda args, **kw: self._answer(args), db_port="3307")
        self.assertEqual([verdict for verdict, _message in lines], ["note"])
        self.assertIn("ERROR 2003", lines[0][1])

    def test_the_recorded_password_is_unescaped_before_logging_in(self):
        util.prop_set(self.properties, "db_reporting_password", util.prop_escape("a=b:c"))
        self._check()
        self.assertEqual(self.logins, ["a=b:c"])

    def test_a_missing_account_is_bad(self):
        self.answers["hosts"] = ""
        self.assertEqual(self.verdicts(), ["bad"])

    def test_unrecorded_credentials_are_bad(self):
        with open(self.properties, "w", encoding="latin-1") as fh:
            fh.write("db_reporting_username =\n")
        self.assertIn("bad", self.verdicts())
        self.assertEqual(self.logins, [])

    def test_a_recorded_username_without_a_password_is_bad(self):
        with open(self.properties, "w", encoding="latin-1") as fh:
            fh.write("db_reporting_username = carlos_report\ndb_reporting_password =\n")
        self.assertIn(reporting.PASSWORD_KEY, self.bad())
        self.assertNotIn("ok", self.verdicts())
        self.assertEqual(self.logins, [])

    def test_an_older_war_is_only_a_note(self):
        os.unlink(self.path)
        self.assertEqual(self.verdicts(), ["note"])


class TestTheLoginProbe(unittest.TestCase):

    def _probe(self, *answers, port="3306"):
        calls = []

        def fake_run(cmd, **kw):
            calls.append((cmd, kw))
            answer = answers[min(len(calls), len(answers)) - 1]
            if isinstance(answer, Exception):
                raise answer
            return answer
        with mock.patch.object(reporting.util, "run", fake_run):
            return reporting._login_problem("s-" + uuid.uuid4().hex, port), calls

    def test_both_hosts_logging_in_is_no_problem(self):
        problem, calls = self._probe(_Cp(0, "1\n"))
        self.assertIsNone(problem)
        self.assertEqual(len(calls), 2)
        for cmd, kw in calls:
            self.assertIn("--user=carlos_report", cmd)
            self.assertTrue(kw["env"]["MYSQL_PWD"].startswith("s-"))
            self.assertFalse(any(kw["env"]["MYSQL_PWD"] in part for part in cmd))

    def test_a_refused_login_names_the_host(self):
        problem, _calls = self._probe(_Cp(0, "1\n"), _Cp(1, "", "ERROR 1045 (28000): Access denied"))
        self.assertEqual(problem[0], "bad")
        self.assertIn("carlos_report@127.0.0.1", problem[1])

    def test_the_tcp_probe_uses_the_configured_port(self):
        _problem, calls = self._probe(_Cp(0, "1\n"), port="3307")
        socket_cmd, tcp_cmd = calls[0][0], calls[1][0]
        self.assertIn("--protocol=socket", socket_cmd)
        self.assertNotIn("--port=3307", socket_cmd)
        self.assertIn("--port=3307", tcp_cmd)
        self.assertIn("--host=127.0.0.1", tcp_cmd)

    def test_a_connection_failure_is_a_note_not_a_bad_password(self):
        problem, _calls = self._probe(_Cp(0, "1\n"), _Cp(1, "", "ERROR 2003 (HY000): Can't connect to "
                                                               "server on '127.0.0.1' (111)"))
        self.assertEqual(problem[0], "note")
        self.assertIn("ERROR 2003", problem[1])

    def test_a_busy_account_proves_nothing(self):
        problem, _calls = self._probe(_Cp(1, "", "ERROR 1203 (42000): User carlos_report already has more "
                                               "than 'max_user_connections' active connections"))
        self.assertEqual(problem[0], "note")

    def test_no_client_is_a_note(self):
        problem, _calls = self._probe(FileNotFoundError("mariadb"))
        self.assertEqual(problem[0], "note")


@unittest.skipUnless(os.environ.get("CARLOS_TEST_MARIADB_SOCKET") and shutil.which("mariadb"),
                     "set CARLOS_TEST_MARIADB_SOCKET to run live SQL regressions")
@requires_carlos_src
class TestTheShippedListAgainstMariaDB(unittest.TestCase):
    """Runs carlos's real grant list against a disposable server, as a uniquely
    named account on a uniquely named schema, and checks what MariaDB then
    lets that account do."""

    def setUp(self):
        suffix = uuid.uuid4().hex[:10]
        self.schema = "carlos_report_test_" + suffix
        self.user = "qbe_report_" + suffix
        self.password = util.genpw()
        self.addCleanup(self.root, f"DROP DATABASE IF EXISTS `{self.schema}`; "
                                   f"DROP USER IF EXISTS '{self.user}'@'localhost'; "
                                   f"REVOKE ALL PRIVILEGES ON `{self.schema}`.* FROM PUBLIC;",
                        check=False)
        self.root(f"CREATE DATABASE `{self.schema}`; "
                  f"CREATE TABLE `{self.schema}`.demographic (demographic_no INT PRIMARY KEY, last_name VARCHAR(30)); "
                  f"CREATE TABLE `{self.schema}`.security (security_no INT PRIMARY KEY, password VARCHAR(255)); "
                  f"CREATE TABLE `{self.schema}`.casemgmt_note (note_id INT PRIMARY KEY, note TEXT, password VARCHAR(255)); "
                  f"CREATE TABLE `{self.schema}`.qbe_unclassified (id INT PRIMARY KEY, secret VARCHAR(40)); "
                  f"INSERT INTO `{self.schema}`.security VALUES (1, 'hash'); "
                  f"CREATE USER '{self.user}'@'localhost' IDENTIFIED BY '{self.password}';")
        with open(carlos_path(*CARLOS_GRANTS), encoding="utf-8") as fh:
            self.script = f"SET @carlos_report_user = '{self.user}';\n" + fh.read()

    @staticmethod
    def _client(args, sql, user="root", password=None):
        env = dict(os.environ)
        if password is not None:
            env["MYSQL_PWD"] = password
        return subprocess.run(
            ["mariadb", "--no-defaults", "--protocol=socket", f"--user={user}",
             "--socket=" + os.environ["CARLOS_TEST_MARIADB_SOCKET"], "-N", "-B"] + args,
            input=sql, text=True, capture_output=True, env=env)

    def root(self, sql, database=None, check=True):
        cp = self._client([database] if database else [], sql)
        if check:
            self.assertEqual(cp.returncode, 0, cp.stderr)
        return cp

    def as_report(self, sql):
        return self._client([self.schema], sql, user=self.user, password=self.password)

    def test_the_account_reads_only_reportable_tables_and_columns(self):
        self.root(self.script, database=self.schema)
        self.assertEqual(self.as_report("SELECT COUNT(*) FROM demographic").returncode, 0)
        self.assertEqual(self.as_report("SELECT note_id, note FROM casemgmt_note").returncode, 0)
        for refused in ("SELECT * FROM security", "SELECT password FROM casemgmt_note",
                        "SELECT * FROM casemgmt_note", "SELECT * FROM qbe_unclassified",
                        "DELETE FROM demographic", "CREATE TABLE x (id INT)"):
            cp = self.as_report(refused)
            self.assertNotEqual(cp.returncode, 0, refused)
            self.assertRegex(cp.stderr, r"ERROR (1142|1143)", refused)

    def test_running_it_again_revokes_what_was_granted_by_hand(self):
        self.root(f"GRANT SELECT, INSERT ON `{self.schema}`.security TO '{self.user}'@'localhost';")
        self.root(self.script, database=self.schema)
        self.root(self.script, database=self.schema)
        grants = self.root(f"SHOW GRANTS FOR '{self.user}'@'localhost'").stdout
        self.assertNotIn("`security`", grants)
        self.assertNotIn("INSERT", grants)
        self.assertIn("MAX_USER_CONNECTIONS 6", grants)

    def test_running_it_again_revokes_a_proxy_grant(self):
        self.root(f"GRANT PROXY ON ''@'%' TO '{self.user}'@'localhost';")
        self.root(self.script, database=self.schema)
        left = self.root(f"SELECT COUNT(*) FROM mysql.proxies_priv WHERE User = '{self.user}'")
        self.assertEqual(left.stdout.strip(), "0")

    def test_a_public_grant_on_the_schema_stops_it(self):
        self.root(f"GRANT SELECT, INSERT ON `{self.schema}`.security TO '{self.user}'@'localhost';")
        self.root(f"GRANT SELECT ON `{self.schema}`.* TO PUBLIC;")
        cp = self.root(self.script, database=self.schema, check=False)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("PUBLIC holds privileges", cp.stderr)
        # It revoked before it refused, so the stale grant is gone all the same.
        grants = self.root(f"SHOW GRANTS FOR '{self.user}'@'localhost'").stdout
        self.assertNotIn("`security`", grants)
        self.assertNotIn("INSERT", grants)
