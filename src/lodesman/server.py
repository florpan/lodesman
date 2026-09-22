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
import difflib
import fnmatch
import hashlib
import json
import os
import re
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from solidlsp import SolidLanguageServer
from solidlsp.ls_config import LanguageServerConfig, LanguageServerId
from solidlsp.settings import SolidLSPSettings

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "lodesman"
SERVER_VERSION = "0.6.0"

# Probe for the index-readiness gate: short, and matches something in any repo.
WARM_PROBE = "a"

def _detected_languages() -> tuple[LanguageServerId, ...]:
    """
    Every language detection may pick, ordered so the tie-break is deterministic.

    Taken from SolidLSP rather than curated here. Detection deliberately covers
    more than the set verified in CI: refusing to start on a language nobody has
    tested is strictly worse than trying it, as the .jsx case showed, where an
    ordinary React project was reported as containing no source files at all.
    What is *verified* is a documentation claim, and belongs in the README.

    Excluded are the experimental servers and the non-programming ones —
    markdown, json, yaml and friends. A server exists for those, but they have
    almost no cross-file symbol structure, so the tools would answer nothing
    useful while making every repository look multi-language.

    Sorted by SolidLSP's own priority, highest first, because several languages
    claim the same extensions. That field exists for exactly this: Vue and
    Svelte are supersets of TypeScript and rank below it, so `.ts` belongs to
    TypeScript while `.vue` still belongs to Vue. Name is the final tie-break,
    so the result does not depend on enum declaration order.
    """
    return tuple(sorted(
        LanguageServerId.iter_all(
            include_experimental=False, include_non_programming_languages=False
        ),
        key=lambda lid: (-lid.get_priority(), lid.value),
    ))


DETECTED_LANGUAGES = _detected_languages()


def _extension_languages() -> dict[str, str]:
    """
    Extension -> language, taken from each server's own idea of what it handles.

    Hand-listing these was wrong in a way that only showed up on real projects.
    The list used to be .ts, .tsx and .js, while tsserver actually handles
    twelve extensions — so a React codebase written in .jsx contained no
    "recognized source files" and the server exited rather than starting.
    Deriving the map means a server that learns a new extension is picked up
    without anyone remembering to.
    """
    mapping: dict[str, str] = {}
    for language_id in DETECTED_LANGUAGES:
        for extension in language_id.get_source_fn_matcher().file_extensions:
            # First wins, and the ordering above makes that the higher-priority
            # language. Equal priority falls back to name, which is arbitrary
            # but stable — .m being claimed by cpp rather than matlab is a
            # coin-toss nobody has a better answer for.
            mapping.setdefault(extension, language_id.value)
    return mapping


EXTENSION_LANGUAGES = _extension_languages()

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


DiskState = dict[str, tuple[int, int]]

# LSP FileChangeType.
FILE_CREATED, FILE_CHANGED, FILE_DELETED = 1, 2, 3


# Files that define what a project *is*, per language. A server rebuilds its
# project model when told they changed, and only then.
PROJECT_FILE_SUFFIXES = {"csharp": (".csproj", ".props", ".targets")}

# Written by a NuGet restore, inside obj/ — which the walk skips, so it is
# looked for beside each .csproj instead.
RESTORE_ASSETS = os.path.join("obj", "project.assets.json")


PROJECT_RELOAD_TIMEOUT = 60


def is_project_file(path: str, language: str) -> bool:
    return path.endswith((*PROJECT_FILE_SUFFIXES.get(language, ()), os.sep + RESTORE_ASSETS))


def scan_sources(root: Path, language: str) -> DiskState:
    """
    Modification time and size of every source and project file of `language`
    under `root`.

    Keyed by absolute path. os.scandir rather than os.walk plus a stat per file:
    on Windows a DirEntry carries its stat from the directory listing, so this
    costs one system call per directory rather than one per file, and it runs
    before every tool call.
    """
    project_suffixes = PROJECT_FILE_SUFFIXES.get(language, ())
    state: DiskState = {}
    pending = [str(root)]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue  # vanished or unreadable mid-scan: its files read as deleted
        subdirs = [e.name for e in entries if e.is_dir(follow_symlinks=False)]
        pending += [os.path.join(directory, name) for name in walkable(subdirs)]
        for entry in entries:
            is_source = EXTENSION_LANGUAGES.get(os.path.splitext(entry.name)[1]) == language
            is_project = entry.name.endswith(project_suffixes) if project_suffixes else False
            if not (is_source or is_project):
                continue
            try:
                if not entry.is_file():
                    continue
                stat = entry.stat()
            except OSError:
                continue
            state[entry.path] = (stat.st_mtime_ns, stat.st_size)
            if entry.name.endswith(".csproj"):
                assets = os.path.join(directory, RESTORE_ASSETS)
                try:
                    stat = os.stat(assets)
                except OSError:
                    continue  # not restored yet; appearing later reads as created
                state[assets] = (stat.st_mtime_ns, stat.st_size)
    return state


def disk_changes(before: DiskState, after: DiskState) -> list[tuple[str, int]]:
    """
    What changed between two scans, as (absolute path, LSP FileChangeType).

    Size is compared as well as mtime because a write inside the filesystem's
    timestamp resolution leaves the mtime unchanged, and an edit that renames a
    symbol almost always changes the length.
    """
    changes = [(path, FILE_DELETED) for path in before if path not in after]
    for path, signature in after.items():
        previous = before.get(path)
        if previous is None:
            changes.append((path, FILE_CREATED))
        elif previous != signature:
            changes.append((path, FILE_CHANGED))
    return sorted(changes)


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


# How many languages one repository will serve at once. A repository can
# legitimately contain a dozen; starting a language server for each would cost
# gigabytes and minutes for languages nobody is going to ask about.
MAX_LANGUAGES = 4

# Languages below this share of the recognised source files are treated as
# incidental — a build script, a single .py tool in a C# repository — rather
# than as part of the project.
MINOR_LANGUAGE_SHARE = 0.05


def detect_languages(root: Path) -> list[tuple[str, int]]:
    """
    Every language worth serving in this repository, most significant first.

    Ordered by file count, then by SolidLSP's own priority, which exists to
    break exactly this tie: Vue and Svelte are supersets of TypeScript and rank
    below it so that the larger language only wins when it matches more
    strongly.

    Returns pairs of (language, file count). Empty if nothing was recognised.
    """
    counts: Counter[str] = Counter()
    for _dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = walkable(dirnames)
        for name in filenames:
            language = EXTENSION_LANGUAGES.get(Path(name).suffix)
            if language:
                counts[language] += 1
    if not counts:
        return []

    total = sum(counts.values())
    priority = {lid.value: lid.get_priority() for lid in DETECTED_LANGUAGES}
    ranked = sorted(
        counts.items(),
        key=lambda item: (item[1], priority.get(item[0], 0)),
        reverse=True,
    )
    # The majority language is always served, however lopsided the split; the
    # rest have to clear the noise floor.
    keep = [ranked[0]]
    keep += [(lang, n) for lang, n in ranked[1:] if n / total >= MINOR_LANGUAGE_SHARE]
    return keep[:MAX_LANGUAGES]


def detect_language(root: Path) -> str:
    """The single most significant language. Raises if nothing is recognised."""
    found = detect_languages(root)
    if not found:
        raise RuntimeError(f"no recognized source files under {root}")
    return found[0][0]


# jdtls leaves methods out of workspace/symbol unless told otherwise, so no
# Java method could be named: "fromJson" found nothing in gson. False is
# jdtls's own default, which SolidLSP copies. Measured on gson (264 files):
# startup 14.7s -> 15.5s, memory +4-6%, and a distinctive method name costs
# nothing — but a common one does: "get" returns 1743 symbols in 1.1s. The
# likeliest reason for the default. Set False to back off.
JAVA_METHODS_IN_SYMBOL_SEARCH = True


def adjust_initialize_params(server: SolidLanguageServer, adjust, what: str) -> None:
    """
    Change one setting in this server instance's initialize parameters.

    Wraps the instance's _create_initialize_params rather than editing the
    vendored tree (workdocs/VENDORING.md, preference 2). Written against
    SolidLSP at oraios/serena 704e8c3d. If that method is renamed or the
    parameters are reshaped, this logs and leaves the server at its default,
    which is how it behaved before the adjustment existed.
    """
    build = server._create_initialize_params

    def adjusted():
        params = build()
        try:
            adjust(params)
        except (KeyError, TypeError, AttributeError) as exc:
            log(f"could not {what} (server default kept): {exc!r}")
        return params

    server._create_initialize_params = adjusted


def include_java_methods(server: SolidLanguageServer) -> None:
    """Make jdtls index method declarations for workspace/symbol."""
    def adjust(params: dict) -> None:
        java = params["initializationOptions"]["settings"]["java"]
        java.setdefault("symbols", {})["includeSourceMethodDeclarations"] = True
    adjust_initialize_params(server, adjust, "enable Java method search")


def rename_without_aliases(server: SolidLanguageServer) -> None:
    """
    Make tsserver rename a symbol, rather than rename it and re-export it under
    the old name.

    With tsserver's default `providePrefixAndSuffixTextForRename`, renaming
    `BookUpdateDto` to `BookPatch` turned a re-export into
    `BookPatch as BookUpdateDto`, so every importer of that module kept the old
    name. Measured on CalibreManager, 3 renames out of 3: two agents noticed and
    spent ~20 calls cleaning up by hand, and one reported "renamed everywhere"
    over the half-done rename. typescript-language-server passes
    initializationOptions.preferences through to tsserver.
    """
    def adjust(params: dict) -> None:
        options = params.setdefault("initializationOptions", {})
        options.setdefault("preferences", {})["providePrefixAndSuffixTextForRename"] = False
    adjust_initialize_params(server, adjust, "turn off tsserver's rename aliases")


class LanguageServerSession:
    """Owns one language server, started on demand and kept warm."""

    def __init__(self, root: Path, language: str) -> None:
        self.root = root
        self.language = language
        self._server: SolidLanguageServer | None = None
        self._context = None
        self._anchors: list = []
        self._disk: DiskState = {}
        self._projects_reloaded = threading.Event()
        self._lock = threading.Lock()

    @property
    def server(self) -> SolidLanguageServer:
        with self._lock:
            if self._server is None:
                log(f"starting {self.language} language server at {self.root} …")
                # Before the server reads anything, so an edit made while it
                # indexes is reported by the first sync instead of being lost
                # between the server's read and ours.
                self._disk = scan_sources(self.root, self.language)
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
                if self.language == "java" and JAVA_METHODS_IN_SYMBOL_SEARCH:
                    include_java_methods(server)
                if self.language == "typescript":
                    rename_without_aliases(server)
                self._context = server.start_server_context()
                self._context.__enter__()
                server.server.on_any_notification(self._observe)
                self._anchor_project(server)
                self._warm_index(server)
                self._server = server
                # Roslyn restores a never-restored project itself while it
                # starts, then keeps answering from the project it loaded
                # before the restore — with no compiler diagnostics at all —
                # until told the restore output exists. This sync is what tells
                # it, and waits for the reload.
                self.sync_with_disk()
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
        `find_symbol` and `find_references` without a
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

    # Roslyn's log line when a project reload triggered by a watched-file change
    # has finished. It sends no notification for this; the log is the signal.
    RELOAD_DONE = "Completed (re)load of all projects"

    def _observe(self, method: str, params) -> None:
        """Runs alongside SolidLSP's own handlers for every server notification."""
        if method == "window/logMessage" and self.RELOAD_DONE in str((params or {}).get("message", "")):
            self._projects_reloaded.set()

    def sync_with_disk(self) -> int:
        """
        Tell the language server about every source file that changed on disk
        since it last heard. Returns how many did.

        Every server config SolidLSP ships declares the client capability
        `didChangeWatchedFiles`, which tells the server the client watches the
        disk on its behalf, so the server does not. Serena keeps that promise
        with its own poller (LanguageServerManager.poll_and_notify), which is
        outside the vendored tree, and until this nothing here kept it. Measured
        before the fix: pyright never saw a file edited on disk nor our own
        rename_symbol's writes, and Roslyn never saw a disk edit. tsserver
        watches the disk itself, but unreliably: the same rename was visible
        at once on some runs and not within 20 s on others.

        This is what makes an agent's plain Edit tool safe to use alongside
        this server: the next question re-syncs before it is asked.

        Files the server holds open are different. For an open document the
        editor's buffer is the truth, so servers ignore watcher events for it;
        those get a didChange with the new contents instead, which SolidLSP's
        file buffer sends when it sees the file's mtime move.
        """
        if self._server is None:
            return 0  # starting the server scans; nothing to catch up on
        started = time.perf_counter()
        current = scan_sources(self.root, self.language)
        elapsed = time.perf_counter() - started
        # 40-120 ms on ordinary repositories, measured; seconds on a directory
        # holding many of them. Said out loud, because it is paid on every call.
        if elapsed > 0.5:
            log(f"disk scan took {elapsed:.1f}s ({len(current)} files); every tool call pays this")
        changes = disk_changes(self._disk, current)
        self._disk = current
        if not changes:
            return 0

        server = self._server
        open_buffers = server.open_file_buffers
        watched, reopen = [], []
        projects_changed = False
        for path, change in changes:
            uri = Path(path).as_uri()
            if is_project_file(path, self.language):
                # Not a document: opening a .csproj as one would hand it to
                # the compiler. The watcher event is the whole signal.
                projects_changed = True
                watched.append({"uri": uri, "type": change})
                continue
            buffer = open_buffers.get(uri)
            if buffer is not None and change != FILE_DELETED:
                try:
                    buffer.ensure_open_in_ls()
                except Exception as exc:  # noqa: BLE001 — one file must not stop the rest
                    log(f"could not resend open file {path}: {exc}")
                continue
            watched.append({"uri": uri, "type": change})
            if change != FILE_DELETED:
                reopen.append(os.path.relpath(path, self.root).replace(os.sep, "/"))

        if projects_changed:
            self._projects_reloaded.clear()
        if watched:
            server.server.notify.did_change_watched_files({"changes": watched})
        # Answering before the reload finishes answers from the old project
        # model. Measured at about a second for a small project.
        if (projects_changed and self.language == "csharp"
                and not self._projects_reloaded.wait(PROJECT_RELOAD_TIMEOUT)):
            log(f"project reload not confirmed within {PROJECT_RELOAD_TIMEOUT}s; continuing")
        # The watcher notification alone is not enough: typescript-language-
        # server ignores it and relies on tsserver's own disk watcher, which
        # picked up a rename within a second on some runs and not within 20 s
        # on others, whatever we sent. Opening the file hands the server its
        # new text directly, and on close the server reads it from disk, which
        # by then holds that same text. One open/close per changed file, not per file.
        for relative in reopen:
            try:
                with server.open_file(relative):
                    pass
            except Exception as exc:  # noqa: BLE001 — one file must not stop the rest
                log(f"could not reopen {relative}: {exc}")
        log(f"synced {len(changes)} changed file(s) with the {self.language} server")
        return len(changes)

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


class LanguageServerPool:
    """
    One repository, one language server per language it actually contains.

    A repository is rarely one language. Serving only the majority one means a
    question about the frontend of a .NET solution returns nothing, which is
    indistinguishable from the symbol not existing — the answer this project
    treats as never trustworthy.

    Servers are started individually and only when something needs them. A
    language server is hundreds of megabytes and tens of seconds; starting one
    per detected language up front would spend both on languages nobody asks
    about. So detection decides what *may* be served, and the first question
    that needs a given language decides whether it actually starts.
    """

    def __init__(self, root: Path, languages: list[str]) -> None:
        self.root = root
        self.languages = languages
        self._sessions: dict[str, LanguageServerSession] = {}
        self._lock = threading.Lock()

    def session(self, language: str) -> LanguageServerSession:
        with self._lock:
            if language not in self._sessions:
                self._sessions[language] = LanguageServerSession(self.root, language)
            return self._sessions[language]

    def started(self) -> list[str]:
        return [lang for lang, s in self._sessions.items() if s._server is not None]

    def language_for_file(self, target: str) -> str | None:
        """Which of our languages owns this file, by extension."""
        language = EXTENSION_LANGUAGES.get(Path(target).suffix)
        return language if language in self.languages else None

    def ordered(self, preferred: str | None = None) -> list[LanguageServerSession]:
        """
        Sessions to try, most likely first.

        Already-running servers come before cold ones: if two languages could
        answer, asking the one that is already warm costs nothing, while
        starting the other costs a download.
        """
        order = list(self.languages)
        if preferred and preferred in order:
            order.remove(preferred)
            order.insert(0, preferred)
        else:
            running = self.started()
            order.sort(key=lambda lang: lang not in running)
        return [self.session(lang) for lang in order]

    def shutdown(self) -> None:
        for session in self._sessions.values():
            session.shutdown()


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


SYMBOL = {
    "type": "string",
    "description": "Address, e.g. Type.member or path/File.cs:Name",
}

TOOLS = [
    {
        "name": "project_info",
        "description": (
            "Orientation, without starting a language server: the repository's "
            "languages, project files, and where its source files are."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "find_symbol",
        "description": (
            "Find declarations, each with its address. By name: exact names in every "
            "language (partial=true adds names containing it). By file only (a file, "
            "folder or glob): the outline of those files."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name, or Type.member"},
                "file": {"type": "string", "description": "File, folder or glob to search in"},
                "language": {"type": "string"},
                "kind": {"type": "string", "description": "e.g. class, method, property, function"},
                "partial": {"type": "boolean"},
                "depth": {"type": "integer", "description": "Outline: 1 = top level only"},
                "limit": {"type": "integer", "description": "Default 25 by name, 200 outline"},
            },
        },
    },
    {
        "name": "find_references",
        "description": (
            "Every usage of a symbol as the compiler sees it, with its line of code and "
            "the declaration it is in. Use before renaming, changing a signature or deleting."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": SYMBOL,
                "context": {"type": "integer", "description": "Lines around each; default 0 (3 for one)"},
                "include_code": {"type": "boolean", "description": "Default true"},
                "limit": {"type": "integer", "description": "Default 50"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "get_symbol_body",
        "description": (
            "Source of one declaration, without reading its file. Exactly the text "
            "replace_symbol_body replaces."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"symbol": SYMBOL},
            "required": ["symbol"],
        },
    },
    {
        "name": "get_file_diagnostics",
        "description": "Compiler errors and warnings for one file, without a build.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Repo-relative path"},
                "limit": {"type": "integer", "description": "Default 40"},
                "severity": {
                    "type": "integer",
                    "description": "Lowest to report: 1 error, 2 warning (default), 3 info, 4 hint",
                },
            },
            "required": ["file"],
        },
    },
    {
        "name": "blast_radius",
        "description": "What references a symbol, and what references those, to a depth.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": SYMBOL,
                "depth": {"type": "integer", "description": "1-4, default 2"},
                "max_queries": {"type": "integer", "description": "Default 60"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "rename_symbol",
        "description": (
            "Rename symbols and every reference to them via the compiler. Give one "
            "address or a list (e.g. the backend and frontend declarations). Previews "
            "unless apply=true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "anyOf": [SYMBOL, {"type": "array", "items": {"type": "string"}}],
                },
                "new_name": {"type": "string"},
                "apply": {"type": "boolean"},
            },
            "required": ["symbol", "new_name"],
        },
    },
    {
        "name": "find_implementations",
        "description": "Implementations of an interface or abstract member, or overrides of a virtual one.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": SYMBOL,
                "limit": {"type": "integer", "description": "Default 50"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "type_definition",
        "description": (
            "Code of the type of a field or property, or of a local or parameter "
            "addressed as file:line:col."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"symbol": SYMBOL},
            "required": ["symbol"],
        },
    },
    {
        "name": "call_hierarchy",
        "description": "Callers (incoming) or callees (outgoing) of a function, to a depth.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": SYMBOL,
                "direction": {"type": "string", "enum": ["incoming", "outgoing"]},
                "depth": {"type": "integer", "description": "1-4, default 1"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "type_hierarchy",
        "description": "Supertypes and subtypes of a type.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": SYMBOL,
                "direction": {"type": "string", "enum": ["both", "supertypes", "subtypes"]},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "replace_symbol_body",
        "description": (
            "Replace a whole declaration, signature included. Returns the diff and "
            "the file's errors after. For a small change inside it, a text edit is cheaper."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": SYMBOL,
                "body": {"type": "string", "description": "The complete new declaration"},
            },
            "required": ["symbol", "body"],
        },
    },
    {
        "name": "insert_at_symbol",
        "description": (
            "Insert code on its own lines before a declaration (above its doc comment) "
            "or after it. Content is verbatim. Returns the diff and the file's errors after."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": SYMBOL,
                "position": {"type": "string", "enum": ["before", "after"]},
                "content": {"type": "string"},
            },
            "required": ["symbol", "position", "content"],
        },
    },
    {
        "name": "safe_delete_symbol",
        "description": "Delete a declaration with its doc comment, only if nothing uses it.",
        "inputSchema": {
            "type": "object",
            "properties": {"symbol": SYMBOL},
            "required": ["symbol"],
        },
    },
    {
        "name": "code_action",
        "description": (
            "The language server's quick fixes and refactorings for a line (add import, "
            "implement interface, extract method...). No title: list them. With title: "
            "preview the diff; apply=true writes it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "at": {"type": "string", "description": "file:line or file:line-line"},
                "title": {"type": "string", "description": "As listed; a unique fragment is enough"},
                "kind": {"type": "string", "description": "e.g. quickfix, refactor, source"},
                "apply": {"type": "boolean"},
            },
            "required": ["at"],
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


# LSP SymbolKind values.
KIND_NAMES = {
    1: "file", 4: "package",
    2: "module", 3: "namespace", 5: "class", 6: "method", 7: "property",
    8: "field", 9: "constructor", 10: "enum", 11: "interface", 12: "function",
    13: "variable", 14: "constant", 22: "enum member", 23: "struct", 24: "event",
}
CALLABLE_KINDS = {6, 9, 12}
# Containers an outline's depth counts from inside of: a C# file-scoped
# namespace is the top level of every file, so "depth 1" meant only it (c6).
NAMESPACE_KINDS = {2, 3, 4}


def kind_of(hit: dict) -> str:
    return KIND_NAMES.get(hit.get("kind"), f"kind={hit.get('kind')}")


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


# Files that mark the root of a project, for languages whose server searches
# one project at a time.
PROJECT_MARKERS = {
    "typescript": ("tsconfig.json", "jsconfig.json"),
    "vue": ("tsconfig.json",),
    "svelte": ("svelte.config.js", "tsconfig.json"),
}


def project_roots(session: LanguageServerSession) -> list[str]:
    """Directories below the root that look like separate projects."""
    markers = PROJECT_MARKERS.get(session.language)
    if not markers:
        return []
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(session.root):
        dirnames[:] = walkable(dirnames)
        if Path(dirpath) == session.root:
            continue
        if any(marker in filenames for marker in markers):
            found.append(os.path.relpath(dirpath, session.root).replace(os.sep, "/"))
            dirnames[:] = []  # a project's subdirectories belong to it
    return found


def sibling_project_caveat(session: LanguageServerSession) -> str:
    """
    Why an empty answer here may be a false negative rather than an absence.

    tsserver's workspace symbol search covers one project at a time — the one
    it most recently saw a file from. Measured on a repository holding a `web/`
    and an `admin/`: asking about a symbol in `web` answered, opening any file
    in `admin` made that symbol *stop* resolving, and the `admin` one start.
    Anchoring a file in each does not help; the search follows the active
    project.

    So a repository with several projects in such a language can report a
    symbol as missing when it plainly exists. That is the one answer this tool
    must never give silently, and since it cannot be fixed from here, it is at
    least declared.
    """
    roots = project_roots(session)
    if len(roots) < 2:
        return ""
    listed = ", ".join(roots[:4]) + ("…" if len(roots) > 4 else "")
    return (
        f"\n\nTreat this as inconclusive. This repository holds {len(roots)} "
        f"separate {session.language} projects ({listed}), and that language "
        "server searches one project at a time, so a symbol in another project "
        "reports as missing. To search them reliably, run one server per "
        "project root rather than one at the repository root."
    )


NEVER_RESTORED = "never restored"


def restore_state(root: Path, target: str) -> tuple[str | None, str | None]:
    """
    (the .csproj that owns `target`, what is wrong with its package restore).

    The project is None when no .csproj owns the file; the problem is None when
    it restored cleanly, and NEVER_RESTORED when there is no restore output.

    Two failures look alike from the diagnostics and mean opposite things:

    * **Never restored.** Roslyn reports no compiler diagnostics at all for a
      project it loaded without obj/project.assets.json, so "no errors" is not
      an answer. It restores such a project itself on startup and
      sync_with_disk makes it reload, so by the time get_file_diagnostics runs this means
      the restore could not run.
    * **Restore failed.** An unreachable feed still writes project.assets.json,
      and records the failure in its `logs`: NU1301, level Error, "Unable to
      load the service index" (reproduced 2026-09-21 with a dead feed).
      Types from the missing packages then report as missing — CalibreManager
      showed 42 such errors, none real.

    A clean restore logs no errors, and then CS0246 is a genuine missing
    using, not a symptom. Guessing from the proportion of missing-type errors,
    as get_file_diagnostics (then called check) used to, told agents not to trust a real, trivially fixable error.

    The owning project is the nearest .csproj walking up from the file. One
    that moves its intermediate output (BaseIntermediateOutputPath, the
    artifacts layout) reads as never restored, which is why the warning says
    "may".
    """
    directory = (root / target).parent
    while True:
        projects = sorted(directory.glob("*.csproj"))
        if projects:
            project = str(projects[0].relative_to(root)).replace(os.sep, "/")
            assets = directory / "obj" / "project.assets.json"
            if not assets.is_file():
                return project, NEVER_RESTORED
            try:
                logs = json.loads(assets.read_text(encoding="utf-8-sig")).get("logs") or []
            except (OSError, ValueError, AttributeError):
                return project, "its restore output could not be read"
            errors = [entry for entry in logs if isinstance(entry, dict)
                      and str(entry.get("level", "")).lower() == "error"]
            if not errors:
                return project, None
            first = errors[0]
            more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
            return project, f"its package restore failed: {first.get('code')} {first.get('message')}{more}"
        if directory == root or root not in directory.parents:
            return None, None
        directory = directory.parent


class Unsupported(Exception):
    """The language server does not implement the method at all."""


def lsp_request(session: LanguageServerSession, file: str, method: str, params: dict):
    """
    Send a request SolidLSP has no wrapper for, with `file` open for its duration.

    A server that lacks the method answers -32601, which is a statement about the
    server rather than about the code, so it is raised as Unsupported and never
    confused with an empty answer.
    """
    server = session.server
    try:
        with server.open_file(file):
            return getattr(server.server.send, method)(params)
    except Exception as exc:  # re-raised unless it is -32601
        if "-32601" in f"{exc} {getattr(exc, 'cause', '')}":
            raise Unsupported(method) from exc
        raise


def position_params(session: LanguageServerSession, file: str, line: int, column: int) -> dict:
    return {
        "textDocument": {"uri": session.server._resolve_file_uri(file)},
        "position": {"line": line, "character": column},
    }


def utf16_units(text: str) -> int:
    """Length of `text` in UTF-16 code units, which is how LSP counts columns."""
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text)


def file_lines(session: LanguageServerSession, file: str) -> list[str]:
    with open(session.root / file, encoding="utf-8", newline="") as handle:
        return handle.read().splitlines()


def source_line(session: LanguageServerSession, file: str | None, line: int) -> str:
    """One line of source, stripped, for showing a call site or declaration inline."""
    if not file:
        return ""
    try:
        return file_lines(session, file)[line].strip()
    except (OSError, IndexError, UnicodeDecodeError):
        return ""


def targets_of(result) -> list[tuple[str | None, int, str]]:
    """
    (file, line, uri) for each Location or LocationLink in a definition-style
    answer. `file` is None for a target outside the repository.
    """
    items = result if isinstance(result, list) else [result] if result else []
    found = []
    for item in items:
        if not isinstance(item, dict):
            continue
        uri = item.get("targetUri") or item.get("uri") or ""
        rng = item.get("targetSelectionRange") or item.get("range") or {}
        line = (rng.get("start") or {}).get("line", 0)
        found.append((to_relative({"uri": uri}) if uri else None, line, uri))
    return found


def hierarchy_item(item: dict) -> tuple[str | None, int, str]:
    """(file, line, label) for a CallHierarchyItem or TypeHierarchyItem."""
    path = to_relative({"uri": item.get("uri", "")})
    rng = item.get("selectionRange") or item.get("range") or {}
    line = (rng.get("start") or {}).get("line", 0)
    detail = f" — {item['detail']}" if item.get("detail") else ""
    label = f"{item.get('name')} ({kind_of(item)}){detail} — {path or item.get('uri')}:{line + 1}"
    return path, line, label


def edit_steps(edit: dict) -> list[tuple]:
    """
    A WorkspaceEdit as ordered steps with repo-relative paths:
    ("edit", path, text_edits) or ("rename", old_path, new_path).

    Servers use either shape: `changes`, or `documentChanges`, which can also
    create, rename and delete files. Renames are real refactorings — jdtls
    renames a class by moving its file too, since Java requires the names to
    match — so they are applied, in the order given. Dropping one would leave
    `class VoidStore` in NullStore.java and report success, which is what
    happened before this was written. Creation and deletion are refused out
    loud rather than skipped, for the same reason.

    Anything that would touch a path outside the repository is refused before
    a single byte is written.
    """
    def inside(uri: str) -> str:
        path = to_relative({"uri": uri})
        if not path:
            raise ToolError(f"this change would touch a file outside the repository: {uri}")
        return path

    document_changes = (edit or {}).get("documentChanges")
    if not document_changes:
        return [("edit", inside(uri), edits)
                for uri, edits in ((edit or {}).get("changes") or {}).items()]
    # Never both: a server may send the same edits in each shape, and the spec
    # makes documentChanges the one that counts. Merging would apply them twice.
    steps: list[tuple] = []
    for entry in document_changes:
        kind = entry.get("kind")
        if kind == "rename":
            steps.append(("rename", inside(entry["oldUri"]), inside(entry["newUri"])))
        elif kind in ("create", "delete"):
            raise ToolError(
                f"this change would {kind} a file, which lodesman does not apply; "
                "only edits and renames are supported"
            )
        else:
            uri = (entry.get("textDocument") or {}).get("uri")
            if uri:
                steps.append(("edit", inside(uri), entry.get("edits") or []))
    return steps


def describe_steps(steps: list[tuple]) -> list[str]:
    """One row per file, with the lines each edit starts on."""
    rows = []
    for step in steps:
        if step[0] == "rename":
            rows.append(f"  renames {step[1]} → {step[2]}")
            continue
        lines = sorted(((e.get("range") or {}).get("start") or {}).get("line", 0) for e in step[2])
        more = f" (+{len(lines) - 12} more)" if len(lines) > 12 else ""
        rows.append(f"  {step[1]} — {', '.join(f'L{n + 1}' for n in lines[:12])}{more}")
    return rows


def preview_steps(root: Path, steps: list[tuple], max_lines: int = 80) -> str:
    """The unified diff the steps would produce, computed without writing anything."""
    def read(path: str) -> str:
        with open(root / path, encoding="utf-8", newline="") as handle:
            return handle.read()

    texts: dict[str, str] = {}
    origin: dict[str, str] = {}  # current path -> the path it started at
    for step in steps:
        if step[0] == "rename":
            _, old, new = step
            texts[new] = texts.pop(old) if old in texts else read(old)
            origin[new] = origin.pop(old, old)
        else:
            _, path, edits = step
            if path not in texts:
                texts[path] = read(path)
                origin.setdefault(path, path)
            texts[path] = apply_edits_to_text(texts[path], edits)

    diff: list[str] = []
    for path, text in texts.items():
        diff += difflib.unified_diff(read(origin[path]).splitlines(), text.splitlines(),
                                     f"a/{origin[path]}", f"b/{path}", n=2, lineterm="")
    return "\n".join(diff[:max_lines]) + ("\n  … diff truncated" if len(diff) > max_lines else "")


def apply_steps(root: Path, steps: list[tuple]) -> tuple[int, int, list[str]]:
    """
    Perform the steps on disk. Returns (files changed, files already identical,
    failures).

    Renames are checked before anything is written: a rename onto an existing
    file, or of a file that is not there, stops the whole change up front
    rather than halfway through.
    """
    for step in steps:
        if step[0] == "rename":
            _, old, new = step
            if (root / new).exists():
                raise ToolError(f"nothing written: renaming {old} would overwrite {new}")
            if not (root / old).exists() and not any(
                s[0] == "rename" and s[2] == old for s in steps
            ):
                raise ToolError(f"nothing written: {old} does not exist to be renamed")

    changed, unchanged, failed = 0, 0, []
    for step in steps:
        try:
            if step[0] == "rename":
                _, old, new = step
                (root / new).parent.mkdir(parents=True, exist_ok=True)
                os.rename(root / old, root / new)
                changed += 1
            elif write_edits(root, step[1], step[2]):
                changed += 1
            else:
                unchanged += 1
        except OSError as exc:
            failed.append(f"  {step[1]}: {exc}")
    return changed, unchanged, failed


def bare_name(name: str) -> str:
    """A symbol name without the signature some servers append: 'Get(string)' -> 'Get'."""
    return re.split(r"[(<]", name or "", maxsplit=1)[0].strip()


def name_path(name: str) -> list[str]:
    """'MemoryStore.get', 'MemoryStore/get' and 'MemoryStore::get' all mean the same."""
    return [part for part in re.split(r"::|\.|/", name or "") if part]


def split_symbol_name(name: str) -> tuple[list[str], str, bool]:
    """
    A symbol name as servers spell it, as (qualifiers, name, is_impl_block).

    Servers put more than the name in `name`, each differently:

    * gopls names a method by its receiver, and lists it at the top level of
      the outline rather than under its type: "(*MemoryStore).Get",
      "(NullStore).Get" (gopls/internal/golang/symbols.go). SolidLSP's
      wrapper strips that to "Get" before it reaches us, so for Go outlines
      go_receiver reads the type from the source instead; this form is
      handled for servers reached without that wrapper. gopls's workspace
      search qualifies differently: "MemoryStore.Get".
    * rust-analyzer groups methods under their impl block, named
      "impl Record" or "impl Store for MemoryStore". Such a block is not a
      declaration of its own but stands for its type as a container — so
      `Record.scaled` finds the method in `impl Record`.
    * Roslyn and jdtls append signatures: "Get(string)", "scaled(int) : int".
    """
    name = (name or "").strip()
    receiver = re.match(r"^\(\*?([\w.]+)(?:\[[^\]]*\])?\)\.(\w+)", name)
    if receiver:
        return [receiver.group(1).split(".")[-1]], receiver.group(2), False
    impl = re.match(r"^impl(?:<.*?>)?\s+(?:.+?\s+for\s+)?([\w:]+)", name)
    if impl:
        return [], impl.group(1).split("::")[-1], True
    parts = name_path(bare_name(name))
    return parts[:-1], (parts[-1] if parts else ""), False


GO_RECEIVER = re.compile(r"^\s*func\s*\(\s*(?:\w+\s+)?\*?(\w+)")


def go_receiver(declaration_line: str) -> str | None:
    """
    The receiver type of a Go method, from its declaration line.

    gopls names methods "(*MemoryStore).Get", but SolidLSP's gopls wrapper
    strips that to "Get" (gopls.py, _normalize_symbol_name), so by the time
    the outline reaches us a method has lost its type. The declaration line
    still has it, and Go's syntax for it is fixed: `func (m *MemoryStore) Get`.
    """
    match = GO_RECEIVER.match(declaration_line)
    return match.group(1) if match else None


def containers(symbol: dict) -> list[str]:
    """Names of the symbols enclosing `symbol`, outermost first."""
    chain = []
    parent = symbol.get("parent")
    while parent:
        qualifiers, leaf, _impl = split_symbol_name(parent.get("name", ""))
        chain += [leaf, *qualifiers[::-1]]
        parent = parent.get("parent")
    return chain[::-1]


def range_contains(outer: dict, inner: dict) -> bool:
    """Whether LSP range `outer` strictly encloses `inner`."""
    def at(position: dict) -> tuple[int, int]:
        return position["line"], position["character"]
    return outer != inner and at(outer["start"]) <= at(inner["start"]) and at(inner["end"]) <= at(outer["end"])


# Line prefixes that belong to the declaration below them: doc comments,
# decorators, annotations, attributes. Language servers disagree about whether
# a declaration's range includes them, so they are found by looking upwards —
# otherwise inserting "before" a method splits it from its doc comment, and
# deleting it leaves the comment behind describing nothing.
_C_COMMENTS = ("//", "/*", "*")
LEADING_PREFIXES = {
    "python": ("#", "@"),
    "ruby": ("#",),
    "csharp": (*_C_COMMENTS, "["),
    "rust": (*_C_COMMENTS, "#["),
    "php": (*_C_COMMENTS, "#["),
}
DEFAULT_LEADING_PREFIXES = (*_C_COMMENTS, "@")


def leading_block_start(lines: list[str], start: int, language: str) -> int:
    """The first line of the doc comment/attributes directly above line `start`."""
    prefixes = LEADING_PREFIXES.get(language, DEFAULT_LEADING_PREFIXES)
    while start > 0:
        above = lines[start - 1].strip()
        if not above or not above.startswith(prefixes):
            break
        start -= 1
    return start


def last_line_of(symbol_range: dict) -> int:
    """The last line a range actually occupies: some servers end it at column 0 of the next."""
    end = symbol_range["end"]
    if end["character"] == 0 and end["line"] > symbol_range["start"]["line"]:
        return end["line"] - 1
    return end["line"]


def as_block(text: str, eol: str) -> str:
    """Content in the file's line endings, without trailing newlines."""
    return text.replace("\r\n", "\n").rstrip("\n").replace("\n", eol)


def deletion_span(lines: list[str], symbol_range: dict, first: int) -> dict:
    """
    The range to delete for a declaration whose leading block starts at `first`.

    Whole lines when the declaration has them to itself, so no indentation or
    empty line is left behind, and one of the blank lines around it when it sat
    between two — deleting a method should not leave a double gap. Otherwise
    the exact range, for a declaration sharing its line with other code.
    """
    start, last = symbol_range["start"], last_line_of(symbol_range)
    head = lines[start["line"]][:utf16_index(lines[start["line"]], start["character"])]
    end_line = lines[last]
    tail = end_line[utf16_index(end_line, symbol_range["end"]["character"]):] \
        if last == symbol_range["end"]["line"] else ""
    if head.strip() or tail.strip(" \t;,"):
        return symbol_range
    stop = last + 1
    if stop >= len(lines):
        # The last thing in the file: take the blank lines above it too, or the
        # file ends in a run of empty lines.
        while first > 0 and not lines[first - 1].strip():
            first -= 1
    elif 0 < first and not lines[first - 1].strip() and not lines[stop].strip():
        stop += 1
    return {"start": {"line": first, "character": 0}, "end": {"line": stop, "character": 0}}


# -- Symbol addresses ----------------------------------------------------------
#
# Every tool that acts on code takes one `symbol` string, the address, and every
# tool prints symbols as addresses the next call can copy. The design, and why a
# bare name was not enough, is in workdocs/ADDRESSING.md.
#
#     [<where>:]<name path>[#n | (types)]      a declaration
#     <file>:<line>[:<column>]                 a position
#
# An address is resolved afresh on every call, from the files as they are, and
# must mean exactly one thing: there is no "most likely" pick, because that is
# how a TypeScript BookDto came back for the C# one.

@dataclass(frozen=True)
class Address:
    text: str
    where: str | None
    path: tuple[str, ...]  # the name path; empty for a position
    overload: str | None   # "#2" or "(int,string)"
    line: int | None       # 1-based
    column: int | None     # 1-based, in characters


def parse_address(text: str) -> Address:
    text = (text or "").strip()
    if not text:
        raise ToolError("symbol is required: a name, Type.member, or file:Name, file:line")
    # The first colon that is not part of "::" (C++, Rust, Ruby name paths), and
    # not a Windows drive's: "C:/repo/a.cs:Name" is a file and a name.
    where, rest = None, text
    skip = 2 if re.match(r"[A-Za-z]:[/\\]", text) else 0
    for i, char in enumerate(text):
        if i < skip:
            continue
        if char == ":" and text[i + 1:i + 2] != ":" and (i == 0 or text[i - 1] != ":"):
            # rstrip only: a leading slash means an absolute path, and dropping
            # it would turn "/etc/passwd" into a path inside the repository.
            where, rest = text[:i].strip().replace("\\", "/").rstrip("/") or None, text[i + 1:].strip()
            break
    position = re.fullmatch(r"(\d+)(?::(\d+))?", rest)
    if position:
        if not where:
            raise ToolError(f"{text!r}: a line needs a file, as in path/to/file.cs:42")
        return Address(text, where, (), None, int(position.group(1)),
                       int(position.group(2)) if position.group(2) else None)
    overload = None
    number = re.search(r"#(\d+)$", rest)
    if number:
        overload, rest = number.group(0), rest[:number.start()]
    elif rest.endswith(")") and "(" in rest:
        start = rest.index("(")
        overload, rest = re.sub(r"\s+", "", rest[start:]), rest[:start]
    path = tuple(name_path(rest))
    if not path:
        raise ToolError(f"{text!r} names no symbol")
    return Address(text, where, path, overload, None, None)


@dataclass(eq=False)
class Declaration:
    """One declaration in one file's outline, with the names enclosing it."""
    symbol: dict
    chain: list[str]  # enclosing names, outermost first, ending with its own


def file_declarations(session: LanguageServerSession, file: str) -> list[Declaration]:
    """Every declaration in `file`, from the language server's outline of it."""
    source = file_lines(session, file) if session.language == "go" else []
    found = []
    for symbol in symbols_of(session.server.request_document_symbols(file)):
        own, leaf, impl_block = split_symbol_name(symbol.get("name", ""))
        if impl_block or not leaf or not symbol.get("range"):
            continue
        chain = [*containers(symbol), *own]
        if source and not own:
            start = symbol["range"]["start"]["line"]
            receiver = go_receiver(source[start]) if start < len(source) else None
            if receiver:
                chain.append(receiver)
        found.append(Declaration(symbol, [*chain, leaf]))
    return found


def parameters_of(symbol: dict) -> str | None:
    """The parameter list a server reports, whitespace removed: '(string,int)'."""
    for text in (symbol.get("name") or "", symbol.get("detail") or ""):
        match = re.search(r"\(([^()]*(?:\([^()]*\)[^()]*)*)\)", text)
        if match:
            return "(" + re.sub(r"\s+", "", match.group(1)) + ")"
    return None


def match_declarations(declarations: list[Declaration], path: tuple[str, ...] | list[str],
                       overload: str | None = None) -> list[Declaration]:
    """
    The declarations in one file that `path` (and `overload`) mean.

    Qualifiers match the end of the enclosing chain, so `Server.GetUser` finds
    `App.Server.GetUser`. A match nested in another match is dropped, so
    "Record" means the class rather than its constructor of the same name;
    `Record.Record` means the constructor.
    """
    path = list(path)
    hits = [d for d in declarations if d.chain[-len(path):] == path]
    hits = [d for d in hits
            if not any(o is not d and range_contains(o.symbol["range"], d.symbol["range"]) for o in hits)]
    hits.sort(key=lambda d: (d.symbol["range"]["start"]["line"], d.symbol["range"]["start"]["character"]))
    if overload and overload.startswith("#"):
        index = int(overload[1:]) - 1
        return [hits[index]] if 0 <= index < len(hits) else []
    if overload:
        return [d for d in hits if parameters_of(d.symbol) == overload]
    return hits


def address_in_file(declaration: Declaration, declarations: list[Declaration]) -> str:
    """
    The shortest name path, plus `#n` for an overload, that means exactly this
    declaration within its file: what an address puts after "file:".
    """
    chain = declaration.chain
    for length in range(1, len(chain) + 1):
        path = chain[-length:]
        hits = match_declarations(declarations, path)
        if not any(h is declaration for h in hits):
            continue  # a constructor, hidden behind its class at this length
        if len(hits) == 1:
            return ".".join(path)
        if all(h.chain == chain for h in hits):  # only overloads of itself remain
            return ".".join(path) + f"#{next(i for i, h in enumerate(hits) if h is declaration) + 1}"
    return ".".join(chain)


def innermost(declarations: list[Declaration], line: int, character: int | None = None) -> Declaration | None:
    """The deepest declaration whose range contains the 0-based position."""
    def contains(symbol_range: dict) -> bool:
        start, end = symbol_range["start"], symbol_range["end"]
        if not start["line"] <= line <= end["line"]:
            return False
        if character is None:
            return True
        return ((line, character) >= (start["line"], start["character"])
                and (line, character) <= (end["line"], end["character"]))
    inside = [d for d in declarations if contains(d.symbol["range"])]
    return min(inside, key=lambda d: (d.symbol["range"]["end"]["line"] - d.symbol["range"]["start"]["line"],
                                      -d.symbol["range"]["start"]["line"]), default=None)


class Addresser:
    """Prints locations as addresses, reading each file's outline once."""

    def __init__(self, session: LanguageServerSession):
        self.session = session
        self._outlines: dict[str, list[Declaration]] = {}

    def declarations(self, file: str) -> list[Declaration]:
        if file not in self._outlines:
            try:
                self._outlines[file] = file_declarations(self.session, file)
            except Exception:  # noqa: BLE001 — an unreadable file prints as a plain location
                self._outlines[file] = []
        return self._outlines[file]

    def of(self, file: str, declaration: Declaration) -> str:
        return f"{file}:{address_in_file(declaration, self.declarations(file))}"

    def at(self, file: str | None, line: int) -> str:
        """The declaration enclosing a 0-based line, as an address; else file:line."""
        if not file:
            return "?"
        found = innermost(self.declarations(file), line)
        return self.of(file, found) if found else f"{file}:{line + 1}"


@dataclass
class Target:
    """What one address resolved to."""
    session: LanguageServerSession
    file: str
    line: int                        # 0-based position that LSP requests use
    column: int                      # 0-based, UTF-16 units
    declaration: Declaration | None  # None for a position outside any declaration
    address: str                     # printable, copyable

    @property
    def kind(self) -> str:
        return kind_of(self.declaration.symbol) if self.declaration else "position"


def require_declaration(target: Target) -> Declaration:
    if target.declaration is None:
        raise ToolError(f"{target.address} is not inside any declaration")
    return target.declaration


RESOLVE_LISTED = 20


def source_files_under(pool: LanguageServerPool, where: str) -> list[tuple[str, str]]:
    """(language, file) for every source file `where` names: a file, a directory or a glob."""
    if not any(char in where for char in "*?["):
        # repo_file refuses anything outside the repository and makes an
        # absolute path inside it relative.
        where = repo_file(where)
    elif where.startswith("/") or re.match(r"[A-Za-z]:", where) or ".." in where.split("/"):
        raise ToolError(f"{where!r} reaches outside the repository ({pool.root}); a pattern "
                        "must be relative to it")
    full = pool.root / where
    if full.is_file():
        file = repo_file(where)
        language = pool.language_for_file(file)
        if language is None:
            raise ToolError(f"{where!r} is not a file this server handles; it serves "
                            f"{', '.join(pool.languages)}")
        return [(language, file)]
    found = []
    for language in pool.languages:
        for absolute in scan_sources(pool.root, language):
            if is_project_file(absolute, language):
                continue
            file = os.path.relpath(absolute, pool.root).replace(os.sep, "/")
            if (file.startswith(where + "/") if full.is_dir() else fnmatch.fnmatch(file, where)):
                found.append((language, file))
    if not found:
        raise ToolError(f"{where!r} matches no source file of {', '.join(pool.languages)}")
    return sorted(found, key=lambda pair: pair[1])


def files_declaring(session: LanguageServerSession, leaf: str) -> list[str]:
    """Files that may declare `leaf`: the workspace index's, else those mentioning it."""
    files = sorted({
        f for f in (to_relative(h.get("location") or {}) for h in workspace_hits(session, leaf)
                    if split_symbol_name(h.get("name", ""))[1] == leaf)
        # The index can name a file that is gone: after a `git mv`, Roslyn
        # still listed the old path (A/B run c3). Such a file is skipped.
        if f and (session.root / f).is_file()
    })
    # The workspace index is the server's slowest-moving view (sourcekit-lsp
    # lost NullStore moments after finding it, in CI), and an agent often edits
    # what it has only just written. A file's outline comes from the file.
    return files or files_mentioning(session, leaf)


def candidate_files(pool: LanguageServerPool, address: Address,
                    language: str | None = None) -> list[tuple[LanguageServerSession, str]]:
    if address.where:
        pairs = source_files_under(pool, address.where)
        return [(pool.session(lang), f) for lang, f in pairs if language in (None, lang)]
    sessions = [pool.session(language)] if language else pool.ordered()
    return [(s, f) for s in sessions for f in files_declaring(s, address.path[-1])]


def resolve_all(pool: LanguageServerPool, address: Address, language: str | None = None
                ) -> list[tuple[LanguageServerSession, str, Declaration, list[Declaration]]]:
    """Every declaration a (non-position) address matches, in every file it covers."""
    found = []
    checked: set[tuple[int, str]] = set()
    for session, file in candidate_files(pool, address, language):
        checked.add((id(session), file))
        declarations = file_declarations(session, file)
        for declaration in match_declarations(declarations, address.path, address.overload):
            found.append((session, file, declaration, declarations))
    if found or address.where:
        return found
    # A stale index can name files that no longer declare it, which leaves no
    # empty answer for files_declaring to fall back from; so fall back here.
    for session in [pool.session(language)] if language else pool.ordered():
        for file in files_mentioning(session, address.path[-1]):
            if (id(session), file) in checked:
                continue
            declarations = file_declarations(session, file)
            for declaration in match_declarations(declarations, address.path, address.overload):
                found.append((session, file, declaration, declarations))
    return found


def resolve(pool: LanguageServerPool, text: str) -> Target:
    """The one thing an address means, or a ToolError listing what it could mean."""
    address = parse_address(text)
    if address.line is not None:
        file = repo_file(address.where)
        language = pool.language_for_file(file)
        if language is None:
            raise ToolError(f"{address.where!r} is not a file this server handles; it serves "
                            f"{', '.join(pool.languages)}")
        session = pool.session(language)
        session.sync_with_disk()
        lines = file_lines(session, file)
        line = address.line - 1
        if not 0 <= line < len(lines):
            raise ToolError(f"{file} has {len(lines)} lines; there is no line {address.line}")
        declarations = file_declarations(session, file)
        if address.column is None:
            found = innermost(declarations, line)
            if found is None:
                raise ToolError(f"no declaration contains {file}:{address.line}")
            start_line, start_char = position_of(found.symbol)
            return Target(session, file, start_line, start_char, found,
                          f"{file}:{address_in_file(found, declarations)}")
        column = utf16_units(lines[line][:address.column - 1])
        found = innermost(declarations, line, column)
        return Target(session, file, line, column, found, f"{file}:{address.line}:{address.column}")

    for session in ([pool.session(lang) for lang, _ in source_files_under(pool, address.where)]
                    if address.where else pool.ordered()):
        session.sync_with_disk()
    matches = resolve_all(pool, address)
    if not matches:
        scope = f"under {address.where!r}" if address.where else f"in {', '.join(pool.languages)}"
        raise ToolError(f"No declaration matches {text!r} {scope}. find_symbol with partial=true "
                        "lists names containing it.")
    if len(matches) > 1:
        rows = [f"  {file}:{address_in_file(d, decls)}  {kind_of(d.symbol)} "
                f"L{d.symbol['range']['start']['line'] + 1}"
                for _session, file, d, decls in matches[:RESOLVE_LISTED]]
        more = (f"\n  … and {len(matches) - RESOLVE_LISTED} more. Too broad to act on: narrow it "
                "with a file or folder, or a qualified name.") if len(matches) > RESOLVE_LISTED else ""
        raise ToolError(f"{text!r} matches {len(matches)} declarations; use one of these "
                        f"addresses:\n" + "\n".join(rows) + more)
    session, file, declaration, declarations = matches[0]
    line, column = name_position(session, file, declaration)
    return Target(session, file, line, column, declaration,
                  f"{file}:{address_in_file(declaration, declarations)}")


def name_position(session: LanguageServerSession, file: str, declaration: Declaration) -> tuple[int, int]:
    """
    Where the declaration's own name is, which is where LSP requests must point.

    A server's selectionRange is supposed to cover the name, and most do, but
    ruby-lsp points at the `class` keyword — and textDocument/implementation
    there answers nothing, which cost the Ruby type hierarchy its subtypes.
    So the name is looked for on that line, and the server's position kept
    only if nothing better is found.
    """
    line, column = position_of(declaration.symbol)
    leaf = declaration.chain[-1]
    try:
        text = file_lines(session, file)[line]
    except (OSError, IndexError, UnicodeDecodeError):
        return line, column
    if text[utf16_index(text, column):].startswith(leaf):
        return line, column
    found = re.search(rf"(?<![\w$]){re.escape(leaf)}(?![\w$])", text)
    return (line, utf16_units(text[:found.start()])) if found else (line, column)


def file_diagnostics(session: LanguageServerSession, target: str, min_severity: int) -> list[dict]:
    """Diagnostics for one file: pulled where the server supports it, else published."""
    try:
        return session.server.request_text_document_diagnostics(
            target, min_severity=min_severity
        ) or []
    except Exception:  # noqa: BLE001 — not every server supports pull diagnostics
        return session.server.request_published_text_document_diagnostics(
            target, min_severity=min_severity
        ) or []


def call_tool(session: LanguageServerSession, name: str, args: dict,
              targets: list[Target] | None = None) -> str:
    """
    Answer one tool call. `targets` are the resolved `symbol` addresses, for
    the tools that take one; file tools and find_symbol are routed elsewhere.
    """
    # Every question is answered from what is on disk now, not from what was
    # there when the server last looked.
    session.sync_with_disk()
    target = targets[0] if targets else None

    if name == "find_references":
        limit = int(args.get("limit", 50))
        refs = session.server.request_references(target.file, target.line, target.column) or []
        if not refs:
            return (
                f"No references to {target.address} found. Treat this as inconclusive "
                "rather than as proof it is unused: reflection, DI and configuration "
                "are invisible to references."
            )

        with_code = args.get("include_code", True)
        # The reference line alone by default. In the A/B test, 15 references
        # with six lines of surroundings each came to 7.5K chars — mostly
        # response headers and catch blocks — carried in every later request.
        # One reference is different: its surroundings are cheap, and often
        # the reason for asking.
        context = int(args.get("context", 3 if len(refs) == 1 else 0))
        by_file: dict[str, list[int]] = {}
        for ref in refs:
            location = ref.get("location") or ref
            path = to_relative(location) or location.get("uri") or "?"
            rng = location.get("range") or {}
            ln = (rng.get("start") or {}).get("line")
            by_file.setdefault(path, []).append(ln if isinstance(ln, int) else 0)

        # Each reference under the declaration that contains it, so the answer
        # says which method uses it, as an address the next call can take.
        addresser = Addresser(session)
        lines = [f"{len(refs)} reference(s) to {target.address} in {len(by_file)} file(s):"]
        shown_refs = 0
        for path, line_numbers in sorted(by_file.items())[:limit]:
            if not with_code:
                rendered = ", ".join(f"L{n + 1}" for n in sorted(line_numbers)[:10])
                more = f" (+{len(line_numbers) - 10} more)" if len(line_numbers) > 10 else ""
                lines.append(f"  {path} — {rendered}{more}")
                continue
            lines.append(f"  {path}")
            for ln in sorted(set(line_numbers)):
                if shown_refs >= limit:
                    break
                enclosing = innermost(addresser.declarations(path), ln)
                within = (address_in_file(enclosing, addresser.declarations(path))
                          if enclosing else "(top level)")
                if context:
                    lines.append(f"    {within} L{ln + 1}:")
                    lines.append(peek(session, path, ln, context, context))
                else:
                    lines.append(f"    {within} L{ln + 1}: {source_line(session, path, ln)}")
                shown_refs += 1
        if len(by_file) > limit or shown_refs < len(refs):
            lines.append(f"  … truncated at {limit}: {len(refs)} references in {len(by_file)} files. "
                         "Raise limit to see all.")
        return "\n".join(lines)

    if name == "get_symbol_body":
        # Cut from the declaration's own range, so the text is exactly what
        # replace_symbol_body replaces. It once asked SolidLSP for the symbol
        # containing the workspace-symbol position, which for a one-line C#
        # member (`public int Scaled(int f) => …;`) returned the whole class.
        declaration = require_declaration(target)
        with open(session.root / target.file, encoding="utf-8", newline="") as handle:
            text = handle.read()
        start = declaration.symbol["range"]["start"]["line"]
        return (f"{target.address} ({target.kind}, L{start + 1})\n\n"
                + range_text(text, declaration.symbol["range"]))

    if name == "get_file_diagnostics":
        target = repo_file(args["file"])
        # LSP severity: 1 error, 2 warning, 3 info, 4 hint. Default to errors and
        # warnings only — a file can carry dozens of style hints, and burying a
        # compile error under "use primary constructor" defeats the purpose.
        min_severity = int(args.get("severity", 2))
        diagnostics = file_diagnostics(session, target, min_severity)
        names = {1: "error", 2: "warning", 3: "info", 4: "hint"}
        project, problem = (restore_state(session.root, target)
                            if session.language == "csharp" else (None, None))
        if problem == NEVER_RESTORED:
            restore_warning = (
                f"  WARNING: {project} may never have been restored (no "
                "obj/project.assets.json). Roslyn reports no compiler errors at all for an "
                "unrestored project, so compile errors may be missing from this answer. "
                "Run `dotnet restore` (or build once) and check again."
            )
        elif problem:
            restore_warning = (
                f"  WARNING: {project}: {problem}. Types from packages that did not "
                "restore report as missing, so missing-type errors (CS0246, CS0234) may "
                "not be real. Fix the restore and check again before changing code."
            )
        else:
            restore_warning = None

        if not diagnostics:
            scope = names.get(min_severity, "issue")
            if restore_warning:
                return (
                    f"{target}: the language server reported no {scope}s, but that cannot "
                    f"be trusted here.\n{restore_warning}"
                )
            return f"{target}: no {scope}s or worse."
        diagnostics.sort(key=lambda d: (d.get("severity") or 9,
                                        ((d.get("range") or {}).get("start") or {}).get("line") or 0))
        counts = Counter(names.get(d.get("severity"), "?") for d in diagnostics)
        rendered = [f"{target}: " + ", ".join(f"{n} {k}(s)" for k, n in counts.items())]
        if restore_warning:
            rendered.append(restore_warning)
        elif session.language == "csharp" and project is None:
            # No .csproj owns the file, so the restore cannot be inspected.
            # Only then fall back to guessing from the mix of errors: a lost
            # restore reports every package type as missing (CalibreManager,
            # 42 phantom errors), but so does a plain missing using.
            broken_project_codes = {"CS8179", "CS0518", "CS0012", "CS1069", "CS0246", "CS0234"}
            suspect = sum(1 for d in diagnostics if str(d.get("code")) in broken_project_codes)
            if suspect and suspect >= len(diagnostics) / 2:
                rendered.append(
                    f"  WARNING: {suspect} of {len(diagnostics)} are missing-type/assembly "
                    "errors and no .csproj owns this file, so its restore cannot be "
                    "checked. That can mean the project did not load fully rather than "
                    "that the code is wrong. Verify with a real build."
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

    if name == "blast_radius":
        depth = max(1, min(int(args.get("depth", 2)), 4))
        budget = int(args.get("max_queries", 60))
        root_target, root_position = target.file, (target.line, target.column)
        addresser = Addresser(session)

        seen: set[str] = set()
        queries = 0
        lines = [f"Blast radius of {target.address} (depth {depth}):"]

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
                declared = position_of(symbol)
                where = addresser.at(path, declared[0]) if declared else f"{path}:?"
                lines.append(f"{prefix}{where}  {kind_of(symbol)}, uses it at L{reference.line + 1}")
                position = position_of(symbol)
                if position and level < depth:
                    expand(path, position[0], position[1], level + 1, prefix + "  ")

        expand(root_target, root_position[0], root_position[1], 1, "  ")
        if len(lines) == 1:
            return (
                f"Nothing references {target.address} — it is a leaf, or it is reached "
                "only by reflection, DI or configuration, which references cannot see."
            )
        lines.append(f"\n{len(seen)} distinct symbol(s) affected, {queries} query(ies).")
        if queries >= budget:
            lines.append("Budget reached — the real radius is larger than shown.")
        return "\n".join(lines)

    if name == "rename_symbol":
        return rename(targets, args)

    if name == "find_implementations":
        limit = int(args.get("limit", 50))
        if not type(session.server).supports_implementation_request():
            raise ToolError(
                f"the {session.language} language server does not support "
                "textDocument/implementation"
            )
        impls = session.server.request_implementation(target.file, target.line, target.column) or []
        if not impls:
            return (
                f"No implementations of {target.address} found. If it is a concrete class "
                "rather than an interface or virtual member, that is expected."
            )
        addresser = Addresser(session)
        lines = [f"{len(impls)} implementation(s) of {target.address}:"]
        for impl in impls[:limit]:
            location = impl.get("location") or impl
            start = ((location.get("range") or {}).get("start") or {}).get("line", 0)
            lines.append(f"  {addresser.at(to_relative(location), start)}")
        if len(impls) > limit:
            lines.append(f"  … truncated at {limit} of {len(impls)}. Raise limit to see all.")
        return "\n".join(lines)

    if name == "type_definition":
        try:
            result = lsp_request(session, target.file, "type_definition",
                                 position_params(session, target.file, target.line, target.column))
        except Unsupported:
            raise ToolError(
                f"the {session.language} language server does not support "
                "textDocument/typeDefinition"
            ) from None
        found = targets_of(result)
        if not found:
            return (
                f"No type definition for {target.address}. It may itself be a type, or "
                "have a type the server cannot resolve (dynamic code)."
            )
        addresser = Addresser(session)
        parts = [f"{target.address} is of type:"]
        for file, target_line, uri in found:
            if file:
                parts.append(f"  {addresser.at(file, target_line)}")
                parts.append(peek(session, file, target_line, 1, 6))
            else:
                parts.append(f"  {uri} (outside the repository)")
        return "\n".join(parts)

    if name == "call_hierarchy":
        direction = args.get("direction") or "incoming"
        if direction not in ("incoming", "outgoing"):
            raise ToolError("direction must be incoming or outgoing")
        depth = max(1, min(int(args.get("depth", 1)), 4))
        budget = 60
        attempts = [(target.file, target.line, target.column, target.address)]
        addresser = Addresser(session)

        def from_references(reason: str) -> str:
            # Incoming calls have an honest substitute: the symbols containing
            # each reference, which is what blast_radius walks. It over-reports
            # rather than under-reports — a reference that is not a call still
            # appears — which is the safe direction to be wrong in. An empty
            # result here is never "nothing calls it": in CI, intelephense
            # without a licence had neither calls nor references, and saying
            # "a leaf" there would be a confident false answer.
            text = call_tool(session, "blast_radius",
                             {"depth": depth, "max_queries": budget}, [target])
            if text.startswith("Nothing references"):
                return (
                    f"{reason} Its references found nothing either. Treat this as "
                    "inconclusive rather than as proof that nothing calls it."
                )
            return (
                f"{reason} Showing the symbols that reference it instead, which also "
                f"includes references that are not calls:\n\n{text}"
            )

        root_item, root_label = None, attempts[0][3]
        try:
            for path, line, column, label in attempts:
                items = lsp_request(session, path, "prepare_call_hierarchy",
                                    position_params(session, path, line, column)) or []
                if items:
                    root_item, root_label = items[0], label
                    break
        except Unsupported:
            if direction == "outgoing":
                raise ToolError(
                    f"the {session.language} language server has no call hierarchy, so "
                    "outgoing calls are not available. get_symbol_body shows the body, "
                    "and with it what it calls."
                ) from None
            return from_references(f"The {session.language} language server has no call hierarchy.")
        if root_item is None:
            if direction == "incoming":
                return from_references(f"The server offers no call hierarchy for {root_label}.")
            return (
                f"{root_label} is not something that is called — the server offers no "
                "call hierarchy for it. get_symbol_body shows what it contains."
            )

        method = "incoming_calls" if direction == "incoming" else "outgoing_calls"
        other_end = "from" if direction == "incoming" else "to"
        lines = [f"{'Callers' if direction == 'incoming' else 'Callees'} of {root_label}:"]
        seen: set[str] = set()
        queries = 0

        def expand(item: dict, level: int, indent: str) -> None:
            nonlocal queries
            if level > depth or queries >= budget:
                return
            queries += 1
            item_file = to_relative({"uri": item.get("uri", "")})
            if not item_file:
                return
            calls = lsp_request(session, item_file, method, {"item": item}) or []
            for call in calls:
                peer = call.get(other_end) or {}
                peer_file, peer_line, peer_label = hierarchy_item(peer)
                if peer_file:
                    peer_label = f"{addresser.at(peer_file, peer_line)}  {kind_of(peer)}"
                # Incoming call sites are in the caller's file; outgoing ones are
                # in the file being expanded.
                site_file = peer_file if direction == "incoming" else item_file
                sites = [((r.get("start") or {}).get("line", 0)) for r in call.get("fromRanges") or []]
                lines.append(f"{indent}{peer_label}")
                for site in sorted(set(sites))[:3]:
                    lines.append(f"{indent}    L{site + 1}: {source_line(session, site_file, site)}")
                key = f"{peer.get('uri')}:{peer_line}:{peer.get('name')}"
                if key not in seen:
                    seen.add(key)
                    expand(peer, level + 1, indent + "  ")

        expand(root_item, 1, "  ")
        if len(lines) == 1:
            if direction == "incoming":
                # Observed in CI: sourcekit-lsp prepared the item and then
                # reported no callers of a method that has one. "No callers"
                # from one source is not believed without asking another.
                return from_references(f"The call hierarchy reported no callers of {root_label}.")
            return f"No calls found in {root_label}."
        if queries >= budget:
            lines.append("Query budget reached — the real hierarchy is larger than shown.")
        return "\n".join(lines)

    if name == "type_hierarchy":
        direction = args.get("direction") or "both"
        if direction not in ("both", "supertypes", "subtypes"):
            raise ToolError("direction must be both, supertypes or subtypes")
        wanted = ["supertypes", "subtypes"] if direction == "both" else [direction]
        attempts = [(target.file, target.line, target.column, target.address)]
        path, line, column, label = attempts[0]
        addresser = Addresser(session)

        try:
            items = []
            for path, line, column, label in attempts:
                items = lsp_request(session, path, "prepare_type_hierarchy",
                                    position_params(session, path, line, column)) or []
                if items:
                    break
        except Unsupported:
            items = None
        if not items:
            path, line, column, label = attempts[0]  # the loop left the last one tried

        parts = [label]
        if items:
            for which in wanted:
                found = lsp_request(session, path, f"type_hierarchy_{which}",
                                    {"item": items[0]}) or []
                parts.append(f"\n{which}:")
                if not found:
                    parts.append("  (none)")
                for item in found:
                    item_file, item_line, item_label = hierarchy_item(item)
                    if item_file:
                        item_label = f"{addresser.at(item_file, item_line)}  {kind_of(item)}"
                    parts.append(f"  {item_label}")
                    code = source_line(session, item_file, item_line)
                    if code:
                        parts.append(f"      {code}")
            return "\n".join(parts)

        # No type hierarchy for this type: the server lacks the method, or —
        # observed in CI for ruby-lsp and intelephense — has it and returns
        # nothing for a plain interface. Substitute what can be had honestly
        # and say which answer came from where.
        why = ("this server has no type hierarchy" if items is None
               else "the server gave no type hierarchy for it")
        for which in wanted:
            if which == "supertypes":
                parts.append(f"\nsupertypes ({why}; the declaration as written, not resolved):")
                parts.append(peek(session, path, line, 0, 1))
                continue
            parts.append(f"\nsubtypes (from textDocument/implementation; {why}):")
            if not type(session.server).supports_implementation_request():
                parts.append("  unavailable: this server supports neither")
                continue
            implementations = session.server.request_implementation(path, line, column) or []
            if not implementations:
                parts.append("  (none found)")
            for implementation in implementations:
                file = to_relative(implementation.get("location") or implementation)
                start = ((implementation.get("range") or {}).get("start") or {}).get("line", 0)
                parts.append(f"  {addresser.at(file, start)}")
                code = source_line(session, file, start)
                if code:
                    parts.append(f"      {code}")
        return "\n".join(parts)

    if name == "code_action":
        return code_action(session, args)

    if name == "insert_at_symbol" and args.get("position") not in ("before", "after"):
        raise ToolError("position must be 'before' or 'after'")
    if name in ("replace_symbol_body", "insert_at_symbol", "safe_delete_symbol"):
        return edit_symbol(target, name, args)

    raise ToolError(f"unknown tool: {name}")


def rename(targets: list[Target], args: dict) -> str:
    """
    Rename every addressed symbol to one new name, each through its own
    language's server, as one change: previewed together, written together.
    """
    new_name = args["new_name"]
    steps: list[tuple] = []
    summary = []
    for target in targets:
        edit = target.session.server.request_rename_symbol_edit(
            target.file, target.line, target.column, new_name
        )
        these = edit_steps(edit)
        if not these:
            raise ToolError(
                f"the language server produced no edits renaming {target.address} to "
                f"{new_name!r}; it may consider the rename illegal there"
            )
        total = sum(len(s[2]) for s in these if s[0] == "edit")
        files = {s[1] for s in these if s[0] == "edit"}
        summary.append(f"Rename {target.address} ({target.kind}) to {new_name!r}: "
                       f"{total} edit(s) across {len(files)} file(s).")
        summary += describe_steps(these)
        steps += these

    if not args.get("apply"):
        summary.append("\nDry run — nothing written. Re-run with apply=true to perform it.")
        return "\n".join(summary)

    root = targets[0].session.root
    applied, unchanged, failed = apply_steps(root, steps)
    # Now rather than at the next call: the server reindexes asynchronously,
    # and this gives it the time the agent spends reading this answer.
    for session in {id(t.session): t.session for t in targets}.values():
        session.sync_with_disk()

    # Report what happened on disk, not what was attempted. An earlier version
    # announced success while writing nothing, which is the worst failure for
    # a tool an agent trusts enough to skip re-reading the file afterwards.
    summary.append(f"\nWritten: {applied} file(s).")
    if unchanged:
        summary.append(f"{unchanged} file(s) were already identical — no bytes changed.")
    if failed:
        summary.append("Failed to write:\n" + "\n".join(failed))
        raise ToolError("\n".join(summary))
    if not applied and not unchanged:
        raise ToolError("\n".join(summary + ["Nothing was written."]))

    # What the rename did not reach. A language server renames the symbol,
    # not comments or strings, and tsserver could leave `New as Old`
    # re-exports. In CalibreManager an agent read this tool's success as
    # "renamed everywhere" and said so, over ten comments and an alias still
    # naming the old type. So the answer lists what is left, in the renamed
    # symbols' own languages.
    left = []
    for target in targets:
        old = target.declaration.chain[-1] if target.declaration else ""
        if not old or old == new_name:
            continue
        pattern = re.compile(rf"(?<![\w$]){re.escape(old)}(?![\w$])")
        for relative in files_mentioning(target.session, old, limit=200):
            for number, text in enumerate(file_lines(target.session, relative)):
                if pattern.search(text):
                    left.append(f"  {relative}:{number + 1}: {text.strip()[:120]}")
    if left:
        summary.append(
            f"\nNot changed by the rename — {len(left)} line(s) still mention the old name "
            "(comments, strings, re-exports, or another symbol of that name). Update them "
            "if the rename should cover them:"
        )
        summary += left[:15] + ([f"  … and {len(left) - 15} more"] if len(left) > 15 else [])
    else:
        summary.append("No other mention of the old name is left in the source.")
    summary.append("Run get_file_diagnostics on an affected file to confirm it still compiles.")
    return "\n".join(summary)


def code_action(session: LanguageServerSession, args: dict) -> str:
    """List, preview or apply the language server's code actions for a line range."""
    target = repo_file(args["file"])
    lines = file_lines(session, target)
    first = int(args["line"]) - 1
    last = int(args.get("end_line") or args["line"]) - 1
    if not (0 <= first <= last < len(lines)):
        raise ToolError(f"{target} has {len(lines)} lines; {first + 1}-{last + 1} is out of range")
    span = {
        "start": {"line": first, "character": 0},
        "end": {"line": last, "character": utf16_units(lines[last])},
    }

    # Quick fixes hang off diagnostics: tsserver computes a fix only for the
    # error codes it is handed in the context, so they have to be sent along.
    diagnostics = [
        d for d in file_diagnostics(session, target, 4)
        if first <= ((d.get("range") or {}).get("start") or {}).get("line", -1) <= last
    ]
    kind = args.get("kind")
    context: dict = {"diagnostics": diagnostics, "triggerKind": 1}
    if kind:
        context["only"] = [kind]
    uri = session.server._resolve_file_uri(target)
    try:
        actions = lsp_request(session, target, "code_action",
                              {"textDocument": {"uri": uri}, "range": span, "context": context}) or []
    except Unsupported:
        raise ToolError(
            f"the {session.language} language server does not support textDocument/codeAction"
        ) from None
    # Filtered here as well: Roslyn ignores `only` and returns everything.
    if kind:
        actions = [a for a in actions
                   if (a.get("kind") or "") == kind or (a.get("kind") or "").startswith(kind + ".")]

    where = f"{target}:{first + 1}" + (f"-{last + 1}" if last != first else "")
    if not actions:
        return (
            f"No code actions at {where}"
            + (f" of kind {kind!r}" if kind else "")
            + (". Diagnostics there: " + "; ".join(d.get("message", "") for d in diagnostics)
               if diagnostics else ".")
        )

    def runs_a_command(action: dict) -> bool:
        # A bare Command, or an action that carries only a command: the change
        # happens inside the server, where it cannot be previewed.
        return isinstance(action.get("command"), str) or (
            not action.get("edit") and action.get("data") is None and action.get("command")
        )

    title = args.get("title")
    if not title:
        rows = [f"{len(actions)} code action(s) at {where}:"]
        shown: set[str] = set()
        for diagnostic in diagnostics:
            line_no = ((diagnostic.get("range") or {}).get("start") or {}).get("line", 0)
            code = f" [{diagnostic['code']}]" if diagnostic.get("code") else ""
            row = f"  for L{line_no + 1}{code}: {diagnostic.get('message')}"
            # Roslyn reports a missing type once per mention on the line.
            if row not in shown:
                shown.add(row)
                rows.append(row)
        for action in actions:
            note = "  (runs a server command; cannot be applied here)" if runs_a_command(action) else ""
            rows.append(f"  - {action.get('title')} [{action.get('kind') or 'command'}]{note}")
        rows.append("\nPass title to preview one; add apply=true to write it.")
        return "\n".join(rows)

    exact = [a for a in actions if a.get("title") == title]
    matches = exact or [a for a in actions if title.lower() in (a.get("title") or "").lower()]
    if len(matches) != 1:
        listed = "\n".join(f"  - {a.get('title')}" for a in (matches or actions))
        raise ToolError(
            f"{'no' if not matches else len(matches)} action(s) match {title!r} at {where}:\n{listed}"
        )
    action = matches[0]

    if not action.get("edit") and action.get("data") is not None:
        # Roslyn returns only a handle and computes the edit on resolve.
        action = lsp_request(session, target, "resolve_code_action", action) or action
    edit = action.get("edit")
    if not edit:
        raise ToolError(
            f"{action.get('title')!r} runs a command inside the language server rather "
            "than returning an edit, so it cannot be previewed or applied here"
        )

    steps = edit_steps(edit)
    total = sum(len(s[2]) for s in steps if s[0] == "edit")
    files = {s[1] for s in steps if s[0] == "edit"}
    summary = [f"{action.get('title')}: {total} edit(s) in {len(files)} file(s)."]
    summary += [row for row in describe_steps(steps) if row.lstrip().startswith("renames")]
    # The diff, not a list of line numbers: the change is small and the agent
    # has to judge it before applying, which a coordinate cannot support.
    summary.append(preview_steps(session.root, steps))

    if not args.get("apply"):
        summary.append("\nDry run — nothing written. Re-run with apply=true to perform it.")
        return "\n".join(summary)

    written, _unchanged, failed = apply_steps(session.root, steps)
    session.sync_with_disk()
    if failed:
        raise ToolError("\n".join(summary + ["Failed to write:"] + failed))
    if not written:
        raise ToolError("\n".join(summary + ["Nothing was written: the files already matched."]))
    summary.append(f"\nWritten: {written} file(s). Run get_file_diagnostics on them to confirm the result compiles.")
    return "\n".join(summary)


def files_mentioning(session: LanguageServerSession, word: str, limit: int = 25) -> list[str]:
    """Source files of the session's language containing `word` as a whole word."""
    pattern = re.compile(rf"(?<![\w$]){re.escape(word)}(?![\w$])")
    found = []
    for absolute in sorted(scan_sources(session.root, session.language)):
        if is_project_file(absolute, session.language):
            continue
        try:
            with open(absolute, encoding="utf-8", errors="replace") as handle:
                if not pattern.search(handle.read()):
                    continue
        except OSError:
            continue
        found.append(os.path.relpath(absolute, session.root).replace(os.sep, "/"))
        if len(found) >= limit:
            break
    return found


def range_text(text: str, symbol_range: dict) -> str:
    """The exact text an LSP range covers, measured the way apply_edits_to_text measures."""
    marker = "\x00"
    marked = apply_edits_to_text(text, [
        {"range": {"start": symbol_range["start"], "end": symbol_range["start"]}, "newText": marker},
        {"range": {"start": symbol_range["end"], "end": symbol_range["end"]}, "newText": marker},
    ])
    return marked.split(marker)[1] if marked.count(marker) == 2 else ""


def error_summary(errors: list[dict], limit: int = 5) -> list[str]:
    rows = []
    for item in errors[:limit]:
        line = ((item.get("range") or {}).get("start") or {}).get("line")
        code = f" [{item['code']}]" if item.get("code") else ""
        rows.append(f"  L{line + 1 if isinstance(line, int) else '?'}{code}: {item.get('message')}")
    if len(errors) > limit:
        rows.append(f"  … and {len(errors) - limit} more")
    return rows


def refuse_if_used(session: LanguageServerSession, path: str, symbol: dict,
                   qualified: str, first: int) -> None:
    """
    Raise unless nothing uses the declaration.

    Two independent sources must agree. The language server's references come
    first. Then a plain text search for the name, because an empty answer is
    exactly what a server gives when it cannot answer: unlicensed intelephense
    returns no references for a class used next door, and no server sees
    reflection, DI registration or a name in a string. Deleting on that
    silence would be the confident wrong answer this project exists to avoid.
    A common name is therefore refused more often than strictly needed; that
    is the safe direction to be wrong in, and the agent can still delete by
    hand.
    """
    last = last_line_of(symbol["range"])

    def inside(file: str | None, line: int) -> bool:
        return file == path and first <= line <= last

    position = position_of(symbol)
    references = session.server.request_references(path, *position) if position else []
    used = []
    for reference in references or []:
        location = reference.get("location") or reference
        file = to_relative(location)
        line = ((location.get("range") or {}).get("start") or {}).get("line")
        if isinstance(line, int) and not inside(file, line):
            used.append((file, line))
    if used:
        rows = [f"  {f}:{n + 1}: {source_line(session, f, n)}" for f, n in used[:10]]
        more = [f"  … and {len(used) - 10} more"] if len(used) > 10 else []
        raise ToolError("\n".join(
            [f"Not deleted: the language server reports {len(used)} reference(s) to {qualified}:",
             *rows, *more]
        ))

    leaf = split_symbol_name(symbol.get("name", ""))[1]
    if not leaf:
        raise ToolError(f"Not deleted: could not tell what {qualified} is called, so its "
                        "usages cannot be searched for.")
    pattern = re.compile(rf"(?<![\w$]){re.escape(leaf)}(?![\w$])")
    mentions = []
    for absolute in sorted(scan_sources(session.root, session.language)):
        if is_project_file(absolute, session.language):
            continue
        file = os.path.relpath(absolute, session.root).replace(os.sep, "/")
        try:
            with open(absolute, encoding="utf-8", errors="replace", newline="") as handle:
                contents = handle.read().splitlines()
        except OSError:
            continue
        mentions += [(file, n, text.strip()) for n, text in enumerate(contents)
                     if pattern.search(text) and not inside(file, n)]
    if mentions:
        rows = [f"  {f}:{n + 1}: {text}" for f, n, text in mentions[:10]]
        more = [f"  … and {len(mentions) - 10} more"] if len(mentions) > 10 else []
        reason = (
            f"Not deleted: the language server reports no references to {qualified}, but "
            f"the name appears {len(mentions)} more time(s) in the source. They may be "
            "usages it cannot see (reflection, DI, strings) or unrelated names — check "
            "them, and delete by hand if they are unrelated:"
        )
        raise ToolError("\n".join([reason, *rows, *more]))


def edit_symbol(target: Target, tool: str, args: dict) -> str:
    """replace_symbol_body, insert_at_symbol, safe_delete_symbol."""
    session, path = target.session, target.file
    symbol, qualified = require_declaration(target).symbol, target.address
    with open(session.root / path, encoding="utf-8", newline="") as handle:
        text = handle.read()
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    symbol_range = symbol["range"]
    first = leading_block_start(lines, symbol_range["start"]["line"], session.language)
    where = f"{qualified} (L{symbol_range['start']['line'] + 1})"

    if tool == "replace_symbol_body":
        # The range starts after the indentation, so only the first line loses
        # its own; the rest is the agent's, verbatim.
        body = args["body"].replace("\r\n", "\n").strip("\n").lstrip(" \t")
        edit = {"range": symbol_range, "newText": body.replace("\n", eol)}
        verb = f"Replaced {where}"
    elif tool == "insert_at_symbol" and args.get("position") == "after":
        last = last_line_of(symbol_range)
        at = {"line": last, "character": utf16_units(lines[last])}
        edit = {"range": {"start": at, "end": at}, "newText": eol + as_block(args["content"], eol)}
        verb = f"Inserted after {where}"
    elif tool == "insert_at_symbol":
        at = {"line": first, "character": 0}
        edit = {"range": {"start": at, "end": at}, "newText": as_block(args["content"], eol) + eol}
        verb = f"Inserted before {where}"
    else:
        refuse_if_used(session, path, symbol, qualified, first)
        edit = {"range": deletion_span(lines, symbol_range, first), "newText": ""}
        verb = f"Deleted {where}"

    # Errors before and after, so the answer says whether the edit caused them.
    try:
        before = len(file_diagnostics(session, path, 1))
    except Exception:  # noqa: BLE001 — a missing count only weakens the report
        before = None
    steps = [("edit", path, [edit])]
    diff = preview_steps(session.root, steps)
    changed, _unchanged, failed = apply_steps(session.root, steps)
    session.sync_with_disk()
    if failed:
        raise ToolError("\n".join([f"{verb}: failed to write", *failed]))
    if not changed:
        return f"{verb}: nothing changed — the file already had exactly this text."

    try:
        errors = file_diagnostics(session, path, 1)
        was = f" (was {before})" if before is not None and before != len(errors) else ""
        verdict = ([f"{path}: no errors after the edit{was}."] if not errors else
                   [f"{path}: {len(errors)} error(s) after the edit{was}:", *error_summary(errors)])
    except Exception as exc:  # noqa: BLE001
        verdict = [f"(could not read diagnostics afterwards: {exc}; run get_file_diagnostics)"]
    return "\n".join([f"{verb}.", diff, "", *verdict])


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


SYMBOL_TOOLS = {
    "find_references", "get_symbol_body", "blast_radius", "rename_symbol",
    "find_implementations", "type_definition", "call_hierarchy", "type_hierarchy",
    "replace_symbol_body", "insert_at_symbol", "safe_delete_symbol",
}


def dispatch(pool: LanguageServerPool, tool: str, args: dict) -> str:
    """
    Route one tool call to the language server that can answer it.

    `project_info` describes the binding and touches no server. A tool taking
    a `file` (or code_action's `at`) goes to that file's language. A tool
    taking a `symbol` resolves the address first, which decides the language:
    an address means exactly one declaration, so there is no trying languages
    in turn and taking the first answer.
    """
    if tool == "project_info":
        return describe_pool(pool)
    if tool == "find_symbol":
        return find_symbol_in(pool, args)

    if tool in SYMBOL_TOOLS:
        wanted = args.get("symbol")
        addresses = wanted if isinstance(wanted, list) else [wanted]
        if len(addresses) > 1 and tool != "rename_symbol":
            raise ToolError(f"{tool} takes one symbol address")
        if tool == "insert_at_symbol" and args.get("position") not in ("before", "after"):
            raise ToolError("position must be 'before' or 'after'")
        targets = [resolve(pool, address) for address in addresses]
        return call_tool(targets[0].session, tool, args, targets)

    if tool == "code_action":
        at = re.fullmatch(r"(.+):(\d+)(?:-(\d+))?", (args.get("at") or "").strip())
        if not at:
            raise ToolError("at must be file:line or file:line-line")
        args = {**args, "file": at.group(1), "line": int(at.group(2)),
                "end_line": int(at.group(3) or at.group(2))}

    target = args.get("file")
    if not target:
        raise ToolError(f"{tool} needs a file")
    language = pool.language_for_file(repo_file(target))
    if language is None:
        raise ToolError(
            f"{target!r} is not a file this server handles. It serves "
            f"{', '.join(pool.languages)} in {pool.root}."
        )
    session = pool.session(language)
    if tool == "code_action":
        session.sync_with_disk()
        return code_action(session, args)
    return call_tool(session, tool, args)


FIND_LIMIT = 25
OUTLINE_LIMIT = 200


def outline_rows(session: LanguageServerSession, file: str, depth: int | None,
                 kind: str | None) -> list[str]:
    """
    One file's declarations, nested by indentation, each as the part of its
    address that follows "file:". Namespaces and modules are left out and not
    counted by depth, and so are a function's locals: tsserver lists every
    variable in every function body, which is the file, not its outline.
    """
    declarations = file_declarations(session, file)
    rows = []
    for declaration in declarations:
        symbol = declaration.symbol
        if symbol.get("kind") in NAMESPACE_KINDS:
            continue
        ancestors, parent = [], symbol.get("parent")
        while parent:
            ancestors.append(parent)
            parent = parent.get("parent")
        if any(p.get("kind") in CALLABLE_KINDS for p in ancestors):
            continue
        level = sum(1 for p in ancestors if p.get("kind") not in NAMESPACE_KINDS)
        if depth and level >= depth:
            continue
        if kind and kind_of(symbol) != kind:
            continue
        # Roslyn's detail repeats the name ("Id : int", "Get(string)"): only the
        # rest of it is shown. Other servers' detail follows the name.
        leaf, detail = declaration.chain[-1], symbol.get("detail") or ""
        rest = detail[len(leaf):] if detail.startswith(leaf) else (f" — {detail}" if detail else "")
        line = symbol["range"]["start"]["line"] + 1
        rows.append(f"{'  ' * (level + 1)}{address_in_file(declaration, declarations)}{rest}"
                    f"  {kind_of(symbol)} L{line}")
    return rows


def under(file: str, where: str | None, root: Path) -> bool:
    if not where:
        return True
    if (root / where).is_dir():
        return file.startswith(where.rstrip("/") + "/")
    return file == where or fnmatch.fnmatch(file, where)


def find_symbol_in(pool: LanguageServerPool, args: dict) -> str:
    """
    Declarations by name, file, language and kind, each printed as an address.

    With only `file` (a file, directory or glob) it outlines those files. With
    a name it lists the declarations of that exact name — servers match
    substrings, and "BookDto" also returned 17 other classes in the A/B test —
    counting the rest, which partial=true lists. Every language is asked unless
    one is given: CalibreManager has a C# BookDto and a TypeScript BookDto, and
    the agent needs to see both to pick one.
    """
    name, where = (args.get("name") or "").strip(), args.get("file")
    language, kind = args.get("language"), args.get("kind")
    if language and language not in pool.languages:
        raise ToolError(f"language {language!r} is not served here; this repository has "
                        f"{', '.join(pool.languages)}")
    if where:
        # Only the trailing slash: a leading one means an absolute path, which
        # source_files_under refuses when it is outside the repository. Stripped,
        # "/etc/passwd" would read as a path inside it.
        where = where.replace("\\", "/").rstrip("/")
    if not name and not where:
        raise ToolError("give name, file, or both")

    if not name:
        limit = int(args.get("limit", OUTLINE_LIMIT))
        depth = int(args["depth"]) if args.get("depth") else None
        pairs = [(lang, f) for lang, f in source_files_under(pool, where) if language in (None, lang)]
        rows, total = [], 0
        for lang, file in pairs:
            session = pool.session(lang)
            session.sync_with_disk()
            these = outline_rows(session, file, depth, kind)
            total += len(these)
            if len(pairs) > 1 and these and len(rows) < limit:
                rows.append(f"{file}")
            rows += these[:max(0, limit - len(rows))]
        if not total:
            return f"No declarations in {where}" + (f" of kind {kind!r}" if kind else "") + "."
        head = (f"{total} declaration(s) in {where}" if len(pairs) > 1
                else f"{total} declaration(s) in {pairs[0][1]}; address each as {pairs[0][1]}:<name>")
        out = [head + ":", *rows]
        if sum(1 for r in rows if r.startswith(" ")) < total:
            out.append(f"… truncated at {limit} of {total}. Too broad to read as a whole: narrow "
                       "the file pattern, or give depth or kind.")
        return "\n".join(out)

    limit = int(args.get("limit", FIND_LIMIT))
    address = parse_address(name if not where else f"{where}:{name}")
    if address.line is not None:
        raise ToolError("find_symbol takes a name; for a position, use the tool that acts on it")
    leaf = address.path[-1]
    sessions = [pool.session(language)] if language else pool.ordered()
    for session in sessions:
        session.sync_with_disk()

    exact = [(file, d, decls) for session, file, d, decls in resolve_all(pool, address, language)
             if not kind or kind_of(d.symbol) == kind]

    # Names that merely contain it. The workspace index is not enough: jdtls
    # and ruby-lsp do not match substrings, so "Store" found neither
    # MemoryStore nor NullStore through them. The files that mention the name
    # are outlined as well — a class named MemoryStore is declared in a file
    # that says Store.
    from_index = [
        (session, to_relative(h.get("location") or {}))
        for session in sessions for h in workspace_hits(session, leaf)
        if split_symbol_name(h.get("name", ""))[1] != leaf
        and leaf.lower() in split_symbol_name(h.get("name", ""))[1].lower()
    ]

    def containing() -> list:
        """Declarations whose name contains the query. Read only when needed."""
        files = [*from_index, *((s, f) for s in sessions for f in files_mentioning(s, leaf))]
        found = []
        for session, file in sorted({(id(s), f): (s, f) for s, f in files
                                     if f and under(f, where, pool.root)}.values(),
                                    key=lambda pair: pair[1]):
            declarations = file_declarations(session, file)
            found += [(file, d, declarations) for d in declarations
                      if d.chain[-1] != leaf and leaf.lower() in d.chain[-1].lower()
                      and (not kind or kind_of(d.symbol) == kind)]
        return found

    listed, label = exact, "named"
    if args.get("partial") or not exact:
        label = "named or containing"
        listed = [*exact, *containing()]

    if not listed:
        caveats = "".join(sibling_project_caveat(s) for s in sessions)
        asked = ", ".join(s.language for s in sessions)
        return f"No declaration named {name!r} in {asked}" + (f" under {where}" if where else "") \
            + "." + caveats
    rows = [f"  {file}:{address_in_file(d, decls)}  {kind_of(d.symbol)} "
            f"L{d.symbol['range']['start']['line'] + 1}" for file, d, decls in listed[:limit]]
    out = [f"{len(listed)} declaration(s) {label} {name!r}:", *rows]
    if len(listed) > limit:
        out.append(f"  … truncated at {limit} of {len(listed)}. Too broad to act on as a set: "
                   "narrow it with file, language, kind or a qualified name.")
    # Counted from the index alone, which is cheap; listing them reads outlines.
    others = len({(id(s), f) for s, f in from_index}) if listed is exact else 0
    if others:
        out.append(f"  (partial=true also lists names containing {leaf!r}; the index knows "
                   f"of {others} such file(s))")
    return "\n".join(out)


def describe_pool(pool: LanguageServerPool) -> str:
    """project_info across every language this repository serves."""
    running = pool.started()
    rows = [
        f"repository: {pool.root}",
        f"languages : {', '.join(pool.languages)}",
        f"running   : {', '.join(running) if running else 'none started yet'}",
    ]
    manifests, sources = repository_layout(pool.root, set(pool.languages))
    if manifests:
        rows += ["", "project files:"]
        rows += [f"  {m}" for m in manifests[:LAYOUT_ROWS]]
        if len(manifests) > LAYOUT_ROWS:
            rows.append(f"  … and {len(manifests) - LAYOUT_ROWS} more")
    if sources:
        rows += ["", "source files by directory:"]
        for directory, counts in sources[:LAYOUT_ROWS]:
            rows.append(f"  {directory}/  " + ", ".join(f"{n} {lang}" for lang, n in counts.most_common()))
        if len(sources) > LAYOUT_ROWS:
            rows.append(f"  … and {len(sources) - LAYOUT_ROWS} more directories")
    return "\n".join(rows)


# What defines a project, by file name or suffix. Listed so an agent can see the
# shape of the repository without an ls and a cat of package.json.
MANIFEST_NAMES = {
    "package.json", "tsconfig.json", "pyproject.toml", "setup.py", "go.mod", "Cargo.toml",
    "pom.xml", "build.gradle", "build.gradle.kts", "Gemfile", "composer.json",
    "Package.swift", "CMakeLists.txt",
}
MANIFEST_SUFFIXES = (".sln", ".slnx", ".csproj", ".fsproj")
LAYOUT_ROWS = 25


def repository_layout(root: Path, languages: set[str]) -> tuple[list[str], list[tuple[str, Counter]]]:
    """
    Project files, and source file counts per directory two levels down.

    Two levels because that is where a repository says what it holds —
    `backend/Api`, `frontend/src` — and deeper is the file tree an agent can
    ask for when it needs it.
    """
    manifests: list[str] = []
    by_directory: dict[str, Counter] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(walkable(dirnames))
        relative = os.path.relpath(dirpath, root).replace(os.sep, "/")
        parts = [] if relative == "." else relative.split("/")
        key = "/".join(parts[:2]) or "."
        for name in sorted(filenames):
            if name in MANIFEST_NAMES or name.endswith(MANIFEST_SUFFIXES):
                manifests.append(name if not parts else f"{relative}/{name}")
            language = EXTENSION_LANGUAGES.get(os.path.splitext(name)[1])
            if language in languages:
                by_directory.setdefault(key, Counter())[language] += 1
    return manifests, sorted(by_directory.items())


def serve(pool: LanguageServerPool) -> None:
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
                    "instructions": server_instructions(),
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
                text = dispatch(pool, tool_name, arguments)
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


def server_instructions() -> str:
    """
    Sent in the initialize response, where MCP clients put it in front of the
    agent. This is the only place guidance reaches the agent without a human
    pasting it, so it carries two things. What this server is bound to, and how
    to spot a misconfiguration, which otherwise shows up as a confusing empty
    answer. And when each tool is cheaper than reading or editing files
    directly, and when it is not: in the A/B test an agent told only "use
    Lodesman as much as possible" rewrote a 100-line method through
    replace_symbol_body to add one line. The tool descriptions say what each
    tool does; the when lives here, in one place.
    """
    root = _ROOT or Path.cwd()
    return (
        f"Lodesman answers code questions from real language servers. Bound to {root}, "
        "it serves every language the repository contains; each language server "
        "starts on the first question that needs it, so a first call can be slow.\n"
        "Tools that act on code take a symbol address: Name, Type.member, "
        "path/File.cs:Name, backend/*:Name (anywhere under a folder), Name#2 (second "
        "overload), file:42 (the declaration at line 42), file:42:17 (a local or "
        "parameter at line 42, column 17). An address must mean exactly one "
        "declaration; if it matches several, the answer lists their addresses. Every "
        "answer prints addresses you can copy into the next call.\n"
        "Lodesman is cheaper than reading files when you need one piece of a file, "
        "and not otherwise:\n"
        "- project_info shows the languages and projects per directory, without "
        "starting a server: a quick overview of an unfamiliar repository.\n"
        "- To see one method or class, get_symbol_body returns just that declaration; "
        "read the whole file only when you need most of it.\n"
        "- To see what a file or folder contains, find_symbol with only file is shorter "
        "than reading it.\n"
        "- To find where a name is declared or used, find_symbol and find_references "
        "give the compiler's answer; a text search is fine for strings, comments and markup.\n"
        "- To rename, rename_symbol changes every reference in one call and lists what it left.\n"
        "- To edit: replace_symbol_body when you rewrite most of a declaration. For a "
        "small change inside one, a plain text edit is cheaper: every character you "
        "send is output you pay for.\n"
        "- After editing, get_file_diagnostics checks one file without a build.\n"
        "If an answer is empty for code you can see, check project_info: the server may "
        "be bound elsewhere, or the language unverified. Empty is then not proof of absence."
    )


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

    if args.language:
        # An explicit language is an instruction, not a hint: serve that and
        # nothing else, even if the repository contains more.
        languages = [args.language]
        log(f"language forced: {args.language}")
    else:
        detected = detect_languages(root)
        if not detected:
            # An MCP client renders a traceback as "failed to connect", which
            # tells the user nothing about what to change. Match how the
            # not-a-directory case already reports itself.
            log(f"no recognized source files under {root}")
            log("Pass --language to force one, or start the server in a "
                "repository that contains source files.")
            return 2
        languages = [language for language, _ in detected]
        summary = ", ".join(f"{language} ({n} files)" for language, n in detected)
        log(f"detected: {summary}")

    pool = LanguageServerPool(root, languages)
    log(f"ready — repo={root} languages={', '.join(languages)} "
        "(each language server starts on the first question that needs it)")
    try:
        serve(pool)
    except KeyboardInterrupt:
        pass
    finally:
        pool.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
