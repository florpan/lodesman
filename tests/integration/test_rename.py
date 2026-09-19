# SPDX-License-Identifier: MIT
"""
Integration tests for rename_symbol, against a real language server.

These are the assertions that would have caught the 0.3.0 bug. Every one of
them checks bytes on disk, because the no-op passed every output-based check
there was: the tool cheerfully reported "Applied to 2 file(s)" while the files
were untouched.

Requires a working language server, which on a cold machine means a
multi-minute download, so they are opt-in:

    LODESMAN_INTEGRATION=1 python -m unittest discover -t . -s tests
"""

from __future__ import annotations

import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from tests.integration import fixtures
from tests.integration.harness import Server

CRLF = b"\r\n"
LF = b"\n"

ENABLED = os.environ.get("LODESMAN_INTEGRATION") == "1"
SKIP_REASON = "set LODESMAN_INTEGRATION=1 to run (may download a language server)"


def lone_line_feeds(raw: bytes) -> int:
    """Line feeds that are not part of a CRLF pair."""
    return raw.count(LF) - raw.count(CRLF)


@unittest.skipUnless(ENABLED, SKIP_REASON)
class RenameCase(unittest.TestCase):
    language = "python"
    fixture = "py_lf"

    def setUp(self) -> None:
        # addCleanup rather than tearDown: tearDown does not run when setUp
        # raises SkipTest, so the skip path below would otherwise leak both the
        # temporary directory and the server process. Registering each cleanup
        # as soon as the resource exists covers every exit from setUp.
        self._tmp = tempfile.TemporaryDirectory(prefix="lodesman-rename-")
        self.addCleanup(self._tmp.cleanup)

        root = Path(self._tmp.name)
        builders = {
            "py_lf": lambda: fixtures.python_repo(root / "r"),
            "py_crlf": lambda: fixtures.python_repo(root / "r", newline="\r\n"),
            "py_nonbmp": lambda: fixtures.python_repo(root / "r", non_bmp=True),
            "ts": lambda: fixtures.typescript_repo(root / "r"),
        }
        self.repo = builders[self.fixture]()

        self.server = Server(self.repo, language=self.language)
        self.addCleanup(self.server.close)
        self.server.initialize()
        if not self.server.language_server_ready():
            self.skipTest(f"no working {self.language} language server on this machine")

    def snapshot(self) -> dict[Path, bytes]:
        return {p: p.read_bytes() for p in self.repo.rglob("*")
                if p.is_file() and p.suffix in {".py", ".ts"}}


class TestRenameWritesToDisk(RenameCase):
    def test_apply_true_changes_bytes(self):
        before = self.snapshot()
        text, is_error = self.server.call(
            "rename_symbol",
            {"name": "NullStore", "new_name": "VoidStore", "apply": True},
        )
        self.assertFalse(is_error, text)
        self.assertIn("Written:", text)

        after = self.snapshot()
        changed = [p for p in before if after[p] != before[p]]
        self.assertTrue(changed, "rename reported success but no file changed on disk")

        joined = b"".join(after.values())
        self.assertNotIn(b"NullStore", joined)
        self.assertIn(b"VoidStore", joined)

    def test_dry_run_writes_nothing(self):
        before = self.snapshot()
        text, is_error = self.server.call(
            "rename_symbol", {"name": "NullStore", "new_name": "VoidStore"}
        )
        self.assertFalse(is_error, text)
        self.assertIn("Dry run", text)
        self.assertEqual(self.snapshot(), before)


class TestRenamePreservesCrlf(RenameCase):
    fixture = "py_crlf"

    def test_crlf_survives_and_no_lone_lf_appears(self):
        before = self.snapshot()
        self.assertTrue(any(raw.count(CRLF) for raw in before.values()),
                        "fixture should have been written with CRLF")

        self.server.call("rename_symbol",
                         {"name": "NullStore", "new_name": "VoidStore", "apply": True})

        for path, raw in self.snapshot().items():
            with self.subTest(file=path.name):
                # Both edited and untouched files: a rename must not reformat
                # either, and must not convert the repository to LF.
                self.assertEqual(raw.count(CRLF), before[path].count(CRLF))
                self.assertEqual(lone_line_feeds(raw), 0)


class TestRenameHandlesNonBmp(RenameCase):
    fixture = "py_nonbmp"

    def test_edit_after_two_astral_characters_lands_correctly(self):
        # Two adjacent non-BMP characters precede the rename target on the same
        # line, so an implementation indexing a Python str with an LSP column
        # drifts by four UTF-16 code units.
        self.server.call("rename_symbol",
                         {"name": "NullStore", "new_name": "VoidStore", "apply": True})

        init = (self.repo / "src" / "store" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn('"\U0001f389\U0001f680"', init, "the emoji were corrupted")
        self.assertIn(
            '__all__ = ["\U0001f389\U0001f680", "Record", "Store", "MemoryStore", "VoidStore"]',
            init,
        )


class TestRenameTypeScript(RenameCase):
    language = "typescript"
    fixture = "ts"

    def test_rename_writes_and_implementations_resolve(self):
        before = self.snapshot()
        text, is_error = self.server.call(
            "rename_symbol",
            {"name": "NullStore", "new_name": "VoidStore", "apply": True},
        )
        self.assertFalse(is_error, text)
        self.assertNotEqual(self.snapshot(), before)

        # TypeScript does serve textDocument/implementation, unlike pyright.
        # The tool answers with locations, not names: MemoryStore and the
        # freshly renamed VoidStore, both in store.ts.
        found, is_error = self.server.call("find_implementations", {"name": "Store"})
        self.assertFalse(is_error, found)
        self.assertIn("2 implementation(s)", found)
        self.assertEqual(found.count("src/store.ts:"), 3)  # 1 resolution + 2 hits


class TestSetUpFailureCleansUp(unittest.TestCase):
    """
    A fault inside setUp must still release what setUp had already acquired.

    The skip path never exercised this: it closed the server explicitly before
    skipping, so only the temporary directory leaked there. The exposure is on
    the paths no explicit close covers — an exception out of Server(),
    initialize(), or language_server_ready() — where tearDown would not run and
    the process would be left to a finalizer. Registering cleanups at creation
    covers all of them, and this is the test for that.

    Needs no language server: the injected failure happens before one is asked
    for, which is why this runs by default rather than behind
    LODESMAN_INTEGRATION.
    """

    def test_exception_from_initialize_closes_server_and_removes_fixture(self):
        class Faulty(TestRenameWritesToDisk):
            pass

        Faulty.__unittest_skip__ = False  # bypass the opt-in gate, not the setUp
        case = Faulty("test_apply_true_changes_bytes")

        closed: list[Server] = []
        real_close = Server.close

        def recording_close(server: Server) -> None:
            closed.append(server)
            real_close(server)

        def boom(server: Server) -> dict:
            raise RuntimeError("injected failure")

        with unittest.mock.patch.object(Server, "initialize", boom), \
                unittest.mock.patch.object(Server, "close", recording_close):
            result = unittest.TestResult()
            case.run(result)

        self.assertEqual(len(result.errors), 1, result.errors)
        self.assertIn("injected failure", result.errors[0][1])

        # Assert close() was called, not that the process died. The process
        # exits either way once its stdin pipe is finalised and it reads EOF,
        # so process death does not distinguish a registered cleanup from a
        # missing one — it would make this test pass against the bug it exists
        # to catch.
        self.assertEqual(len(closed), 1,
                         "setUp failed without closing the server it had started")
        self.assertFalse(Path(case._tmp.name).exists(),
                         "the fixture directory was left on disk")


if __name__ == "__main__":
    unittest.main()
