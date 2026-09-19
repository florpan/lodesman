# SPDX-License-Identifier: MIT
"""
Lodesman — compiler-grade code intelligence over MCP.

Exposes a language server's own answers (not a text search, not a heuristic
parser) as MCP tools, for the languages SolidLSP supports.

The language server is started lazily on the first tool call and kept warm for
the life of the process. That is the whole point: a cold Roslyn or rust-analyzer
costs seconds to minutes, while a warm one answers in milliseconds and tracks
edits incrementally.

Protocol: MCP over stdio — newline-delimited JSON-RPC 2.0. stdout carries
protocol messages only; all diagnostics go to stderr.

Usage:
    lodesman-mcp [repo_root] [--language csharp]

`repo_root` defaults to the working directory. Downloaded language servers live
in ~/.solidlsp (override with SOLIDLSP_HOME); per-project caches live under
~/.solidlsp/projects/<name>-<hash>, keyed by the repo's absolute path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

from solidlsp import SolidLanguageServer
from solidlsp.ls_config import LanguageServerConfig, LanguageServerId
from solidlsp.settings import SolidLSPSettings

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "lodesman"
SERVER_VERSION = "0.3.1"

# Probe for the index-readiness gate: short, and matches something in any repo.
WARM_PROBE = "a"

# Extension -> language server id, for auto-detection.
EXTENSION_LANGUAGES = {
    ".cs": "csharp",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "typescript",
    ".py": "python",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".cpp": "cpp",
    ".c": "cpp",
}

SKIP_DIRS = {
    "obj", "bin", "node_modules", "dist", "build",
    "venv", "Migrations", "__pycache__", "target",
}


def walkable(dirnames: list[str]) -> list[str]:
    """
    Which subdirectories are worth descending into when surveying a repository.

    Dot-directories are skipped wholesale rather than blacklisted one at a time.
    The named list could never keep up — .tox, .mypy_cache, .direnv, .pixi,
    .gradle, .m2 and friends all hold source-shaped files that are nobody's
    source — and on Linux the problem is categorically worse: a survey of a home
    directory found 2294 .py files and every single one of them was inside
    .cache, .local or a tool's state directory. Counting those picks a language
    for a project made entirely of other people's caches.
    """
    return [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]


def log(message: str) -> None:
    print(f"[{SERVER_NAME}] {message}", file=sys.stderr, flush=True)


def solidlsp_home() -> Path:
    """
    Machine-global storage for downloaded language servers.

    Roslyn, tsserver and friends are hundreds of megabytes and are identical for
    every project, so they live once per machine — never beside this script and
    never inside a repository. Override with SOLIDLSP_HOME.
    """
    return Path(os.environ.get("SOLIDLSP_HOME") or (Path.home() / ".solidlsp"))


def project_data_dir(root: Path) -> Path:
    """
    Per-project cache directory, derived from the repo's absolute path.

    SolidLSP keys its document-symbol cache by *relative* path, so two
    repositories sharing one data directory would share cache entries for any
    file whose relative path matches. Deriving the directory from the absolute
    path keeps them apart; putting it under the global home keeps it out of the
    repo, so no project needs a .gitignore entry for us.
    """
    key = str(root).replace("\\", "/").rstrip("/")
    # Case-fold only where the filesystem does. On Windows C:\Dev\Foo and
    # c:\dev\foo are one repository and must share a cache; on Linux they are
    # two repositories and must not, which is the whole point of this function.
    if os.name == "nt":
        key = key.lower()
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return solidlsp_home() / "projects" / f"{root.name}-{digest}"


def detect_language(root: Path) -> str:
    """Pick the language server by counting source files."""
    counts: Counter[str] = Counter()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = walkable(dirnames)
        for name in filenames:
            language = EXTENSION_LANGUAGES.get(Path(name).suffix)
            if language:
                counts[language] += 1
    if not counts:
        raise RuntimeError(f"no recognized source files under {root}")
    language, count = counts.most_common(1)[0]
    log(f"detected language: {language} ({count} files)")
    return language


class LanguageServerSession:
    """Owns one language server, started on demand and kept warm."""

    def __init__(self, root: Path, language: str) -> None:
        self.root = root
        self.language = language
        self._server: SolidLanguageServer | None = None
        self._context = None
        self._anchors: list = []
        self._lock = threading.Lock()

    @property
    def server(self) -> SolidLanguageServer:
        with self._lock:
            if self._server is None:
                log(f"starting {self.language} language server at {self.root} …")
                config = LanguageServerConfig(ls_id=LanguageServerId(self.language))
                data_dir = project_data_dir(self.root)
                data_dir.mkdir(parents=True, exist_ok=True)
                log(f"cache: {data_dir}")
                settings = SolidLSPSettings(
                    solidlsp_dir=str(solidlsp_home()),
                    project_data_path=str(data_dir),
                )
                server = SolidLanguageServer.create(
                    config, str(self.root), timeout=600, solidlsp_settings=settings
                )
                self._context = server.start_server_context()
                self._context.__enter__()
                self._anchor_project(server)
                self._warm_index(server)
                self._server = server
                log("language server ready")
            return self._server

    # Conventional source roots, in preference order. A root-level file such as
    # drizzle.config.ts is usually outside the main tsconfig and lands tsserver
    # in a one-file inferred project, which loads the wrong project entirely.
    SOURCE_DIRS = ("src", "lib", "app", "source", "packages", "components")

    def _find_source_file(self, within: Path | None = None) -> str | None:
        """The first source file of this session's language under `within`."""
        for dirpath, dirnames, filenames in os.walk(within or self.root):
            dirnames[:] = sorted(walkable(dirnames))
            for filename in sorted(filenames):
                if filename.endswith(".d.ts"):
                    continue
                if EXTENSION_LANGUAGES.get(Path(filename).suffix) == self.language:
                    return os.path.join(dirpath, filename)
        return None

    def _anchor_candidates(self, server: SolidLanguageServer) -> list[str]:
        """
        Files to hold open, most likely to belong to the real project first.

        More than one, because a repository can hold several projects and a
        language server only answers for the projects it has actually loaded —
        so a single anchor in the wrong place looks exactly like no anchor.
        """
        candidates: list[str] = []
        for name in self.SOURCE_DIRS:
            directory = self.root / name
            if directory.is_dir():
                found = self._find_source_file(directory)
                if found:
                    candidates.append(found)
        try:
            representative = server._find_representative_source_file(str(self.root))
        except Exception:  # noqa: BLE001 — not implemented for most languages
            representative = None
        if representative:
            candidates.append(representative)
        if not candidates:
            fallback = self._find_source_file()
            if fallback:
                candidates.append(fallback)
        # Preserve order, drop duplicates, keep it bounded.
        seen: set[str] = set()
        unique = [c for c in candidates if not (c in seen or seen.add(c))]
        return unique[:4]

    def _anchor_project(self, server: SolidLanguageServer) -> None:
        """
        Hold one source file open for the life of the server, so the language
        server actually has a project.

        Several language servers load projects lazily, on the first `didOpen`,
        and answer workspace-wide queries out of whatever projects are loaded.
        tsserver is one: with no file open it has no project, and
        `workspace/symbol` fails with "No Project" — which takes out
        `find_symbol`, `find_definition`, and `find_references` without a
        file/line hint. Opening a file per request and closing it again is not
        enough; the project goes away with it.

        C# never hits this because the Roslyn server is handed the real build
        entry point: SolidLSP sends it `solution/open` and `project/open` with
        the .sln/.csproj (csharp_language_server.py:730). This is the moral
        equivalent for servers that have no such notion.

        SolidLSP already does exactly this for *additional* workspace folders
        (`ls.py:1109`) and never for the primary one, which is the gap.
        """
        candidates = self._anchor_candidates(server)
        if not candidates:
            log("no anchor file found; workspace-wide queries may fail")
            return

        # Tell the server indexing is about to be triggered, then wait for it
        # afterwards. These are SolidLSP's own hooks for exactly this sequence —
        # _activate_additional_workspaces calls the same pair around its own
        # didOpen (ls.py:1136, ls.py:1150) — and both are no-ops for languages
        # that do not track indexing progress, so this is safe everywhere.
        try:
            server._signal_expect_indexing()
        except Exception as exc:  # noqa: BLE001
            log(f"could not signal expected indexing: {exc}")

        for source in candidates:
            relative = os.path.relpath(source, self.root).replace(os.sep, "/")
            try:
                anchor = server.open_file(relative)
                anchor.__enter__()
                self._anchors.append(anchor)
                log(f"anchored project on {relative}")
            except Exception as exc:  # noqa: BLE001 — never block startup
                log(f"could not anchor project on {relative}: {exc}")

        if self._anchors:
            try:
                server._wait_for_additional_workspace_indexing()
                log("anchor indexing complete")
            except Exception as exc:  # noqa: BLE001
                log(f"wait for anchor indexing failed: {exc}")

    def settle(self, timeout: float = 15.0) -> None:
        """Re-run the readiness gate against the running server."""
        if self._server is not None:
            self._warm_index(self._server, timeout=timeout)

    @staticmethod
    def _warm_index(server: SolidLanguageServer, timeout: float = 30.0) -> None:
        """
        Poll a probe query until two consecutive answers are identical, so no
        real query is ever served from a half-built index.

        Two different language servers, two different ways of lying early:

        * **Roslyn** answers the first workspace/symbol request of the process
          from a partially-built index, with *stale positions*. Measured on
          CalibreManager, the first call put `RagQueryService` at line 14 char
          14 and every call after it at line 15 char 13, where the class really
          is. Asking for references at the stale position hits no symbol, so
          `find_references` reported "no references" for a type with three.
        * **tsserver** indexes asynchronously after the anchor file is opened
          and returns *incomplete* results meanwhile. Measured on specplanner,
          `resolveSpecRoot` returned 0 hits immediately after anchoring and the
          correct 2 hits five seconds later.

        Neither is caught by waiting for a readiness notification: SolidLSP
        already waits for `workspace/projectInitializationComplete` for C#
        (csharp_language_server.py:706) and for `$/progress` for TypeScript, and
        both bugs happen after those complete.

        The fingerprint has to include positions, not just a result count —
        Roslyn's stale and correct answers have the same count and differ only
        in where they point, so counting would pass the bad answer through.
        """
        # SolidLSP gates *file-scoped* requests on cross-file indexing
        # (ls.py:1460), so definition/references/implementation already wait for
        # tsserver to finish. workspace/symbol is not file-scoped, so it never
        # passes through that gate — which is precisely why TypeScript name
        # lookups were intermittently empty while hinted find_references never
        # was. Invoke the same pair here. Both are base-class no-ops for
        # languages that do not track indexing, and the TypeScript
        # implementation is one-shot, so this costs nothing after the first call.
        try:
            server._pre_open_for_cross_file_references()
            server._wait_for_cross_file_references_if_needed()
        except Exception as exc:  # noqa: BLE001 — never block startup
            log(f"cross-file indexing wait failed (continuing): {exc}")

        def fingerprint() -> tuple | None:
            hits = server.request_workspace_symbol(WARM_PROBE) or []
            items = []
            for hit in hits:
                if not isinstance(hit, dict):
                    continue
                rng = (hit.get("location") or {}).get("range") or {}
                start = rng.get("start") or {}
                items.append((hit.get("name"), hit.get("kind"), start.get("line"), start.get("character")))
            return tuple(sorted(items, key=repr))

        deadline = time.monotonic() + timeout
        previous: tuple | None = None
        polls = 0
        while time.monotonic() < deadline:
            try:
                current = fingerprint()
            except Exception as exc:  # noqa: BLE001 — warming must never break startup
                log(f"index warm-up failed (continuing): {exc}")
                return
            polls += 1
            if previous is not None and current == previous:
                log(f"index settled after {polls} probe(s)")
                return
            previous = current
            time.sleep(0.3)
        log(f"index did not settle within {timeout:.0f}s; proceeding anyway")

    def shutdown(self) -> None:
        with self._lock:
            for anchor in reversed(self._anchors):
                try:
                    anchor.__exit__(None, None, None)
                except Exception as exc:  # noqa: BLE001
                    log(f"anchor release error: {exc}")
            self._anchors = []
            if self._context is not None:
                try:
                    self._context.__exit__(None, None, None)
                except Exception as exc:  # noqa: BLE001
                    log(f"shutdown error: {exc}")
                self._context = None
                self._server = None


_ROOT: Path | None = None


def to_relative(location: dict) -> str | None:
    """
    A repo-relative path for a location.

    Workspace-symbol results carry a `file://` URI rather than the
    `relativePath` that document-symbol results have, so both shapes must be
    handled or every lookup by name fails.
    """
    path = location.get("relativePath")
    if path:
        return path.replace("\\", "/")
    uri = location.get("uri")
    if not uri:
        return None
    from urllib.parse import unquote, urlparse

    raw = unquote(urlparse(uri).path)
    # A Windows URI path arrives as "/C:/…" — strip the leading slash.
    if len(raw) > 2 and raw[0] == "/" and raw[2] == ":":
        raw = raw[1:]
    if _ROOT is None:
        return raw
    try:
        return str(Path(raw).resolve().relative_to(_ROOT)).replace(os.sep, "/")
    except ValueError:
        return None


def location_of(item: dict) -> str:
    """Render 'path:line' from a symbol or a reference."""
    location = item.get("location") or item
    path = to_relative(location) or location.get("uri") or "?"
    rng = location.get("range") or item.get("range") or {}
    line = (rng.get("start") or {}).get("line")
    return f"{path}:{line + 1}" if isinstance(line, int) else str(path)


def symbols_of(document_symbols) -> list[dict]:
    if hasattr(document_symbols, "get_all_symbols_and_roots"):
        all_symbols, _roots = document_symbols.get_all_symbols_and_roots()
        return [s for s in all_symbols if isinstance(s, dict)]
    if isinstance(document_symbols, list):
        return [s for s in document_symbols if isinstance(s, dict)]
    return []


def position_of(symbol: dict) -> tuple[int, int] | None:
    for key in ("selectionRange", "range"):
        rng = symbol.get(key)
        if rng and "start" in rng:
            return rng["start"]["line"], rng["start"]["character"]
    location = symbol.get("location") or {}
    rng = location.get("selectionRange") or location.get("range")
    if rng and "start" in rng:
        return rng["start"]["line"], rng["start"]["character"]
    return None


TOOLS = [
    {
        "name": "project_info",
        "description": (
            "Which repository this server bound to, and how. Answers cheaply without "
            "starting the language server — use it first to confirm the server is "
            "pointed at the project you think it is."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "find_symbol",
        "description": (
            "Find a symbol by name anywhere in the project, resolved by the language "
            "server rather than by text search. Returns each match with its file and line."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Symbol name or fragment"},
                "limit": {"type": "integer", "description": "Max results (default 25)"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "document_symbols",
        "description": (
            "Outline one file: every type, method, and field it declares, with line "
            "numbers. The API surface of the file without reading its body."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Path relative to the repo root"},
            },
            "required": ["file"],
        },
    },
    {
        "name": "find_references",
        "description": (
            "Find everything that references a symbol — the real call/usage sites the "
            "compiler sees, including dependency-injection registrations and interface "
            "implementations that a text search misses. Run this before renaming, "
            "changing a signature, or deleting."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Symbol name to look up"},
                "file": {"type": "string", "description": "Optional: file containing the symbol"},
                "line": {"type": "integer", "description": "Optional: 1-based line of the symbol"},
                "limit": {"type": "integer", "description": "Max results (default 50)"},
                "include_code": {
                    "type": "boolean",
                    "description": (
                        "Show the source at each reference (default true). Leave it on: "
                        "without it you must open each file to use the answer."
                    ),
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_symbol_body",
        "description": (
            "The full source of one declaration, by name — a method, class or function — "
            "without reading the file it lives in. Use this instead of opening a file to "
            "look at a single member."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Symbol name"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "check",
        "description": (
            "Compiler diagnostics for one file, from the already-running language server — "
            "errors and warnings in milliseconds, without a build. Run this after editing "
            "to confirm the change compiles."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Path relative to the repo root"},
                "limit": {"type": "integer", "description": "Max diagnostics (default 40)"},
                "severity": {
                    "type": "integer",
                    "description": (
                        "Lowest severity to report: 1 errors only, 2 errors+warnings "
                        "(default), 3 adds info, 4 adds style hints."
                    ),
                },
            },
            "required": ["file"],
        },
    },
    {
        "name": "find_definition",
        "description": "Jump to where a symbol is defined, resolved by the language server.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Symbol name to look up"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "explain_symbol",
        "description": (
            "What a symbol is: resolved type, signature and documentation, plus its "
            "declaration line. Answers 'what does this call do' without opening the file."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Symbol name"}},
            "required": ["name"],
        },
    },
    {
        "name": "blast_radius",
        "description": (
            "What breaks if this symbol changes: everything that references it, then "
            "everything that references those, to the given depth. Run before changing a "
            "signature or deleting anything."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Symbol name"},
                "depth": {"type": "integer", "description": "Hops to follow, 1-4 (default 2)"},
                "max_queries": {"type": "integer", "description": "Query budget (default 60)"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "rename_symbol",
        "description": (
            "Rename a symbol everywhere, using the compiler's own understanding rather "
            "than text replacement — so it renames the right things and leaves unrelated "
            "same-named symbols alone. Defaults to a dry run showing what would change."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Current symbol name"},
                "new_name": {"type": "string", "description": "New name"},
                "apply": {
                    "type": "boolean",
                    "description": "Write the changes. Omit or false to preview only.",
                },
            },
            "required": ["name", "new_name"],
        },
    },
    {
        "name": "find_implementations",
        "description": (
            "Find the concrete implementations of an interface or abstract member, or "
            "the overrides of a virtual one. Answers 'what actually runs when this is "
            "called', which references alone cannot tell you."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Interface or member name"},
                "limit": {"type": "integer", "description": "Max results (default 50)"},
            },
            "required": ["name"],
        },
    },
]


class ToolError(Exception):
    pass


def repo_file(target: str) -> str:
    """
    Validate a caller-supplied file path and return it relative to the repo.

    Every tool that takes a `file` argument routes through here. The language
    server will happily open anything the process can read, so without this a
    caller could ask for diagnostics on /etc/passwd or a credentials file and
    get identifiers echoed back out of it. Agents construct these paths from
    model output, so "the caller wouldn't do that" is not an assumption
    available to us.

    Absolute paths inside the repo are accepted and made relative; anything that
    resolves outside it is refused, including by way of "..".
    """
    cleaned = (target or "").replace("\\", "/").strip()
    if not cleaned:
        raise ToolError("file is required")
    if _ROOT is None:
        return cleaned

    candidate = Path(cleaned)
    resolved = (candidate if candidate.is_absolute() else _ROOT / candidate).resolve()
    try:
        return str(resolved.relative_to(_ROOT)).replace(os.sep, "/")
    except ValueError:
        raise ToolError(
            f"{target!r} is outside the repository ({_ROOT}). This server serves "
            "one repository; paths must stay inside it."
        ) from None


def utf16_index(line: str, units: int) -> int:
    """
    Convert an LSP character offset into a Python string index.

    LSP counts a line in UTF-16 code units, not characters, so anything outside
    the BMP — emoji, some CJK extensions — counts as two where Python counts
    one. Indexing a str directly with an LSP offset therefore corrupts every
    edit that follows such a character on the same line.
    """
    if units <= 0:
        return 0
    count = 0
    for i, char in enumerate(line):
        if count >= units:
            return i
        count += 2 if ord(char) > 0xFFFF else 1
    return len(line)


def apply_edits_to_text(text: str, edits: list[dict]) -> str:
    """
    Apply LSP TextEdits to a string, returning the new text.

    Kept pure and separate from the file I/O so it can be tested without a
    language server: this is the logic that was silently doing nothing.

    Edits are applied last-position-first so that earlier offsets stay valid
    while later ones are rewritten. Line terminators are never touched, which
    is what preserves CRLF through the round trip.
    """
    lines = text.splitlines(keepends=True)
    starts: list[int] = []
    running = 0
    for line in lines:
        starts.append(running)
        running += len(line)
    starts.append(running)

    def offset(position: dict) -> int:
        line_no = position.get("line", 0)
        if line_no >= len(lines):
            return len(text)
        # Measure the column against the line without its terminator, so a
        # character offset can never run past the end of the line into the next.
        bare = lines[line_no].rstrip("\r\n")
        return starts[line_no] + utf16_index(bare, position.get("character", 0))

    resolved = []
    for edit in edits:
        span = edit.get("range") or {}
        start = offset(span.get("start") or {})
        end = offset(span.get("end") or {})
        resolved.append((start, max(start, end), edit.get("newText", "")))

    for start, end, replacement in sorted(resolved, key=lambda e: e[0], reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def write_edits(root: Path, relative_path: str, edits: list[dict]) -> bool:
    """
    Apply edits to a file on disk. Returns whether the bytes actually changed.

    SolidLSP's apply_text_edits_to_file only mutates its in-memory buffer and
    notifies the language server; nothing is ever written, and the buffer is
    discarded when its context manager exits. So we do the write ourselves,
    which also keeps the vendored tree unmodified.

    newline="" on both ends is load-bearing: without it Python's universal
    newline translation rewrites a CRLF file to LF on the way out, quietly
    reformatting every line of a file the user only asked to rename one symbol
    in.
    """
    path = (root / relative_path).resolve()
    with open(path, "r", encoding="utf-8", newline="") as handle:
        original = handle.read()

    updated = apply_edits_to_text(original, edits)
    if updated == original:
        return False

    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(updated)
    return True


DEFAULT_CONTEXT_BEFORE = 2
DEFAULT_CONTEXT_AFTER = 4


def peek(session: LanguageServerSession, file: str, line: int,
         before: int = DEFAULT_CONTEXT_BEFORE, after: int = DEFAULT_CONTEXT_AFTER) -> str:
    """
    The source around a location, indented for display.

    The point of the whole tool: a location is a promise of future cost, because
    the agent has to read the file to use it. Measured on CalibreManager,
    answering "what uses RagQueryService" by reading the three files involved
    costs ~17.7k tokens; the same three references with a few lines of context
    each cost ~370. Returning the code is not a convenience, it is the product.
    """
    try:
        block = session.server.retrieve_content_around_line(file, line, before, after)
    except Exception as exc:  # noqa: BLE001 — a missing file must not fail the query
        return f"      (could not read {file}:{line + 1}: {exc})"
    # Render 1-based line numbers. SolidLSP works in 0-based LSP positions, but
    # every number we show the caller elsewhere is 1-based, and mixing the two
    # in one block is how you get an agent editing the wrong line.
    rows = []
    for text_line in block.lines:
        marker = ">" if text_line.is_match else " "
        rows.append(f"      {marker} {text_line.line_number + 1:>5}: {text_line.text}")
    return "\n".join(rows)


# LSP SymbolKind values. A C# name is routinely both a type and its constructor,
# and asking about "RagQueryService" almost always means the type.
KIND_NAMES = {
    1: "file", 4: "package",
    2: "module", 3: "namespace", 5: "class", 6: "method", 7: "property",
    8: "field", 9: "constructor", 10: "enum", 11: "interface", 12: "function",
    13: "variable", 14: "constant", 22: "enum member", 23: "struct", 24: "event",
}
TYPE_LIKE_KINDS = {2, 3, 5, 10, 11, 23}


def kind_of(hit: dict) -> str:
    return KIND_NAMES.get(hit.get("kind"), f"kind={hit.get('kind')}")


def describe(hit: dict) -> str:
    return f"{hit.get('name')} ({kind_of(hit)}) — {location_of(hit)}"


def rank_candidates(hits: list[dict], name: str) -> list[dict]:
    """
    Order workspace-symbol hits by how likely they are to be what was asked for:
    exact name first, then types over members, then location.

    Roslyn's ordering is not stable between processes, so taking the first exact
    match made results depend on which one it happened to list first — for a C#
    service that is a coin flip between the class and its constructor, and the
    constructor has no references of its own. Ranking, with location as the
    tie-break, means the same question gets the same answer twice.
    """
    return sorted(
        hits,
        key=lambda h: (
            0 if h.get("name") == name else 1,
            0 if h.get("kind") in TYPE_LIKE_KINDS else 1,
            location_of(h),
        ),
    )


def workspace_hits(session: LanguageServerSession, query: str) -> list[dict]:
    """
    Workspace symbol search that does not report an empty result until the index
    has been given a second chance to settle.

    The startup gate is not sufficient on its own for TypeScript: the probe query
    can stabilise while tsserver is still indexing the rest of the project, and
    then a real query returns nothing. Measured as an intermittent miss on
    specplanner — one run found `resolveSpecRoot`, the next did not.

    Empty is the one answer worth paying to double-check, because an agent reads
    "not found" as "does not exist" and acts on it. A hit costs nothing extra;
    a miss costs one re-settle.
    """
    def query_once() -> list[dict]:
        return [h for h in (session.server.request_workspace_symbol(query) or []) if isinstance(h, dict)]

    hits = query_once()
    if not hits:
        log(f"empty result for {query!r}; re-settling the index before believing it")
        session.settle()
        hits = query_once()
    return hits


def candidates_for(session: LanguageServerSession, name: str) -> list[dict]:
    """Ranked declarations to try for `name` — exact matches only if any exist."""
    hits = workspace_hits(session, name)
    if not hits:
        raise ToolError(f"no symbol named {name!r} found in the project")
    exact = [h for h in hits if h.get("name") == name]
    return rank_candidates(exact or hits, name)


def resolve_symbol(session: LanguageServerSession, name: str) -> dict:
    """The most likely workspace match for `name`."""
    return candidates_for(session, name)[0]


def call_tool(session: LanguageServerSession, name: str, args: dict) -> str:
    if name == "project_info":
        # Deliberately does not touch session.server: the point is to confirm the
        # binding before paying for a language server start.
        started = "running" if session._server is not None else "not started yet"
        return "\n".join([
            f"repository    : {session.root}",
            f"language      : {session.language}",
            f"language server: {started}",
            f"cache         : {project_data_dir(session.root)}",
            f"servers       : {solidlsp_home()}",
            "",
            "The repository is the server process's working directory unless a path "
            "was passed on the command line. One server binds to one repository for "
            "its lifetime.",
        ])

    if name == "find_symbol":
        query = args["name"]
        limit = int(args.get("limit", 25))
        hits = workspace_hits(session, query)
        if not hits:
            return f"No symbol matching {query!r}."
        # Most-likely-intended first, so the ordering does not depend on Roslyn's.
        hits = rank_candidates(hits, query)
        lines = [f"{len(hits)} match(es) for {query!r}:"]
        lines += [f"  {describe(h)}" for h in hits[:limit]]
        if len(hits) > limit:
            lines.append(f"  … and {len(hits) - limit} more")
        return "\n".join(lines)

    if name == "document_symbols":
        target = repo_file(args["file"])
        symbols = symbols_of(session.server.request_document_symbols(target))
        if not symbols:
            return f"No symbols in {target} (is it excluded from the project?)."
        lines = [f"{len(symbols)} symbol(s) in {target}:"]
        for symbol in symbols:
            position = position_of(symbol)
            where = f"L{position[0] + 1}" if position else "?"
            detail = symbol.get("detail")
            suffix = f" — {detail}" if detail else ""
            lines.append(f"  {symbol.get('name')} (kind={symbol.get('kind')}) {where}{suffix}")
        return "\n".join(lines)

    if name == "find_references":
        limit = int(args.get("limit", 50))
        symbol_name = args["name"]

        if args.get("file") and args.get("line") is not None:
            target = repo_file(args["file"])
            line = int(args["line"]) - 1
            column = 0
            for symbol in symbols_of(session.server.request_document_symbols(target)):
                if symbol.get("name") == symbol_name:
                    found = position_of(symbol)
                    if found:
                        line, column = found
                    break
            attempts = [(target, line, column, f"{target}:{line + 1}")]
        else:
            attempts = []
            for candidate in candidates_for(session, symbol_name):
                location = candidate.get("location") or {}
                path = to_relative(location)
                position = position_of(candidate)
                if path and position:
                    attempts.append((path, position[0], position[1], describe(candidate)))
            if not attempts:
                raise ToolError(f"symbol {symbol_name!r} has no usable location")

        # Try each candidate rather than trusting the top-ranked one. A name that
        # resolves to a constructor or a DI field genuinely has no references of
        # its own, and reporting that as "no references" invites an agent to
        # delete something that is very much in use.
        refs: list = []
        used = attempts[0]
        for attempt in attempts:
            path, line, column, _label = attempt
            found_refs = session.server.request_references(path, line, column) or []
            if found_refs:
                refs, used = found_refs, attempt
                break

        if not refs:
            tried = "\n".join(f"  tried {a[3]}" for a in attempts)
            return (
                f"No references to {symbol_name!r} found from any of its "
                f"{len(attempts)} declaration(s):\n{tried}\n"
                "Treat this as inconclusive rather than as proof it is unused."
            )

        with_code = args.get("include_code", True)
        by_file: dict[str, list[int]] = {}
        for ref in refs:
            location = ref.get("location") or ref
            path = to_relative(location) or location.get("uri") or "?"
            rng = location.get("range") or {}
            ln = (rng.get("start") or {}).get("line")
            by_file.setdefault(path, []).append(ln if isinstance(ln, int) else 0)

        lines = [
            f"{len(refs)} reference(s) to {symbol_name!r} in {len(by_file)} file(s) "
            f"(resolved to {used[3]}):"
        ]
        shown_refs = 0
        for path, line_numbers in sorted(by_file.items())[:limit]:
            if with_code:
                lines.append(f"  {path}")
                for ln in sorted(set(line_numbers)):
                    if shown_refs >= limit:
                        break
                    lines.append(f"    L{ln + 1}:")
                    lines.append(peek(session, path, ln))
                    shown_refs += 1
            else:
                rendered = ", ".join(f"L{n + 1}" for n in sorted(line_numbers)[:10])
                more = f" (+{len(line_numbers) - 10} more)" if len(line_numbers) > 10 else ""
                lines.append(f"  {path} — {rendered}{more}")
        if len(by_file) > limit:
            lines.append(f"  … and {len(by_file) - limit} more files")
        if len(attempts) > 1:
            others = [a[3] for a in attempts if a is not used]
            lines.append(f"  (other declarations of this name: {'; '.join(others)})")
        return "\n".join(lines)

    if name == "find_definition":
        symbol = resolve_symbol(session, args["name"])
        location = symbol.get("location") or {}
        target = to_relative(location)
        position = position_of(symbol)
        if not target or not position:
            raise ToolError(f"symbol {args['name']!r} has no usable location")
        defs = session.server.request_definition(target, position[0], position[1]) or []
        if not defs:
            return f"{symbol.get('name')} — declared at {location_of(symbol)}"
        lines = [f"{symbol.get('name')} is defined at:"]
        lines += [f"  {location_of(d)}" for d in defs]
        return "\n".join(lines)

    if name == "get_symbol_body":
        candidates = candidates_for(session, args["name"])
        for candidate in candidates:
            location = candidate.get("location") or {}
            target = to_relative(location)
            position = position_of(candidate)
            if not target or not position:
                continue
            symbol = session.server.request_containing_symbol(
                target, position[0], position[1], include_body=True
            )
            body = (symbol or {}).get("body")
            if body is not None:
                # SymbolBody holds a line buffer plus offsets; its repr is the
                # offsets, not the code. get_text() is what we actually want.
                text = body.get_text() if hasattr(body, "get_text") else str(body)
                if text.strip():
                    return f"{describe(candidate)}\n\n{text}"
        # Fall back to the declaration range when the server returns no body.
        best = candidates[0]
        location = best.get("location") or {}
        target = to_relative(location)
        position = position_of(best)
        if target and position:
            return (
                f"{describe(best)}\n\n(no body from the language server; showing context)\n"
                + peek(session, target, position[0], 0, 30)
            )
        raise ToolError(f"symbol {args['name']!r} has no usable location")

    if name == "check":
        target = repo_file(args["file"])
        # LSP severity: 1 error, 2 warning, 3 info, 4 hint. Default to errors and
        # warnings only — a file can carry dozens of style hints, and burying a
        # compile error under "use primary constructor" defeats the purpose.
        min_severity = int(args.get("severity", 2))
        try:
            diagnostics = session.server.request_text_document_diagnostics(
                target, min_severity=min_severity
            ) or []
        except Exception:  # noqa: BLE001 — not every server supports pull diagnostics
            diagnostics = session.server.request_published_text_document_diagnostics(
                target, min_severity=min_severity
            ) or []
        names = {1: "error", 2: "warning", 3: "info", 4: "hint"}
        if not diagnostics:
            scope = names.get(min_severity, "issue")
            return (
                f"{target}: no {scope}s or worse. Note this is the language server's view "
                "of the project, not a full build."
            )
        diagnostics.sort(key=lambda d: (d.get("severity") or 9,
                                        ((d.get("range") or {}).get("start") or {}).get("line") or 0))
        counts = Counter(names.get(d.get("severity"), "?") for d in diagnostics)
        rendered = [f"{target}: " + ", ".join(f"{n} {k}(s)" for k, n in counts.items())]

        # Distinguish "your code is broken" from "the project did not load".
        # A failed NuGet restore or an unreachable feed makes the compiler lose
        # the core assemblies, and it then reports every tuple and every base
        # type as undefined. Observed on CalibreManager, which references a
        # private feed that is not reachable: 42 phantom errors, none real. An
        # agent told to fix those would edit correct code.
        broken_project_codes = {"CS8179", "CS0518", "CS0012", "CS1069", "CS0246", "CS0234"}
        suspect = sum(1 for d in diagnostics if str(d.get("code")) in broken_project_codes)
        if suspect and suspect >= len(diagnostics) / 2:
            rendered.append(
                f"  WARNING: {suspect} of {len(diagnostics)} are missing-type/assembly errors. "
                "That usually means the project did not load fully (failed or incomplete "
                "package restore), not that the code is wrong. Verify with a real build "
                "before changing anything."
            )
        for item in diagnostics[: int(args.get("limit", 40))]:
            start = (item.get("range") or {}).get("start") or {}
            line = start.get("line")
            code = item.get("code")
            suffix = f" [{code}]" if code else ""
            where = f"L{line + 1}" if isinstance(line, int) else "?"
            rendered.append(
                f"  {names.get(item.get('severity'), '?')} {where}{suffix}: {item.get('message')}"
            )
        if len(diagnostics) > int(args.get("limit", 40)):
            rendered.append(f"  … and {len(diagnostics) - int(args.get('limit', 40))} more")
        return "\n".join(rendered)

    if name == "explain_symbol":
        candidate = resolve_symbol(session, args["name"])
        location = candidate.get("location") or {}
        target = to_relative(location)
        position = position_of(candidate)
        if not target or not position:
            raise ToolError(f"symbol {args['name']!r} has no usable location")
        parts = [describe(candidate)]

        hover = session.server.request_hover(target, position[0], position[1])
        text = ((hover or {}).get("contents") or {})
        if isinstance(text, dict):
            text = text.get("value") or ""
        elif isinstance(text, list):
            text = "\n".join(t.get("value", str(t)) if isinstance(t, dict) else str(t) for t in text)
        if text and str(text).strip():
            parts.append(f"\n{str(text).strip()}")

        try:
            signature = session.server.request_signature_help(target, position[0], position[1])
        except Exception:  # noqa: BLE001 — optional extra
            signature = None
        for item in ((signature or {}).get("signatures") or [])[:3]:
            parts.append(f"\nsignature: {item.get('label')}")

        parts.append(f"\ndeclared at:\n{peek(session, target, position[0], 1, 3)}")
        return "\n".join(parts)

    if name == "blast_radius":
        depth = max(1, min(int(args.get("depth", 2)), 4))
        budget = int(args.get("max_queries", 60))
        root = resolve_symbol(session, args["name"])
        root_location = root.get("location") or {}
        root_target = to_relative(root_location)
        root_position = position_of(root)
        if not root_target or not root_position:
            raise ToolError(f"symbol {args['name']!r} has no usable location")

        seen: set[str] = set()
        queries = 0
        lines = [f"Blast radius of {describe(root)} (depth {depth}):"]

        def expand(file: str, line: int, column: int, level: int, prefix: str) -> None:
            nonlocal queries
            if level > depth or queries >= budget:
                return
            queries += 1
            try:
                references = session.server.request_referencing_symbols(
                    file, line, column, include_file_symbols=True
                )
            except Exception as exc:  # noqa: BLE001
                lines.append(f"{prefix}(query failed: {type(exc).__name__})")
                return
            for reference in references:
                symbol = getattr(reference, "symbol", None) or {}
                location = symbol.get("location") or {}
                path = to_relative(location)
                if not path:
                    continue
                key = f"{path}:{symbol.get('name')}:{symbol.get('kind')}"
                if key in seen:
                    continue
                seen.add(key)
                lines.append(
                    f"{prefix}{symbol.get('name')} ({kind_of(symbol)}) — {path}:{reference.line + 1}"
                )
                position = position_of(symbol)
                if position and level < depth:
                    expand(path, position[0], position[1], level + 1, prefix + "  ")

        expand(root_target, root_position[0], root_position[1], 1, "  ")
        if len(lines) == 1:
            return (
                f"Nothing references {describe(root)} — it is a leaf, or it is reached "
                "only by reflection, DI or configuration, which references cannot see."
            )
        lines.append(f"\n{len(seen)} distinct symbol(s) affected, {queries} query(ies).")
        if queries >= budget:
            lines.append("Budget reached — the real radius is larger than shown.")
        return "\n".join(lines)

    if name == "rename_symbol":
        new_name = args["new_name"]
        candidate = resolve_symbol(session, args["name"])
        location = candidate.get("location") or {}
        target = to_relative(location)
        position = position_of(candidate)
        if not target or not position:
            raise ToolError(f"symbol {args['name']!r} has no usable location")

        edit = session.server.request_rename_symbol_edit(
            target, position[0], position[1], new_name
        )
        changes = (edit or {}).get("changes") or {}
        if not changes and (edit or {}).get("documentChanges"):
            for entry in edit["documentChanges"]:
                uri = (entry.get("textDocument") or {}).get("uri")
                if uri:
                    changes[uri] = entry.get("edits") or []
        if not changes:
            raise ToolError(
                f"the language server produced no edits renaming {args['name']!r} to "
                f"{new_name!r} — it may consider the rename illegal here"
            )

        total = sum(len(v) for v in changes.values())
        summary = [
            f"Rename {describe(candidate)} to {new_name!r}: "
            f"{total} edit(s) across {len(changes)} file(s)."
        ]
        for uri, edits in sorted(changes.items()):
            path = to_relative({"uri": uri}) or uri
            rows = sorted(((e.get("range") or {}).get("start") or {}).get("line", 0) for e in edits)
            summary.append(f"  {path} — {', '.join(f'L{r + 1}' for r in rows[:12])}"
                           + (f" (+{len(rows) - 12} more)" if len(rows) > 12 else ""))

        if not args.get("apply"):
            summary.append(
                "\nDry run — nothing written. Re-run with apply=true to perform it. "
                "Review the file list first: a rename touching unexpected files usually "
                "means the symbol resolved to something other than what you meant."
            )
            return "\n".join(summary)

        applied, unchanged, failed = 0, 0, []
        for uri, edits in changes.items():
            path = to_relative({"uri": uri})
            if not path:
                summary.append(f"  skipped (outside the repo): {uri}")
                continue
            try:
                if write_edits(session.root, path, edits):
                    applied += 1
                else:
                    unchanged += 1
            except OSError as exc:
                failed.append(f"  {path}: {exc}")

        # Report what happened on disk, not what was attempted. The previous
        # implementation announced success unconditionally while writing
        # nothing, which is the worst possible failure for a tool an agent
        # trusts enough to skip re-reading the file afterwards.
        summary.append(f"\nWritten: {applied} file(s).")
        if unchanged:
            summary.append(
                f"{unchanged} file(s) were already identical — no bytes changed."
            )
        if failed:
            summary.append("Failed to write:\n" + "\n".join(failed))
            raise ToolError("\n".join(summary))
        if not applied and not unchanged:
            raise ToolError("\n".join(summary + ["Nothing was written."]))
        summary.append(
            "Run check on an affected file to confirm the project still builds."
        )
        return "\n".join(summary)

    if name == "find_implementations":
        limit = int(args.get("limit", 50))
        if not type(session.server).supports_implementation_request():
            raise ToolError(
                f"the {session.language} language server does not support "
                "textDocument/implementation"
            )
        # Try each declaration, as find_references does: asking an interface's
        # constructor for implementations returns nothing, which is not the same
        # as the interface having none.
        for candidate in candidates_for(session, args["name"]):
            location = candidate.get("location") or {}
            target = to_relative(location)
            position = position_of(candidate)
            if not target or not position:
                continue
            impls = session.server.request_implementation(target, position[0], position[1]) or []
            if impls:
                lines = [
                    f"{len(impls)} implementation(s) of {args['name']!r} "
                    f"(resolved to {describe(candidate)}):"
                ]
                lines += [f"  {location_of(i)}" for i in impls[:limit]]
                if len(impls) > limit:
                    lines.append(f"  … and {len(impls) - limit} more")
                return "\n".join(lines)
        return (
            f"No implementations of {args['name']!r} found. If it is a concrete class "
            "rather than an interface or virtual member, that is expected."
        )

    raise ToolError(f"unknown tool: {name}")


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def serve(session: LanguageServerSession) -> None:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            request = json.loads(raw)
        except json.JSONDecodeError as exc:
            log(f"bad JSON: {exc}")
            continue

        method = request.get("method")
        request_id = request.get("id")

        if method == "initialize":
            send({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            })
        elif method in ("notifications/initialized", "initialized"):
            continue
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = request.get("params") or {}
            tool_name = params.get("name", "")
            arguments = params.get("arguments") or {}
            try:
                text = call_tool(session, tool_name, arguments)
                is_error = False
            except ToolError as exc:
                text, is_error = str(exc), True
            except Exception as exc:  # noqa: BLE001
                log(f"tool {tool_name} failed: {exc}")
                text, is_error = f"{type(exc).__name__}: {exc}", True
            send({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": text}], "isError": is_error},
            })
        elif method == "shutdown":
            send({"jsonrpc": "2.0", "id": request_id, "result": {}})
        elif method == "exit":
            break
        elif request_id is not None:
            send({
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            })


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "repo_root",
        nargs="?",
        default=None,
        help="Repository to analyse. Defaults to the working directory, which is "
             "how an MCP client launches us — one user-scoped config entry then "
             "works in every project without per-project arguments.",
    )
    parser.add_argument("--language", default=None)
    args = parser.parse_args()

    root = Path(args.repo_root or os.getcwd()).resolve()
    if not root.is_dir():
        log(f"not a directory: {root}")
        return 2

    globals()["_ROOT"] = root
    try:
        language = args.language or detect_language(root)
    except RuntimeError as exc:
        # An MCP client renders a traceback as "failed to connect", which tells
        # the user nothing about what to change. Match how the not-a-directory
        # case already reports itself.
        log(str(exc))
        log("Pass --language to force one, or start the server in a repository "
            "that contains source files.")
        return 2
    session = LanguageServerSession(root, language)
    log(f"ready — repo={root} language={language} (server starts on first tool call)")
    try:
        serve(session)
    except KeyboardInterrupt:
        pass
    finally:
        session.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
