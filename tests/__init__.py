# SPDX-License-Identifier: MIT
"""
Tests for Lodesman.

    python -m unittest discover -t . -s tests

Two layers. The modules here are unit tests: stdlib only, no language server,
sub-second. tests/integration/ drives a real server process over MCP stdio;
the parts of it that need a language server are opt-in via LODESMAN_INTEGRATION.
"""
