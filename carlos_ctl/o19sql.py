# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""Bounded, incremental SQL tokenization for dump metadata.

Values are never interpreted or rewritten. Quote/comment state survives read
boundaries; long clinical literals are discarded after a bounded token prefix.
MariaDB/MySQL executable comments expose their SQL, ordinary comments do not.
The supported dump syntax has backslash escapes enabled (the importer checks it).
"""
import re

WORD = re.compile(rb"[A-Za-z0-9_$]+")
SPACE = re.compile(rb"\s+")
VERSION = re.compile(rb"[0-9]*")
DATA_RUN = re.compile(rb"[^\x27\x22`/#;\x2d]+")
QUOTED_SPECIAL = {quote: re.compile(rb"[\\" + bytes([quote]) + rb"]")
                  for quote in (39, 34, 96)}


class Lexer:
    def __init__(self, mark_truncated=False):
        self.pending = b""
        self.state = "sql"
        self.value = b""
        self.value_truncated = False
        self.mark_truncated = mark_truncated
        self.quote = 0
        self.executable = False
        self.received = 0
        self.boundaries = []
        self.first_token_position = None
        self.statement_start = True
        self.data_tokens = 0

    def feed(self, chunk, final=False):
        origin = self.received - len(self.pending)
        self.received += len(chunk)
        data = self.pending + chunk
        self.pending = b""
        self.boundaries = []
        out = []
        i = 0
        end = len(data)

        def remember(value):
            if self.data_tokens < 8:
                if len(self.value) + len(value) > 1024:
                    self.value_truncated = True
                self.value += value[:max(0, 1024 - len(self.value))]

        def emit(kind, value):
            # Dump INSERT payloads need quote/boundary tracking, not a token
            # per clinical value. Retain their bounded metadata prefix only.
            if kind == "symbol" and value == b";":
                self.statement_start = True
                self.data_tokens = 0
                out.append((kind, value))
                return
            if self.statement_start:
                self.statement_start = False
                if kind == "word" and value.upper() in (b"INSERT", b"REPLACE"):
                    self.data_tokens = 1
            elif self.data_tokens:
                self.data_tokens += 1
            if self.data_tokens <= 8:
                out.append((kind, value))

        while i < end:
            if self.state == "line":
                stop = data.find(b"\n", i)
                if stop < 0:
                    i = len(data)
                    break
                self.state = "sql"
                i = stop + 1
                continue
            if self.state == "comment":
                stop = data.find(b"*/", i)
                if stop < 0:
                    i = end - 1 if not final and data.endswith(b"*") else end
                    break
                self.state = "sql"
                i = stop + 2
                continue
            if self.state == "version":
                match = VERSION.match(data, i)
                i = match.end()
                if i >= end and not final:
                    break
                self.state = "sql"
                continue
            if self.state == "quoted":
                # Jump over literal content, rather than visiting every byte of
                # multi-megabyte INSERT values in Python.
                special = QUOTED_SPECIAL[self.quote].search(data, i, end)
                if special is None:
                    remember(data[i:end])
                    i = end
                    break
                stop = special.start()
                remember(data[i:stop])
                i = stop
                c = data[i]
                if not final and i + 1 == len(data):
                    break
                if c == 92:
                    if i + 1 >= len(data):
                        break
                    remember(data[i + 1:i + 2])
                    i += 2
                elif i + 1 < len(data) and data[i + 1] == self.quote:
                    remember(data[i:i + 1])
                    i += 2
                else:
                    kind = "identifier" if self.quote == 96 else "string"
                    if self.mark_truncated and self.value_truncated:
                        kind = "truncated_" + kind
                    emit(kind, self.value)
                    self.value = b""
                    self.state = "sql"
                    i += 1
                continue
            if self.state == "word":
                match = WORD.match(data, i)
                if match:
                    remember(match.group())
                    i = match.end()
                    if i >= end and not final:
                        break
                emit("word", self.value)
                self.value = b""
                self.state = "sql"
                continue

            if self.data_tokens >= 8:
                run = DATA_RUN.match(data, i)
                if run:
                    i = run.end()
                    continue
            c = data[i]
            if not final and end - i <= 3 and data[i:] in (b"/", b"/*", b"/*M", b"-", b"--", b"*"):
                break
            if data[i:i + 2] == b"*/" and self.executable:
                self.executable = False
                i += 2
            elif data[i:i + 3] == b"/*!" or data[i:i + 4] == b"/*M!":
                if self.first_token_position is None:
                    self.first_token_position = origin + i
                self.executable = True
                self.state = "version"
                i += 4 if data[i:i + 4] == b"/*M!" else 3
            elif data[i:i + 2] == b"/*":
                self.state = "comment"
                i += 2
            elif c == 35 or (data[i:i + 2] == b"--" and
                            (i + 2 == len(data) or data[i + 2] <= 32)):
                self.state = "line"
                i += 1 if c == 35 else 2
            elif c in (39, 34, 96):
                if self.first_token_position is None:
                    self.first_token_position = origin + i
                self.quote = c
                self.state = "quoted"
                self.value = b""
                self.value_truncated = False
                i += 1
            elif (65 <= c <= 90) or (97 <= c <= 122) or (48 <= c <= 57) or c in (95, 36):
                if self.first_token_position is None:
                    self.first_token_position = origin + i
                self.state = "word"
                self.value_truncated = False
            elif c <= 32:
                match = SPACE.match(data, i)
                i = match.end() if match else i + 1
            else:
                if self.first_token_position is None:
                    self.first_token_position = origin + i
                emit("symbol", data[i:i + 1])
                if c == 59 and not self.executable:
                    self.boundaries.append(origin + i + 1)
                i += 1
        self.pending = data[i:]
        if final and self.state == "word":
            emit("word", self.value)
            self.value = b""
            self.state = "sql"
        return out


class Collations:
    """Extract only syntactic COLLATE clauses, including quoted names."""
    def __init__(self):
        self.lexer = Lexer()
        self.expected = False

    def feed(self, chunk, final=False):
        names = set()
        for kind, value in self.lexer.feed(chunk, final):
            if self.expected:
                if kind == "symbol" and value == b"=":
                    continue
                if kind in ("word", "identifier", "string") and re.fullmatch(rb"[A-Za-z0-9_]+", value):
                    names.add(value.decode("ascii").lower())
                self.expected = False
            if kind == "word" and value.upper() == b"COLLATE":
                self.expected = True
        return names


class Redirects:
    """Database-changing syntax, ignoring strings/comments across read boundaries."""
    def __init__(self):
        self.lexer = Lexer()
        self.prefix = []
        self.recent = []
        self.gtid_assignment = False

    def feed(self, chunk, final=False):
        for token in self.lexer.feed(chunk, final):
            kind, value = token
            upper = value.upper()
            if token == ("symbol", b";"):
                self.prefix = []
                self.recent = []
                self.gtid_assignment = False
                continue
            if len(self.prefix) < 5:
                self.prefix.append((kind, upper))
            if self.prefix[0] == ("word", b"USE"):
                return "USE"
            if self.prefix[0] == ("word", b"CREATE"):
                tail = [value for kind, value in self.prefix[1:] if kind == "word"]
                while tail and tail[0] in (b"OR", b"REPLACE"):
                    tail.pop(0)
                if tail and tail[0] in (b"DATABASE", b"SCHEMA"):
                    return "CREATE " + tail[0].decode("ascii")
            if self.prefix[0] == ("word", b"SET"):
                if self.gtid_assignment and token == ("symbol", b"="):
                    return "GTID_PURGED"
                self.gtid_assignment = (kind == "word" and upper == b"GTID_PURGED" and (
                    self.recent[-2:] == [("symbol", b"@"), ("symbol", b"@")]
                    or self.recent[-2:] == [("word", b"GLOBAL"), ("symbol", b".")]
                    or self.recent[-1:] == [("word", b"GLOBAL")]))
            self.recent = (self.recent + [(kind, upper)])[-3:]
        return None


def available_collations(rows, old_mode):
    """Names MariaDB accepts, including utf8 aliases under this session mode.

    MariaDB 10.6+ omits utf8_* aliases from SHOW COLLATION. With the old-mode
    flag they denote utf8mb3; without it they denote utf8mb4. UCA14's generic
    SHOW COLLATION names also have charset-qualified spellings.
    """
    names = {str(row[0]).lower() for row in rows}
    family = "utf8mb3_" if "UTF8_IS_UTF8MB3" in str(old_mode).upper().split(",") else "utf8mb4_"
    names.update("utf8_" + name[len(family):] for name in tuple(names) if name.startswith(family))
    for name in tuple(names):
        if name.startswith("uca1400_"):
            names.add("utf8mb4_" + name)
            names.add("utf8mb3_" + name)
            names.add("utf8_" + name)
    return names
