"""Python sources carry no comments; only shebangs and ruff noqa directives (never RUF100)."""

import io
import re
import tokenize
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
CODE = r"(?!RUF100)[A-Z]+[0-9]+"
DIRECTIVE = re.compile(rf"# noqa: ?{CODE}(, ?{CODE})*")


class NoCommentTests(unittest.TestCase):
    def test_sources_have_no_comments(self):
        found = []
        for folder in ("src", "tests", "scripts"):
            for path in sorted((ROOT / folder).rglob("*.py")):
                source = io.StringIO(path.read_text())
                for token in tokenize.generate_tokens(source.readline):
                    shebang = token.start == (1, 0) and token.string.startswith("#!")
                    if (
                        token.type == tokenize.COMMENT
                        and not shebang
                        and not DIRECTIVE.fullmatch(token.string)
                    ):
                        found.append(f"{path.relative_to(ROOT)}:{token.start[0]}")
        self.assertEqual(found, [], "Remove comments; docstrings may explain intent")
