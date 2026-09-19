# Changelog

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
