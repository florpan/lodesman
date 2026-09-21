# SPDX-License-Identifier: MIT
"""
Finding the .csproj that owns a file, and whether it was ever restored.

Roslyn reports no compiler diagnostics for an unrestored project, so check said
"no errors" about a file with three plain compile errors. These pin the
detection that now makes check say it cannot tell. The end-to-end behaviour is
in tests/integration/test_check.py.

Fast, no language server, no subprocess.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lodesman.server import unrestored_project


class TestUnrestoredProject(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="lodesman-restore-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def touch(self, relative: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    def test_unrestored_project_is_named(self):
        self.touch("App.csproj")
        self.touch("Program.cs")
        self.assertEqual(unrestored_project(self.root, "Program.cs"), "App.csproj")

    def test_restored_project_is_fine(self):
        self.touch("App.csproj")
        self.touch("obj/project.assets.json")
        self.touch("Program.cs")
        self.assertIsNone(unrestored_project(self.root, "Program.cs"))

    def test_the_nearest_project_decides(self):
        # A restored project nested inside an unrestored one owns its files.
        self.touch("Outer.csproj")
        self.touch("src/Inner/Inner.csproj")
        self.touch("src/Inner/obj/project.assets.json")
        self.touch("src/Inner/Deep/Thing.cs")
        self.assertIsNone(unrestored_project(self.root, "src/Inner/Deep/Thing.cs"))
        self.touch("src/Loose.cs")
        self.assertEqual(unrestored_project(self.root, "src/Loose.cs"), "Outer.csproj")

    def test_no_project_means_no_verdict(self):
        self.touch("scripts/tool.cs")
        self.assertIsNone(unrestored_project(self.root, "scripts/tool.cs"))


if __name__ == "__main__":
    unittest.main()
