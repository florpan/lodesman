# SPDX-License-Identifier: MIT
"""
Regression tests for applying LSP TextEdits to files.

These exist because rename_symbol(apply=true) spent its first release reporting
"Applied to N file(s)" while writing nothing at all: the vendored SolidLSP call
it delegated to mutates an in-memory buffer and discards it. A test asserting
that bytes on disk actually change would have caught that immediately, so that
assertion is the first one here.

Deliberately language-server-free: this is pure text manipulation and should
stay runnable in a second. It asserts against nothing but the standard library,
though importing lodesman.server does pull in SolidLSP's runtime dependencies,
so the package needs installing first.

    pip install -e .
    python -m unittest discover -t . -s tests
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lodesman.server import apply_edits_to_text, utf16_index, write_edits  # noqa: E402


def edit(line: int, start: int, end: int, text: str) -> dict:
    return {
        "range": {"start": {"line": line, "character": start},
                  "end": {"line": line, "character": end}},
        "newText": text,
    }


class TestApplyEditsToText(unittest.TestCase):
    def test_single_edit(self):
        self.assertEqual(
            apply_edits_to_text("foo = 1\n", [edit(0, 0, 3, "bar")]),
            "bar = 1\n",
        )

    def test_multiple_edits_on_one_line_do_not_shift_each_other(self):
        # Applied left-to-right with naive offsets, the second edit would land
        # in the wrong place once the first changed the line's length.
        text = "aa + aa\n"
        result = apply_edits_to_text(text, [edit(0, 0, 2, "bbbb"), edit(0, 5, 7, "c")])
        self.assertEqual(result, "bbbb + c\n")

    def test_edits_across_lines(self):
        text = "one\ntwo\nthree\n"
        result = apply_edits_to_text(text, [edit(0, 0, 3, "1"), edit(2, 0, 5, "3")])
        self.assertEqual(result, "1\ntwo\n3\n")

    def test_crlf_is_preserved(self):
        text = "foo = 1\r\nfoo = 2\r\n"
        result = apply_edits_to_text(text, [edit(0, 0, 3, "bar"), edit(1, 0, 3, "bar")])
        self.assertEqual(result, "bar = 1\r\nbar = 2\r\n")
        self.assertEqual(result.count("\r\n"), 2)

    def test_column_cannot_run_past_end_of_line(self):
        # A range ending past the line's content must not swallow the newline
        # and merge two lines together.
        text = "ab\ncd\n"
        result = apply_edits_to_text(text, [edit(0, 0, 99, "X")])
        self.assertEqual(result, "X\ncd\n")

    def test_no_edits_is_identity(self):
        self.assertEqual(apply_edits_to_text("unchanged\n", []), "unchanged\n")


class TestUtf16Index(unittest.TestCase):
    def test_ascii_offsets_are_unchanged(self):
        self.assertEqual(utf16_index("hello", 3), 3)

    def test_bmp_characters_count_as_one(self):
        self.assertEqual(utf16_index("héllo", 3), 3)

    def test_astral_characters_count_as_two(self):
        # The emoji is one Python character but two UTF-16 code units, so an
        # LSP offset of 3 points at index 2, not 3.
        line = "a🎉b"
        self.assertEqual(utf16_index(line, 3), 2)
        self.assertEqual(line[utf16_index(line, 3):], "b")

    def test_offset_past_end_clamps(self):
        self.assertEqual(utf16_index("ab", 99), 2)

    def test_edit_after_an_emoji_lands_correctly(self):
        text = "x = '🎉'; name = 1\n"
        character = text.index("name")
        # Convert the Python index to the UTF-16 offset an LSP server would send.
        units = sum(2 if ord(c) > 0xFFFF else 1 for c in text[:character])
        result = apply_edits_to_text(text, [edit(0, units, units + 4, "label")])
        self.assertEqual(result, "x = '🎉'; label = 1\n")


class TestWriteEdits(unittest.TestCase):
    def test_bytes_actually_change_on_disk(self):
        # The regression that started all this.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "sample.py"
            target.write_text("foo = 1\n", encoding="utf-8")
            before = target.read_bytes()

            changed = write_edits(root, "sample.py", [edit(0, 0, 3, "bar")])

            self.assertTrue(changed)
            self.assertNotEqual(target.read_bytes(), before)
            self.assertEqual(target.read_text(encoding="utf-8"), "bar = 1\n")

    def test_crlf_file_stays_crlf_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "sample.py"
            with open(target, "w", encoding="utf-8", newline="") as handle:
                handle.write("foo = 1\r\nfoo = 2\r\n")

            write_edits(root, "sample.py", [edit(0, 0, 3, "bar")])

            raw = target.read_bytes()
            self.assertEqual(raw, b"bar = 1\r\nfoo = 2\r\n")
            # Every newline is part of a CRLF pair — no lone LF was introduced.
            self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))

    def test_no_op_edit_reports_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "sample.py"
            target.write_text("foo = 1\n", encoding="utf-8")

            changed = write_edits(root, "sample.py", [edit(0, 0, 3, "foo")])

            self.assertFalse(changed)
            self.assertEqual(target.read_text(encoding="utf-8"), "foo = 1\n")


if __name__ == "__main__":
    unittest.main()
