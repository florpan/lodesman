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
