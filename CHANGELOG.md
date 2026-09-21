# Changelog

## Unreleased

Three fixes found by an A/B test of Claude Code with and without Lodesman on
a real repository: CalibreManager, with a C# backend and a React-TS frontend.

- **`find_symbol` in a multi-language repository answered "No symbol matching"
  for symbols that exist.** Languages are asked in turn, and the first one's
  "none" was taken as the answer. TypeScript went first and had never heard of
  the C# class, which happened 7 times out of 7. A "none" now falls through to
  the next language. It is only reported once every language has said so, and
  the answer names the languages that were asked.
- **A TypeScript rename could leave the old name exported.** tsserver's
  default turned a re-export into `BookPatch as BookUpdateDto`, so every
  importer kept the old name. This happened in 3 renames out of 3. tsserver
  is now asked to rename outright.
- **`rename_symbol` now lists what it didn't change:** the lines that still
  mention the old name, such as comments, strings or re-exports. In the test,
  an agent took the tool's success report as "renamed everywhere" and told
  the user so, while ten comments and an alias still named the old type.

These fixes are not yet verified by a test run.

## 0.5.0 — 2026-09-21

Lodesman can now edit code as well as navigate it. Its answers also stay
current while you edit files by any means, with no need to go through
Lodesman.

- **Seven new tools:**
  - **Navigation:** `type_definition`, `call_hierarchy`, `type_hierarchy`.
  - **Editing:** `code_action`, `replace_symbol_body`, `insert_before_symbol`
    / `insert_after_symbol`, `safe_delete_symbol`.
- **Two tools renamed.** `document_symbols` and `check` are now
  `get_symbols_overview` and `get_file_diagnostics`. Update any prompts or
  permission rules that name them.
- **Four answers that were confidently wrong are fixed:**
  - Stale results after edits on disk.
  - "No errors" on broken C# in a fresh clone.
  - Java renames that silently dropped the file move.
  - A whole class returned as the "body" of a one-line C# member.
- **The language contract** grew from 8 tests to 18 per language.

### Two tools renamed

- `document_symbols` is now **`get_symbols_overview`**.
- `check` is now **`get_file_diagnostics`**.

The old names didn't say what the tools return. **Anything that calls the
old names, such as prompts or permission rules, needs updating.** The entries
below use the new names, including for changes made before the rename.

### Editing tools

- **`replace_symbol_body`**: replace a declaration, signature and body, by
  name.
- **`insert_before_symbol`** / **`insert_after_symbol`**: add code next to a
  declaration.
- **`safe_delete_symbol`**: delete a declaration, but only if nothing uses it.

What they have in common:

- **Names resolve through the file's symbol outline**, so they can be
  qualified (`MemoryStore.get`) and narrowed with `file` and `line`. An
  ambiguous name is refused with the candidates listed; an edit never guesses.
- **They write directly.** The agent supplies the text, so there's nothing to
  preview. The answer is the diff plus the file's errors after the edit, with
  the count from before when it changed.
- **Line endings are preserved.** Doc comments, attributes and decorators
  directly above a declaration stay with it: they're skipped by an insert
  "before" and removed by a delete.
- **`safe_delete_symbol` needs two sources to agree:** the language server
  reports no references, and a text search finds the name nowhere else. An
  empty reference list is also what a server returns when it can't answer (an
  unlicensed intelephense, or usages through reflection or DI), so a common
  name is refused more often than strictly necessary. That's the safe
  direction to be wrong in.

Since the disk sync, a plain file edit is equally safe. These tools exist to
save reading the file, and to report the result without a second call.

### Names in Go, Rust and Swift

Tools that take a name resolve it through the language server, and three
servers needed handling of their own:

- **Go:** gopls names methods by receiver, `(*MemoryStore).Get`, and
  SolidLSP's wrapper then strips that to `Get`. Lodesman reads the receiver
  from the declaration line, so `MemoryStore.Get` can be told from
  `NullStore.Get`.
- **Rust:** rust-analyzer lists methods under their `impl` block, so
  `Record.scaled` now looks inside `impl Record`.
- **Swift:** sourcekit-lsp answers project-wide searches from an index store
  that sometimes briefly loses a symbol. In CI that failed a different tool
  on each of three runs. When the search comes back empty, every tool that
  takes a name now falls back to the outlines of the files that mention it.
  An outline comes from the file itself, not the index. This also covers
  editing a symbol straight after writing it, before any index has caught up.

### `get_symbol_body` returned a whole class for a one-line C# member

For a member like `public int Scaled(int f) => Value * f;` it returned the
entire enclosing class. It now resolves names like the editing tools do and
returns exactly the declaration's text, which is what `replace_symbol_body`
replaces, so the output can be edited and passed straight back. It also
accepts qualified names, and shows every match for an ambiguous one.

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

### `get_file_diagnostics` said "no errors" about broken C# in a fresh clone

On a project that had never been built, `get_file_diagnostics` answered **"no errors"** for a
file with plain compile errors. Roslyn restores such a project itself while it
starts up. It keeps using the project it loaded before that restore, and that
project yields no compiler diagnostics at all (its analyzers still run). In
VS Code the file watcher reports the restore output and Roslyn reloads; here
nothing did.

The disk sync now also watches project files (`.csproj`, `.props`,
`.targets`) and each project's `obj/project.assets.json`. When one changes,
the sync waits for Roslyn to confirm it has reloaded. It also runs once right
after startup, which covers Roslyn's own restore.

### `get_file_diagnostics` no longer guesses whether the project loaded

`get_file_diagnostics` used to warn that the project "did not load fully, not that the code
is wrong" whenever at least half of a file's errors were missing-type errors
(CS0246 and similar). That is also exactly what a plain missing `using` looks
like, so a real, trivially fixable error came with advice not to trust it.

`get_file_diagnostics` now reads the owning project's restore state instead of guessing:

- **Never restored** (no `obj/project.assets.json`): compile errors may be
  missing from the answer. Run `dotnet restore`.
- **Restore failed**: `get_file_diagnostics` names the failure. A failed restore still writes
  `project.assets.json` and records the error in it, for example
  `NU1301 Unable to load the service index` from an unreachable feed.
  Missing-type errors may then be phantoms.
- **Restored cleanly**: no warning. A CS0246 is reported as the missing `using`
  it is, and `code_action` can add it.

The old guess remains only for a file that no `.csproj` owns.

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
