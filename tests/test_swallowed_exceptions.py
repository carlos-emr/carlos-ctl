# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""An `except` that only passes must say why, in the handler itself.

carlos-ctl runs the install, upgrade and import paths on a clinic's box. A
handler that swallows an error with a bare `pass` leaves nothing for the
operator, or the next maintainer, to find: it cannot be told a deliberate
best-effort cleanup from a forgotten failure. The code-quality scanner on
this organisation's pull requests flags exactly that shape (`Empty except`:
the clause does nothing but `pass` and has no explanatory comment), which
carlos-emr/carlos#3605 tracks.

The comment has to sit INSIDE the handler -- on the `except` line or between
it and the `pass`. A paragraph above the enclosing `try` explains the try, not
the swallow, and does not satisfy the scanner or tell a reader who lands on
the `except` why that failure is safe to drop.

The detector is tested on its own first so the package-wide contract cannot
pass vacuously.

Run (from the repository root):
    python3 -m unittest discover -v -s tests -t .
"""

import ast
import io
import textwrap
import tokenize
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "carlos_ctl"


def unexplained_pass_handlers(source):
    """(line, caught exception) of each `except` whose whole body is `pass`
    and which carries no comment between its first and last line."""
    tree = ast.parse(source)
    comment_lines = {
        tok.start[0]
        for tok in tokenize.generate_tokens(io.StringIO(source).readline)
        if tok.type == tokenize.COMMENT}
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if not all(isinstance(stmt, ast.Pass) for stmt in node.body):
            continue
        if any(node.lineno <= line <= node.end_lineno for line in comment_lines):
            continue
        found.append((node.lineno, ast.unparse(node.type) if node.type else "bare"))
    return sorted(found)


class TestTheDetector(unittest.TestCase):

    def flagged(self, source):
        return unexplained_pass_handlers(textwrap.dedent(source))

    def test_a_bare_pass_is_flagged(self):
        self.assertEqual(self.flagged("""\
            try:
                work()
            except OSError:
                pass
            """), [(3, "OSError")])

    def test_a_typed_except_is_not_an_explanation(self):
        # narrowing the type is not what the scanner asks for
        self.assertEqual(self.flagged("""\
            try:
                work()
            except FileNotFoundError:
                pass
            """), [(3, "FileNotFoundError")])

    def test_a_bare_except_clause_is_flagged(self):
        self.assertEqual(self.flagged("""\
            try:
                work()
            except:
                pass
            """), [(3, "bare")])

    def test_a_comment_above_the_try_does_not_explain_the_swallow(self):
        self.assertEqual(self.flagged("""\
            # this paragraph is about the link() call
            try:
                work()
            except OSError:
                pass
            """), [(4, "OSError")])

    def test_a_comment_inside_the_handler_explains_it(self):
        self.assertEqual(self.flagged("""\
            try:
                work()
            except OSError:
                # best-effort: the file may already be gone
                pass
            """), [])

    def test_a_comment_on_the_except_line_explains_it(self):
        self.assertEqual(self.flagged("""\
            try:
                work()
            except OSError:  # best-effort cleanup
                pass
            """), [])

    def test_a_trailing_comment_on_the_pass_explains_it(self):
        self.assertEqual(self.flagged("""\
            try:
                work()
            except OSError:
                pass  # best-effort cleanup
            """), [])

    def test_a_handler_that_does_something_is_not_flagged(self):
        self.assertEqual(self.flagged("""\
            try:
                work()
            except OSError as exc:
                warn(str(exc))
            """), [])

    def test_a_comment_belonging_to_a_neighbouring_handler_does_not_leak(self):
        self.assertEqual(self.flagged("""\
            try:
                work()
            except FileNotFoundError:
                pass
            except OSError as exc:
                # this one is explained
                warn(str(exc))
            """), [(3, "FileNotFoundError")])

    def test_every_handler_of_a_stacked_pair_is_judged_separately(self):
        self.assertEqual(self.flagged("""\
            try:
                work()
            except FileNotFoundError:
                # already gone: nothing to do
                pass
            except PermissionError:
                pass
            """), [(6, "PermissionError")])


class TestThePackage(unittest.TestCase):

    def test_there_are_modules_to_check(self):
        # a moved package directory must fail here, not pass an empty loop
        self.assertGreater(len(sorted(PACKAGE.glob("*.py"))), 10)

    def test_no_handler_swallows_an_error_without_saying_why(self):
        problems = []
        for path in sorted(PACKAGE.rglob("*.py")):
            for line, caught in unexplained_pass_handlers(path.read_text(encoding="utf-8")):
                problems.append(f"{path.relative_to(PACKAGE.parent)}:{line}: except {caught}: pass")
        self.assertEqual(
            problems, [],
            "an `except` that only passes needs a comment inside the handler saying why the "
            "failure is safe to drop (or a warn()/log line instead); see carlos-emr/carlos#3605")


if __name__ == "__main__":
    unittest.main()
