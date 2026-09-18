# SPDX-License-Identifier: MIT
"""Allows `python -m lodesman` alongside the `lodesman-mcp` console script."""

from __future__ import annotations

import sys

from lodesman.server import main

if __name__ == "__main__":
    sys.exit(main())
