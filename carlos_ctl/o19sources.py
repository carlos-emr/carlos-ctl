# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""Complete OSCAR dump sets, with checked table ownership before restore.

Never concatenate overlapping dumps blindly: mysqldump emits DROP TABLE and
the last copy would erase the first. Complementary tables are combined; an
overlap is accepted only for identical schema/data statement fingerprints.
All input bytes are retained in private snapshots. Ambiguous overlaps stop
before the live schema is touched. Literals are never rewritten.
"""
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import tarfile
import zlib

from . import o19sql

FORMAT = 1
TRAILER = b"-- Dump completed"
COMBINED_TRAILER = b"\n-- Dump completed (complete OSCAR dump set)\n"
STATEMENT_SPOOL_LIMIT = 1 << 20
SET_TOKEN_LIMIT = 65536
QUOTING_MODES = re.compile(
    rb"\b(?:ANSI_QUOTES|NO_BACKSLASH_ESCAPES|ANSI|DB2|MAXDB|MSSQL|ORACLE|POSTGRESQL)\b", re.I)


def snapshot_file(path, destination):
    """Copy an opened input atomically, including when source equals destination."""
    incoming = destination + ".incoming"
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as source:
            fd = os.open(incoming, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as dest:
                os.fchmod(dest.fileno(), 0o600)
                for chunk in iter(lambda: source.read(1 << 20), b""):
                    digest.update(chunk)
                    dest.write(chunk)
                dest.flush()
                os.fsync(dest.fileno())
        os.replace(incoming, destination)
    finally:
        if os.path.exists(incoming):
            os.unlink(incoming)
    return digest.hexdigest()


def statements(stream, spool_dir=None):
    """Yield seekable statement files; large INSERTs spill to disk.

    Semicolon boundaries come from the quote/comment-aware lexer. One trailing
    comment fragment is yielded too, so dump completion markers survive.
    """
    lexer = o19sql.Lexer()
    spool = tempfile.SpooledTemporaryFile(max_size=STATEMENT_SPOOL_LIMIT, dir=spool_dir)
    start = 0
    position = 0
    try:
        while True:
            chunk = stream.read(1 << 20)
            lexer.feed(chunk, final=not chunk)
            offset = 0
            for boundary in lexer.boundaries:
                take = boundary - position
                spool.write(chunk[offset:take])
                spool.seek(0)
                yield spool
                spool.close()
                spool = tempfile.SpooledTemporaryFile(max_size=STATEMENT_SPOOL_LIMIT, dir=spool_dir)
                start = boundary
                offset = take
            spool.write(chunk[offset:])
            position += len(chunk)
            if not chunk:
                if lexer.state not in ("sql", "line") or lexer.executable:
                    raise ValueError("incomplete SQL statement (unclosed quote or comment)")
                if position > start:
                    spool.seek(0)
                    yield spool
                break
    finally:
        spool.close()


def statement_info(statement):
    """(verb, table, first keyword offset) without materializing INSERT data."""
    statement.seek(0)
    lexer = o19sql.Lexer()
    tokens = []
    while len(tokens) < 8:
        chunk = statement.read(8192)
        tokens.extend(lexer.feed(chunk, final=not chunk))
        if not chunk:
            break
        if tokens and tokens[0][1].upper() not in (b"CREATE", b"DROP", b"INSERT", b"REPLACE", b"ALTER", b"LOCK"):
            break
    offset = lexer.first_token_position or 0
    words = [(kind, value.decode("ascii", "replace")) for kind, value in tokens]
    if not words:
        return "", None, 0
    upper = [value.upper() for _, value in words]
    verb = upper[0]
    index = None
    if upper[:2] == ["CREATE", "TABLE"]:
        index = 2
        if upper[2:5] == ["IF", "NOT", "EXISTS"]:
            index = 5
    elif upper[:2] == ["DROP", "TABLE"]:
        index = 2
        if upper[2:4] == ["IF", "EXISTS"]:
            index = 4
    elif verb in ("INSERT", "REPLACE"):
        if "INTO" in upper[:4]:
            index = upper.index("INTO") + 1
    elif upper[:2] in (["ALTER", "TABLE"], ["LOCK", "TABLES"]):
        index = 2
    if index is not None:
        if index >= len(words) or words[index][0] not in ("word", "identifier"):
            raise ValueError("cannot identify table in dump statement")
        if index + 1 < len(words) and words[index + 1][1] == ".":
            raise ValueError("schema-qualified dump statements need a database-only export (no --databases)")
        return verb, words[index][1], offset
    if verb in ("USE", "DELIMITER", "DELETE", "UPDATE", "TRUNCATE") or verb == "CREATE":
        raise ValueError("unsupported {0} statement in dump set; export base tables without --databases, views, routines or triggers".format(verb))
    return verb, None, offset


def statement_session_assignments(statement, offset, mode_captures=None):
    """Scan every SET assignment, including those after a large prefix.

    Keep quote state across reads and finalize only at the statement's EOF.
    INSERT payloads never enter this metadata scan and remain disk-spooled.
    """
    statement.seek(offset)
    lexer = o19sql.Lexer(mark_truncated=True)
    tokens = []
    while True:
        chunk = statement.read(65536)
        tokens.extend(lexer.feed(chunk, final=not chunk))
        if len(tokens) > SET_TOKEN_LIMIT:
            raise ValueError("SET statement has too many metadata tokens; export with ordinary mysqldump session settings")
        if not chunk:
            return session_assignments(tokens, mode_captures)


def restore_chunks(stream, spool_dir=None):
    """Restore original SQL with uniqueness and diagnostics kept enabled.

    mysqldump itself sets UNIQUE_CHECKS=0, overriding --init-command. Append
    a separate control statement after session SET statements; never edit a
    literal, INSERT or source file. Large statements remain disk-spooled.
    """
    mode_captures = set()
    for statement in statements(stream, spool_dir):
        verb, _, offset = statement_info(statement)
        if verb == "SET":
            statement_session_assignments(statement, offset, mode_captures)
        statement.seek(0)
        for chunk in iter(lambda: statement.read(1 << 20), b""):
            yield chunk
        if verb == "SET":
            # SQL_NOTES=0 suppresses Notes even when note_verbosity is 'all'.
            # Both controls are needed on the packaged MariaDB floor (11.4).
            yield b"\nSET SESSION UNIQUE_CHECKS=1, SQL_NOTES=1, note_verbosity='all';\n"


def session_assignments(tokens, mode_captures=None):
    """Relevant literal interpretation controls, excluding @OLD_* captures."""
    result = {}
    if mode_captures is None:
        mode_captures = set()
    keys = {b"SQL_MODE", b"TIME_ZONE", b"CHARACTER_SET_CLIENT", b"COLLATION_CONNECTION", b"OLD_MODE", b"OLD_SQL_MODE"}
    for index, (kind, value) in enumerate(tokens):
        value = value.upper()
        if kind not in ("word", "identifier") and not (
                kind == "string" and value == b"OLD_SQL_MODE" and index
                and tokens[index - 1] == ("symbol", b"@")):
            continue
        if value == b"NAMES":
            result["NAMES"] = tokens[index + 1:]
        elif value in keys and index + 1 < len(tokens):
            equals = index + 1
            if tokens[equals] == ("symbol", b":"):
                equals += 1
            if equals >= len(tokens) or tokens[equals] != ("symbol", b"="):
                continue
            # A system variable may be quoted or scoped (@@SESSION.sql_mode).
            # User variable names may also contain dots (@session.sql_mode).
            # Find the start of a qualified name before distinguishing @/@@.
            start = index
            while (start > 1 and tokens[start - 1] == ("symbol", b".")
                   and tokens[start - 2][0] in ("word", "identifier")):
                start -= 2
            user_variable = (start and tokens[start - 1] == ("symbol", b"@")
                             and not (start > 1 and tokens[start - 2] == ("symbol", b"@")))
            end = equals + 1
            while end < len(tokens) and tokens[end] not in (("symbol", b","), ("symbol", b";")):
                end += 1
            assigned = tokens[equals + 1:end]
            upper_assigned = [(kind, item.upper()) for kind, item in assigned]
            if user_variable:
                if value == b"OLD_SQL_MODE":
                    # mysqldump saves the current ordinary session mode and
                    # restores it in its footer. No other expression is safe
                    # to infer without evaluating arbitrary SQL.
                    capture = [("symbol", b"@"), ("symbol", b"@"), ("word", b"SQL_MODE")]
                    scoped = capture[:2] + [("word", b"SESSION"), ("symbol", b".")] + capture[2:]
                    normalized = [("word" if kind == "identifier" else kind, item)
                                  for kind, item in upper_assigned]
                    if normalized not in (capture, scoped) or start != index:
                        raise ValueError("dump overwrites saved SQL_MODE; export with ordinary mysqldump session settings")
                    mode_captures.add(b"OLD_SQL_MODE")
                continue
            if value == b"SQL_MODE" and any(kind == "truncated_string" for kind, _ in assigned):
                raise ValueError("dump SQL_MODE literal exceeds the metadata limit; export with ordinary mysqldump quoting")
            if value == b"SQL_MODE" and any(kind in ("string", "word") and QUOTING_MODES.search(item)
                                            for kind, item in assigned):
                raise ValueError("dump changes SQL quoting modes; export with ordinary mysqldump quoting (no ANSI_QUOTES or NO_BACKSLASH_ESCAPES)")
            if value == b"SQL_MODE":
                literal = (len(assigned) == 1 and assigned[0][0] in ("string", "word"))
                if literal and assigned[0][0] == "word":
                    item = upper_assigned[0][1]
                    literal = (item == b"0" or (re.fullmatch(rb"[A-Z_][A-Z0-9_]*", item)
                                               and item not in (b"DEFAULT", b"NULL")))
                saved = (len(upper_assigned) == 2 and upper_assigned[0] == ("symbol", b"@")
                         and upper_assigned[1][0] in ("word", "identifier", "string")
                         and upper_assigned[1][1] == b"OLD_SQL_MODE" and b"OLD_SQL_MODE" in mode_captures)
                if not literal and not saved:
                    raise ValueError("unsupported SQL_MODE expression; export with ordinary mysqldump session settings")
            if value == b"OLD_SQL_MODE":
                raise ValueError("unsupported saved SQL_MODE assignment; export with ordinary mysqldump session settings")
            result[value.decode("ascii")] = assigned
    return result


def inventory(path):
    tables = {}
    created = set()
    opener = gzip.open if path.endswith(".gz") else open
    tail = b""
    session = {}
    mode_captures = set()
    with opener(path, "rb") as stream:
        for statement in statements(stream, os.path.dirname(path)):
            verb, table, offset = statement_info(statement)
            if verb == "SET":
                session.update(statement_session_assignments(statement, offset, mode_captures))
            # Ignore comments surrounding statements, locks, and key-disable
            # wrappers. Compare the actual CREATE and INSERT/REPLACE bytes.
            wrapper = False
            if verb == "ALTER":
                statement.seek(offset)
                tokens = o19sql.Lexer().feed(statement.read(32768), final=True)
                wrapper = len(tokens) == 6 and [value.upper() for _, value in tokens[3:5]] in ([b"DISABLE", b"KEYS"], [b"ENABLE", b"KEYS"])
            if table and (verb in ("CREATE", "INSERT", "REPLACE") or (verb == "ALTER" and not wrapper)):
                if verb == "CREATE":
                    if table in created:
                        raise ValueError("table {0!r} is defined twice within one dump".format(table))
                    created.add(table)
                digest = tables.setdefault(table, hashlib.sha256())
                digest.update(repr(sorted(session.items())).encode("ascii"))
                statement.seek(offset)
                for chunk in iter(lambda: statement.read(1 << 20), b""):
                    digest.update(chunk)
            statement.seek(0)
            for chunk in iter(lambda: statement.read(1 << 20), b""):
                tail = (tail + chunk)[-8192:]
    if TRAILER not in tail:
        raise ValueError("dump part lacks its completion trailer; obtain a complete export")
    if set(tables) != created:
        raise ValueError("dump part has table data without a CREATE TABLE definition")
    return {table: digest.hexdigest() for table, digest in sorted(tables.items())}


def preparation_footprint(path):
    """Measure combined-output bytes and largest disk spool without writing SQL.

    Count the newline appended after every statement, including its trailing
    comment fragment. Inventory and combination each keep at most one statement
    spool at once; only statements exceeding the memory limit consume disk.
    """
    lexer = o19sql.Lexer()
    opener = gzip.open if path.endswith(".gz") else open
    position = start = count = largest = 0
    try:
        with opener(path, "rb") as stream:
            while True:
                chunk = stream.read(1 << 20)
                lexer.feed(chunk, final=not chunk)
                for boundary in lexer.boundaries:
                    largest = max(largest, boundary - start)
                    start = boundary
                    count += 1
                position += len(chunk)
                if not chunk:
                    if lexer.state not in ("sql", "line") or lexer.executable:
                        raise ValueError("incomplete SQL statement (unclosed quote or comment)")
                    if position > start:
                        largest = max(largest, position - start)
                        count += 1
                    break
    except zlib.error as exc:
        raise ValueError("corrupt compressed SQL dump") from exc
    return position + count, largest if largest > STATEMENT_SPOOL_LIMIT else 0


def prepare_dump_set(paths, workdir):
    """Snapshot, inventory and combine two or more dumps; return paths/provenance.

    A single dump retains the established interface. For multiple dumps the
    output contains one owner for each table, with each part's session prelude
    and epilogue intact. File order and all part digests are bound to the run.
    Before inventory can spill expanded statements to disk, reserve room for
    the complete output and its largest statement spool. Identical overlaps
    may reduce actual output; the budget conservatively includes every part.
    """
    if not paths:
        raise ValueError("no SQL dump selected")
    os.makedirs(workdir, mode=0o700, exist_ok=True)
    parts = []
    owners = {}
    duplicates = []
    for number, path in enumerate(paths):
        size = os.path.getsize(path)
        if shutil.disk_usage(workdir).free < size:
            raise ValueError("insufficient disk to snapshot the complete SQL dump set")
        snapshot = os.path.join(workdir, "part-{0:03d}.sql{1}".format(number, ".gz" if path.endswith(".gz") else ""))
        digest = snapshot_file(path, snapshot)
        parts.append({"name": os.path.basename(path), "sha256": digest,
                      "snapshot": snapshot})
    if len(paths) == 1:
        return {"dump": parts[0]["snapshot"], "parts": [
            {"name": parts[0]["name"], "sha256": parts[0]["sha256"]}], "duplicates": []}
    footprints = [preparation_footprint(part["snapshot"]) for part in parts]
    output_bytes = sum(output for output, _ in footprints) + len(COMBINED_TRAILER)
    spool_bytes = max(spool for _, spool in footprints)
    required = output_bytes + spool_bytes
    # These snapshots already consume space: the current free-space figure
    # accounts for them, as well as any output retained from an earlier run.
    if shutil.disk_usage(workdir).free < required:
        raise ValueError("insufficient disk to prepare the expanded SQL dump set and temporary statement spool")
    for number, part in enumerate(parts):
        tables = inventory(part["snapshot"])
        part["tables"] = tables
        for table, fingerprint in tables.items():
            if table in owners:
                previous, previous_fingerprint = owners[table]
                if fingerprint != previous_fingerprint:
                    raise ValueError("overlapping table {0!r} differs between dump parts {1} and {2}; select one consistent snapshot"
                                     .format(table, previous + 1, number + 1))
                duplicates.append({"table": table, "part": number, "owner": previous})
            else:
                owners[table] = number, fingerprint
    provenance = {"format": FORMAT, "parts": [
        {key: value for key, value in part.items() if key != "snapshot"} for part in parts],
        "duplicates": duplicates}
    provenance_text = json.dumps(provenance, sort_keys=True, indent=2)
    # Provenance is written after the combined file, when its spool is gone.
    # Recheck current capacity now that its exact size and table set are known.
    required = output_bytes + max(spool_bytes, len(provenance_text.encode("utf-8")))
    if shutil.disk_usage(workdir).free < required:
        raise ValueError("insufficient disk to prepare the expanded SQL dump set and provenance")
    dest_path = os.path.join(workdir, "combined.sql")
    incoming = dest_path + ".incoming"
    fd = os.open(incoming, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as dest:
            os.fchmod(dest.fileno(), 0o600)
            for number, part in enumerate(parts):
                opener = gzip.open if part["snapshot"].endswith(".gz") else open
                with opener(part["snapshot"], "rb") as source:
                    for statement in statements(source, workdir):
                        verb, table, _ = statement_info(statement)
                        if table and owners[table][0] != number:
                            continue
                        statement.seek(0)
                        shutil.copyfileobj(statement, dest, 1 << 20)
                        dest.write(b"\n")
            dest.write(COMBINED_TRAILER)
            dest.flush()
            os.fsync(dest.fileno())
        os.replace(incoming, dest_path)
    finally:
        if os.path.exists(incoming):
            os.unlink(incoming)
    with open(os.path.join(workdir, "sources.json"), "w", encoding="utf-8") as document:
        os.fchmod(document.fileno(), 0o600)
        document.write(provenance_text)
    return {"dump": dest_path, "parts": provenance["parts"], "duplicates": duplicates}


def prepared_document_member(member, name):
    """Apply the output path and ownership consistently to tar metadata."""
    member.name = name
    # A parsed PAX path overrides TarInfo.name when serialized. Recompute it
    # from the normalized name, including an incremental backup's context.
    member.pax_headers = dict(member.pax_headers)
    member.pax_headers.pop("path", None)
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    member.mode = 0o600
    return member


def prepare_documents(paths, workdir):
    """Combine full plus incremental document archives in explicit input order.

    The last occurrence of a path wins, as in OSCAR's documented restore.
    Emit one tar member per final path so P5's duplicate-path checks remain
    meaningful. All input digests and order belong to the provenance.
    """
    if not paths:
        return {"documents": None, "parts": []}
    os.makedirs(workdir, mode=0o700, exist_ok=True)
    snapshots = []
    parts = []
    for number, path in enumerate(paths):
        if shutil.disk_usage(workdir).free < os.path.getsize(path):
            raise ValueError("insufficient disk to snapshot the documents backup set")
        snapshot = os.path.join(workdir, "documents-{0:03d}.tar{1}".format(number, ".gz" if path.endswith(".gz") else ""))
        digest = snapshot_file(path, snapshot)
        snapshots.append(snapshot)
        parts.append({"name": os.path.basename(path), "sha256": digest})
    paths = snapshots
    if len(paths) == 1:
        return {"documents": paths[0], "parts": parts}
    from . import o19bundle, o19docs
    os.makedirs(workdir, mode=0o700, exist_ok=True)
    owners = {}
    context = None
    prefixes = []
    required = 0
    for number, path in enumerate(paths):
        entries = o19bundle.read_tar_entries(path, path.endswith(".gz"))
        o19bundle.validate_tar_members([(kind, name) for kind, name, _ in entries], allow_dirs=True)
        # Native incremental backups are made from INSIDE oscar/, unlike
        # full document backups. Recognize their known context subdirectories.
        tops = {os.path.normpath(name).split("/")[0].lower() for kind, name, _ in entries
                if kind == o19bundle.TAR_TYPE_FILE}
        prefix = ""
        if context and tops and tops <= o19bundle._DOCUMENT_DIRECTORIES:
            prefix = context
        else:
            current = o19docs.detect_context_dir([(kind, name) for kind, name, _ in entries])
            if context and context != current:
                raise ValueError("document backup parts use different context directories")
            context = current
        prefixes.append(prefix)
        for kind, name, size in entries:
            if kind == o19bundle.TAR_TYPE_FILE:
                owners[os.path.join(prefix, os.path.normpath(name))] = number, size
    # PAX headers for Unicode/long paths or source metadata can exceed the
    # ordinary header allowance. Measure exactly what the writer will emit.
    required = 0
    with tarfile.open(fileobj=io.BytesIO(), mode="w") as probe:
        for number, path in enumerate(paths):
            with tarfile.open(path, mode="r|*") as source:
                for member in source:
                    key = os.path.join(prefixes[number], os.path.normpath(member.name))
                    if member.isfile() and owners.get(key, (None,))[0] == number:
                        member = prepared_document_member(member, key)
                        required += len(member.tobuf(probe.format, probe.encoding, probe.errors))
                        required += (member.size + tarfile.BLOCKSIZE - 1) // tarfile.BLOCKSIZE * tarfile.BLOCKSIZE
    required += 2 * tarfile.BLOCKSIZE
    required = (required + tarfile.RECORDSIZE - 1) // tarfile.RECORDSIZE * tarfile.RECORDSIZE
    if shutil.disk_usage(workdir).free < required:
        raise ValueError("insufficient disk to prepare the complete documents backup set")
    output = os.path.join(workdir, "documents.tar")
    incoming = output + ".incoming"
    fd = os.open(incoming, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as dest, tarfile.open(fileobj=dest, mode="w") as combined:
            os.fchmod(dest.fileno(), 0o600)
            for number, path in enumerate(paths):
                with tarfile.open(path, mode="r|*") as source:
                    for member in source:
                        key = os.path.join(prefixes[number], os.path.normpath(member.name))
                        if member.isfile() and owners.get(key, (None,))[0] == number:
                            member = prepared_document_member(member, key)
                            with source.extractfile(member) as content:
                                combined.addfile(member, content)
            dest.flush()
            os.fsync(dest.fileno())
        os.replace(incoming, output)
    finally:
        if os.path.exists(incoming):
            os.unlink(incoming)
    return {"documents": output, "parts": parts}
