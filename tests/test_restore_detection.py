# SPDX-License-Identifier: MIT
"""
Finding the .csproj that owns a file, and what state its package restore is in.

check has to tell three situations apart, because they call for opposite
advice: a project never restored (compile errors may be missing), a restore
that failed (missing-type errors may be phantoms), and a clean restore (a
CS0246 is a real missing using). check used to guess from the proportion of
missing-type errors, and told agents not to trust a genuine missing using.
The end-to-end behaviour is in tests/integration/test_check.py.

Fast, no language server, no subprocess.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lodesman.server import NEVER_RESTORED, restore_state

# What `dotnet restore` wrote against an unreachable feed, reproduced
# 2026-09-21: the assets file exists and records the failure in `logs`.
FAILED_RESTORE = {
    "version": 3,
    "logs": [
        {"code": "NU1301", "level": "Error",
         "message": "Unable to load the service index for source https://nuget.invalid.example/v3/index.json."},
        {"code": "NU1900", "level": "Warning",
         "message": "Error occurred while getting package vulnerability data."},
    ],
}
# A clean restore can still log warnings; those are not failures.
CLEAN_RESTORE = {"version": 3, "logs": [{"code": "NU1900", "level": "Warning", "message": "…"}]}


class TestRestoreState(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="lodesman-restore-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def write(self, relative: str, content: str = "") -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        # utf-8-sig: NuGet writes the assets file with a byte-order mark.
        path.write_text(content, encoding="utf-8-sig" if relative.endswith(".json") else "utf-8")

    def assets(self, relative: str, data: dict) -> None:
        self.write(relative, json.dumps(data))

    def test_never_restored(self):
        self.write("App.csproj")
        self.write("Program.cs")
        self.assertEqual(restore_state(self.root, "Program.cs"), ("App.csproj", NEVER_RESTORED))

    def test_clean_restore_with_warnings_is_clean(self):
        self.write("App.csproj")
        self.assets("obj/project.assets.json", CLEAN_RESTORE)
        self.write("Program.cs")
        self.assertEqual(restore_state(self.root, "Program.cs"), ("App.csproj", None))

    def test_failed_restore_is_reported_with_its_error(self):
        self.write("App.csproj")
        self.assets("obj/project.assets.json", FAILED_RESTORE)
        self.write("Program.cs")
        project, problem = restore_state(self.root, "Program.cs")
        self.assertEqual(project, "App.csproj")
        self.assertIn("NU1301", problem)
        self.assertIn("Unable to load the service index", problem)
        self.assertNotIn("NU1900", problem)  # a warning is not the failure

    def test_unreadable_restore_output_is_a_problem_not_a_pass(self):
        self.write("App.csproj")
        self.write("obj/project.assets.json", "{ truncated")
        self.write("Program.cs")
        self.assertIsNotNone(restore_state(self.root, "Program.cs")[1])

    def test_the_nearest_project_decides(self):
        # A cleanly restored project nested in a never-restored one owns its files.
        self.write("Outer.csproj")
        self.write("src/Inner/Inner.csproj")
        self.assets("src/Inner/obj/project.assets.json", CLEAN_RESTORE)
        self.write("src/Inner/Deep/Thing.cs")
        self.assertEqual(restore_state(self.root, "src/Inner/Deep/Thing.cs"),
                         ("src/Inner/Inner.csproj", None))
        self.write("src/Loose.cs")
        self.assertEqual(restore_state(self.root, "src/Loose.cs"), ("Outer.csproj", NEVER_RESTORED))

    def test_no_project_means_no_verdict(self):
        self.write("scripts/tool.cs")
        self.assertEqual(restore_state(self.root, "scripts/tool.cs"), (None, None))


if __name__ == "__main__":
    unittest.main()
