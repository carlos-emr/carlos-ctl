# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""Lossless Ontario billing filename allocation and its independent witnesses.

Every member of a colliding group is renamed: a surviving file with the old
name is not evidence that it belongs to the earliest (or latest) submission.
The live audit also reserves the old names and lets CARLOS refuse ambiguous
downloads/regeneration until an administrator has reconciled the actual file.
"""
import os
import re

from .util import sql_escape

RULE = "o19-billing-v1"
AUDIT = "billing_filename_import"
TABLES = {
    "billing_on_diskname": ("ohipfilename", "createdatetime", "claim"),
    "billing_on_filename": ("htmlfilename", "timestamp", "html"),
}
AUDIT_COLUMNS = ("source_table", "source_id", "original_filename", "imported_filename",
                 "group_size", "reason", "file_status", "file_sha256", "reconciled_by",
                 "reconciled_at")


def ident(value):
    return "`" + value.replace("`", "``") + "`"


def literal(value):
    return "'" + sql_escape(value) + "'"


def helper(table):
    return table + "__filenames"


def expression(table):
    return ("(SELECT f.`imported_filename` FROM {archive}." + ident(helper(table))
            + " f WHERE f.`row_id` = s.`id`)")


def mapped(table, entry):
    """Recognize only the exact helper expression and versioned copy rule."""
    return (table in TABLES and entry.get("class") == "copy"
            and entry.get("billing_filename") == RULE
            and entry.get("value_exprs", {}).get(TABLES[table][0]) == expression(table))


def manifest_problems(tables, source_tables, province=None):
    """Refuse unaudited Ontario copies while allowing archived BC tables."""
    return [table + ": the application manifest lacks the audited billing filename rule; "
            "install the fixed carlos-emr/carlos-ctl package pair"
            for table in TABLES if table in source_tables
            and (province == "on" or tables.get(table, {}).get("class") == "copy")
            and not mapped(table, tables.get(table, {}))]


def key(expr, info):
    """Compare source text using the destination column's charset/collation."""
    charset, collation = info.get("charset", ""), info.get("collation", "")
    if not all(re.fullmatch(r"[A-Za-z0-9_]+", item or "") for item in (charset, collation)):
        raise ValueError("billing filename destination charset/collation is unavailable")
    return "CONVERT({0} USING {1}) COLLATE {2}".format(expr, charset, collation)


def ranked_sql(table, src, cols):
    """Derive collision-group sizes and year tags without changing staging."""
    column, date, _ = TABLES[table]
    filename = "s." + ident(column)
    year = "COALESCE(YEAR(s.{0}), 'x')".format(ident(date)) if date in cols else "'x'"
    return ("SELECT s.`id` AS row_id, CONVERT({0} USING utf8mb4) AS original_filename, {1} AS year_tag, "
            "CASE WHEN {0} IS NULL THEN 1 ELSE COUNT(*) OVER (PARTITION BY {2}) END AS group_size "
            "FROM {3}.{4} s".format(filename, year, key(filename, cols[column]), ident(src), ident(table)))


def generated(table, alias="m"):
    """Create bounded basenames carrying the complete row ID and retry nonce."""
    token = TABLES[table][2]
    # Complete numeric id + delimited nonce: different source ids cannot
    # generate each other's candidates. No filename prefix is truncated.
    return ("CONCAT('o19-{0}-', {1}.`year_tag`, '-', {1}.`row_id`, '-', {1}.`nonce`, '{2}')"
            .format(token, alias, ".html" if token == "html" else ".claim"))


def precheck(query, src, dst_info, src_info, entries):
    """Check mapped columns, original capacity and audit structure before writes."""
    problems = manifest_problems(entries, src_info)
    active = [t for t in TABLES if t in src_info and t in dst_info]
    problems.extend(manifest_problems(entries, active, province="on"))
    if not active:
        return problems
    if AUDIT not in dst_info or not set(AUDIT_COLUMNS) <= set(dst_info[AUDIT]):
        problems.append("the target lacks the billing filename audit schema; upgrade the package pair before importing")
    for table in active:
        column = TABLES[table][0]
        s, d = src_info[table], dst_info.get(table, {})
        if "id" not in s or column not in s or column not in d:
            problems.append(table + ": missing billing filename or id column")
            continue
        if s["id"]["type"] not in ("tinyint", "smallint", "mediumint", "int", "integer", "bigint"):
            problems.append(table + ": billing filename allocation requires an integer primary key")
            continue
        if d[column].get("char_len") != 50:
            problems.append(table + ": expected the canonical 50-character filename column")
            continue
        try:
            key("s." + ident(column), d[column])
        except ValueError as failure:
            problems.append(table + ": " + str(failure))
            continue
        # Archive expressions are not available until helpers exist. Measure
        # raw source values here, including conversion warnings via the ETL
        # executor, instead of letting the absent helper turn checks into NULL.
        n = int(query("SELECT COUNT(*) FROM {0}.{1} s WHERE CHAR_LENGTH(s.{2}) > 50".format(
            ident(src), ident(table), ident(column)))[0][0])
        if n:
            problems.append(table + ": {0} original filename(s) exceed 50 characters".format(n))
    return problems


def _source_shape(table, src_cols, dst_cols):
    # Ranking uses the destination comparison, while source dates/columns
    # remain the ones the dump actually carries.
    return dict(src_cols, **{TABLES[table][0]: dst_cols[TABLES[table][0]]})


def source_billing_tables(query, src):
    """Find normalized claim/HTML tables sharing the billing file namespace."""
    rows = query("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = {0} "
                 "AND TABLE_NAME IN ({1})".format(literal(src), ",".join(literal(t) for t in TABLES)))
    return [row[0] for row in rows]


def collision_predicate(table, src, info, alias="m", sources=None):
    """Find allocated names that would reuse any original billing basename."""
    # OHIP and HTML files share HOME_DIR. A generated claim name must also
    # avoid an original HTML name (including vendor/custom filenames).
    occupied = ["EXISTS (SELECT 1 FROM {0}.{1} s WHERE {2} = {3}.`imported_filename`)".format(
        ident(src), ident(source), key("s." + ident(TABLES[source][0]), info), alias)
        for source in (sources or [table])]
    return "{0}.`group_size` > 1 AND ({1})".format(alias, " OR ".join(occupied))


def collision_count(query, table, src, info, source_table=None):
    """Count affected rows before P4 normalizes restored table-name case."""
    filename = "s." + ident(TABLES[table][0])
    return int(query("SELECT COALESCE(SUM(n), 0) FROM (SELECT COUNT(*) AS n FROM {0}.{1} s "
                     "WHERE {2} IS NOT NULL GROUP BY {3} HAVING COUNT(*) > 1) groups_to_rename".format(
                         ident(src), ident(source_table or table), filename, key(filename, info)))[0][0])


def build(query, table, src, arch, src_cols, dst_cols):
    """Build aside, prove, then atomically publish an archive helper.

    Allocation checks all original names under target equality. Each occupied
    candidate consumes a distinct original name, so N source rows bound the
    loop by N+1; exhaustion is an error, never a truncation/ignored insert.
    """
    name = helper(table)
    fresh, old = name + "__new", name + "__old"
    path = ident(arch) + "." + ident(fresh)
    info = dst_cols[TABLES[table][0]]
    query("DROP TABLE IF EXISTS " + path)
    query("CREATE TABLE {0} (row_id BIGINT NOT NULL PRIMARY KEY, "
          "original_filename VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL, "
          "imported_filename VARCHAR(50) CHARACTER SET {1} COLLATE {2} NULL, "
          "group_size BIGINT NOT NULL, year_tag VARCHAR(8) NOT NULL, "
          "nonce BIGINT UNSIGNED NOT NULL, reason VARCHAR(32) NOT NULL) ENGINE=InnoDB".format(
              path, info["charset"], info["collation"]))
    ranked = ranked_sql(table, src, _source_shape(table, src_cols, dst_cols))
    query("INSERT INTO {0} SELECT row_id, original_filename, original_filename, group_size, "
          "year_tag, 0, IF(group_size > 1, 'collision', 'unchanged') FROM ({1}) r".format(path, ranked))
    query("UPDATE {0} m SET imported_filename = {1} WHERE group_size > 1".format(path, generated(table)))
    sources = source_billing_tables(query, src)
    count = sum(int(query("SELECT COUNT(*) FROM {0}.{1}".format(ident(src), ident(source)))[0][0])
                for source in sources)
    occupied = collision_predicate(table, src, info, sources=sources)
    for _ in range(count + 1):
        n = int(query("SELECT COUNT(*) FROM {0} m WHERE {1}".format(path, occupied))[0][0])
        if not n:
            break
        # Assign nonce first: MariaDB's single-table UPDATE evaluates its SET
        # list left-to-right. The candidate therefore carries the new nonce.
        query("UPDATE {0} m SET nonce = nonce + 1, imported_filename = {1} WHERE {2}".format(
            path, generated(table), occupied))
    else:
        raise ValueError(table + ": billing filename allocation exhausted its source-row bound")
    query("ALTER TABLE {0} ADD UNIQUE KEY uq_imported_filename (imported_filename)".format(path))
    issues = check_helper(query, table, src, arch, src_cols, dst_cols, name=fresh)
    if issues:
        raise ValueError("; ".join(issues))
    # An empty final table makes this swap shape the same on the first run
    # and every retry. A crash cannot expose a partly populated final helper.
    query("CREATE TABLE IF NOT EXISTS {0}.{1} LIKE {2}".format(ident(arch), ident(name), path))
    query("DROP TABLE IF EXISTS {0}.{1}".format(ident(arch), ident(old)))
    query("RENAME TABLE {0}.{1} TO {0}.{2}, {3} TO {0}.{1}".format(
        ident(arch), ident(name), ident(old), path))
    query("DROP TABLE {0}.{1}".format(ident(arch), ident(old)))


def check_helper(query, table, src, arch, src_cols, dst_cols, name=None):
    """Re-derive rank/group membership from immutable staging, not the helper.

    Checks cover deleted/extra rows, byte-exact originals, the allocation
    formula, reserved originals and actual uniqueness. No source value is
    included in errors (filenames can identify a patient).
    """
    path = ident(arch) + "." + ident(name or helper(table))
    rank = ranked_sql(table, src, _source_shape(table, src_cols, dst_cols))
    sources = source_billing_tables(query, src)
    source_count = " + ".join("(SELECT COUNT(*) FROM {0}.{1})".format(ident(src), ident(source))
                              for source in sources)
    checks = [
        ("mapping rows or original values differ from staging",
         "SELECT COUNT(*) FROM ({0}) r LEFT JOIN {1} m ON m.row_id = r.row_id WHERE "
         "m.row_id IS NULL OR NOT (BINARY m.original_filename <=> BINARY r.original_filename) "
         "OR m.group_size <> r.group_size OR m.year_tag <> r.year_tag".format(rank, path)),
        ("mapping contains extra rows",
         "SELECT COUNT(*) FROM {0} m LEFT JOIN {1}.{2} s ON s.id = m.row_id WHERE s.id IS NULL".format(
             path, ident(src), ident(table))),
        ("mapping values do not follow the allocation policy",
         "SELECT COUNT(*) FROM {0} m WHERE NOT (BINARY m.imported_filename <=> BINARY "
         "IF(m.group_size > 1, {1}, m.original_filename)) OR m.nonce > ({2}) "
         "OR m.reason <> IF(m.group_size > 1, 'collision', 'unchanged')".format(
             path, generated(table), source_count)),
        ("allocated filename collides with an original name",
         "SELECT COUNT(*) FROM {0} m WHERE {1}".format(
             path, collision_predicate(table, src, dst_cols[TABLES[table][0]], sources=sources))),
        ("allocated filenames are not unique",
         "SELECT COUNT(*) FROM (SELECT imported_filename FROM {0} WHERE imported_filename IS NOT NULL "
         "GROUP BY imported_filename HAVING COUNT(*) > 1) d".format(path)),
    ]
    return [table + ": " + why for why, sql in checks if int(query(sql)[0][0])]


def check_audit(query, table, dst, arch):
    """Prove live evidence matches its helper and reconciliation is explicit."""
    mapping = ident(arch) + "." + ident(helper(table))
    audit = ident(dst) + "." + ident(AUDIT)
    same = ("(BINARY a.original_filename <=> BINARY m.original_filename) AND "
            "(BINARY a.imported_filename <=> BINARY m.imported_filename) AND "
            "a.group_size = m.group_size AND BINARY a.reason = BINARY m.reason")
    sql = ("SELECT COUNT(*) FROM {0} m LEFT JOIN {1} a ON a.source_table = {2} AND a.source_id = m.row_id "
           "WHERE a.source_id IS NULL OR NOT (({3})) OR "
           "(m.group_size = 1 AND a.file_status <> 'unchanged') OR "
           "(m.group_size > 1 AND a.file_status NOT IN ('ambiguous', 'reconciled')) OR "
           "(a.file_status = 'reconciled' AND (a.file_sha256 IS NULL "
           "OR BINARY a.file_sha256 NOT REGEXP '^[0-9a-f]{{64}}$' OR a.reconciled_at IS NULL "
           "OR a.reconciled_by IS NULL OR TRIM(a.reconciled_by) = ''))".format(
               mapping, audit, literal(table), same))
    extra = ("SELECT COUNT(*) FROM {0} a LEFT JOIN {1} m ON m.row_id = a.source_id "
             "WHERE a.source_table = {2} AND m.row_id IS NULL".format(audit, mapping, literal(table)))
    return [table + ": live billing filename audit differs from the verified mapping"
            for _ in [0] if int(query(sql)[0][0]) or int(query(extra)[0][0])]


def prepare(run, table, entry, state, dst_cols):
    """Publish verified helper/audit evidence before resumable clinical copying."""
    from .o19etl import QueryError, save_progress
    if not mapped(table, entry):
        raise QueryError(table + ": missing audited billing filename mapping")
    if not state.get("done"):
        # Record intent before metadata writes. Package/resume guards must
        # treat even an interrupted metadata publication as a partial import.
        state["billing_started"] = True
        save_progress(run.state_dir, run.progress)
        build(run.query, table, run.src, run.arch, run.src_info[table], dst_cols)
        mapping = ident(run.arch) + "." + ident(helper(table))
        audit = ident(run.dst) + "." + ident(AUDIT)
        count = int(run.plain("SELECT COUNT(*) FROM {0} WHERE source_table = {1}".format(audit, literal(table)))[0][0])
        if count:
            issues = check_audit(run.plain, table, run.dst, run.arch)
            if issues:
                raise QueryError("; ".join(issues))
        else:
            run.query("INSERT INTO {0} (source_table, source_id, original_filename, imported_filename, "
                      "group_size, reason, file_status) SELECT {1}, row_id, original_filename, "
                      "imported_filename, group_size, reason, IF(group_size > 1, 'ambiguous', 'unchanged') "
                      "FROM {2}".format(audit, literal(table), mapping))
    issues = check_helper(run.plain, table, run.src, run.arch, run.src_info[table], dst_cols)
    issues += check_audit(run.plain, table, run.dst, run.arch)
    if issues:
        raise QueryError("; ".join(issues))
    n = int(run.plain("SELECT COUNT(*) FROM {0}.{1} WHERE group_size > 1".format(
        ident(run.arch), ident(helper(table))))[0][0])
    lines = run.progress.setdefault("report_lines", {}).setdefault("billing", [])
    line = (table + ": {0} historical row(s) received unique filenames; originals and mappings are in "
            "{1}.{2} and {3}.{4}. Ambiguous downloads/regeneration require file reconciliation.".format(
                n, run.arch, helper(table), run.dst, AUDIT))
    lines[:] = [item for item in lines if not item.startswith(table + ": ")] + [line]
    save_progress(run.state_dir, run.progress)


def verify(query, src, dst, arch, src_info, dst_info, tables, live=True):
    """Verify durable evidence; also compare live submissions during P4/P7."""
    ok, bad = [], []
    for table in TABLES:
        if table not in src_info or tables.get(table, {}).get("class") != "copy":
            continue
        if not mapped(table, tables.get(table, {})):
            bad.extend(manifest_problems(tables, [table]))
            continue
        try:
            failures = check_helper(query, table, src, arch, src_info[table], dst_info[table])
            failures += check_audit(query, table, dst, arch)
            if live:
                column = TABLES[table][0]
                sql = ("SELECT COUNT(*) FROM {0}.{1} m LEFT JOIN {2}.{3} d ON d.id = m.row_id "
                       "WHERE d.id IS NULL OR NOT (BINARY d.{4} <=> BINARY m.imported_filename)".format(
                           ident(arch), ident(helper(table)), ident(dst), ident(table), ident(column)))
                extra = ("SELECT COUNT(*) FROM {0}.{1} d LEFT JOIN {2}.{3} m ON m.row_id = d.id "
                         "WHERE m.row_id IS NULL".format(ident(dst), ident(table), ident(arch), ident(helper(table))))
                if int(query(sql)[0][0]) or int(query(extra)[0][0]):
                    failures.append(table + ": live submissions differ from the verified filename mapping")
            bad.extend(failures)
            if not failures:
                ok.append(table + ": original filenames, allocation and live audit verified against staging")
        except (RuntimeError, ValueError, KeyError, IndexError, TypeError):
            bad.append(table + ": billing filename mapping could not be verified")
    return ok, bad


def inventory(ctx, root):
    """Private inventory only: a shared surviving file is never assigned to a row.

    All source files stay in the restored tree. The audit's ambiguous state
    guards both old and allocated names in CARLOS until deliberate reconciliation.
    """
    from . import o19etl, o19import
    progress = o19etl.load_progress(ctx["state_dir"])
    if not any(value.get("billing_started") for table, value in progress.get("tables", {}).items()
               if table in TABLES):
        return []
    query = ctx["query"]
    rows = query("SELECT source_table, source_id, HEX(CONVERT(original_filename USING utf8mb4)), "
                 "imported_filename, file_status FROM {0}.{1} WHERE group_size > 1 ORDER BY source_table, source_id".format(
                     ident(ctx["target_db"]), ident(AUDIT)))
    if not rows:
        o19import.write_private(os.path.join(ctx["state_dir"], "billing-files-details.txt"), "clean\n")
        return []
    details = []
    for table, row_id, encoded, imported, status in rows:
        original = bytes.fromhex(encoded).decode("utf-8")
        # Inventory only safe basenames in the standard billing directory.
        # A custom/external HOME_DIR is explicitly left for the technical review.
        safe = bool(original) and original not in (".", "..") and not any(c in original for c in ("/", "\\", "\0"))
        path = os.path.join(root, "billing", "download", original) if safe else None
        present = bool(path and os.path.isfile(path) and not os.path.islink(path))
        details.append("{0} id {1}: original={2!r}; imported={3!r}; status={4}; "
                       "original file in standard HOME_DIR={5}".format(table, row_id, original, imported, status, present))
    o19import.write_private(os.path.join(ctx["state_dir"], "billing-files-details.txt"), "\n".join(details) + "\n")
    return ["{0} historical billing filename mapping(s) need file reconciliation; originals remain preserved. "
            "See billing-files-details.txt and the billing_filename_import archive. Check any custom/external HOME_DIR separately.".format(len(rows))]
