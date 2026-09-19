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
import tempfile
import unittest
from pathlib import Path

from tests.integration import languages
from tests.integration.harness import Server

ENABLED = os.environ.get("LODESMAN_INTEGRATION") == "1"
SKIP_REASON = "set LODESMAN_INTEGRATION=1 to run (needs language servers)"

CRLF = b"\r\n"
LF = b"\n"


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
        cls.repo = cls.spec.build(Path(tmp.name) / "repo")

        cls.server = Server(cls.repo, language=cls.spec.language)
        cls.addClassCleanup(cls.server.close)
        cls.server.initialize()
        if not cls.server.language_server_ready():
            raise unittest.SkipTest(
                f"{cls.spec.language}: language server did not start. "
                + " | ".join(cls.server.stderr[-4:])
            )

    def fresh_server(self) -> tuple[Path, Server]:
        """A private repository and server, for a test that writes."""
        tmp = tempfile.TemporaryDirectory(prefix=f"lodesman-{self.spec.language}-rw-")
        self.addCleanup(tmp.cleanup)
        repo = self.spec.build(Path(tmp.name) / "repo")
        server = Server(repo, language=self.spec.language)
        self.addCleanup(server.close)
        server.initialize()
        if not server.language_server_ready():
            self.skipTest(f"{self.spec.language}: language server did not start")
        return repo, server

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

    def test_rename_writes_to_disk_and_preserves_line_endings(self):
        # Its own repository and server: this one mutates, and the read-only
        # tests share a warm server that must not see the fixture change
        # underneath them.
        repo, server = self.fresh_server()
        before = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()}

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
            after = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()}
            self.assertEqual(after, before, "refused the rename but wrote anyway")
            return

        self.assertFalse(is_error, text)
        self.assertIn("Written:", text)

        after = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()}
        changed = [p for p in before if after.get(p) != before[p]]
        self.assertTrue(changed, "rename reported success but nothing changed on disk")
        self.assertIn(b"VoidStore", b"".join(after.values()))

        for path, raw in after.items():
            with self.subTest(file=path.name):
                self.assertEqual(raw.count(CRLF), before[path].count(CRLF))
                self.assertEqual(raw.count(LF) - raw.count(CRLF),
                                 before[path].count(LF) - before[path].count(CRLF))


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


class TestCoverage(unittest.TestCase):
    """The suite must not fall behind what the server claims to detect."""

    def test_every_detectable_language_has_a_fixture(self):
        missing = languages.detectable_languages() - set(languages.BY_LANGUAGE)
        self.assertEqual(
            missing, set(),
            "EXTENSION_LANGUAGES maps these to a language server, but no fixture "
            f"exists for them: {sorted(missing)}. Add one to languages.py, or stop "
            "claiming to detect them.",
        )

    def test_no_fixture_is_orphaned(self):
        extra = set(languages.BY_LANGUAGE) - languages.detectable_languages()
        self.assertEqual(
            extra, set(),
            f"fixtures exist for languages the detector does not recognise: {sorted(extra)}",
        )


if __name__ == "__main__":
    unittest.main()
