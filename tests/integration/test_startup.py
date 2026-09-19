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
    "project_info", "find_symbol", "document_symbols", "find_references",
    "get_symbol_body", "check", "find_definition", "explain_symbol",
    "blast_radius", "rename_symbol", "find_implementations",
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
        self.assertIn("--language", instructions)

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
            line = self.stderr_line(server, "detected language")
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


class TestPathContainment(IntegrationCase):
    """Finding #4: check() would read and echo any file the process could open."""

    def assertRejected(self, server: Server, tool: str, path: str) -> None:
        text, is_error = server.call(tool, {"file": path})
        self.assertTrue(is_error, f"{tool}({path!r}) was not refused: {text[:200]}")
        self.assertIn("outside the repository", text)

    def test_absolute_path_outside_repo_is_refused(self):
        outside = "C:/Windows/win.ini" if sys.platform == "win32" else "/etc/passwd"
        with Server(self.repos["py_lf"], language="python") as server:
            self.assertRejected(server, "check", outside)
            self.assertRejected(server, "document_symbols", outside)

    def test_dotdot_escape_is_refused(self):
        with Server(self.repos["py_lf"], language="python") as server:
            self.assertRejected(server, "check", "../../../../../../etc/hosts")

    def test_refusal_explains_the_one_repository_model(self):
        outside = "C:/Windows/win.ini" if sys.platform == "win32" else "/etc/passwd"
        with Server(self.repos["py_lf"], language="python") as server:
            text, _ = server.call("check", {"file": outside})
            self.assertIn("one repository", text)


if __name__ == "__main__":
    unittest.main()
