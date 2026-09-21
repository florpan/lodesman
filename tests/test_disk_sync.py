# SPDX-License-Identifier: MIT
"""
Noticing what changed on disk between two tool calls.

Every language server here was told the client watches files for it, so what
these functions miss, the server never learns: pyright went on answering from
the version of a file it indexed at startup, including after our own
rename_symbol had rewritten it. The end-to-end property is asserted per
language in tests/integration/test_languages.py; these pin the diffing itself.

Fast, no language server, no subprocess.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from lodesman.server import (
    FILE_CHANGED,
    FILE_CREATED,
    FILE_DELETED,
    disk_changes,
    scan_sources,
)


class TestDiskChanges(unittest.TestCase):
    def test_reports_each_kind_of_change(self):
        before = {"a.py": (1, 10), "b.py": (1, 10), "gone.py": (1, 10)}
        after = {"a.py": (1, 10), "b.py": (2, 10), "new.py": (1, 5)}
        self.assertEqual(disk_changes(before, after), [
            ("b.py", FILE_CHANGED),
            ("gone.py", FILE_DELETED),
            ("new.py", FILE_CREATED),
        ])

    def test_same_mtime_different_size_is_a_change(self):
        # A write inside the filesystem's timestamp resolution keeps the mtime.
        self.assertEqual(disk_changes({"a.py": (1, 10)}, {"a.py": (1, 11)}),
                         [("a.py", FILE_CHANGED)])

    def test_nothing_changed(self):
        state = {"a.py": (1, 10)}
        self.assertEqual(disk_changes(state, dict(state)), [])


class TestScanSources(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="lodesman-scan-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def write(self, relative: str, text: str = "x = 1\n") -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_only_this_language_and_only_walkable_directories(self):
        kept = self.write("src/app.py")
        self.write("src/app.ts", "export {};\n")
        self.write("node_modules/dep/index.py")
        self.write(".venv/lib/site.py")
        self.assertEqual(set(scan_sources(self.root, "python")), {str(kept)})

    def test_csharp_scan_includes_project_files_and_restore_output(self):
        # Roslyn reloads its project model only when told these changed, and
        # the restore output lives in obj/, which the walk otherwise skips.
        project = self.write("App.csproj", "<Project />\n")
        source = self.write("Program.cs", "class P {}\n")
        before = scan_sources(self.root, "csharp")
        self.assertEqual(set(before), {str(project), str(source)})

        assets = self.write("obj/project.assets.json", "{}\n")
        self.assertEqual(disk_changes(before, scan_sources(self.root, "csharp")),
                         [(str(assets), FILE_CREATED)])

    def test_an_edit_is_seen_by_the_next_scan(self):
        path = self.write("src/app.py")
        before = scan_sources(self.root, "python")
        path.write_text("x = 2\ny = 3\n", encoding="utf-8")
        self.assertEqual(disk_changes(before, scan_sources(self.root, "python")),
                         [(str(path), FILE_CHANGED)])

    def test_an_edit_that_keeps_mtime_and_size_is_not_seen(self):
        # Declared rather than hidden: the one blind spot. A same-length write
        # that restores the old mtime looks unchanged; content hashing every
        # file on every call would close it at a cost no other case needs.
        path = self.write("src/app.py", "x = 1\n")
        stat = path.stat()
        before = scan_sources(self.root, "python")
        path.write_text("x = 2\n", encoding="utf-8")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(disk_changes(before, scan_sources(self.root, "python")), [])


if __name__ == "__main__":
    unittest.main()
