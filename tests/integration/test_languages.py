# SPDX-License-Identifier: MIT
"""
The same contract, asserted against every language lodesman claims to detect.

One TestCase class is generated per language, so a failure names the language
in its test id rather than hiding inside a parameterised loop, and one language
being unavailable never masks another.

Skipping is two-stage and deliberate:

  1. `requires` — a cheap PATH check. Missing toolchain, skip immediately.
  2. a live probe — the toolchain being present does not mean the server works,
     so the server is asked a real question before any test trusts it.

A skip is not a pass. test_coverage below fails if a language is added to
EXTENSION_LANGUAGES without a fixture here, so the suite cannot silently fall
behind the detector.

    LODESMAN_INTEGRATION=1 python -m unittest discover -t . -s tests
"""

from __future__ import annotations

import os
import re
import tempfile
import time
import unittest
from pathlib import Path

from tests.integration import languages
from tests.integration.harness import Server

ENABLED = os.environ.get("LODESMAN_INTEGRATION") == "1"
SKIP_REASON = "set LODESMAN_INTEGRATION=1 to run (needs language servers)"

CRLF = b"\r\n"
LF = b"\n"

# Every symbol the contract asserts on. Readiness waits for all of them,
# because a workspace index does not necessarily populate atomically:
# sourcekit-lsp resolved Store while Record was still missing, so probing one
# symbol declared the server ready and the next test asked about the other and
# got nothing back.
CONTRACT_SYMBOLS = ("Record", "Store", "NullStore")


class LanguageContract:
    """
    The behaviour every supported language is expected to provide.

    Deliberately not a TestCase: unittest would discover this base directly and
    run it with no `spec` bound, producing a phantom failure that belongs to no
    language. The generated subclasses mix it with TestCase instead.
    """

    spec: languages.LanguageSpec

    @classmethod
    def setUpClass(cls) -> None:
        """
        One server per language, not one per test.

        A cold jdtls or rust-analyzer costs tens of seconds, and the read-only
        half of the contract cannot disturb each other, so they share a single
        warm server. Only the rename test — which mutates its fixture — gets
        its own, built in the test itself.
        """
        if not ENABLED:
            raise unittest.SkipTest(SKIP_REASON)
        missing = cls.spec.missing_tools()
        if missing:
            raise unittest.SkipTest(
                f"{cls.spec.language}: not installed: {', '.join(missing)}"
            )

        tmp = tempfile.TemporaryDirectory(prefix=f"lodesman-{cls.spec.language}-")
        cls.addClassCleanup(tmp.cleanup)
        cls.repo = cls.spec.build(Path(tmp.name).resolve() / "repo")
        failure = cls.spec.prepare(cls.repo)
        if failure:
            raise unittest.SkipTest(f"{cls.spec.language}: prebuild failed: {failure}")

        cls.server = Server(cls.repo, language=cls.spec.language)
        cls.addClassCleanup(cls.server.close)
        cls.server.initialize()
        if not cls.server.language_server_ready(symbols=CONTRACT_SYMBOLS):
            raise unittest.SkipTest(
                f"{cls.spec.language}: language server did not start. "
                + " | ".join(cls.server.stderr[-4:])
            )

    def fresh_server(self) -> tuple[Path, Server]:
        """A private repository and server, for a test that writes."""
        tmp = tempfile.TemporaryDirectory(prefix=f"lodesman-{self.spec.language}-rw-")
        self.addCleanup(tmp.cleanup)
        repo = self.spec.build(Path(tmp.name).resolve() / "repo")
        failure = self.spec.prepare(repo)
        if failure:
            self.skipTest(f"{self.spec.language}: prebuild failed: {failure}")
        server = Server(repo, language=self.spec.language)
        self.addCleanup(server.close)
        server.initialize()
        if not server.language_server_ready(symbols=CONTRACT_SYMBOLS):
            self.skipTest(f"{self.spec.language}: language server did not start")
        return repo, server

    def setUp(self) -> None:
        # A test recorded as a known failure is skipped with its reason, so that
        # a language which mostly works is not shown as a red build while a new
        # failure in the same language still is one.
        reason = self.spec.known_failures.get(self._testMethodName)
        if reason:
            self.skipTest(f"{self.spec.language}: known failure — {reason}")

    def snapshot(self, repo: Path) -> dict[str, bytes]:
        """
        The bytes of the fixture's own files, and nothing else.

        Deliberately not repo.rglob("*"): a language server may write inside
        the repository it is given — sourcekit-lsp builds an index there — and
        those artefacts appear in an "after" snapshot but not a "before" one.
        That produced a KeyError and a line-ending count mismatch on a fixture
        with no CRLF in it at all, which is a test measuring the wrong files
        rather than a rename misbehaving.
        """
        return {
            relative: (repo / relative).read_bytes()
            for relative in self.spec.files
            if (repo / relative).is_file()
        }

    def call(self, tool: str, **arguments) -> str:
        text, is_error = self.server.call(tool, arguments)
        self.assertFalse(is_error, f"{tool} failed: {text}")
        return text

    # -- the contract -----------------------------------------------------

    def test_find_symbol_resolves_a_type(self):
        self.assertIn("Record", self.call("find_symbol", name="Record"))

    def test_document_symbols_outlines_a_file(self):
        outline = self.call("document_symbols", file=self.spec.outline_file)
        for symbol in self.spec.outline_symbols:
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, outline)

    def test_find_definition_locates_the_declaration(self):
        self.assertIn("Record", self.call("find_definition", name="Record"))

    def test_find_references_crosses_files(self):
        # The fixture uses Record in both the store file and the service file,
        # so a single-file answer means the project did not fully load.
        text = self.call("find_references", name="Record")
        if not self.spec.supports_references:
            # Where the server cannot answer, the requirement inverts: lodesman
            # must not present an empty result as proof of no usage.
            self.assertIn("inconclusive", text.lower())
            return
        self.assertNotIn("no references", text.lower())

    def test_get_symbol_body_returns_source(self):
        self.assertIn("Record", self.call("get_symbol_body", name="Record"))

    def test_explain_symbol_describes_it(self):
        self.assertTrue(self.call("explain_symbol", name="Record").strip())

    def test_find_implementations_answers_or_says_it_cannot(self):
        # Two implementations of Store exist in every fixture. A server that
        # does not serve textDocument/implementation must say so — what is not
        # acceptable is a confident empty answer.
        text, is_error = self.server.call("find_implementations", {"name": "Store"})
        if is_error:
            self.assertIn("does not support", text)
        else:
            self.assertIn("implementation", text.lower())

    def locate(self, pattern: str) -> tuple[str, int, str]:
        """(file, 1-based line, matched text) of the first match across the fixture."""
        for relative, content in self.spec.files.items():
            match = re.search(pattern, content)
            if match:
                return relative, content.count("\n", 0, match.start(1)) + 1, match.group(1)
        raise LookupError(f"{self.spec.language} fixture has nothing matching {pattern}")

    def test_call_hierarchy_finds_the_caller(self):
        # Service's total calls Record's scaled in every fixture. Servers
        # without a call hierarchy must still answer incoming calls, from the
        # reference machinery, and say that they did.
        method = self.locate(r"\b((?i:scaled))\s*\(")[2]
        text = self.call("call_hierarchy", name=method)
        if not self.spec.supports_references and "inconclusive" in text:
            return  # no calls and no references to fall back on: declared, not denied
        self.assertIn("total", text.lower())

    def test_type_hierarchy_finds_both_implementations(self):
        text = self.call("type_hierarchy", name="Store", direction="subtypes")
        if "unavailable" in text:
            return  # declared, which is what a server with neither method owes us
        self.assertIn("MemoryStore", text)
        self.assertIn("NullStore", text)

    def test_type_definition_resolves_a_local(self):
        # Pointed at by file and line, because a local is not a workspace
        # symbol. Most fixtures have `record` in `record.scaled(2)` (`->` in
        # C++, `$record` in PHP); Swift and Kotlin pass closure parameters
        # instead, so their `store` field stands in, whose type is Store.
        try:
            target, expected = self.locate(r"(?<![\w$])(\$?record)\s*(?:\.|\?\.|->)\s*(?i:scaled)"), "Record"
        except LookupError:
            target, expected = self.locate(r"(?<![\w$])(store)\s*(?:\.|\?\.|->)\s*(?i:get)\b"), "Store"
        file, line, identifier = target
        text, is_error = self.server.call(
            "type_definition", {"file": file, "line": line, "symbol": identifier}
        )
        if is_error:
            # A server without typeDefinition must say so, not answer emptily.
            self.assertIn("does not support", text)
            return
        self.assertIn("is of type", text)
        self.assertIn(expected, text.split("is of type", 1)[1])

    def test_rename_writes_to_disk_and_preserves_line_endings(self):
        # Its own repository and server: this one mutates, and the read-only
        # tests share a warm server that must not see the fixture change
        # underneath them.
        repo, server = self.fresh_server()
        before = self.snapshot(repo)

        text, is_error = server.call(
            "rename_symbol",
            {"name": "NullStore", "new_name": "VoidStore", "apply": True},
        )

        if not self.spec.supports_rename:
            # Must refuse audibly and leave the tree alone — the dangerous
            # outcome is reporting a rename that never happened, which is
            # exactly the bug 0.3.0 shipped with.
            self.assertTrue(is_error, f"expected a refusal, got: {text[:200]}")
            self.assertNotIn("Written:", text)
            self.assertEqual(self.snapshot(repo), before,
                             "refused the rename but wrote anyway")
            return

        self.assertFalse(is_error, text)
        self.assertIn("Written:", text)

        after = self.snapshot(repo)
        changed = [p for p in before if after.get(p) != before[p]]
        self.assertTrue(changed, "rename reported success but nothing changed on disk")
        # Some servers move the file with the type — jdtls must, since Java
        # requires the names to match — and the snapshot only knows the
        # fixture's original names.
        moved = [p for p in repo.rglob("*VoidStore*") if p.is_file()]
        for path in moved:
            with self.subTest(moved=path.name):
                self.assertIn(b"VoidStore", path.read_bytes())
        self.assertIn(b"VoidStore",
                      b"".join([*after.values(), *(p.read_bytes() for p in moved)]))

        for path, raw in after.items():
            with self.subTest(file=path):
                self.assertEqual(raw.count(CRLF), before[path].count(CRLF))
                self.assertEqual(raw.count(LF) - raw.count(CRLF),
                                 before[path].count(LF) - before[path].count(CRLF))

    # -- staying in sync with the disk -------------------------------------

    def resolves(self, server: Server, name: str, timeout: float = 30) -> bool:
        """
        Whether find_symbol reports `name` within `timeout` seconds.

        Polls rather than asking once: a server that was told about a change
        reindexes asynchronously, and the property under test is that it
        catches up, not that it does so before the next request arrives. A
        server that was never told stays wrong for the whole window.
        """
        deadline = time.time() + timeout
        while True:
            text, is_error = server.call("find_symbol", {"name": name})
            if not is_error and f"No symbol matching {name!r}" not in text:
                return True
            if time.time() >= deadline:
                return False
            time.sleep(1)

    def test_sees_files_edited_on_disk(self):
        # What an agent does with its own Edit tool: change files the language
        # server has no reason to have open. Every server config advertises
        # didChangeWatchedFiles, which tells the server the client watches the
        # disk on its behalf — so if lodesman does not, the server keeps
        # answering from the version it indexed at startup.
        repo, server = self.fresh_server()
        for relative in self.spec.files:
            path = repo / relative
            raw = path.read_bytes()
            if b"NullStore" in raw:
                path.write_bytes(raw.replace(b"NullStore", b"VoidStore"))

        self.assertTrue(self.resolves(server, "VoidStore"),
                        "a type added on disk never became visible")

    def test_sees_its_own_rename(self):
        # rename_symbol writes the files itself. A second question straight
        # after must be answered from what was written, or a follow-up rename
        # computes its edits against positions that no longer exist.
        if not self.spec.supports_rename:
            self.skipTest(f"{self.spec.language}: server does not rename")
        broken = self.spec.known_failures.get("test_rename_writes_to_disk_and_preserves_line_endings")
        if broken:
            self.skipTest(f"{self.spec.language}: rename is a known failure — {broken}")
        _repo, server = self.fresh_server()
        text, is_error = server.call(
            "rename_symbol",
            {"name": "NullStore", "new_name": "VoidStore", "apply": True},
        )
        self.assertFalse(is_error, text)
        self.assertTrue(self.resolves(server, "VoidStore"),
                        "the renamed symbol never became visible to the server")


def _make_case(spec: languages.LanguageSpec) -> type[unittest.TestCase]:
    # Named for the language id verbatim rather than title-cased, so CI can
    # address one language with Test_${{ matrix.language }} and no mapping.
    return type(
        f"Test_{spec.language}",
        (LanguageContract, unittest.TestCase),
        {"spec": spec, "__doc__": f"Language contract for {spec.language}."},
    )


# One class per language, bound into this module so unittest discovers them.
for _spec in languages.SPECS:
    _case = _make_case(_spec)
    globals()[_case.__name__] = _case
del _spec, _case


# The languages this project claims to have verified, which is a documentation
# claim and therefore deliberately maintained by hand. Detection covers far
# more — trying an untested language beats refusing to start — but the README
# may only advertise what has a fixture behind it. Changing this list is a
# conscious act that shows up in review.
VERIFIED_LANGUAGES = {
    "csharp", "typescript", "python", "go", "rust",
    "java", "kotlin", "ruby", "php", "swift", "cpp",
}


class TestCoverage(unittest.TestCase):
    """Detection may be generous; the claim of verification may not be."""

    def test_every_verified_language_has_a_fixture(self):
        missing = VERIFIED_LANGUAGES - set(languages.BY_LANGUAGE)
        self.assertEqual(
            missing, set(),
            f"claimed as verified but no fixture exists: {sorted(missing)}. Add one "
            "to languages.py, or stop claiming it.",
        )

    def test_no_fixture_is_orphaned(self):
        extra = set(languages.BY_LANGUAGE) - VERIFIED_LANGUAGES
        self.assertEqual(
            extra, set(),
            f"fixtures exist for languages not listed as verified: {sorted(extra)}. "
            "Add them to VERIFIED_LANGUAGES and to the README table.",
        )

    def test_verified_languages_are_all_detectable(self):
        # A language we test but cannot detect would only ever be reachable by
        # naming it explicitly, which is not what the README implies.
        undetectable = VERIFIED_LANGUAGES - languages.detectable_languages()
        self.assertEqual(
            undetectable, set(),
            f"verified but not detectable: {sorted(undetectable)}",
        )


if __name__ == "__main__":
    unittest.main()
