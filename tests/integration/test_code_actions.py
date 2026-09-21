# SPDX-License-Identifier: MIT
"""
Integration tests for code_action, against a real language server.

The scenario is the one agents hit most: a type used without its import. Each
test asserts on bytes on disk and on the compiler's verdict afterwards, not on
what the tool says it did — rename_symbol once reported success while writing
nothing, and only a byte comparison could see it.

Only languages whose server returns the fix as an edit are covered. Probed on
2026-09-21:

  * TypeScript offers "Add import from ..." with the edit inline.
  * Roslyn offers "using System.Text;" as a handle, resolved on request — but
    only once the project is restored. Unrestored, it reports no compiler
    diagnostics at all, so there is nothing to attach a fix to.
  * pyright offers no import fixes. jdtls offers none either, because
    SolidLSP's jdtls configuration does not declare the client capability.

    LODESMAN_INTEGRATION=1 python -m unittest tests.integration.test_code_actions
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.integration import languages
from tests.integration.harness import Server

ENABLED = os.environ.get("LODESMAN_INTEGRATION") == "1"
SKIP_REASON = "set LODESMAN_INTEGRATION=1 to run (needs language servers)"


@unittest.skipUnless(ENABLED, SKIP_REASON)
class MissingImportCase:
    """Deliberately not a TestCase, so unittest does not run the base itself."""

    language: str
    file: str
    source: str
    line: int
    title: str
    import_line: bytes

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix=f"lodesman-actions-{self.language}-")
        self.addCleanup(tmp.cleanup)
        spec = languages.BY_LANGUAGE[self.language]
        self.repo = spec.build(Path(tmp.name).resolve() / "repo")
        self.path = self.repo / self.file
        self.path.write_text(self.source, encoding="utf-8", newline="")
        self.prepare()

        self.server = Server(self.repo, language=self.language)
        self.addCleanup(self.server.close)
        self.server.initialize()
        if not self.server.language_server_ready(symbols=("Record", "Store")):
            self.skipTest(f"no working {self.language} language server on this machine")

    def prepare(self) -> None:
        pass

    def call(self, **arguments) -> str:
        text, is_error = self.server.call("code_action", {"file": self.file, **arguments})
        self.assertFalse(is_error, text)
        return text

    def test_lists_the_import_fix(self):
        self.assertIn(self.title, self.call(line=self.line))

    def test_preview_writes_nothing(self):
        before = self.path.read_bytes()
        text = self.call(line=self.line, title=self.title)
        self.assertIn("Dry run", text)
        self.assertIn(self.import_line.decode(), text)  # the diff shows the change
        self.assertEqual(self.path.read_bytes(), before)

    def test_apply_writes_the_import_and_the_file_compiles(self):
        text = self.call(line=self.line, title=self.title, apply=True)
        self.assertIn("Written: 1 file(s)", text)
        self.assertIn(self.import_line, self.path.read_bytes())

        verdict, is_error = self.server.call("check", {"file": self.file, "severity": 1})
        self.assertFalse(is_error, verdict)
        self.assertIn("no errors", verdict)

    def test_an_ambiguous_or_unknown_title_is_refused(self):
        text, is_error = self.server.call(
            "code_action", {"file": self.file, "line": self.line, "title": "no such action"}
        )
        self.assertTrue(is_error)
        self.assertIn("match", text)


class TestTypeScript(MissingImportCase, unittest.TestCase):
    language = "typescript"
    file = "src/extra.ts"
    source = "export const m = new MemoryStore();\n"
    line = 1
    title = "Add import"
    import_line = b'import { MemoryStore } from "./store";'


class TestCSharp(MissingImportCase, unittest.TestCase):
    language = "csharp"
    file = "Extra.cs"
    source = (
        "namespace Fixture;\n\n"
        "public class Extra\n{\n"
        "    public StringBuilder Build() => new StringBuilder();\n"
        "}\n"
    )
    line = 5
    title = "using System.Text"
    import_line = b"using System.Text;"

    def prepare(self) -> None:
        # Roslyn reports no compiler diagnostics for an unrestored project.
        if shutil.which("dotnet") is None:
            self.skipTest("dotnet SDK not installed; Roslyn needs a restored project here")
        done = subprocess.run(["dotnet", "restore"], cwd=self.repo,
                              capture_output=True, text=True, timeout=600, check=False)
        if done.returncode != 0:
            self.skipTest(f"dotnet restore failed: {done.stderr[-300:]}")


if __name__ == "__main__":
    unittest.main()
