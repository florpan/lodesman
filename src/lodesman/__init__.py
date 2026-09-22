# SPDX-License-Identifier: MIT
"""
Lodesman — compiler-grade code intelligence over MCP.

SolidLSP is vendored under `_vendor/` rather than depended on, because it is
not published to PyPI independently of the Serena application. The vendored
tree is kept byte-for-byte pristine so it can be re-synced with upstream, which
means its own imports are absolute (`from solidlsp.ls_config import ...`) and
must keep resolving under that exact name.

Putting `_vendor/` on `sys.path` here satisfies that without editing a single
vendored file, and without claiming the top-level `solidlsp` name inside
site-packages the way a flat vendored package would.
"""

from __future__ import annotations

import sys
from pathlib import Path

_VENDOR = Path(__file__).parent / "_vendor"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

__version__ = "0.6.0"
