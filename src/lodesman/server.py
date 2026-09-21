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
import hashlib
import json
import os
import re
import sys
import threading
import time
from collections import Counter
from pathlib import Path

from solidlsp import SolidLanguageServer
from solidlsp.ls_config import LanguageServerConfig, LanguageServerId
from solidlsp.settings import SolidLSPSettings

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "lodesman"
SERVER_VERSION = "0.4.0"

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


def include_java_methods(server: SolidLanguageServer) -> None:
    """
    Make jdtls index method declarations for workspace/symbol.

    Wraps this one instance's initialize parameters rather than editing the
    vendored tree (workdocs/VENDORING.md, preference 2). Written against
    SolidLSP at oraios/serena 704e8c3d. If that method is renamed, or the settings are
    reshaped, this logs and leaves jdtls at its default: Java methods then
    cannot be found by name, which is how it was before this was written.
    """
    build = server._create_initialize_params

    def with_methods():
        params = build()
        try:
            java = params["initializationOptions"]["settings"]["java"]
            java.setdefault("symbols", {})["includeSourceMethodDeclarations"] = True
        except (KeyError, TypeError) as exc:
            log(f"could not enable Java method search (jdtls default kept): {exc!r}")
        return params

    server._create_initialize_params = with_methods


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
    {
        "name": "type_definition",
        "description": (
            "The declaration of a symbol's type: what a variable, field, parameter or "
            "property actually is, shown as code. Name a field or property directly, or "
            "point at a local variable with file, line and symbol."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Field, property or member name"},
                "file": {"type": "string", "description": "For a local: file it appears in"},
                "line": {"type": "integer", "description": "For a local: 1-based line it appears on"},
                "symbol": {"type": "string", "description": "For a local: the identifier on that line"},
            },
        },
    },
    {
        "name": "call_hierarchy",
        "description": (
            "Who calls this function (incoming), or what it calls (outgoing), with the "
            "line of each call. Follows the chain to the given depth, so it answers "
            "'how does execution reach this' in one call."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Function or method name"},
                "direction": {
                    "type": "string",
                    "enum": ["incoming", "outgoing"],
                    "description": "incoming (default): callers. outgoing: callees.",
                },
                "depth": {"type": "integer", "description": "Levels to follow, 1-4 (default 1)"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "type_hierarchy",
        "description": (
            "What a type inherits from and implements (supertypes), and what derives "
            "from or implements it (subtypes), each with its declaration line."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Type name"},
                "direction": {
                    "type": "string",
                    "enum": ["both", "supertypes", "subtypes"],
                    "description": "Default both.",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "code_action",
        "description": (
            "The language server's quick fixes and refactorings for a line: add a "
            "missing import or using, implement an interface, extract a method and "
            "so on. Without a title, lists what is available. With a title, previews "
            "the change as a diff; add apply=true to write it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Path relative to the repo root"},
                "line": {"type": "integer", "description": "1-based line"},
                "end_line": {"type": "integer", "description": "Optional last line of a range"},
                "title": {
                    "type": "string",
                    "description": "The action to take, as listed. A unique fragment is enough.",
                },
                "kind": {
                    "type": "string",
                    "description": "Only actions of this kind, e.g. quickfix, refactor, source",
                },
                "apply": {
                    "type": "boolean",
                    "description": "Write the change. Omit or false to preview only.",
                },
            },
            "required": ["file", "line"],
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
      sync_with_disk makes it reload, so by the time check runs this means
      the restore could not run.
    * **Restore failed.** An unreachable feed still writes project.assets.json,
      and records the failure in its `logs`: NU1301, level Error, "Unable to
      load the service index" (reproduced 2026-09-21 with a dead feed).
      Types from the missing packages then report as missing — CalibreManager
      showed 42 such errors, none real.

    A clean restore logs no errors, and then CS0246 is a genuine missing
    using, not a symptom. Guessing from the proportion of missing-type errors,
    as check used to, told agents not to trust a real, trivially fixable error.

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


def declarations(session: LanguageServerSession, name: str) -> list[tuple[str, int, int, str]]:
    """Every usable declaration of `name` as (file, line, column, label), most likely first."""
    found = []
    for candidate in candidates_for(session, name):
        path = to_relative(candidate.get("location") or {})
        position = position_of(candidate)
        if path and position:
            found.append((path, position[0], position[1], describe(candidate)))
    if not found:
        raise ToolError(f"symbol {name!r} has no usable location")
    return found


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

    # Every question is answered from what is on disk now, not from what was
    # there when the server last looked.
    session.sync_with_disk()

    if name == "find_symbol":
        query = args["name"]
        limit = int(args.get("limit", 25))
        hits = workspace_hits(session, query)
        if not hits:
            return f"No symbol matching {query!r}." + sibling_project_caveat(session)
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
            return (
                f"{target}: no {scope}s or worse. Note this is the language server's view "
                "of the project, not a full build."
            )
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
        steps = edit_steps(edit)
        if not steps:
            raise ToolError(
                f"the language server produced no edits renaming {args['name']!r} to "
                f"{new_name!r} — it may consider the rename illegal here"
            )

        total = sum(len(s[2]) for s in steps if s[0] == "edit")
        files = {s[1] for s in steps if s[0] == "edit"}
        summary = [
            f"Rename {describe(candidate)} to {new_name!r}: "
            f"{total} edit(s) across {len(files)} file(s)."
        ]
        summary += describe_steps(steps)

        if not args.get("apply"):
            summary.append(
                "\nDry run — nothing written. Re-run with apply=true to perform it. "
                "Review the file list first: a rename touching unexpected files usually "
                "means the symbol resolved to something other than what you meant."
            )
            return "\n".join(summary)

        applied, unchanged, failed = apply_steps(session.root, steps)
        # Now rather than at the next call: the server reindexes asynchronously,
        # and this gives it the time the agent spends reading this answer.
        session.sync_with_disk()

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

    if name == "type_definition":
        if args.get("file"):
            # A local variable is not a workspace symbol, so it cannot be found by
            # name; it has to be pointed at.
            target = repo_file(args["file"])
            identifier = args.get("symbol") or ""
            if args.get("line") is None or not identifier:
                raise ToolError("with file, give line and symbol too")
            line = int(args["line"]) - 1
            lines = file_lines(session, target)
            if not 0 <= line < len(lines):
                raise ToolError(f"{target} has no line {line + 1}")
            match = re.search(rf"(?<![\w$]){re.escape(identifier)}(?![\w$])", lines[line])
            if not match:
                raise ToolError(f"{identifier!r} does not appear on {target}:{line + 1}")
            column = utf16_units(lines[line][:match.start()])
            attempts = [(target, line, column, f"{identifier} — {target}:{line + 1}")]
        elif args.get("name"):
            attempts = declarations(session, args["name"])
        else:
            raise ToolError("give name, or file with line and symbol")

        for path, line, column, label in attempts:
            try:
                result = lsp_request(session, path, "type_definition",
                                     position_params(session, path, line, column))
            except Unsupported:
                raise ToolError(
                    f"the {session.language} language server does not support "
                    "textDocument/typeDefinition"
                ) from None
            targets = targets_of(result)
            if targets:
                parts = [f"{label} is of type:"]
                for file, target_line, uri in targets:
                    if file:
                        parts.append(f"  {file}:{target_line + 1}")
                        parts.append(peek(session, file, target_line, 1, 6))
                    else:
                        # A library or framework type.
                        parts.append(f"  {uri} (outside the repository)")
                return "\n".join(parts)
        return (
            f"No type definition for {args.get('symbol') or args.get('name')!r}. It may "
            "itself be a type, or have a type the server cannot resolve (dynamic code)."
        )

    if name == "call_hierarchy":
        direction = args.get("direction") or "incoming"
        if direction not in ("incoming", "outgoing"):
            raise ToolError("direction must be incoming or outgoing")
        depth = max(1, min(int(args.get("depth", 1)), 4))
        budget = 60
        attempts = declarations(session, args["name"])

        def from_references(reason: str) -> str:
            # Incoming calls have an honest substitute: the symbols containing
            # each reference, which is what blast_radius walks. It over-reports
            # rather than under-reports — a reference that is not a call still
            # appears — which is the safe direction to be wrong in. An empty
            # result here is never "nothing calls it": in CI, intelephense
            # without a licence had neither calls nor references, and saying
            # "a leaf" there would be a confident false answer.
            text = call_tool(session, "blast_radius",
                             {"name": args["name"], "depth": depth, "max_queries": budget})
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
        attempts = declarations(session, args["name"])
        path, line, column, label = attempts[0]

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
                where = location_of(implementation)
                file = to_relative(implementation.get("location") or implementation)
                start = ((implementation.get("range") or {}).get("start") or {}).get("line", 0)
                parts.append(f"  {where}")
                code = source_line(session, file, start)
                if code:
                    parts.append(f"      {code}")
        return "\n".join(parts)

    if name == "code_action":
        return code_action(session, args)

    raise ToolError(f"unknown tool: {name}")


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
    summary.append(f"\nWritten: {written} file(s). Run check on them to confirm the result compiles.")
    return "\n".join(summary)


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def dispatch(pool: LanguageServerPool, tool: str, args: dict) -> str:
    """
    Route one tool call to the language server that can answer it.

    Three cases, in order of how much they can be decided up front:

    `project_info` describes the binding itself, so it never touches a server
    and reports every language rather than one.

    A tool naming a `file` is decided by that file's extension. Guessing is not
    needed and would be wrong: asking Roslyn about a .ts file produces a
    confusing error rather than an answer.

    A tool naming a symbol could be answered by any of them, so they are tried
    in turn and the first real answer wins. Cross-language references do not
    exist at the language-server level — a C# symbol has no TypeScript
    references — so the first language that resolves a name is the one that
    owns it. Trying in order, warm servers first, also means a repository with
    four languages does not start four servers to answer one question.
    """
    if tool == "project_info":
        return describe_pool(pool)

    target = args.get("file")
    if target:
        language = pool.language_for_file(repo_file(target))
        if language is None:
            known = ", ".join(pool.languages)
            raise ToolError(
                f"{target!r} is not a file this server handles. It serves "
                f"{known} in {pool.root}. A file of another language needs a "
                "server bound to that language."
            )
        return call_tool(pool.session(language), tool, args)

    # Symbol-named tools. Keep the first genuine failure to report if nobody
    # can answer, rather than the last, which is usually the least relevant
    # language's complaint.
    first_error: ToolError | None = None
    for session in pool.ordered():
        try:
            return call_tool(session, tool, args)
        except ToolError as exc:
            if first_error is None:
                first_error = exc
            continue
    raise first_error or ToolError(f"no language server could answer {tool}")


def describe_pool(pool: LanguageServerPool) -> str:
    """project_info across every language this repository serves."""
    running = pool.started()
    rows = [
        f"repository    : {pool.root}",
        f"languages     : {', '.join(pool.languages)}",
        f"running       : {', '.join(running) if running else 'none started yet'}",
        f"servers       : {solidlsp_home()}",
        "",
        "One server process, one repository, and a language server per language "
        "it contains. Each starts on the first question that needs it, so a "
        "language listed but not running has simply not been asked about.",
    ]
    return "\n".join(rows)


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
    agent. This is the only place configuration guidance reaches the agent
    without a human pasting it, so it states what this server is bound to and
    how to fix a misconfiguration — the two things that otherwise get
    discovered by a confusing empty answer.
    """
    root = _ROOT or Path.cwd()
    return (
        "Lodesman answers questions about code using a real language server, so "
        "results come from the compiler's understanding rather than a text "
        f"search. This server is bound to {root} and serves it alone.\n\n"
        "It serves every language the repository contains, not just the main "
        "one: the languages are detected at startup and a language server is "
        "started for each, individually, on the first question that needs it. "
        "So a .NET solution with a TypeScript frontend is one server entry, not "
        "two, and questions about either half work without configuration.\n\n"
        "Call project_info to see which languages this repository was found to "
        "contain and which of their servers are running. A language listed but "
        "not running has simply not been asked about yet; the first question "
        "starts it, which for a cold language server can take a while.\n\n"
        "Tools naming a file are answered by the server for that file's "
        "language. Tools naming a symbol are tried against each language in "
        "turn, so a symbol is found whichever half of the repository it lives "
        "in.\n\n"
        "The repository is fixed at startup and no tool changes it. A different "
        "repository needs another entry in the MCP configuration. Relative "
        "paths in that entry resolve against the directory the client launches "
        "in.\n\n"
        "If a question about code you can see returns nothing, check "
        "project_info before concluding the symbol is unused: the usual cause "
        "is that this server is bound to a different directory than you expect. "
        "Detection covers far more languages than have been verified, so an "
        "unverified language may answer partially or not at all — that is not "
        "the same as the symbol being absent, and the difference matters."
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
