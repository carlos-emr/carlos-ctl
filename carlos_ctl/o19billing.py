# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""Select backed-up Ontario billing generations and preserve their history.

The earlier filename allocator remains available for interrupted imports
that already wrote its mappings.
"""
from collections import defaultdict
from dataclasses import dataclass
import os
import re
import stat
from typing import Dict, Optional, Tuple

from .util import sql_escape

RULE = "o19-billing-v1"
AUDIT = "billing_filename_import"
IMPORT_ACTOR = "carlos-ctl/import-o19"
GENERATION_FILES = "billing_generation_files"
LATEST_GENERATION_ACCEPT = "latest-billing-generation"
LATEST_GENERATION_POLICY = "billing_assume_latest"
DETAILS = "billing-files-details.txt"
TABLES = {
    "billing_on_diskname": ("ohipfilename", "createdatetime", "claim"),
    "billing_on_filename": ("htmlfilename", "timestamp", "html"),
}
AUDIT_COLUMNS = ("source_table", "source_id", "original_filename", "imported_filename",
                 "group_size", "reason", "file_status", "file_sha256", "reconciled_by",
                 "reconciled_at", "lastUpdateUser", "lastUpdateDate")


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


def namespace_groups(src, info, sources):
    """Count all references sharing a basename in the OHIP/HTML file namespace."""
    names = " UNION ALL ".join(
        "SELECT {0} AS filename FROM {1}.{2} s".format(
            key("s." + ident(TABLES[table][0]), info), ident(src), ident(actual))
        for table, actual in sources.items())
    return ("SELECT filename, COUNT(*) AS group_size FROM ({0}) billing_names "
            "WHERE filename IS NOT NULL GROUP BY filename".format(names))


def ranked_sql(table, src, cols, sources=None):
    """Derive collision-group sizes and year tags without changing staging."""
    column, date, _ = TABLES[table]
    filename = "s." + ident(column)
    year = "COALESCE(YEAR(s.{0}), 'x')".format(ident(date)) if date in cols else "'x'"
    groups = namespace_groups(src, cols[column], sources or {table: table})
    return ("SELECT s.`id` AS row_id, CONVERT({0} USING utf8mb4) AS original_filename, {1} AS year_tag, "
            "COALESCE(g.group_size, 1) AS group_size FROM {2}.{3} s LEFT JOIN ({4}) g "
            "ON g.filename = {5}".format(filename, year, ident(src), ident(table), groups,
                                         key(filename, cols[column])))


def generated(table, alias="m"):
    """Create bounded basenames carrying the complete row ID and retry nonce."""
    token = TABLES[table][2]
    # Complete numeric id + delimited nonce: different source ids cannot
    # generate each other's candidates. No filename prefix is truncated.
    return ("CONCAT('o19-{0}-', {1}.`year_tag`, '-', {1}.`row_id`, '-', {1}.`nonce`, '{2}')"
            .format(token, alias, ".html" if token == "html" else ".claim"))


def precheck(query, src, dst_info, src_info, entries, province="on"):
    """Check mapped columns, original capacity and audit structure before writes."""
    problems = manifest_problems(entries, src_info, province=province)
    active = [t for t in TABLES if t in src_info and t in dst_info
              and (province == "on" or entries.get(t, {}).get("class") == "copy")]
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


@dataclass
class GenerationPlan:
    """Source rows and the files available in the backed-up billing directory."""

    directory: Optional[str]
    files: Dict[str, int]
    rows: Dict[str, Dict[int, Tuple[str, str]]]
    assume_latest: bool = False
    repeated_names: int = 0

    def retained(self, table):
        return {row_id: name for row_id, (name, status) in self.rows[table].items()
                if status == "retained"}


def archived_billing_files(ctx):
    """Inventory the source HOME_DIR when it is inside the exported context."""
    from . import o19_preflight, o19bundle, o19docs, o19props
    archive = ctx.get("documents")
    if not archive:
        return None, {}
    entries, context = ctx.get("document_archive") or o19docs.read_documents_archive(archive)
    directory = "billing/download"
    properties = ctx.get("properties")
    home = o19_preflight.parse_properties(properties).get("HOME_DIR") if properties else None
    if home:
        # Use the same path translation shipped in the properties fragment.
        # A directory under a different OSCAR context is outside this backup.
        pattern = r"/(?:.*/)?OscarDocument/" + re.escape(context) + r"(?:/.*)?"
        if not re.fullmatch(pattern, home):
            return None, {}
        translated = o19props.translate_docpath(home, documents_root="")
        if translated is None:
            return None, {}
        directory = os.path.relpath(translated, o19docs.TARGET_CTX)
    prefix = os.path.normpath(os.path.join(context, directory)) + "/"
    files = {}
    for kind, raw, size in entries:
        clean = os.path.normpath(raw)
        if kind == o19bundle.TAR_TYPE_FILE and size and clean.startswith(prefix):
            name = clean[len(prefix):]
            if "/" not in name and "\\" not in name:
                files[name] = size
    return directory, files


def plan_generations(ctx, src, sources, dst_info, assume_latest=None):
    """Select backed-up filenames that can be assigned to a generation.

    OSCAR can regenerate an older disk under its existing name. Repeated
    names therefore require operator confirmation before assigning the
    file to the highest ID. Cross-table and target index conflicts remain
    available for review in the archive.
    """
    if assume_latest is None:
        assume_latest = LATEST_GENERATION_ACCEPT in ctx.get("accepted", ())
    cached = ctx.get("billing_generation_plan")
    if cached and cached.assume_latest == assume_latest:
        return cached
    directory, files = archived_billing_files(ctx)
    rows = {table: {} for table in sources}
    references = defaultdict(list)
    for table, actual in sources.items():
        column = "s." + ident(TABLES[table][0])
        comparison = key(column, dst_info[table][TABLES[table][0]])
        sql = ("SELECT s.id, IFNULL(HEX(CONVERT({0} USING utf8mb4)), ''), "
               "DENSE_RANK() OVER (ORDER BY {1}) FROM {2}.{3} s ORDER BY s.id"
               .format(column, comparison, ident(src), ident(actual)))
        for raw_id, encoded, rank in ctx["query"](sql):
            row_id = int(raw_id)
            name = bytes.fromhex(encoded).decode("utf-8")
            rows[table][row_id] = name, "file unavailable in backup"
            if name in files:
                references[name].append((table, row_id, rank))

    candidates = defaultdict(list)
    repeated_names = 0
    for name, refs in references.items():
        if len({table for table, _, _ in refs}) > 1:
            for table, row_id, _ in refs:
                rows[table][row_id] = name, "shared between generation tables"
            continue
        if len(refs) > 1:
            repeated_names += 1
            reason = ("older generation archived by selection policy"
                      if assume_latest else "shared by several generations")
            for table, row_id, _ in refs:
                rows[table][row_id] = name, reason
            if not assume_latest:
                continue
        table, row_id, rank = max(refs, key=lambda row: row[1])
        candidates[table, rank].append((row_id, name))
    for (table, _), group in candidates.items():
        status = "retained" if len(group) == 1 else "conflicts with another surviving filename"
        for row_id, name in group:
            rows[table][row_id] = name, status
    plan = GenerationPlan(directory, files, rows, assume_latest, repeated_names)
    ctx["billing_generation_plan"] = plan
    return plan


def generation_tables(progress):
    """Billing tables whose copy used the file-backed selection."""
    return {table for table, state in progress.get("tables", {}).items()
            if table in TABLES and state.get(GENERATION_FILES)}


def retained_predicate(table, archive):
    """Limit a copy or its row checks to the selected source IDs."""
    return "EXISTS (SELECT 1 FROM {0}.{1} f WHERE f.row_id = s.id)".format(
        ident(archive), ident(helper(table)))


def generation_summary(table, plan):
    retained = len(plan.retained(table))
    archived = len(plan.rows[table]) - retained
    return "{0}: {1} generation row(s) backed by surviving files, {2} archive-only".format(
        table, retained, archived)


def recorded_latest_policy(progress):
    """Bind the choice to the billing copy when continuing a run."""
    tables = progress.get("tables", {})
    policies = {tables[table].get(LATEST_GENERATION_POLICY, True)
                for table in generation_tables(progress)}
    if len(policies) > 1:
        raise ValueError("billing generation tables have conflicting selection policies")
    # Earlier file-backed checkpoints used the highest ID for every repeat.
    return next(iter(policies), None)


def recorded_plan(ctx, progress, dst_info=None):
    """Recover the file-backed selection on a checkpointed import."""
    from . import o19etl, o19import
    tables = generation_tables(progress)
    if not tables:
        return None
    assume_latest = recorded_latest_policy(progress)
    cached = ctx.get("billing_generation_plan")
    if dst_info is None and (not cached or cached.assume_latest != assume_latest):
        dst_info = o19etl.introspect_columns(ctx["query"], ctx["target_db"])
    return plan_generations(ctx, o19import.STAGING_SCHEMA, {table: table for table in tables},
                            dst_info, assume_latest)


def report_generations(ctx, plan):
    """Write each source generation's disposition to the private report."""
    from . import o19import
    details = ["Billing directory in the source backup: {0}".format(
        repr(plan.directory) if plan.directory is not None else "outside the supplied archive")]
    for table, rows in sorted(plan.rows.items()):
        details.extend("{0} id {1}: {2!r} — {3}".format(table, row_id, name, status)
                       for row_id, (name, status) in sorted(rows.items()))
    o19import.write_private(os.path.join(ctx["state_dir"], DETAILS), "\n".join(details) + "\n")
    lines = [generation_summary(table, plan) for table in sorted(plan.rows)]
    if plan.repeated_names:
        if plan.assume_latest:
            lines.append("For {0} repeated filename(s), the highest generation ID was selected "
                         "where eligible under the recorded billing policy.".format(plan.repeated_names))
        else:
            lines.append("{0} repeated filename(s) need review. Their generation rows are archived. "
                         "Use --accept {1} only if the clinic confirms the highest IDs own all "
                         "eligible files with repeated names.".format(
                             plan.repeated_names, LATEST_GENERATION_ACCEPT))
    return lines


def generation_file_problems(ctx, root):
    """Check the restored file for every retained live generation."""
    from . import o19docs, o19etl
    plan = recorded_plan(ctx, o19etl.load_progress(ctx["state_dir"]))
    if not plan or plan.directory is None:
        return []
    problems = []
    for table in sorted(plan.rows):
        for row_id, name in plan.retained(table).items():
            relative = os.path.join(plan.directory, name)
            try:
                file = os.lstat(os.path.join(root, relative))
                if (stat.S_ISREG(file.st_mode) and file.st_size == plan.files[name]
                        and o19docs.contained(root, relative)):
                    continue
            except OSError:
                pass  # name the affected row in the private reconciliation finding below
            problems.append("{0} id {1}: the restored billing file {2!r} is missing or differs in size"
                            .format(table, row_id, name))
    return problems


def collision_predicate(table, src, info, alias="m", sources=None):
    """Find allocated names that would reuse any original billing basename."""
    # OHIP and HTML files share HOME_DIR. A generated claim name must also
    # avoid an original HTML name (including vendor/custom filenames).
    occupied = ["EXISTS (SELECT 1 FROM {0}.{1} s WHERE {2} = {3}.`imported_filename`)".format(
        ident(src), ident(source), key("s." + ident(TABLES[source][0]), info), alias)
        for source in (sources or [table])]
    return "{0}.`group_size` > 1 AND ({1})".format(alias, " OR ".join(occupied))


def collision_count(query, table, src, info, source_table=None, source_tables=None):
    """Count affected rows before P4 normalizes restored table-name case."""
    filename = "s." + ident(TABLES[table][0])
    sources = source_tables or {table: source_table or table}
    groups = namespace_groups(src, info, sources)
    return int(query("SELECT COUNT(*) FROM {0}.{1} s JOIN ({2}) g ON g.filename = {3} "
                     "WHERE g.group_size > 1".format(ident(src), ident(source_table or sources[table]),
                                                    groups, key(filename, info)))[0][0])


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
    sources = source_billing_tables(query, src)
    ranked = ranked_sql(table, src, _source_shape(table, src_cols, dst_cols),
                        {source: source for source in sources})
    query("INSERT INTO {0} SELECT row_id, original_filename, original_filename, group_size, "
          "year_tag, 0, IF(group_size > 1, 'collision', 'unchanged') FROM ({1}) r".format(path, ranked))
    query("UPDATE {0} m SET imported_filename = {1} WHERE group_size > 1".format(path, generated(table)))
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
    sources = source_billing_tables(query, src)
    rank = ranked_sql(table, src, _source_shape(table, src_cols, dst_cols),
                      {source: source for source in sources})
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
           "a.lastUpdateUser IS NULL OR a.lastUpdateUser NOT REGEXP '[^[:space:]]' OR "
           "a.lastUpdateDate IS NULL OR YEAR(a.lastUpdateDate) = 0 OR "
           "(m.group_size = 1 AND a.file_status <> 'unchanged') OR "
           "(m.group_size > 1 AND a.file_status NOT IN ('ambiguous', 'reconciled')) OR "
           "(a.file_status = 'reconciled' AND (a.file_sha256 IS NULL "
           "OR BINARY a.file_sha256 NOT REGEXP '^[0-9a-f]{{64}}$' OR a.reconciled_at IS NULL "
           "OR a.reconciled_by IS NULL OR a.reconciled_by NOT REGEXP '[^[:space:]]'))".format(
               mapping, audit, literal(table), same))
    extra = ("SELECT COUNT(*) FROM {0} a LEFT JOIN {1} m ON m.row_id = a.source_id "
             "WHERE a.source_table = {2} AND m.row_id IS NULL".format(audit, mapping, literal(table)))
    return [table + ": live billing filename audit differs from the verified mapping"
            for _ in [0] if int(query(sql)[0][0]) or int(query(extra)[0][0])]


def build_generation_mapping(query, table, src, arch, dst_cols, retained):
    """Publish a mapping for selected rows with their original on-disk names."""
    from . import o19etl
    info = dst_cols[TABLES[table][0]]
    column = "s." + ident(TABLES[table][0])
    source = ident(src) + "." + ident(table)
    ids = sorted(retained)

    def build(scratch):
        path = ident(arch) + "." + ident(scratch)
        statements = [
            "CREATE TABLE {0} (row_id BIGINT NOT NULL PRIMARY KEY, "
            "original_filename VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL, "
            "imported_filename VARCHAR(50) CHARACTER SET {1} COLLATE {2} NOT NULL UNIQUE, "
            "group_size BIGINT NOT NULL, reason VARCHAR(32) NOT NULL) ENGINE=InnoDB"
            .format(path, info["charset"], info["collation"])]
        for start in range(0, len(ids), 1000):
            batch = ", ".join(str(row_id) for row_id in ids[start:start + 1000])
            statements.append(
                "INSERT INTO {0} SELECT s.id, {1}, {1}, 1, 'unchanged' FROM {2} s "
                "WHERE s.id IN ({3})".format(path, column, source, batch))
        return statements

    for statement in o19etl.rebuild_statements(arch, helper(table), build):
        query(statement)


def check_generation_mapping(query, table, src, arch, retained=None):
    """Compare every mapping to its staged row and the file inventory when open."""
    mapping = ident(arch) + "." + ident(helper(table))
    source = ident(src) + "." + ident(table)
    sql = ("SELECT COUNT(*) FROM {0} f LEFT JOIN {1} s ON s.id = f.row_id "
           "WHERE s.id IS NULL OR NOT (BINARY f.original_filename <=> BINARY "
           "CONVERT(s.{2} USING utf8mb4)) OR NOT (BINARY f.imported_filename <=> "
           "BINARY f.original_filename) OR f.group_size <> 1 OR f.reason <> 'unchanged'"
           .format(mapping, source, ident(TABLES[table][0])))
    if int(query(sql)[0][0]):
        return [table + ": retained billing filename mapping differs from staging"]
    if retained is not None:
        ids = {int(row[0]) for row in query("SELECT row_id FROM " + mapping)}
        if ids != set(retained):
            return [table + ": retained billing generations differ from the source file inventory"]
    return []


def next_generation_id(query, schema, table):
    rows = query("SELECT AUTO_INCREMENT FROM information_schema.TABLES "
                 "WHERE TABLE_SCHEMA = {0} AND TABLE_NAME = {1}".format(literal(schema), literal(table)))
    return int(rows[0][0]) if rows and rows[0][0] not in (None, "", "NULL") else None


def generation_id_floor(query, src, table):
    """Keep future generations from reusing IDs that still appear in history."""
    largest = int(query("SELECT COALESCE(MAX(id), 0) FROM {0}.{1}".format(
        ident(src), ident(table)))[0][0])
    return max(largest + 1, next_generation_id(query, src, table) or 1)


def prepare(run, table, entry, state, dst_cols, plan=None):
    """Publish verified helper/audit evidence before resumable clinical copying."""
    from .o19etl import QueryError, archive_statements, save_progress
    if not mapped(table, entry):
        raise QueryError(table + ": missing audited billing filename mapping")
    if not state.get("done"):
        # Record intent before metadata writes. Package/resume guards must
        # treat even an interrupted metadata publication as a partial import.
        state["billing_started"] = True
        if plan is not None:
            state[GENERATION_FILES] = True
            state[LATEST_GENERATION_POLICY] = plan.assume_latest
        save_progress(run.state_dir, run.progress)
        if plan is None:
            build(run.query, table, run.src, run.arch, run.src_info[table], dst_cols)
        else:
            if not state.get("billing_archived"):
                for sql in archive_statements(table, run.src, run.arch):
                    run.query(sql)
                state["billing_archived"] = True
                save_progress(run.state_dir, run.progress)
            build_generation_mapping(run.query, table, run.src, run.arch, dst_cols,
                                     plan.retained(table))
            current = next_generation_id(run.plain, run.dst, table)
            if current is not None:
                minimum = generation_id_floor(run.plain, run.src, table)
                if current < minimum:
                    run.query("ALTER TABLE {0}.{1} AUTO_INCREMENT = {2}".format(
                        ident(run.dst), ident(table), minimum))
        mapping = ident(run.arch) + "." + ident(helper(table))
        audit = ident(run.dst) + "." + ident(AUDIT)
        count = int(run.plain("SELECT COUNT(*) FROM {0} WHERE source_table = {1}".format(audit, literal(table)))[0][0])
        if count:
            issues = check_audit(run.plain, table, run.dst, run.arch)
            if issues:
                raise QueryError("; ".join(issues))
        else:
            run.query("INSERT INTO {0} (source_table, source_id, original_filename, imported_filename, "
                      "group_size, reason, file_status, lastUpdateUser) SELECT {1}, row_id, original_filename, "
                      "imported_filename, group_size, reason, IF(group_size > 1, 'ambiguous', 'unchanged'), {3} "
                      "FROM {2}".format(audit, literal(table), mapping, literal(IMPORT_ACTOR)))
    issues = (check_helper(run.plain, table, run.src, run.arch, run.src_info[table], dst_cols)
              if plan is None else check_generation_mapping(
                  run.plain, table, run.src, run.arch, plan.retained(table)))
    issues += check_audit(run.plain, table, run.dst, run.arch)
    if issues:
        raise QueryError("; ".join(issues))
    lines = run.progress.setdefault("report_lines", {}).setdefault("billing", [])
    if plan is None:
        n = int(run.plain("SELECT COUNT(*) FROM {0}.{1} WHERE group_size > 1".format(
            ident(run.arch), ident(helper(table))))[0][0])
        line = (table + ": {0} historical row(s) received unique filenames; originals and mappings are in "
                "{1}.{2} and {3}.{4}. Ambiguous downloads/regeneration require file reconciliation.".format(
                    n, run.arch, helper(table), run.dst, AUDIT))
    else:
        line = generation_summary(table, plan) + ". Source rows are preserved at {0}.{1}.".format(
            run.arch, table)
        if plan.assume_latest and plan.repeated_names:
            line += " Highest IDs were used for repeated filenames under the recorded billing policy."
    lines[:] = [item for item in lines if not item.startswith(table + ": ")] + [line]
    save_progress(run.state_dir, run.progress)


def verify(query, src, dst, arch, src_info, dst_info, tables, live=True,
           generation_tables=(), plan=None):
    """Verify durable evidence; also compare live submissions during P4/P7."""
    ok, bad = [], []
    for table in TABLES:
        if table not in src_info or tables.get(table, {}).get("class") != "copy":
            continue
        if not mapped(table, tables.get(table, {})):
            bad.extend(manifest_problems(tables, [table]))
            continue
        try:
            if table in generation_tables:
                failures = check_generation_mapping(query, table, src, arch,
                                                    plan.retained(table) if plan else None)
            else:
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
                if table in generation_tables:
                    next_id = next_generation_id(query, dst, table)
                    if next_id is not None and next_id < generation_id_floor(query, src, table):
                        failures.append(table + ": future generations could reuse a source ID")
            bad.extend(failures)
            if not failures:
                ok.append(table + ": original filenames, allocation and live audit verified against staging")
        except (RuntimeError, ValueError, KeyError, IndexError, TypeError):
            bad.append(table + ": billing filename mapping could not be verified")
    return ok, bad


def inventory(ctx, root):
    """Private inventory of file-backed generations or legacy ambiguous mappings."""
    from . import o19etl, o19import
    progress = o19etl.load_progress(ctx["state_dir"])
    plan = recorded_plan(ctx, progress)
    if plan is not None:
        return report_generations(ctx, plan)
    if not any(value.get("billing_started") for table, value in progress.get("tables", {}).items()
               if table in TABLES):
        return []
    query = ctx["query"]
    rows = query("SELECT source_table, source_id, HEX(CONVERT(original_filename USING utf8mb4)), "
                 "imported_filename, file_status FROM {0}.{1} WHERE group_size > 1 ORDER BY source_table, source_id".format(
                     ident(ctx["target_db"]), ident(AUDIT)))
    if not rows:
        o19import.write_private(os.path.join(ctx["state_dir"], DETAILS), "clean\n")
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
    o19import.write_private(os.path.join(ctx["state_dir"], DETAILS), "\n".join(details) + "\n")
    return ["{0} historical billing filename mapping(s) need file reconciliation; originals remain preserved. "
            "See billing-files-details.txt and the billing_filename_import archive. Check any custom/external HOME_DIR separately.".format(len(rows))]
