# SPDX-License-Identifier: MIT
"""
check on C#: it must never call broken code clean.

Roslyn reports no compiler diagnostics for a project it loaded unrestored.
Measured 2026-09-21: a file with plain compile errors got "no errors" from
check on a fresh fixture, because Roslyn restored the project on startup but
never reloaded it. A fresh clone that was never built is exactly that state.
A restore that fails outright is covered by the warning in
tests/test_restore_detection.py, since it needs no server to test.

    LODESMAN_INTEGRATION=1 python -m unittest tests.integration.test_check
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
SKIP_REASON = "set LODESMAN_INTEGRATION=1 to run (needs a language server)"

BROKEN = (
    "namespace Fixture;\n\n"
    "public class Broken\n{\n"
    "    public int NotAnInt() => \"text\";\n"
    "    public void Missing() { UndefinedThing(); }\n"
    "}\n"
)


@unittest.skipUnless(ENABLED, SKIP_REASON)
class TestCSharpCheck(unittest.TestCase):
    def start(self, restore: bool) -> Server:
        tmp = tempfile.TemporaryDirectory(prefix="lodesman-check-")
        self.addCleanup(tmp.cleanup)
        repo = languages.BY_LANGUAGE["csharp"].build(Path(tmp.name).resolve() / "repo")
        (repo / "Broken.cs").write_text(BROKEN, encoding="utf-8")
        if restore:
            if shutil.which("dotnet") is None:
                self.skipTest("dotnet SDK not installed")
            done = subprocess.run(["dotnet", "restore"], cwd=repo, capture_output=True,
                                  text=True, timeout=600, check=False)
            if done.returncode != 0:
                self.skipTest(f"dotnet restore failed: {done.stderr[-300:]}")
        server = Server(repo, language="csharp")
        self.addCleanup(server.close)
        server.initialize()
        if not server.language_server_ready(symbols=("Record", "Store")):
            self.skipTest("no working C# language server on this machine")
        return server

    def test_a_never_restored_project_still_reports_the_errors(self):
        # Roslyn restores the project itself while starting, then keeps the
        # project model it loaded before that restore until it is told the
        # restore output exists. Without that, this answered "no errors".
        text, is_error = self.start(restore=False).call(
            "check", {"file": "Broken.cs", "severity": 1}
        )
        self.assertFalse(is_error, text)
        self.assertIn("CS0029", text)
        self.assertIn("CS0103", text)

    def test_restored_project_reports_the_errors(self):
        text, is_error = self.start(restore=True).call(
            "check", {"file": "Broken.cs", "severity": 1}
        )
        self.assertFalse(is_error, text)
        self.assertIn("CS0029", text)  # string to int
        self.assertIn("CS0103", text)  # UndefinedThing
        self.assertNotIn("restored", text)


if __name__ == "__main__":
    unittest.main()
