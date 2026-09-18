# SPDX-License-Identifier: MIT
"""
Smoke test for the vendored SolidLSP: start a real language server against a
real repository and prove it answers the two questions we care about —
"what symbols are in this file" and "who references this symbol" — with
cross-file results the compiler agrees with.

Usage:
    python scripts/smoke_test.py <repo_root> [--language csharp] [--file <relative path>]

The first C# run downloads the Roslyn language server from NuGet and can take
several minutes; later runs reuse it from ~/.solidlsp.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Runnable straight from a source checkout, without installing first: the whole
# point of a smoke test is to work before you trust the install.
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import lodesman  # noqa: F401  — puts the vendored SolidLSP on sys.path
from lodesman.server import project_data_dir, solidlsp_home
from solidlsp import SolidLanguageServer
from solidlsp.ls_config import LanguageServerConfig, LanguageServerId
from solidlsp.settings import SolidLSPSettings

EXTENSIONS = {
    "csharp": ".cs",
    "typescript": ".ts",
    "python": ".py",
    "go": ".go",
    "rust": ".rs",
    "java": ".java",
}

SKIP_DIRS = {"obj", "bin", "node_modules", ".git", "dist", "build", ".venv", "venv", "Migrations"}


def elapsed(started: float) -> str:
    return f"{time.monotonic() - started:6.1f}s"


def pick_file(root: Path, extension: str) -> str | None:
    """The largest source file — the most likely to hold something referenced."""
    best, best_size = None, -1
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if not name.endswith(extension):
                continue
            full = Path(dirpath) / name
            try:
                size = full.stat().st_size
            except OSError:
                continue
            if size > best_size:
                best, best_size = full, size
    if best is None:
        return None
    return str(best.relative_to(root)).replace(os.sep, "/")


def describe(symbol: dict) -> str:
    name = symbol.get("name", "?")
    kind = symbol.get("kind", "?")
    location = symbol.get("location") or {}
    rng = location.get("range") or {}
    line = (rng.get("start") or {}).get("line")
    where = f" @L{line + 1}" if isinstance(line, int) else ""
    return f"{name} (kind={kind}){where}"


def symbol_position(symbol: dict) -> tuple[int, int] | None:
    """The position to ask about: prefer the name's own range."""
    for key in ("selectionRange", "range"):
        rng = symbol.get(key)
        if rng and "start" in rng:
            return rng["start"]["line"], rng["start"]["character"]
    location = symbol.get("location") or {}
    rng = location.get("selectionRange") or location.get("range")
    if rng and "start" in rng:
        return rng["start"]["line"], rng["start"]["character"]
    return None


def flatten(symbols) -> list[dict]:
    """
    `request_document_symbols` returns a `DocumentSymbols` object, not a list —
    it exposes the tree via `get_all_symbols_and_roots()` / `iter_symbols()`.
    The list walking below is only a fallback for other query methods.
    """
    if hasattr(symbols, "get_all_symbols_and_roots"):
        all_symbols, _roots = symbols.get_all_symbols_and_roots()
        return [s for s in all_symbols if isinstance(s, dict)]
    if hasattr(symbols, "iter_symbols"):
        return [s for s in symbols.iter_symbols() if isinstance(s, dict)]

    out: list[dict] = []

    def walk(items):
        for item in items or []:
            if isinstance(item, dict):
                out.append(item)
                walk(item.get("children"))

    if isinstance(symbols, (list, tuple)):
        for group in symbols:
            walk(group if isinstance(group, list) else [group])
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo_root")
    parser.add_argument("--language", default="csharp")
    parser.add_argument("--file", default=None)
    args = parser.parse_args()

    root = Path(args.repo_root).resolve()
    if not root.is_dir():
        print(f"not a directory: {root}")
        return 2

    language = LanguageServerId(args.language)
    extension = EXTENSIONS.get(args.language, ".cs")
    target = args.file or pick_file(root, extension)
    if target is None:
        print(f"no {extension} files under {root}")
        return 2

    print(f"repo     : {root}")
    print(f"language : {language}")
    print(f"file     : {target}")
    print("-" * 70)

    config = LanguageServerConfig(ls_id=language)
    # Same storage layout as the MCP server, so the two share the downloaded
    # language servers and the per-project cache instead of each paying for
    # their own.
    data_dir = project_data_dir(root)
    data_dir.mkdir(parents=True, exist_ok=True)
    settings = SolidLSPSettings(
        solidlsp_dir=str(solidlsp_home()),
        project_data_path=str(data_dir),
    )

    started = time.monotonic()
    server = SolidLanguageServer.create(
        config, str(root), timeout=600, solidlsp_settings=settings
    )
    print(f"[{elapsed(started)}] server object created")

    with server.start_server_context():
        print(f"[{elapsed(started)}] server started and initialized")

        t0 = time.monotonic()
        symbols = server.request_document_symbols(target)
        print(f"[{elapsed(started)}] document symbols in {time.monotonic() - t0:.2f}s")

        flat = flatten(symbols)
        print(f"          -> {len(flat)} symbols")
        for symbol in flat[:10]:
            print(f"             - {describe(symbol)}")
        if not flat:
            print("!! no symbols returned — the server did not analyze this file")
            return 1

        # Ask "who references this?" for the first few symbols that have a position.
        asked = 0
        for symbol in flat:
            position = symbol_position(symbol)
            if position is None:
                continue
            line, column = position
            t0 = time.monotonic()
            try:
                refs = server.request_references(target, line, column)
            except Exception as exc:  # noqa: BLE001
                print(f"          references({symbol.get('name')}) failed: {exc}")
                continue
            files = sorted({(r.get("relativePath") or r.get("uri", "?")) for r in refs})
            print(
                f"[{elapsed(started)}] references to {symbol.get('name')!r}: "
                f"{len(refs)} in {len(files)} file(s) ({time.monotonic() - t0:.2f}s)"
            )
            for f in files[:5]:
                print(f"             - {f}")
            asked += 1
            if asked >= 3:
                break

        t0 = time.monotonic()
        try:
            hits = server.request_workspace_symbol("Service")
            count = len(hits) if hits else 0
            print(
                f"[{elapsed(started)}] workspace symbol search 'Service': "
                f"{count} hits ({time.monotonic() - t0:.2f}s)"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"          workspace symbol search failed: {exc}")

    print("-" * 70)
    print(f"done in {elapsed(started)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
