# SPDX-License-Identifier: MIT
"""
Language detection: the extension map and what it lets through.

These exist because a hand-written map silently under-covered its own language
servers. It listed .ts, .tsx and .js while tsserver handles twelve extensions,
so a React project written in .jsx contained no "recognized source files" at
all and the server exited instead of starting — on a completely ordinary
codebase.

Fast, no language server, no subprocess.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lodesman.server import DETECTED_LANGUAGES, EXTENSION_LANGUAGES, detect_language


class TestExtensionCoverage(unittest.TestCase):
    def test_map_covers_every_extension_its_servers_handle(self):
        # The guard against the original bug: our map must not be a subset of
        # what the language servers themselves claim to handle.
        for language_id in DETECTED_LANGUAGES:
            for extension in language_id.get_source_fn_matcher().file_extensions:
                with self.subTest(language=language_id.value, extension=extension):
                    self.assertIn(extension, EXTENSION_LANGUAGES)

    def test_contested_extensions_go_to_the_higher_priority_language(self):
        # Several languages claim the same extensions — Vue and Svelte both
        # handle .ts, because they are supersets of TypeScript. SolidLSP's
        # priority field exists to settle exactly that, and the map must honour
        # it rather than whichever language happened to be declared first.
        claimants: dict[str, list] = {}
        for language_id in DETECTED_LANGUAGES:
            for extension in language_id.get_source_fn_matcher().file_extensions:
                claimants.setdefault(extension, []).append(language_id)

        contested = {e: c for e, c in claimants.items() if len(c) > 1}
        self.assertTrue(contested, "expected some contested extensions to check")

        for extension, candidates in contested.items():
            best = max(candidate.get_priority() for candidate in candidates)
            winners = {c.value for c in candidates if c.get_priority() == best}
            with self.subTest(extension=extension):
                self.assertIn(EXTENSION_LANGUAGES[extension], winners)

    def test_supersets_do_not_steal_the_base_language(self):
        # The concrete case this protects: a TypeScript project must not be
        # detected as Vue merely because the Vue server also handles .ts.
        self.assertEqual(EXTENSION_LANGUAGES[".ts"], "typescript")
        self.assertEqual(EXTENSION_LANGUAGES[".js"], "typescript")
        # while their own formats still belong to them
        self.assertEqual(EXTENSION_LANGUAGES[".vue"], "vue")
        self.assertEqual(EXTENSION_LANGUAGES[".svelte"], "svelte")

    def test_the_familiar_extensions_resolve(self):
        expected = {
            ".cs": "csharp", ".ts": "typescript", ".tsx": "typescript",
            ".jsx": "typescript", ".mts": "typescript", ".mjs": "typescript",
            ".py": "python", ".pyi": "python", ".go": "go", ".rs": "rust",
            ".java": "java", ".kt": "kotlin", ".rb": "ruby", ".php": "php",
            ".swift": "swift", ".cpp": "cpp", ".h": "cpp",
        }
        for extension, language in expected.items():
            with self.subTest(extension=extension):
                self.assertEqual(EXTENSION_LANGUAGES.get(extension), language)


class TestDetectionOnRealShapes(unittest.TestCase):
    """Project layouts that exist in the wild, including the one that broke."""

    def detect(self, *filenames: str) -> str:
        with tempfile.TemporaryDirectory(prefix="lodesman-detect-") as tmp:
            root = Path(tmp) / "app"
            (root / "src").mkdir(parents=True)
            for name in filenames:
                (root / "src" / name).write_text("x\n", encoding="utf-8")
            return detect_language(root)

    def test_react_written_in_jsx(self):
        # The regression: this used to raise "no recognized source files".
        self.assertEqual(self.detect("App.jsx", "Button.jsx"), "typescript")

    def test_modern_module_extensions(self):
        self.assertEqual(self.detect("a.mts", "b.mjs"), "typescript")

    def test_react_with_typescript(self):
        self.assertEqual(self.detect("api.ts", "App.tsx"), "typescript")

    def test_python_stubs_only(self):
        self.assertEqual(self.detect("mod.pyi", "other.pyi"), "python")

    def test_majority_wins_across_languages(self):
        self.assertEqual(
            self.detect("a.ts", "b.tsx", "c.jsx", "only.cs"), "typescript"
        )

    def test_no_source_files_still_raises(self):
        with self.assertRaises(RuntimeError):
            self.detect("README.md", "notes.txt")


if __name__ == "__main__":
    unittest.main()
