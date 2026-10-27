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
                      reportable table, which is only granted once it exists),
                      first dropping any login other than ``REPORT_HOSTS``:
                      the list grants every login the account has;
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
# format; carlos's QueryByExampleReportingClassificationUnitTest enforces it.
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
    except (OSError, UnicodeDecodeError):
        # Unreadable and undecodable alike: there is no list to trust.
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


def sync_grants(db_name: str, db_root) -> bool:
    """Rebuild the account's grants from the deployed WAR's list. Returns
    True when applied. Never raises or dies: every reason it could not run
    is said once, and the account is left with whatever MariaDB committed
    (at worst fewer grants, never more: the list starts with REVOKE ALL)."""
    if not os.path.isfile(GRANTS_SQL):
        log("the deployed carlos-emr ships no Query by Example grant list "
            f"({GRANTS_SQL}); the reporting account was left as it is")
        return False
    hosts = _account_hosts(db_root)
    if hosts is None:
        warn(f"could not check whether the {REPORT_USER} account exists; its "
             "Query by Example grants were not refreshed (carlos-ctl check)")
        return False
    if not hosts:
        log(f"the {REPORT_USER} account does not exist yet ('carlos-ctl db-users' "
            "creates it); Query by Example stays closed until it does")
        return False
    try:
        with open(GRANTS_SQL, encoding="utf-8") as fh:
            script = fh.read()
    except (OSError, UnicodeDecodeError) as e:
        reason = e.strerror if isinstance(e, OSError) else "not valid UTF-8"
        warn(f"cannot read {GRANTS_SQL} ({reason}); the Query by Example "
             "grants were not refreshed (reinstall carlos-emr)")
        return False
    if not _drop_stray_hosts(db_root, hosts):
        return False
    # The script selects nothing itself: the schema is the default database.
    cp = db_root([db_name], input=script, capture_output=True)
    if cp.returncode != 0:
        # MariaDB's message names the cause (a PUBLIC grant, a missing schema)
        # and never a credential: the script carries no password.
        warn(f"the Query by Example grant list did not apply: {util.mariadb_error(cp)}")
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


def _account_hosts(db_root) -> Optional[set]:
    cp = db_root(["-N", "-B", "-e",
                  f"SELECT Host FROM mysql.global_priv WHERE User = '{REPORT_USER}'"],
                 capture_output=True)
    if cp.returncode != 0:
        return None
    return {line.strip() for line in cp.stdout.splitlines() if line.strip()}


def _drop_stray_hosts(db_root, hosts: set) -> bool:
    """Drop every login of the account other than REPORT_HOSTS, before the
    grant list runs. The list grants each login the account has, so a stray
    one (an operator's carlos_report@'%', say) would be handed the same
    clinical tables, behind a password carlos-ctl neither sets nor records.
    carlos-ctl creates no other login, and check calls one drift.

    Returns False, having said why, when a stray could not be removed or is
    still there afterwards (a host name the client printed escaped): the
    list must not run then."""
    approved = set(REPORT_HOSTS)
    stray = sorted(hosts - approved)
    for host in stray:
        cp = db_root(["-e", f"DROP USER IF EXISTS '{REPORT_USER}'@'{util.sql_escape(host)}'"],
                     capture_output=True)
        if cp.returncode != 0:
            warn(f"could not drop the {REPORT_USER} login for host '{host}' "
                 f"({util.mariadb_error(cp)}); the Query by Example grants were not "
                 "refreshed (carlos-ctl check)")
            return False
        warn(f"dropped the {REPORT_USER} login for host '{host}': Query by Example's "
             f"account logs in only from {' and '.join(REPORT_HOSTS)}, and the grant "
             "list would have given that login the same tables")
    if stray:
        left = _account_hosts(db_root)
        if left is None or left - approved:
            warn(f"the {REPORT_USER} account still has a login other than "
                 f"{' and '.join(REPORT_HOSTS)}; the Query by Example grants were not "
                 "refreshed (carlos-ctl check)")
            return False
    return True


def _granted_tables(db_root) -> Optional[List[Tuple[str, str, str, str]]]:
    """(host, db, table, "Table_priv|Column_priv") for each table grant."""
    cp = db_root(["-N", "-B", "-e",
                  "SELECT Host, Db, Table_name, CONCAT(Table_priv, '|', Column_priv) "
                  "FROM mysql.tables_priv "
                  f"WHERE User = '{REPORT_USER}'"], capture_output=True)
    if cp.returncode != 0:
        return None
    return [tuple(parts) for parts in (line.split("\t") for line in cp.stdout.splitlines())
            if len(parts) == 4]


def _granted_columns(db_root) -> Optional[List[Tuple[str, str, str, str]]]:
    """(host, db, table, column) for each column grant."""
    cp = db_root(["-N", "-B", "-e",
                  "SELECT Host, Db, Table_name, Column_name FROM mysql.columns_priv "
                  f"WHERE User = '{REPORT_USER}'"], capture_output=True)
    if cp.returncode != 0:
        return None
    return [tuple(parts) for parts in (line.split("\t") for line in cp.stdout.splitlines())
            if len(parts) == 4]


def _existing_tables(db_root, db_name: str) -> Optional[set]:
    cp = db_root(["-N", "-B", "-e",
                  "SELECT TABLE_NAME FROM information_schema.TABLES "
                  f"WHERE TABLE_SCHEMA = '{db_name}' AND TABLE_TYPE = 'BASE TABLE'"],
                 capture_output=True)
    if cp.returncode != 0:
        return None
    return {line.strip() for line in cp.stdout.splitlines() if line.strip()}


def _existing_columns(db_root, db_name: str, tables) -> Optional[Dict[str, set]]:
    """Lower-cased table -> its lower-cased column names, for the given tables
    (names from the grant list, so plain identifiers)."""
    if not tables:
        return {}
    names = ", ".join(f"'{t}'" for t in sorted(tables))
    cp = db_root(["-N", "-B", "-e",
                  "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS "
                  f"WHERE TABLE_SCHEMA = '{db_name}' AND TABLE_NAME IN ({names})"],
                 capture_output=True)
    if cp.returncode != 0:
        return None
    columns: Dict[str, set] = {}
    for line in cp.stdout.splitlines():
        if line.count("\t") == 1:
            table, column = line.split("\t")
            columns.setdefault(table.lower(), set()).add(column.lower())
    return columns


def _grant_shape(table: str, privs: str, has_hidden: bool) -> Tuple[Optional[str], Optional[str]]:
    """(over-grant, under-grant) description of one tables_priv row, either
    None. The list grants a table without hidden columns as a whole
    ("Select|") and a table with hidden columns column by column ("|Select");
    nothing else."""
    table_priv, _sep, column_priv = privs.partition("|")
    if table_priv not in ("", "Select") or column_priv not in ("", "Select"):
        return f"{table} ({' '.join(privs.replace('|', ' ').split())})", None
    if has_hidden and table_priv:
        return f"{table} (every column, hidden ones included)", None
    if not has_hidden and not table_priv:
        return None, f"{table} (only some columns)"
    return None, None


def _login_problem(password: str, port: str = "3306") -> Optional[Tuple[str, str]]:
    """(verdict, message) when the recorded password does not log in as the
    reporting account, over the socket (localhost) and over TCP to 127.0.0.1
    on the configured CARLOS_DB_PORT, the two ways the application can reach
    MariaDB; None when both work. Only MariaDB refusing the password (1045)
    is bad: a connection that fails for another reason proves nothing about
    the password. The password travels in MYSQL_PWD, never argv."""
    env = dict(os.environ, MYSQL_PWD=password)
    for host, how in (("localhost", ["--protocol=socket"]),
                      ("127.0.0.1", ["--protocol=tcp", "--host=127.0.0.1", f"--port={port}"])):
        try:
            cp = util.run(["mariadb", "--no-defaults", *how, f"--user={REPORT_USER}",
                           "-N", "-B", "-e", "SELECT 1"], env=env, capture_output=True)
        except OSError:
            return ("note", "the mariadb client is not installed; the recorded "
                            f"{REPORT_USER} password was not tried")
        if cp.returncode == 0:
            continue
        error = util.mariadb_error(cp)
        if "ERROR 1045" in error:
            return ("bad", f"the {REPORT_USER} password recorded in {PROPERTIES} does not log in "
                           f"as {REPORT_USER}@{host}, so Query by Example cannot connect "
                           "(run 'carlos-ctl db-users')")
        if "ERROR 1203" in error:
            # MAX_USER_CONNECTIONS: Query by Example is busy, which proves nothing.
            return ("note", f"{REPORT_USER} is at its connection limit (Query by Example "
                            "is busy); the recorded password was not tried")
        return ("note", f"could not try the recorded {REPORT_USER} password as "
                        f"{REPORT_USER}@{host}: {error}")
    return None


def check_lines(db_name: str, db_root, login=None, *,
                db_port: str = "3306") -> List[Tuple[str, str]]:
    """(verdict, message) pairs for carlos-ctl check, verdict one of ok, note,
    bad. Read-only: the one thing it does besides reading is log in once with
    the recorded password, the way the application will (on db_port; login
    replaces that probe in tests)."""
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
    password = util.prop_unescape(prop_get(PROPERTIES, PASSWORD_KEY) or "").strip()
    recorded = user == REPORT_USER and bool(password)
    if not recorded:
        out.append(("bad", f"{PROPERTIES} does not name the {REPORT_USER} account "
                           f"({USERNAME_KEY}/{PASSWORD_KEY}); Query by Example is closed "
                           "until 'carlos-ctl db-users' sets them"))

    hosts = _account_hosts(db_root)
    if hosts is None:
        out.append(("bad", f"could not read the {REPORT_USER} account from mysql.global_priv"))
        return out
    if any(h not in hosts for h in REPORT_HOSTS):
        out.append(("bad", f"the {REPORT_USER} account is missing (Query by Example is closed); "
                           "run 'carlos-ctl db-users'"))
        return out
    # Each extra host is one more login (with '%', from anywhere on the
    # network) that the grant list then grants like the others.
    stray = sorted(hosts - set(REPORT_HOSTS))
    if stray:
        out.append(("bad", f"the {REPORT_USER} account also exists for host(s) "
                           + ", ".join(stray) + ", which carlos-ctl never creates; "
                           "'carlos-ctl db-users' drops them before it applies the grant "
                           f"list (or DROP USER '{REPORT_USER}'@'<host>')"))

    extra = _count(db_root,
                   "SELECT (SELECT COUNT(*) FROM mysql.global_priv "
                   f"WHERE User = '{REPORT_USER}' "
                   "AND COALESCE(JSON_VALUE(Priv, '$.access'), 0) <> 0)"
                   f" + (SELECT COUNT(*) FROM mysql.db WHERE User = '{REPORT_USER}')"
                   f" + (SELECT COUNT(*) FROM mysql.procs_priv WHERE User = '{REPORT_USER}')"
                   f" + (SELECT COUNT(*) FROM mysql.proxies_priv WHERE User = '{REPORT_USER}')"
                   f" + (SELECT COUNT(*) FROM mysql.roles_mapping WHERE User = '{REPORT_USER}')")
    # Mirrors the refusal in the grant list itself: whatever PUBLIC holds,
    # every account inherits, and no revoke on carlos_report takes it back.
    public = _count(db_root,
                    "SELECT (SELECT COUNT(*) FROM mysql.global_priv WHERE User = 'PUBLIC' "
                    "AND COALESCE(JSON_VALUE(Priv, '$.access'), 0) <> 0)"
                    f" + (SELECT COUNT(*) FROM mysql.db WHERE User = 'PUBLIC' AND '{db_name}' LIKE Db)"
                    f" + (SELECT COUNT(*) FROM mysql.tables_priv WHERE User = 'PUBLIC' AND Db = '{db_name}')"
                    f" + (SELECT COUNT(*) FROM mysql.columns_priv WHERE User = 'PUBLIC' AND Db = '{db_name}')"
                    f" + (SELECT COUNT(*) FROM mysql.procs_priv WHERE User = 'PUBLIC' AND Db = '{db_name}')")
    granted = _granted_tables(db_root)
    columns = _granted_columns(db_root)
    existing = _existing_tables(db_root, db_name)
    if extra is None or public is None or granted is None or columns is None or existing is None:
        out.append(("bad", f"could not read the {REPORT_USER} account's privileges"))
        return out
    if public:
        out.append(("bad", "PUBLIC holds privileges that reach the CARLOS schema, so every "
                           f"account, {REPORT_USER} included, inherits them "
                           "(SHOW GRANTS FOR PUBLIC; revoke them, then 'carlos-ctl db-users')"))
    if extra:
        out.append(("bad", f"the {REPORT_USER} account holds privileges beyond SELECT on "
                           "reportable tables (a global, schema, routine, proxy or role grant); "
                           "Query by Example refuses it until 'carlos-ctl db-users' rebuilds them"))

    # Folded names throughout: a server with lower_case_table_names stores
    # them that way, and the list spells some tables in mixed case.
    spelled = {t.lower(): t for t in grant_list.reportable}
    restricted = {t.lower() for t in grant_list.restricted}
    hidden = {t.lower(): cols for t, cols in grant_list.reportable.items() if cols}
    present = {t.lower() for t in existing}
    schema_columns = _existing_columns(db_root, db_name,
                                       [spelled[t] for t in hidden if t in present])
    if schema_columns is None:
        out.append(("bad", f"could not read the columns of the {REPORT_USER} account's tables"))
        return out
    # Tables the list grants: every reportable one that exists here, except one
    # whose every column is hidden.
    expected = {t for t in spelled if t in present
                and not (t in hidden and not schema_columns.get(t, set()) - hidden[t])}

    wrong, short = [], []
    have = {h: set() for h in REPORT_HOSTS}
    for host, db, table, privs in granted:
        folded = table.lower()
        if db != db_name:
            wrong.append(f"{db}.{table}")
        elif folded in restricted:
            wrong.append(f"{table} (restricted)")
        elif folded not in spelled:
            wrong.append(f"{table} (not in the grant list)")
        else:
            over, under = _grant_shape(table, privs, folded in hidden)
            wrong += [over] if over else []
            short += [f"{under} for {host}"] if under else []
            if host in have:
                have[host].add(folded)
    granted_columns = {h: set() for h in REPORT_HOSTS}
    for host, db, table, column in columns:
        if db != db_name:
            continue
        if column.lower() in hidden.get(table.lower(), frozenset()):
            wrong.append(f"{table}.{column}")
        if host in granted_columns:
            granted_columns[host].add((table.lower(), column.lower()))
    for table, cols in hidden.items():
        permitted = schema_columns.get(table, set()) - cols
        for host in REPORT_HOSTS:
            if table not in have[host]:
                continue    # reported below as a table not granted at all
            gaps = sorted(c for c in permitted if (table, c) not in granted_columns[host])
            if gaps:
                short.append(f"{spelled[table]}.{gaps[0]}"
                             + (f" and {len(gaps) - 1} more column(s)" if len(gaps) > 1 else "")
                             + f" for {host}")
    if wrong:
        wrong = sorted(set(wrong))
        out.append(("bad", f"the {REPORT_USER} account is granted tables it must not read: "
                           + ", ".join(wrong[:10]) + (" ..." if len(wrong) > 10 else "")
                           + " (run 'carlos-ctl db-users')"))
    missing = []
    for host in REPORT_HOSTS:
        gaps = sorted(spelled[t] for t in expected - have[host])
        if gaps:
            missing.append(gaps)
            out.append(("bad", f"{len(gaps)} reportable table(s) are not granted to "
                               f"{REPORT_USER}@{host} yet (for example {gaps[0]}); "
                               "run 'carlos-ctl db-users' to refresh the Query by Example grants"))
    if short:
        short = sorted(set(short))
        out.append(("bad", f"the {REPORT_USER} account is missing columns the grant list "
                           "gives it: " + ", ".join(short[:10]) + (" ..." if len(short) > 10 else "")
                           + " (run 'carlos-ctl db-users')"))
    if not recorded:
        problem = None
    elif login is not None:
        problem = login(password)
    else:
        problem = _login_problem(password, db_port)
    if problem:
        out.append(problem)
    # Only a login that worked earns the ok line. A probe that could not try
    # the password (the account at its connection limit, MariaDB not answering
    # on db_port, no client) leaves its note, and no claim that Query by
    # Example runs.
    if not (stray or public or extra or wrong or missing or short) and recorded and problem is None:
        out.append(("ok", f"Query by Example runs as {REPORT_USER}: SELECT on "
                          f"{len(have[REPORT_HOSTS[0]])} reportable table(s), nothing else"))
    return out
