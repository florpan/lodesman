# Changelog

## Unreleased

### Four new tools

- **`type_definition`**: what type a field, property or local variable is. The
  tool shows the type's declaration as code.
- **`call_hierarchy`**: callers or callees, to a chosen depth, with the line
  of each call.
- **`type_hierarchy`**: supertypes and subtypes, each with its declaration
  line.
- **`code_action`**: the language server's quick fixes and refactorings.
  Without a title it lists them; with one it previews the diff; with
  `apply=true` it writes. Actions that run a command inside the server
  instead of returning an edit are listed as such and refused, not skipped.

Support differs by server. This is what was probed on 2026-09-21:

| | call hierarchy | type hierarchy | type definition |
|---|---|---|---|
| C# (Roslyn) | no | no | yes |
| Python (pyright) | yes | no | yes |
| TypeScript | yes | no | yes |
| Java (jdtls) | yes | yes | yes |

Where a server lacks one, the tool falls back, and the answer says so.

- **`call_hierarchy`, incoming:** falls back to the symbols that reference the
  function.
- **`call_hierarchy`, outgoing:** declines rather than guessing.
- **`type_hierarchy`, subtypes:** uses `textDocument/implementation`.
- **`type_hierarchy`, supertypes:** shows the declaration line as written.

### Java methods can be found by name

Name lookups found Java classes but never methods. `find_references`,
`call_hierarchy` and the other tools that take a name answered "no symbol
named 'fromJson'". jdtls leaves methods out of its workspace symbol search by
default (`java.symbols.includeSourceMethodDeclarations: false`). Lodesman now
turns the setting on.

The cost, measured on gson (264 files): startup went from 14.7 to 15.5
seconds and memory rose 4–6%. Distinctive names like `fromJson` stay instant.
Common names get expensive: `get` returns 1,743 symbols and takes 1.1 seconds,
and that likely grows with project size. This is the likeliest reason for the
default. It can be switched back off with one constant,
`JAVA_METHODS_IN_SYMBOL_SEARCH`.

### `check` said "no errors" about broken C# in a fresh clone

On a project that had never been built, `check` answered **"no errors"** for a
file with plain compile errors. Roslyn restores such a project itself while it
starts up. It keeps using the project it loaded before that restore, and that
project yields no compiler diagnostics at all (its analyzers still run). In
VS Code the file watcher reports the restore output and Roslyn reloads; here
nothing did.

The disk sync now also watches project files (`.csproj`, `.props`,
`.targets`) and each project's `obj/project.assets.json`. When one changes,
the sync waits for Roslyn to confirm it has reloaded. It also runs once right
after startup, which covers Roslyn's own restore. If the restore itself fails,
for example because a package feed is unreachable, `check` now says its
answer cannot be trusted and suggests `dotnet restore`. Before, it reported
clean.

### `rename_symbol` dropped file renames

jdtls renames a Java class by renaming its file too, since Java requires the
two to match. Lodesman applied the text edits and **silently discarded the
file rename**. That left `class VoidStore` inside `NullStore.java`, a file
that no longer compiles, and reported success. File renames are now applied,
in the order the server sends them. File creations and deletions are refused
out loud. A change that would touch anything outside the repository is refused
as a whole before anything is written. Before, only that part was skipped.

### Answers no longer go stale after files change on disk

The language servers are told at startup that the client watches the disk for
them, so they don't watch it themselves. Nothing did. An edit made outside this
server, including an agent's ordinary file edit, went unseen by any query that
didn't open the edited file itself:

- **Python:** pyright went on reporting the old code, even after
  `rename_symbol`'s own writes.
- **C#:** Roslyn missed edits made on disk.
- **TypeScript** was stale some of the time. tsserver watches the disk itself,
  but in repeated runs the same rename showed up at once on some and not
  within 20 seconds on others.

Every tool call now checks the language's source files for changes first. Each
changed file is reported to the language server and briefly reopened, so the
server receives its new contents directly. The first query after an edit is
correct: it was measured that way on Python, C# and TypeScript, the last across
five repeated runs.

**You no longer need to route edits through this server to keep it accurate.**
Edit files however you like.

The check costs 40–120 ms per call on an ordinary repository. It is logged
when it exceeds half a second, which happens on a directory holding many
repositories.

## 0.4.0 — 2026-09-19

### One server now covers a whole repository

A server used to serve the single language with the most source files, and
knew nothing about the rest. A .NET solution with a TypeScript frontend needed
two MCP entries, and a question about the wrong half returned nothing —
indistinguishable from the symbol not existing.

It now detects every language the repository contains and starts a language
server for each, **individually, on the first question that needs it**. A
repository with four languages does not pay for four servers to answer one
question about one.

Tools naming a file are answered by the server for that file's language. Tools
naming a symbol are tried against each language in turn, warm servers first.

`project_info` reports which languages were found and which are running.

**If you configured two entries for a mixed repository, one will now do.**

### Language detection was badly wrong

The extension map was hand-written: `.ts`, `.tsx`, `.js`. tsserver actually
handles twelve extensions. So a React project written in `.jsx`, or anything
using `.mts` or `.mjs`, contained no "recognized source files" at all and **the
server exited instead of starting**. On an ordinary codebase.

Detection is now derived from each language server's own file matcher, across
51 languages — everything SolidLSP supports minus the experimental servers and
the ones with no real symbol structure. 14 extensions became 152, including
`.jsx`, `.pyi`, `.kts`, `.phtml` and the C/C++/Objective-C family.

Detection is deliberately wider than what CI verifies: trying an untested
language beats refusing to start. See the README for what is actually verified.

### False negatives are declared rather than reported as absences

tsserver's workspace symbol search follows whichever project it most recently
saw a file from, so in a repository holding several TypeScript projects a
symbol that exists can report as missing, depending on what was asked before.

This cannot be fixed from outside the language server, so `find_symbol` now
says the result is inconclusive when the repository holds several projects in
such a language, rather than reporting an absence it cannot vouch for.

### Agents are told how this is configured

The MCP `initialize` response now carries `instructions`, which clients put in
front of the agent: what this instance is bound to, that servers start lazily,
and that an empty answer about visible code is a configuration symptom worth
checking `project_info` over.

### Also

- Language detection no longer descends into dot-directories. Starting in a
  home directory used to bind to a "project" made of `.cache` and `.local`.
- Tools taking a `file` argument verify the path stays inside the repository.
  `check` would previously return diagnostics for any readable file.
- A directory with no recognized source files exits with a message instead of
  an unhandled traceback, which clients render as "failed to connect".
- The per-project cache key is case-folded only on Windows, so two
  case-distinct repositories no longer share one cache on Linux.

## 0.3.1 — 2026-09-19

- `rename_symbol(apply=true)` reported success while writing nothing. It now
  writes, preserves CRLF line endings, handles UTF-16 column offsets, and
  reports how many files actually changed on disk.

## 0.3.0 — 2026-09-19

First public release. Eleven tools over MCP, backed by a real language server.
