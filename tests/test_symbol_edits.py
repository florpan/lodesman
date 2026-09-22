# SPDX-License-Identifier: MIT
"""
The text arithmetic behind replace_symbol_body, insert_at_symbol and
safe_delete_symbol.

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
    containers,
    deletion_span,
    go_receiver,
    last_line_of,
    leading_block_start,
    name_path,
    range_text,
    split_symbol_name,
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

    def test_server_spellings_split_into_container_and_name(self):
        cases = {
            # gopls outline: receiver-qualified, top level (symbols.go)
            "(*MemoryStore).Get": (["MemoryStore"], "Get", False),
            "(NullStore).Get": (["NullStore"], "Get", False),
            "(*Cache[K]).Put": (["Cache"], "Put", False),
            # gopls workspace search: qualified by type (and package)
            "store.MemoryStore.Get": (["store", "MemoryStore"], "Get", False),
            # rust-analyzer impl blocks stand for their type as containers
            "impl Record": ([], "Record", True),
            "impl Store for MemoryStore": ([], "MemoryStore", True),
            "impl<T> Store for Cache<T>": ([], "Cache", True),
            # signatures appended by Roslyn and jdtls
            "Get(string)": ([], "Get", False),
            "scaled(int) : int": ([], "scaled", False),
        }
        for raw, expected in cases.items():
            with self.subTest(name=raw):
                self.assertEqual(split_symbol_name(raw), expected)

    def test_go_receivers_are_read_from_the_declaration_line(self):
        # SolidLSP's gopls wrapper strips "(Record).Scaled" to "Scaled", so the
        # receiver has to come from the source.
        self.assertEqual(go_receiver("func (r Record) Scaled(factor int) int {"), "Record")
        self.assertEqual(go_receiver("func (m *MemoryStore) Get(key string) *Record {"), "MemoryStore")
        self.assertEqual(go_receiver("func (NullStore) Put(record Record) {}"), "NullStore")
        self.assertEqual(go_receiver("func (c *Cache[K]) Put(k K) {"), "Cache")
        self.assertIsNone(go_receiver("func NewMemoryStore() *MemoryStore {"))

    def test_an_impl_block_contributes_its_type_to_the_chain(self):
        impl = {"name": "impl Record", "parent": {"name": "fixture", "parent": None}}
        self.assertEqual(containers({"name": "scaled", "parent": impl}), ["fixture", "Record"])

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


class TestIndexFallback(unittest.TestCase):
    """
    Finding a declaration when the workspace index does not know it.

    Seen in CI: sourcekit-lsp found NullStore, then moments later its workspace
    search returned nothing for the same name. The fallback reads the outline
    of each file that mentions the name, which comes from the file itself.
    """

    def fake(self, root):
        from types import SimpleNamespace

        outline = [{"name": "NullStore", "kind": 5, "parent": None,
                    "range": span(0, 0, 1, 8), "selectionRange": span(0, 6, 0, 15)}]
        asked: list[str] = []

        def document_symbols(path: str):
            asked.append(path)
            return outline if path == "store.py" else []

        session = SimpleNamespace(
            root=root, language="python", sync_with_disk=lambda: 0,
            server=SimpleNamespace(request_document_symbols=document_symbols),
        )
        return session, asked

    def test_a_declaration_is_found_when_workspace_search_is_empty(self):
        import tempfile
        import unittest.mock
        from pathlib import Path

        from lodesman import server

        with tempfile.TemporaryDirectory(prefix="lodesman-fallback-") as tmp:
            root = Path(tmp).resolve()
            (root / "store.py").write_text("class NullStore:\n    pass\n", encoding="utf-8")
            (root / "other.py").write_text("x = 1\n", encoding="utf-8")
            session, asked = self.fake(root)
            with unittest.mock.patch.object(server, "workspace_hits", return_value=[]):
                files = server.files_declaring(session, "NullStore")
                matches = server.match_declarations(server.file_declarations(session, files[0]),
                                                    ["NullStore"])

        self.assertEqual(files, ["store.py"], "only files mentioning the name are outlined")
        self.assertEqual([m.chain for m in matches], [["NullStore"]])
        self.assertEqual(asked, ["store.py"])

    def test_every_symbol_tool_gets_the_fallback(self):
        # Every tool taking a symbol resolves it through resolve(); the rename
        # test failed in CI exactly as safe_delete had, when each tool had its
        # own lookup.
        import tempfile
        import unittest.mock
        from pathlib import Path
        from types import SimpleNamespace

        from lodesman import server

        with tempfile.TemporaryDirectory(prefix="lodesman-fallback-") as tmp:
            root = Path(tmp).resolve()
            (root / "store.py").write_text("class NullStore:\n    pass\n", encoding="utf-8")
            session, _asked = self.fake(root)
            pool = SimpleNamespace(root=root, languages=["python"], ordered=lambda: [session],
                                   session=lambda language: session)
            with unittest.mock.patch.object(server, "workspace_hits", return_value=[]):
                target = server.resolve(pool, "NullStore")

        self.assertEqual((target.file, target.address), ("store.py", "store.py:NullStore"))
        # The position tools act on is the name, not the start of the class.
        self.assertEqual((target.line, target.column), (0, 6))


if __name__ == "__main__":
    unittest.main()
