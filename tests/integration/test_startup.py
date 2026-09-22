# SPDX-License-Identifier: MIT
"""
Integration tests that need no language server.

Startup, language detection and path containment are all decided before any
language server is contacted, so these run on any machine with Python — no
Roslyn, no tsserver, no network.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.integration import fixtures
from tests.integration.harness import SRC, Server

EXPECTED_TOOLS = {
    "project_info", "find_symbol", "find_references",
    "get_symbol_body", "get_file_diagnostics",
    "blast_radius", "rename_symbol", "find_implementations",
    "type_definition", "call_hierarchy", "type_hierarchy", "code_action",
    "replace_symbol_body", "insert_at_symbol", "safe_delete_symbol",
}


class IntegrationCase(unittest.TestCase):
    """Builds every fixture once into a temporary directory."""

    @classmethod
    def setUpClass(cls) -> None:
        # addClassCleanup for the same reason setUp uses addCleanup elsewhere:
        # tearDownClass does not run if setUpClass raises, so registering the
        # cleanup with the resource covers every exit.
        cls._tmp = tempfile.TemporaryDirectory(prefix="lodesman-it-")
        cls.addClassCleanup(cls._tmp.cleanup)
        # resolve(): on Windows, TMP can be an 8.3 short path — the GitHub
        # runner's is C:\Users\RUNNER~1\... — and lodesman resolves the root it
        # is given, so an unresolved fixture path never matches what the server
        # reports back. Canonicalise here rather than at each comparison.
        cls.repos = fixtures.build_all(Path(cls._tmp.name).resolve())

    def stderr_line(self, server: Server, needle: str, timeout: float = 30) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for line in server.stderr:
                if needle in line:
                    return line
            time.sleep(0.05)
        self.fail(f"no stderr line containing {needle!r}. Saw:\n"
                  + "\n".join(server.stderr[-20:]))


class TestToolSurface(IntegrationCase):
    def test_initialize_and_tool_list(self):
        with Server(self.repos["py_lf"], language="python") as server:
            self.assertEqual(set(server.tool_names()), EXPECTED_TOOLS)

    def test_initialize_carries_configuration_instructions(self):
        # The only channel that reaches an agent without a human pasting
        # something, so it has to name the actual binding and the way out of a
        # misconfiguration — not generic prose.
        with Server(self.repos["py_lf"], language="python") as server:
            instructions = server.initialize().get("instructions", "")
        self.assertTrue(instructions, "initialize carried no instructions")
        self.assertIn(str(self.repos["py_lf"]), instructions)
        self.assertIn("project_info", instructions)
        # It has to say that several languages are served and that servers start
        # lazily, or an agent will read a slow first call as a hang.
        self.assertIn("every language the repository contains", instructions)

    def test_project_info_reports_the_bound_repository(self):
        with Server(self.repos["py_lf"], language="python") as server:
            text, is_error = server.call("project_info", {})
            self.assertFalse(is_error)
            self.assertIn(str(self.repos["py_lf"]).lower(), text.lower())


class TestLanguageDetection(IntegrationCase):
    def test_dot_directories_do_not_decide_the_language(self):
        # 130 .py files under .tox/.mypy_cache/.direnv, one real src/app.ts.
        # 0.3.0 reported "python (130 files)" here.
        with Server(self.repos["hidden"]) as server:
            line = self.stderr_line(server, "detected:")
            self.assertIn("typescript", line)
            self.assertNotIn("python", line)


class TestNoSourceFiles(unittest.TestCase):
    def test_exits_cleanly_rather_than_raising(self):
        with tempfile.TemporaryDirectory(prefix="lodesman-nosrc-") as tmp:
            repo = fixtures.no_source_repo(Path(tmp) / "nosource")
            result = subprocess.run(
                [sys.executable, "-m", "lodesman", str(repo)],
                capture_output=True, text=True, encoding="utf-8", timeout=120,
                env={**os.environ, "PYTHONPATH": str(SRC)},
            )
        # Assert on stderr before the exit code. Any other crash — a missing
        # runtime dependency, say — also exits non-zero, and "1 != 2" points at
        # the wrong thing; the traceback in stderr names the real cause.
        self.assertNotIn("Traceback", result.stderr, f"crashed instead:\n{result.stderr}")
        self.assertIn("no recognized source files", result.stderr)
        # The message has to say what to do about it, not just what went wrong.
        self.assertIn("--language", result.stderr)
        self.assertEqual(result.returncode, 2)


class TestMultipleLanguages(unittest.TestCase):
    """
    One server, several languages.

    None of this needs a language server: detection happens at startup,
    project_info never touches one, and a file routed to a language nobody
    serves is refused before anything starts. So it runs everywhere.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="lodesman-multi-")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve() / "repo"
        # A .NET solution with a TypeScript frontend, the shape the README
        # documents and the reason the pool exists.
        fixtures.python_repo(root / "service")
        fixtures.typescript_repo(root / "web")
        self.repo = root

    def start(self, *extra: str) -> Server:
        server = Server(self.repo, extra_args=extra)
        self.addCleanup(server.close)
        server.initialize()
        return server

    def test_both_languages_are_detected_and_reported(self):
        server = self.start()
        info, is_error = server.call("project_info", {})
        self.assertFalse(is_error, info)
        self.assertIn("python", info)
        self.assertIn("typescript", info)

    def test_nothing_starts_until_something_asks(self):
        server = self.start()
        info, _ = server.call("project_info", {})
        # The whole point of the pool: detection is cheap, servers are not.
        self.assertIn("none started yet", info)

    def test_a_file_of_an_unserved_language_is_refused_clearly(self):
        server = self.start()
        (self.repo / "notes.md").write_text("# notes\n", encoding="utf-8")
        text, is_error = server.call("find_symbol", {"file": "notes.md"})
        self.assertTrue(is_error, text)
        self.assertIn("not a file this server handles", text)
        # The refusal has to say what it does serve, or it is a dead end.
        self.assertIn("python", text)
        self.assertIn("typescript", text)

    def test_forcing_a_language_serves_only_that_one(self):
        server = self.start("--language", "typescript")
        info, _ = server.call("project_info", {})
        self.assertIn("typescript", info)
        self.assertNotIn("python", info.split("running")[0])


class TestPathContainment(IntegrationCase):
    """Finding #4: check() would read and echo any file the process could open."""

    def assertRejected(self, server: Server, tool: str, path: str) -> None:
        text, is_error = server.call(tool, {"file": path})
        self.assertTrue(is_error, f"{tool}({path!r}) was not refused: {text[:200]}")
        self.assertIn("outside the repository", text)

    def test_absolute_path_outside_repo_is_refused(self):
        outside = "C:/Windows/win.ini" if sys.platform == "win32" else "/etc/passwd"
        with Server(self.repos["py_lf"], language="python") as server:
            self.assertRejected(server, "get_file_diagnostics", outside)
            self.assertRejected(server, "find_symbol", outside)

    def test_dotdot_escape_is_refused(self):
        with Server(self.repos["py_lf"], language="python") as server:
            self.assertRejected(server, "get_file_diagnostics", "../../../../../../etc/hosts")

    def test_refusal_explains_the_one_repository_model(self):
        outside = "C:/Windows/win.ini" if sys.platform == "win32" else "/etc/passwd"
        with Server(self.repos["py_lf"], language="python") as server:
            text, _ = server.call("get_file_diagnostics", {"file": outside})
            self.assertIn("one repository", text)


if __name__ == "__main__":
    unittest.main()
