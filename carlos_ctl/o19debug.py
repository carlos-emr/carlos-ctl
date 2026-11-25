# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""Structured diagnostics for hosted OSCAR import database operations."""

import errno
import json
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone

from . import o19sql
from .util import log, warn

FILENAME = "import-debug.jsonl"
_SERVER_ERROR = re.compile(r"ERROR ([0-9]{1,5})(?: \(([0-9A-Z]{5})\))?(?: at line [0-9]+)?:")
_ECHO_LINE = re.compile(r"^--------------\r?$", re.M)
_ENGINE_ERRNO = re.compile(r"\s*Got error (-?[0-9]{1,10})\b", re.I)
_ENGINE = re.compile(r" from (?:storage engine )?(InnoDB|MyISAM|Aria|MEMORY|MyRocks|RocksDB)\s*$", re.I)
_WARNINGS = re.compile(r"ETL produced SQL warning\(s\) (\d{1,5}(?:, \d{1,5})*) \(")


def _expression_references(expression, actual, archive):
    """Project identifiers from a generated expression. Never return SQL or literal tokens."""
    # Start with SELECT so the dump lexer treats REPLACE(...) and INSERT(...)
    # as expressions instead of switching into its bulk-INSERT payload mode.
    tokens = o19sql.Lexer(mark_truncated=True).feed(b"SELECT " + expression.encode("utf-8", "replace"), final=True)
    names = [value.decode("utf-8", "replace") if kind in ("word", "identifier") else None
             for kind, value in tokens]
    references = {}
    for i in range(len(tokens) - 2):
        if names[i] and tokens[i+1] == ("symbol", b".") and names[i+2]:
            references.setdefault(names[i].lower(), set()).add(names[i+2])
    inputs = sorted({actual[name.lower()] for name in references.get("s", ()) if name.lower() in actual})
    helpers = []
    for i in range(len(tokens) - 3):
        if (not archive or tokens[i][0] != "word" or tokens[i][1].upper() not in (b"FROM", b"JOIN")
                or names[i+1] != archive or tokens[i+2] != ("symbol", b".") or not names[i+3]):
            continue
        table = names[i+3]
        alias = i + 4
        if alias < len(tokens) and tokens[alias][0] == "word" and tokens[alias][1].upper() == b"AS":
            alias += 1
        cols = references.get(table.lower(), set())
        if alias < len(names) and names[alias]:
            cols = cols.union(references.get(names[alias].lower(), ()))
        helpers.append({"schema": archive, "table": table, "columns": sorted(cols)})
    return inputs, helpers


def table_fields(src, table, dst, target_table, entry, src_cols, columns, *, expressions=None, archive=None):
    actual = {name.lower(): name for name in src_cols}
    renames = entry.get("renames", {})
    fields = {"source_schema": src, "source_table": table, "target_schema": dst, "target_table": target_table,
              "source_column_by_target": {col: actual.get(renames.get(col, col).lower()) for col in columns}}
    for col, expression in (expressions or {}).items():
        inputs, helpers = _expression_references(expression, actual, archive)
        if col in entry.get("value_exprs", {}):
            fields["source_column_by_target"][col] = None
            fields.setdefault("expression_source_columns", {})[col] = inputs
        if helpers:
            fields.setdefault("archive_reads_by_target", {})[col] = helpers
    return fields


def server_error(stderr):
    """Identify a diagnostic only at the client's boundary. SQL and error values may span lines."""
    text = stderr.lstrip("\r\n")
    while text.lower().startswith(("warning:", "[warning]", "mariadb:", "mysql:")):
        _, _, text = text.partition("\n")
        text = text.lstrip("\r\n")
    if _ECHO_LINE.match(text):
        delimiters = _ECHO_LINE.finditer(text)
        next(delimiters)  # opening delimiter
        closing = next(delimiters, None)
        # An extra delimiter could belong to the SQL or to a value in the server message.
        # Guessing which one closes the echo can report a clinical literal as a SQLSTATE.
        if closing is None or next(delimiters, None) is not None:
            return None
        text = text[closing.end():].lstrip("\r\n")
    return _SERVER_ERROR.match(text)


def error_fields(exc):
    """Project only diagnostic codes. Client stderr can echo the SQL and row values."""
    fields = {"exception": type(exc).__name__}
    while exc is not None:
        if type(getattr(exc, "client_exit_status", None)) is int:
            fields["client_exit_status"] = exc.client_exit_status
        stderr = getattr(exc, "stderr", "")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        message = stderr or str(exc)
        match = server_error(stderr) if stderr else _SERVER_ERROR.match(message)
        if match:
            fields["mariadb_errno"] = int(match[1])
            if match[2]:
                fields["sqlstate"] = match[2]
            if fields["mariadb_errno"] == 1030:
                diagnostic = match.string[match.end():].split("\n", 1)[0]
                errno = _ENGINE_ERRNO.match(diagnostic)
                if errno:
                    fields["engine_errno"] = int(errno[1])
                    engine = _ENGINE.search(diagnostic)
                    if engine:
                        fields["engine"] = engine[1]
            break
        warnings = _WARNINGS.match(message)
        if warnings:
            fields["warning_codes"] = [int(code) for code in warnings[1].split(", ")]
            break
        exc = exc.__cause__
    return fields


def _discard_incomplete_tail(path):
    """Under the import workspace lock, keep every newline-terminated record after a torn append."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    with os.fdopen(fd, "r+b") as trace:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "import trace must be a regular file")
        end = trace.seek(0, os.SEEK_END)
        if not end:
            return False
        trace.seek(end - 1)
        if trace.read(1) == b"\n":
            return False
        # Trace files can span several attempts. Read backwards only through the final fragment.
        while end:
            start = max(0, end - 8192)
            trace.seek(start)
            newline = trace.read(end - start).rfind(b"\n")
            if newline >= 0:
                trace.truncate(start + newline + 1)
                return True
            end = start
        trace.truncate(0)
        return True


class Diagnostics:
    def __init__(self, state_dir, append, *, verbose, mode, resume, version, manifest, timeout, emr_version="unknown"):
        self.path = os.path.join(state_dir, FILENAME)
        self.append = append
        self.verbose = verbose
        self.write_failed = False
        self.first_record = True
        self.attempt = {"attempt": uuid.uuid4().hex, "mode": mode, "resume": resume,
                        "carlos_ctl_version": version, "carlos_emr_version": emr_version,
                        "manifest": manifest, "statement_timeout_s": timeout}
        if verbose and self.record("opened"):
            log(f"Database trace for this import attempt is in {self.path}")

    def record(self, event, **fields):
        if self.write_failed:
            return False
        try:
            if self.first_record:
                if _discard_incomplete_tail(self.path):
                    fields["discarded_incomplete_record"] = True
                self.first_record = False
            row = dict(self.attempt, utc=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                       event=event, **fields)
            self.append(self.path, json.dumps(row, ensure_ascii=True) + "\n")
        except OSError as exc:
            self.write_failed = True
            warn(f"Cannot write import diagnostics to {self.path} (OS error {exc.errno}).")
            return False
        return True

    @contextmanager
    def measure(self, phase, operation, context, key, window, checkpoint, scope):
        info = dict(context, phase=phase, operation=operation)
        if scope:
            info["scope"] = scope
        if window is not None:
            info.update(scope="ID range", range_key=key, first_id=window[0] + 1, last_id=window[1])
        elif key and scope == "range discovery":
            info["candidate_range_key"] = key
        if checkpoint is not None:
            info["checkpoint_through"] = checkpoint
        started = datetime.now(timezone.utc)
        tick = time.monotonic()
        if self.verbose:
            self.record("started", **info)
        try:
            yield info
        except Exception as exc:
            info.update(error_fields(exc))
            info.update(started_utc=started.isoformat(timespec="milliseconds"),
                        elapsed_seconds=round(time.monotonic() - tick, 3))
            hint = ""
            if info.get("mariadb_errno") == 1030:
                since = started - timedelta(minutes=1)
                until = datetime.now(timezone.utc) + timedelta(minutes=1)
                hint = ("journalctl --utc -u mariadb "
                        f"--since '{since:%Y-%m-%d %H:%M:%S} UTC' --until '{until:%Y-%m-%d %H:%M:%S} UTC'")
                info["server_journal_command"] = hint
                info["server_log_location_sql"] = "SHOW GLOBAL VARIABLES LIKE 'log_error'"
            saved = self.record("failed", **info)
            if saved:
                warn(f"{phase} {operation} failed. Database diagnostics recorded in {self.path}")
            if hint:
                warn(f"Check the database host's MariaDB error log for this UTC interval. On systemd use {hint}")
            raise
        else:
            if self.verbose:
                self.record("completed", elapsed_seconds=round(time.monotonic() - tick, 3), **info)


def operation(trace, phase, name, context, key=None, window=None, checkpoint=None, *, scope=None):
    if trace is None:
        return nullcontext()
    return trace.measure(phase, name, context, key, window, checkpoint, scope)
