# SPDX-License-Identifier: MIT
"""
MIT replacements for the three helpers SolidLSP imported from Serena's
GPL-licensed application code.

SolidLSP itself is MIT and explicitly intended to be usable on its own, but four
of its files reach into `serena.util.*`, which is GPL-3.0-or-later. This module
reimplements exactly what those call sites need, so the vendored tree carries no
GPL code:

  - match_path              (solidlsp/ls.py)
  - MatchedConsecutiveLines (solidlsp/ls.py)
  - DotNETUtil              (language_servers/{csharp,fsharp}_language_server.py)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from pathspec import PathSpec


def match_path(relative_path: str, path_spec: PathSpec, root_path: str = "") -> bool:
    """
    Test `relative_path` against a gitignore-style `path_spec`.

    Two adjustments are needed on top of a plain `path_spec.match_file`:

    - A pattern anchored at the repository root (`/src/...`) only matches a path
      that is itself anchored, and pathspec cannot tell whether the path it is
      given is root-relative. Every path here is, so prefix it with "/".
    - A directory pattern only matches when the candidate ends in a slash, so a
      path that exists on disk as a directory gets one appended.
    """
    text = str(relative_path)
    if text in ("", "."):
        return False

    normalized = text.replace(os.path.sep, "/")
    if not normalized.startswith("/"):
        normalized = "/" + normalized

    if not normalized.endswith("/"):
        absolute = os.path.abspath(os.path.join(root_path, text))
        if os.path.isdir(absolute):
            normalized += "/"

    return path_spec.match_file(normalized)


@dataclass
class _TextLine:
    line_number: int
    text: str
    is_match: bool

    def format_line(self, include_line_numbers: bool = True) -> str:
        if not include_line_numbers:
            return self.text
        return f"{self.line_number:>6}: {self.text}"


@dataclass
class MatchedConsecutiveLines:
    """
    A run of consecutive lines around a match, with optional leading/trailing
    context. `ls.retrieve_content_around_line` is the only consumer.
    """

    lines: list[_TextLine]
    source_file_path: str | None = None

    lines_before_matched: list[_TextLine] = field(default_factory=list)
    matched_lines: list[_TextLine] = field(default_factory=list)
    lines_after_matched: list[_TextLine] = field(default_factory=list)

    def __post_init__(self) -> None:
        matched_at = [line for line in self.lines if line.is_match]
        assert matched_at, "At least one matched line is required"
        first, last = matched_at[0].line_number, matched_at[-1].line_number
        self.lines_before_matched = [l for l in self.lines if l.line_number < first]
        self.matched_lines = matched_at
        self.lines_after_matched = [l for l in self.lines if l.line_number > last]

    @property
    def start_line(self) -> int:
        return self.lines[0].line_number

    @property
    def end_line(self) -> int:
        return self.lines[-1].line_number

    @property
    def num_matched_lines(self) -> int:
        return len(self.matched_lines)

    def to_display_string(self, include_line_numbers: bool = True) -> str:
        return "\n".join(l.format_line(include_line_numbers) for l in self.lines)

    @classmethod
    def from_file_contents(
        cls,
        file_contents: str,
        line: int,
        context_lines_before: int = 0,
        context_lines_after: int = 0,
        source_file_path: str | None = None,
    ) -> "MatchedConsecutiveLines":
        """`line` is 0-based, matching the LSP positions solidlsp works in."""
        all_lines = file_contents.split("\n")
        first = max(0, line - context_lines_before)
        last = min(len(all_lines) - 1, line + context_lines_after)
        window = [
            _TextLine(line_number=n, text=all_lines[n], is_match=(n == line))
            for n in range(first, last + 1)
        ]
        return cls(lines=window, source_file_path=source_file_path)


class DotNETRuntimeError(RuntimeError):
    """Raised when no suitable .NET runtime is available."""


class DotNETUtil:
    """
    Locates a `dotnet` executable whose installed runtimes satisfy a required
    version. The Roslyn and F# language servers are .NET applications, so this
    decides whether they can start at all.
    """

    _RUNTIME_RE = re.compile(r"Microsoft\.NETCore\.App\s+(\d+)\.(\d+)\.(\d+)")

    def __init__(self, required_version: str, allow_higher_version: bool = True) -> None:
        self.required_version = required_version
        self.allow_higher_version = allow_higher_version
        self._required = tuple(int(part) for part in required_version.split("."))
        self._dotnet = shutil.which("dotnet")
        self._installed = self._installed_runtimes()

    def _installed_runtimes(self) -> list[tuple[int, ...]]:
        if not self._dotnet:
            return []
        try:
            result = subprocess.run(
                [self._dotnet, "--list-runtimes"],
                capture_output=True,
                text=True,
                check=True,
            )
        except Exception:
            return []
        return [
            tuple(int(g) for g in m.groups())
            for m in self._RUNTIME_RE.finditer(result.stdout)
        ]

    def is_required_version_available(self) -> bool:
        width = len(self._required)
        for version in self._installed:
            prefix = version[:width]
            if self.allow_higher_version:
                if prefix >= self._required:
                    return True
            elif prefix == self._required:
                return True
        return False

    def get_dotnet_path_or_raise(self) -> str:
        if not self._dotnet:
            raise DotNETRuntimeError(
                "No `dotnet` executable found on PATH. Install the .NET runtime "
                f"{self.required_version} from https://dotnet.microsoft.com/download/dotnet"
            )
        if not self.is_required_version_available():
            installed = ", ".join(".".join(str(p) for p in v) for v in self._installed) or "none"
            raise DotNETRuntimeError(
                f"Required .NET runtime {self.required_version} not found (installed: {installed}). "
                "Install it from https://dotnet.microsoft.com/download/dotnet"
            )
        return self._dotnet
