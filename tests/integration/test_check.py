# SPDX-License-Identifier: MIT
"""
check on C#: never call broken code clean, and never blame real errors on the
package restore.

Three situations look alike from the diagnostics and need opposite advice:

* Never restored. Roslyn reports no compiler diagnostics for a project it
  loaded unrestored. Measured 2026-09-21: "no errors" on a fresh fixture with
  plain compile errors, because Roslyn restored on startup but never reloaded.
* Restore failed. An unreachable feed still writes obj/project.assets.json and
  records NU1301 in it; types from the missing packages then report as
  missing, so those errors may not be real.
* Restored cleanly. A CS0246 is then a real missing using. check used to guess
  from the proportion of missing-type errors and warned that the project "did
  not load fully" — telling an agent not to trust a real, fixable error.

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

MISSING_USING = (
    "using System.Collections.Generic;\n\n"
    "namespace Fixture;\n\n"
    "public class Extra\n{\n"
    "    public StringBuilder Build() => new StringBuilder();\n"
    "}\n"
)

# A feed that cannot resolve, and a package no local cache can hold, so the
# restore genuinely fails rather than succeeding from the cache. Offline-safe:
# the failure is a DNS lookup of a reserved name.
DEAD_FEED = (
    '<?xml version="1.0" encoding="utf-8"?>\n<configuration>\n  <packageSources>\n'
    '    <clear />\n'
    '    <add key="unreachable" value="https://nuget.invalid.example/v3/index.json" />\n'
    "  </packageSources>\n</configuration>\n"
)
MISSING_PACKAGE = (
    '  <ItemGroup><PackageReference Include="Lodesman.NotCachedAnywhere" '
    'Version="1.0.0" /></ItemGroup>\n</Project>'
)


@unittest.skipUnless(ENABLED, SKIP_REASON)
class TestCSharpCheck(unittest.TestCase):
    def start(self, files: dict[str, str], restore: str) -> Server:
        """restore: 'none' (leave it to Roslyn), 'clean', or 'dead-feed'."""
        tmp = tempfile.TemporaryDirectory(prefix="lodesman-check-")
        self.addCleanup(tmp.cleanup)
        repo = languages.BY_LANGUAGE["csharp"].build(Path(tmp.name).resolve() / "repo")
        for name, content in files.items():
            (repo / name).write_text(content, encoding="utf-8")
        if restore == "dead-feed":
            (repo / "nuget.config").write_text(DEAD_FEED, encoding="utf-8")
            project = repo / "Fixture.csproj"
            project.write_text(project.read_text(encoding="utf-8").replace("</Project>", MISSING_PACKAGE),
                               encoding="utf-8")
        if restore != "none":
            if shutil.which("dotnet") is None:
                self.skipTest("dotnet SDK not installed")
            done = subprocess.run(["dotnet", "restore"], cwd=repo, capture_output=True,
                                  text=True, timeout=600, check=False)
            if restore == "clean" and done.returncode != 0:
                self.skipTest(f"dotnet restore failed: {done.stderr[-300:]}")
            if restore == "dead-feed" and done.returncode == 0:
                self.fail("the dead-feed restore succeeded; the scenario did not happen")
        server = Server(repo, language="csharp")
        self.addCleanup(server.close)
        server.initialize()
        if not server.language_server_ready(symbols=("Record", "Store")):
            self.skipTest("no working C# language server on this machine")
        return server

    def check(self, server: Server, file: str) -> str:
        text, is_error = server.call("check", {"file": file, "severity": 1})
        self.assertFalse(is_error, text)
        return text

    def test_a_never_restored_project_still_reports_the_errors(self):
        # Roslyn restores the project itself while starting, then keeps the
        # project model it loaded before that restore until it is told the
        # restore output exists. Without that, this answered "no errors".
        text = self.check(self.start({"Broken.cs": BROKEN}, restore="none"), "Broken.cs")
        self.assertIn("CS0029", text)
        self.assertIn("CS0103", text)

    def test_restored_project_reports_the_errors(self):
        text = self.check(self.start({"Broken.cs": BROKEN}, restore="clean"), "Broken.cs")
        self.assertIn("CS0029", text)  # string to int
        self.assertIn("CS0103", text)  # UndefinedThing
        self.assertNotIn("WARNING", text)

    def test_a_genuine_missing_using_is_not_blamed_on_the_restore(self):
        text = self.check(self.start({"Extra.cs": MISSING_USING}, restore="clean"), "Extra.cs")
        self.assertIn("CS0246", text)
        self.assertIn("StringBuilder", text)
        self.assertNotIn("WARNING", text)

    def test_a_failed_restore_is_named(self):
        text = self.check(self.start({"Extra.cs": MISSING_USING}, restore="dead-feed"), "Extra.cs")
        self.assertIn("package restore failed", text)
        self.assertIn("NU1301", text)


if __name__ == "__main__":
    unittest.main()
