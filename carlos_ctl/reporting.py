# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""The read-only database account Query by Example runs as (carlos-emr/carlos#3372).

Query by Example lets an authorised user type a SELECT. Before carlos#3372 it
ran on the application account, which holds ALL PRIVILEGES on the clinical
schema, so the application's SQL validator was the only thing between a
report and the password hashes, OAuth secrets and live session identifiers
stored there. carlos-emr now runs every Query by Example query as
``carlos_report`` instead, and refuses to run one at all without it.

What that account may read is decided by carlos-emr, not here: the WAR ships
``WEB-INF/classes/db/reporting/carlos_report_grants.sql``, which lists every
table as ``reportable`` or ``restricted`` and rebuilds the account's grants
from that list (revoke everything, then SELECT on each reportable table that
exists, minus hidden columns). This module owns the rest:

  * ``account_sql``   the CREATE/ALTER USER statements db-users runs;
  * ``sync_grants``   apply the deployed WAR's list, after db-users and after
                      every successful migration (a migration can add a
                      reportable table, which is only granted once it exists);
  * ``check_lines``   what ``carlos-ctl check`` reports about the account.

A grant sync that fails never fails the verb that ran it: the clinical
system keeps working, Query by Example stays closed (the application refuses
an account it cannot verify), and ``check`` reports the cause.
"""

import os
import re
from typing import Dict, List, NamedTuple, Optional, Tuple

from . import util
from .util import PROPERTIES, WEBAPP, log, prop_get, warn

REPORT_USER = "carlos_report"
REPORT_HOSTS = ("localhost", "127.0.0.1")
USERNAME_KEY = "db_reporting_username"
PASSWORD_KEY = "db_reporting_password"
GRANTS_SQL = os.path.join(WEBAPP, "WEB-INF", "classes", "db", "reporting",
                          "carlos_report_grants.sql")

# One classification row of the grant list. The file's header documents the
# format; carlos's QueryByExampleReportingClassificationTest enforces it.
_ROW = re.compile(
    r"^\('(?P<table>[A-Za-z0-9_]+)', '(?P<access>reportable|restricted)', "
    r"(?:NULL|'(?P<columns>[A-Za-z0-9_]+(?:,[A-Za-z0-9_]+)*)'), "
    r"(?:NULL|'[^']+')\)[,;]$")
_ROWS_HEADER = ("INSERT INTO carlos_report_tables (table_name, access, "
                "excluded_columns, reason) VALUES")


class GrantList(NamedTuple):
    """The deployed WAR's classification: table -> hidden columns (empty set
    when every column is granted) for reportable tables, and the restricted
    table names."""
    reportable: Dict[str, frozenset]
    restricted: frozenset


def account_sql(password: str) -> str:
    """CREATE-or-ALTER for every host the application may connect from. No
    GRANT here: until the grant list runs, the account can log in and read
    nothing, which is exactly the fail-closed state the application expects
    of an account that is not provisioned yet."""
    pw = util.sql_escape(password)
    lines = []
    for host in REPORT_HOSTS:
        lines.append(f"CREATE USER IF NOT EXISTS '{REPORT_USER}'@'{host}' IDENTIFIED BY '{pw}';")
        lines.append(f"ALTER USER '{REPORT_USER}'@'{host}' IDENTIFIED BY '{pw}';")
    return "\n".join(lines) + "\n"


def drop_sql() -> str:
    """DROP USER for every host, for destroy-data."""
    return "".join(f"DROP USER IF EXISTS '{REPORT_USER}'@'{host}';\n"
                   for host in REPORT_HOSTS)


def read_grant_list(path: Optional[str] = None) -> Optional[GrantList]:
    """Parse the classification rows (of the deployed WAR's list unless a path
    is given), or None when the file is absent (a carlos-emr from before
    carlos#3372) or not in the expected format."""
    try:
        with open(path or GRANTS_SQL, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    try:
        start = lines.index(_ROWS_HEADER)
    except ValueError:
        return None
    reportable, restricted = {}, set()
    for line in lines[start + 1:]:
        m = _ROW.match(line)
        if not m:
            return None
        if m.group("access") == "restricted":
            restricted.add(m.group("table"))
        else:
            columns = m.group("columns")
            reportable[m.group("table")] = frozenset(
                c.lower() for c in columns.split(",")) if columns else frozenset()
        if line.endswith(";"):
            return GrantList(reportable, frozenset(restricted))
    return None


def _account_exists(db_root) -> Optional[bool]:
    cp = db_root(["-N", "-B", "-e",
                  "SELECT COUNT(*) FROM mysql.global_priv "
                  f"WHERE User = '{REPORT_USER}'"], capture_output=True)
    if cp.returncode != 0:
        return None
    return cp.stdout.strip() not in ("", "0")


def sync_grants(db_name: str, db_root) -> bool:
    """Rebuild the account's grants from the deployed WAR's list. Returns
    True when applied. Never raises or dies: every reason it could not run
    is said once, and the account is left with whatever MariaDB committed
    (at worst fewer grants, never more: the list starts with REVOKE ALL)."""
    if not os.path.isfile(GRANTS_SQL):
        log("the deployed carlos-emr ships no Query by Example grant list "
            f"({GRANTS_SQL}); the reporting account was left as it is")
        return False
    exists = _account_exists(db_root)
    if exists is None:
        warn(f"could not check whether the {REPORT_USER} account exists; its "
             "Query by Example grants were not refreshed (carlos-ctl check)")
        return False
    if not exists:
        log(f"the {REPORT_USER} account does not exist yet ('carlos-ctl db-users' "
            "creates it); Query by Example stays closed until it does")
        return False
    try:
        with open(GRANTS_SQL, encoding="utf-8") as fh:
            script = fh.read()
    except OSError as e:
        warn(f"cannot read {GRANTS_SQL} ({e.strerror}); the Query by Example "
             "grants were not refreshed")
        return False
    # The script selects nothing itself: the schema is the default database.
    cp = db_root([db_name], input=script, capture_output=True)
    if cp.returncode != 0:
        # MariaDB's message names the cause (a PUBLIC grant, a missing schema)
        # and never a credential: the script carries no password.
        reason = " ".join((cp.stderr or "").split())[:400] or f"exit {cp.returncode}"
        warn(f"the Query by Example grant list did not apply: {reason}")
        warn("Query by Example stays closed until it does; fix the cause, then "
             "run 'carlos-ctl db-users'")
        return False
    return True


def _count(db_root, sql: str) -> Optional[int]:
    cp = db_root(["-N", "-B", "-e", sql], capture_output=True)
    if cp.returncode != 0:
        return None
    try:
        return int(cp.stdout.strip() or "0")
    except ValueError:
        return None


def _granted_tables(db_root, db_name: str) -> Optional[List[Tuple[str, str, str]]]:
    cp = db_root(["-N", "-B", "-e",
                  "SELECT Db, Table_name, CONCAT(Table_priv, '|', Column_priv) "
                  "FROM mysql.tables_priv "
                  f"WHERE User = '{REPORT_USER}'"], capture_output=True)
    if cp.returncode != 0:
        return None
    rows = []
    for line in cp.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            rows.append((parts[0], parts[1], parts[2]))
    return rows


def check_lines(db_name: str, db_root) -> List[Tuple[str, str]]:
    """(verdict, message) pairs for carlos-ctl check, verdict one of ok, note,
    bad. Read-only."""
    out: List[Tuple[str, str]] = []
    grant_list = read_grant_list()
    if grant_list is None:
        if not os.path.isfile(GRANTS_SQL):
            out.append(("note", "the deployed carlos-emr predates the Query by Example "
                                "reporting account; nothing to check"))
        else:
            out.append(("bad", f"cannot parse the Query by Example grant list {GRANTS_SQL} "
                               "(reinstall carlos-emr)"))
        return out

    user = (prop_get(PROPERTIES, USERNAME_KEY) or "").strip()
    password = (prop_get(PROPERTIES, PASSWORD_KEY) or "").strip()
    if user != REPORT_USER or not password:
        out.append(("bad", f"{PROPERTIES} does not name the {REPORT_USER} account "
                           f"({USERNAME_KEY}/{PASSWORD_KEY}); Query by Example is closed "
                           "until 'carlos-ctl db-users' sets them"))

    hosts = _count(db_root, "SELECT COUNT(*) FROM mysql.global_priv "
                            f"WHERE User = '{REPORT_USER}' AND Host IN "
                            + "(" + ", ".join(f"'{h}'" for h in REPORT_HOSTS) + ")")
    if hosts is None:
        out.append(("bad", f"could not read the {REPORT_USER} account from mysql.global_priv"))
        return out
    if hosts < len(REPORT_HOSTS):
        out.append(("bad", f"the {REPORT_USER} account is missing (Query by Example is closed); "
                           "run 'carlos-ctl db-users'"))
        return out

    extra = _count(db_root,
                   "SELECT (SELECT COUNT(*) FROM mysql.global_priv "
                   f"WHERE User = '{REPORT_USER}' "
                   "AND COALESCE(JSON_VALUE(Priv, '$.access'), 0) <> 0)"
                   f" + (SELECT COUNT(*) FROM mysql.db WHERE User = '{REPORT_USER}')"
                   f" + (SELECT COUNT(*) FROM mysql.procs_priv WHERE User = '{REPORT_USER}')"
                   f" + (SELECT COUNT(*) FROM mysql.roles_mapping WHERE User = '{REPORT_USER}')")
    public = _count(db_root,
                    "SELECT (SELECT COUNT(*) FROM mysql.global_priv WHERE User = 'PUBLIC' "
                    "AND COALESCE(JSON_VALUE(Priv, '$.access'), 0) <> 0)"
                    f" + (SELECT COUNT(*) FROM mysql.db WHERE User = 'PUBLIC' AND '{db_name}' LIKE Db)"
                    f" + (SELECT COUNT(*) FROM mysql.tables_priv WHERE User = 'PUBLIC' AND Db = '{db_name}')"
                    f" + (SELECT COUNT(*) FROM mysql.columns_priv WHERE User = 'PUBLIC' AND Db = '{db_name}')")
    granted = _granted_tables(db_root, db_name)
    existing = _existing_tables(db_root, db_name)
    if extra is None or public is None or granted is None or existing is None:
        out.append(("bad", f"could not read the {REPORT_USER} account's privileges"))
        return out
    if public:
        out.append(("bad", "PUBLIC holds privileges that reach the CARLOS schema, so every "
                           f"account, {REPORT_USER} included, inherits them "
                           "(SHOW GRANTS FOR PUBLIC; revoke them, then 'carlos-ctl db-users')"))
    if extra:
        out.append(("bad", f"the {REPORT_USER} account holds privileges beyond SELECT on "
                           "reportable tables (a global, schema, routine or role grant); "
                           "Query by Example refuses it until 'carlos-ctl db-users' rebuilds them"))

    reportable_lower = {t.lower() for t in grant_list.reportable}
    restricted_lower = {t.lower() for t in grant_list.restricted}
    # Table_priv|Column_priv: "Select|" for a table grant, "|Select" for a
    # column-list grant; anything else is a privilege the list never grants.
    wrong = sorted(t for db, t, privs in granted
                   if db != db_name or t.lower() in restricted_lower
                   or t.lower() not in reportable_lower
                   or privs.strip("|") != "Select")
    hidden = {t.lower(): cols for t, cols in grant_list.reportable.items() if cols}
    # A table-wide SELECT ("Select|") on a table with hidden columns reads those
    # columns too; the list grants such tables column by column ("|Select").
    wrong += sorted(f"{t} (every column)" for db, t, privs in granted
                    if db == db_name and t.lower() in hidden
                    and privs.split("|", 1)[0] != "")
    columns = _granted_columns(db_root)
    if columns is None:
        out.append(("bad", f"could not read the {REPORT_USER} account's column privileges"))
        return out
    wrong += sorted(f"{t}.{c}" for t, c in columns
                    if c.lower() in hidden.get(t.lower(), frozenset()))
    if wrong:
        out.append(("bad", f"the {REPORT_USER} account is granted tables it must not read: "
                           + ", ".join(wrong[:10]) + (" ..." if len(wrong) > 10 else "")
                           + " (run 'carlos-ctl db-users')"))
    expected = {t for t in grant_list.reportable if t in existing}
    have = {t for db, t, _p in granted if db == db_name}
    missing = sorted(expected - have)
    if missing:
        out.append(("bad", f"{len(missing)} reportable table(s) are not granted to "
                           f"{REPORT_USER} yet (for example {missing[0]}); "
                           "run 'carlos-ctl db-users' to refresh the Query by Example grants"))
    if not public and not extra and not wrong and not missing:
        out.append(("ok", f"Query by Example runs as {REPORT_USER}: SELECT on "
                          f"{len(have)} reportable table(s), nothing else"))
    return out


def _granted_columns(db_root) -> Optional[List[Tuple[str, str]]]:
    cp = db_root(["-N", "-B", "-e",
                  "SELECT Table_name, Column_name FROM mysql.columns_priv "
                  f"WHERE User = '{REPORT_USER}'"], capture_output=True)
    if cp.returncode != 0:
        return None
    return [tuple(line.split("\t", 1)) for line in cp.stdout.splitlines()
            if line.count("\t") == 1]


def _existing_tables(db_root, db_name: str) -> Optional[set]:
    cp = db_root(["-N", "-B", "-e",
                  "SELECT TABLE_NAME FROM information_schema.TABLES "
                  f"WHERE TABLE_SCHEMA = '{db_name}' AND TABLE_TYPE = 'BASE TABLE'"],
                 capture_output=True)
    if cp.returncode != 0:
        return None
    return {line.strip() for line in cp.stdout.splitlines() if line.strip()}
