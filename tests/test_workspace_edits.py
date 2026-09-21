# SPDX-License-Identifier: MIT
"""
Applying a language server's WorkspaceEdit: text edits, and the file renames
that come with some refactorings.

jdtls renames a Java class by renaming its file as well, because Java requires
the two to match. Before this, rename_symbol applied the text edits and silently
dropped the file rename, leaving `class VoidStore` in NullStore.java — a file
that does not compile — while reporting success. The language contract only
checked that bytes changed, so it passed.

Fast, no language server, no subprocess.
"""

from __future__ import annotations

import tempfile
import unittest
import unittest.mock
from pathlib import Path

from lodesman import server
from lodesman.server import ToolError, apply_steps, edit_steps, preview_steps


def insert(line: int, text: str) -> dict:
    point = {"line": line, "character": 0}
    return {"range": {"start": point, "end": point}, "newText": text}


class WorkspaceEditCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="lodesman-wsedit-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        # to_relative resolves against the bound repository.
        patcher = unittest.mock.patch.object(server, "_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, relative: str, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")

    def uri(self, relative: str) -> str:
        return (self.root / relative).as_uri()

    def read(self, relative: str) -> str:
        return (self.root / relative).read_text(encoding="utf-8")


class TestEditSteps(WorkspaceEditCase):
    def test_changes_shape(self):
        steps = edit_steps({"changes": {self.uri("a.py"): [insert(0, "x")]}})
        self.assertEqual(steps, [("edit", "a.py", [insert(0, "x")])])

    def test_document_changes_win_over_changes_and_are_not_doubled(self):
        edits = [insert(0, "import os\n")]
        steps = edit_steps({
            "changes": {self.uri("a.py"): edits},
            "documentChanges": [{"textDocument": {"uri": self.uri("a.py")}, "edits": edits}],
        })
        self.assertEqual(steps, [("edit", "a.py", edits)])

    def test_rename_is_kept_in_order(self):
        steps = edit_steps({"documentChanges": [
            {"textDocument": {"uri": self.uri("Old.java")}, "edits": [insert(0, "x")]},
            {"kind": "rename", "oldUri": self.uri("Old.java"), "newUri": self.uri("New.java")},
        ]})
        self.assertEqual([s[0] for s in steps], ["edit", "rename"])
        self.assertEqual(steps[1], ("rename", "Old.java", "New.java"))

    def test_create_and_delete_are_refused(self):
        for kind in ("create", "delete"):
            with self.subTest(kind=kind), self.assertRaises(ToolError):
                edit_steps({"documentChanges": [{"kind": kind, "uri": self.uri("x.py")}]})

    def test_a_path_outside_the_repository_is_refused(self):
        outside = (self.root.parent / "elsewhere.py").as_uri()
        with self.assertRaises(ToolError):
            edit_steps({"changes": {outside: [insert(0, "x")]}})


class TestApplyAndPreview(WorkspaceEditCase):
    def java_rename(self) -> list[tuple]:
        # The shape jdtls sends: edit the file under its old name, then move it.
        self.write("NullStore.java", "public class NullStore {}\n")
        return edit_steps({"documentChanges": [
            {"textDocument": {"uri": self.uri("NullStore.java")},
             "edits": [{"range": {"start": {"line": 0, "character": 13},
                                  "end": {"line": 0, "character": 22}},
                        "newText": "VoidStore"}]},
            {"kind": "rename", "oldUri": self.uri("NullStore.java"),
             "newUri": self.uri("VoidStore.java")},
        ]})

    def test_edit_then_rename_lands_in_the_new_file(self):
        changed, _unchanged, failed = apply_steps(self.root, self.java_rename())
        self.assertEqual(failed, [])
        self.assertEqual(changed, 2)
        self.assertFalse((self.root / "NullStore.java").exists())
        self.assertEqual(self.read("VoidStore.java"), "public class VoidStore {}\n")

    def test_preview_shows_the_move_and_writes_nothing(self):
        steps = self.java_rename()
        diff = preview_steps(self.root, steps)
        self.assertIn("--- a/NullStore.java", diff)
        self.assertIn("+++ b/VoidStore.java", diff)
        self.assertIn("+public class VoidStore {}", diff)
        self.assertEqual(self.read("NullStore.java"), "public class NullStore {}\n")
        self.assertFalse((self.root / "VoidStore.java").exists())

    def test_rename_onto_an_existing_file_writes_nothing(self):
        steps = self.java_rename()
        self.write("VoidStore.java", "keep me\n")
        with self.assertRaises(ToolError):
            apply_steps(self.root, steps)
        # Checked up front: the text edit that precedes the rename must not
        # have happened either.
        self.assertEqual(self.read("NullStore.java"), "public class NullStore {}\n")
        self.assertEqual(self.read("VoidStore.java"), "keep me\n")


if __name__ == "__main__":
    unittest.main()
