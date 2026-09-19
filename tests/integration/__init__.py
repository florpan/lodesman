# SPDX-License-Identifier: MIT
"""
Integration tests: a real lodesman process, driven over MCP stdio.

Split by what they need, so as much as possible runs everywhere:

  test_startup.py  no language server required — startup, language detection,
                   the tool surface, and path containment (which is enforced
                   before any language server is touched). Runs by default.
  test_rename.py   requires a working language server, which on a cold machine
                   means a multi-minute download. Skipped unless
                   LODESMAN_INTEGRATION=1 is set.

The fast unit tests in tests/ stay independent of all of this.
"""
