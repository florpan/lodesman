# SPDX-License-Identifier: MIT
"""
Tests for Lodesman.

    pip install -e .        # or: uv pip install -e .
    python -m unittest discover -t . -s tests

Install first. Nothing here asserts against a third-party library, but
importing lodesman.server pulls in the vendored SolidLSP and therefore its
runtime dependencies, so on a bare interpreter every module fails at the
loader with ModuleNotFoundError rather than at an assertion.

Two layers. The modules here are unit tests: no language server, no subprocess,
sub-second. tests/integration/ drives a real server process over MCP stdio; the
parts of it that need a language server are opt-in via LODESMAN_INTEGRATION.
"""

from __future__ import annotations

import sys
from pathlib import Path

# src/ on the path for every test module, so the suite runs from a checkout
# whether or not the package is installed in editable mode. The harness passes
# PYTHONPATH to the server it spawns; this is for the tests' own imports.
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
