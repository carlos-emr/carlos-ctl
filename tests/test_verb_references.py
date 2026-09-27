# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""Every `carlos-ctl <verb>` this package prints must be a verb it has.

Run (from the repository root):
    python3 -m unittest discover -v -s tests -t .
"""

import re
import unittest
from pathlib import Path


class TestEveryCommandWeTellAnOperatorToTypeExists(unittest.TestCase):

    """A `carlos-ctl <verb>` printed in an operator instruction must be
    a verb the tool dispatches.

    The failed-import next steps sent an operator to `carlos-ctl
    restore` — which has never existed. It reads as a real instruction,
    it is printed at the worst possible moment (a verification failure,
    with the clinic's data half-migrated), and the only answer it gets
    is `unknown command: restore`. Nothing checked, because the string
    lives in a tuple far from the verb table.

    Scoped to backticked commands: the modules also write `carlos-ctl`
    in prose ("carlos-ctl carries ...") and the point is to check what
    an operator would COPY."""

    #: `carlos-ctl x` inside backticks -- the way every instruction in
    #: this package writes a command
    COMMAND = re.compile(r"`carlos-ctl\s+([A-Za-z][A-Za-z0-9-]*)")

    def test_every_backticked_verb_is_a_real_verb(self):
        from carlos_ctl import cli
        verbs = set(cli._VERBS)
        self.assertIn("import-o19", verbs)          # the table was read
        package = Path(cli.__file__).parent
        seen = 0
        for path in sorted(package.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for m in self.COMMAND.finditer(text):
                seen += 1
                self.assertIn(
                    m.group(1), verbs,
                    "{0} tells an operator to run `carlos-ctl {1}`, which "
                    "is not a verb".format(path.name, m.group(1)))
        # the scan is only worth anything if it found the instructions
        self.assertGreater(seen, 10)


if __name__ == "__main__":
    unittest.main()
