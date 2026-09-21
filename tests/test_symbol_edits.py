# SPDX-License-Identifier: MIT
"""
The text arithmetic behind replace_symbol_body, insert_before_symbol,
insert_after_symbol and safe_delete_symbol.

Every helper here decides which bytes an edit touches, and each one was
written against a failure seen with a real server: a C# one-line member whose
"body" came back as its whole class, a deleted method leaving its doc comment
behind, a file left ending in a run of blank lines. The per-language behaviour
is in tests/integration/test_languages.py.

Fast, no language server, no subprocess.
"""

from __future__ import annotations

import unittest

from lodesman.server import (
    apply_edits_to_text,
    as_block,
    bare_name,
    deletion_span,
    last_line_of,
    leading_block_start,
    name_path,
    range_text,
)


def span(start_line: int, start_char: int, end_line: int, end_char: int) -> dict:
    return {"start": {"line": start_line, "character": start_char},
            "end": {"line": end_line, "character": end_char}}


class TestNames(unittest.TestCase):
    def test_signature_suffixes_are_dropped(self):
        self.assertEqual(bare_name("Get(string)"), "Get")
        self.assertEqual(bare_name("scaled(int) : int"), "scaled")
        self.assertEqual(bare_name("List<T>"), "List")
        self.assertEqual(bare_name("plain"), "plain")

    def test_every_qualifier_separator_means_the_same(self):
        for name in ("MemoryStore.get", "MemoryStore/get", "MemoryStore::get"):
            with self.subTest(name=name):
                self.assertEqual(name_path(name), ["MemoryStore", "get"])


class TestLeadingBlock(unittest.TestCase):
    def test_doc_comment_and_attribute_belong_to_the_declaration(self):
        lines = ["", "    /// <summary>Doc</summary>", "    [Obsolete]", "    public void M() {}"]
        self.assertEqual(leading_block_start(lines, 3, "csharp"), 1)

    def test_python_decorators_and_comments(self):
        lines = ["x = 1", "", "# explains f", "@cache", "def f():", "    pass"]
        self.assertEqual(leading_block_start(lines, 4, "python"), 2)

    def test_a_blank_line_ends_the_block(self):
        lines = ["// unrelated", "", "void f() {}"]
        self.assertEqual(leading_block_start(lines, 2, "cpp"), 2)

    def test_a_cpp_preprocessor_line_is_not_a_comment(self):
        lines = ["#include <x>", "void f() {}"]
        self.assertEqual(leading_block_start(lines, 1, "cpp"), 1)


class TestRanges(unittest.TestCase):
    def test_a_range_ending_at_column_zero_ends_on_the_line_before(self):
        self.assertEqual(last_line_of(span(2, 4, 5, 0)), 4)
        self.assertEqual(last_line_of(span(2, 4, 5, 1)), 5)
        self.assertEqual(last_line_of(span(2, 0, 2, 0)), 2)

    def test_range_text_is_exact_and_utf16_aware(self):
        # The emoji is two UTF-16 units: a naive index would cut one early.
        text = 'a = "\U0001f389"; int Scaled(int f) => f;\r\nnext\r\n'
        start = len('a = "') + 2 + len('"; ')
        self.assertEqual(range_text(text, span(0, start, 0, start + len("int Scaled(int f) => f;"))),
                         "int Scaled(int f) => f;")

    def test_as_block_uses_the_files_line_endings(self):
        self.assertEqual(as_block("a\nb\n\n", "\r\n"), "a\r\nb")
        self.assertEqual(as_block("\na\r\nb", "\n"), "\na\nb")  # a leading blank line is kept


class TestDeletionSpan(unittest.TestCase):
    def delete(self, text: str, symbol_range: dict, language: str = "csharp") -> str:
        lines = text.splitlines()
        first = leading_block_start(lines, symbol_range["start"]["line"], language)
        return apply_edits_to_text(text, [{"range": deletion_span(lines, symbol_range, first),
                                           "newText": ""}])

    def test_a_method_between_two_others_leaves_one_blank_line(self):
        text = ("class C\n{\n    void A() {}\n\n    /// doc\n    void B()\n    {\n    }\n\n"
                "    void D() {}\n}\n")
        self.assertEqual(self.delete(text, span(5, 4, 7, 5)),
                         "class C\n{\n    void A() {}\n\n    void D() {}\n}\n")

    def test_the_last_declaration_takes_the_blank_lines_above_it(self):
        text = "class A:\n    pass\n\n\nclass B:\n    pass\n"
        self.assertEqual(self.delete(text, span(4, 0, 5, 8), "python"), "class A:\n    pass\n")

    def test_a_declaration_sharing_its_line_is_cut_exactly(self):
        text = "int a = 1; int b = 2;\n"
        self.assertEqual(self.delete(text, span(0, 11, 0, 21)), "int a = 1; \n")

    def test_crlf_survives(self):
        text = "void A() {}\r\n\r\nvoid B() {}\r\n\r\nvoid C() {}\r\n"
        self.assertEqual(self.delete(text, span(2, 0, 2, 11)),
                         "void A() {}\r\n\r\nvoid C() {}\r\n")


if __name__ == "__main__":
    unittest.main()
