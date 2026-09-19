# SPDX-License-Identifier: MIT
"""
A minimal MCP stdio client, for driving a real server against a real repository.

Originally contributed by the Linux test pass that found the 0.3.0 rename bug.
Adapted to run the working tree by default rather than a published release:
a repository's own test suite should test the code in front of it.

Point it at a published build instead by setting LODESMAN_PKG, which is how you
verify that what actually reached PyPI works:

    LODESMAN_PKG=lodesman-mcp@0.3.1 UVX_FLAGS=--refresh python -m unittest ...

UVX_FLAGS exists because uv's own flags must precede the package name —
everything after it is forwarded to lodesman-mcp, where --refresh reaches
argparse and is rejected.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"

# The server logs this once the language server is up. Absence of it is how we
# tell "no language server on this machine" from "the tool is broken": a
# missing tsserver starts the process happily and only fails at the first tool
# call that needs it.
READY_MARKER = "language server ready"


class ServerError(RuntimeError):
    pass


class Server:
    """One lodesman process, speaking MCP over stdio."""

    def __init__(self, repo: Path | str, *, language: str | None = None,
                 extra_args: tuple[str, ...] = (), env: dict | None = None) -> None:
        args = [str(repo), *extra_args]
        if language:
            args += ["--language", language]

        package = os.environ.get("LODESMAN_PKG")
        if package:
            uv_flags = shlex.split(os.environ.get("UVX_FLAGS", ""))
            command = ["uvx", *uv_flags, package, *args]
            child_env = dict(os.environ)
        else:
            command = [sys.executable, "-m", "lodesman", *args]
            child_env = dict(os.environ, PYTHONPATH=str(SRC))
        child_env.update(env or {})

        self.command = command
        self._id = 0
        self.stderr: list[str] = []
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", bufsize=1, env=child_env,
        )
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self) -> None:
        for line in self.process.stderr:  # type: ignore[union-attr]
            self.stderr.append(line.rstrip())

    # -- protocol ---------------------------------------------------------

    def request(self, method: str, params: dict | None = None, timeout: float = 300) -> dict:
        self._id += 1
        message: dict = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            message["params"] = params
        self.process.stdin.write(json.dumps(message) + "\n")  # type: ignore[union-attr]
        self.process.stdin.flush()  # type: ignore[union-attr]

        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.process.stdout.readline()  # type: ignore[union-attr]
            if not line:
                raise ServerError(
                    "server closed stdout. stderr:\n" + "\n".join(self.stderr[-25:])
                )
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue  # stray output is not fatal; protocol lines are JSON
            if response.get("id") == self._id:
                return response
        raise TimeoutError(f"{method} timed out after {timeout}s")

    def initialize(self) -> dict:
        return self.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "lodesman-tests", "version": "0"},
        })["result"]

    def tool_names(self) -> list[str]:
        return [t["name"] for t in self.request("tools/list", {})["result"]["tools"]]

    def call(self, tool: str, arguments: dict, timeout: float = 300) -> tuple[str, bool]:
        """Returns (text, is_error). Tool errors are values here, not exceptions."""
        response = self.request(
            "tools/call", {"name": tool, "arguments": arguments}, timeout=timeout
        )
        if "error" in response:
            return json.dumps(response["error"]), True
        result = response.get("result", {})
        text = "\n".join(part.get("text", "") for part in result.get("content", []))
        return text, bool(result.get("isError"))

    # -- lifecycle --------------------------------------------------------

    def language_server_ready(self, timeout: float = 600) -> bool:
        """
        Whether the language server came up, by provoking it and watching stderr.

        Deliberately not an assertion: a machine without tsserver or Roslyn
        should skip the tests that need one, not fail them.
        """
        try:
            self.call("find_symbol", {"name": "Store"}, timeout=timeout)
        except (ServerError, TimeoutError):
            return False
        return any(READY_MARKER in line for line in self.stderr)

    def wait_for_exit(self, timeout: float = 30) -> int:
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            raise

    def close(self) -> None:
        # Every pipe is closed explicitly: left to the garbage collector they
        # surface as ResourceWarnings from a background thread, which is noise
        # in front of a real failure.
        for closer in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                if closer is not None:
                    closer.close()
            except Exception:  # noqa: BLE001 — teardown must not mask a failure
                pass
        try:
            self.process.terminate()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()

    def __enter__(self) -> "Server":
        self.initialize()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
