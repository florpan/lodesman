# Lodesman

**Compiler-grade code intelligence over MCP.** Lodesman puts a real language
server behind your coding agent's tools, so "who calls this?" is answered by the
compiler rather than by a text search.

A lodesman is the pilot who comes aboard and steers a ship through waters the
captain doesn't know. That is the job: an agent dropped into an unfamiliar
repository, guided by something with local knowledge.

> **Status: alpha.** The tool surface is still growing and not every tool is
> finished. If you hit something broken, an issue is genuinely useful — see
> [Status](#status).

## Why

An agent's dominant cost is reading files into context, and a tool that returns
*coordinates* makes that worse, not better — `Program.cs — L135` forces the
agent to read the file anyway.

Measured on a real C# repository, answering "what uses `RagQueryService`":

| approach | cost |
|---|---|
| read the three files involved | 1745 lines, ~17.7k tokens |
| the 3 references, with ±6 lines of code each | ~36 lines, ~370 tokens |

**~48× cheaper**, and it is a better answer. So Lodesman's tools return code,
not locations. A location is a promise of future cost; the code is the answer.

The second reason is correctness. An agent cannot tell a true "no results" from
a broken query, so a plausible empty answer is the most dangerous thing a
navigation tool can produce. Lodesman gates every answer on the language server
actually being ready, and distinguishes "none" from "couldn't tell".

## Install

Requires Python 3.11+.

```bash
uvx lodesman-mcp          # run without installing
pipx install lodesman-mcp # or install it
```

### Wire it into Claude Code

```bash
claude mcp add lodesman --scope user -- uvx lodesman-mcp
```

Or, for any MCP client, in the config directly:

```json
{
  "mcpServers": {
    "lodesman": {
      "command": "uvx",
      "args": ["lodesman-mcp"]
    }
  }
}
```

With no arguments Lodesman binds to the working directory it is launched in,
which is what MCP clients give it. One user-scoped entry therefore works across
every project — no per-project configuration.

To point it somewhere explicitly:

```bash
lodesman-mcp /path/to/repo
```

### First run

The language server itself is downloaded on first use into `~/.solidlsp`
(override with `SOLIDLSP_HOME`). For C# this pulls Roslyn from NuGet and **can
take several minutes**. Later runs reuse it and start in seconds.

The server process starts immediately; the language server behind it starts
lazily on the first tool call and is then kept warm for the life of the process.
That is the whole design: a cold Roslyn costs minutes, a warm one answers in
milliseconds and tracks your edits incrementally.

## Verify your install

```bash
python scripts/smoke_test.py /path/to/repo --language csharp
```

This starts a real language server against a real repository and proves it
answers the two questions everything else is built on — what symbols are in this
file, and who references this symbol — with cross-file results the compiler
agrees with.

## Tools

| tool | what it answers |
|---|---|
| `project_info` | which repository this server bound to, and how |
| `find_symbol` | find a symbol by name anywhere in the project |
| `find_definition` | where is this defined |
| `find_references` | what actually uses this, with the surrounding code |
| `find_implementations` | concrete implementations of an interface or abstract member |
| `document_symbols` | outline one file: its types, methods and fields |
| `get_symbol_body` | the full source of one declaration, by name |
| `explain_symbol` | resolved type, signature and documentation |
| `blast_radius` | what breaks if this symbol changes |
| `rename_symbol` | rename everywhere, using the compiler's understanding |
| `check` | compiler diagnostics for one file, from the warm server |
| `type_definition` | what type a variable, field or parameter actually is, as code |
| `call_hierarchy` | who calls this, or what it calls, to a chosen depth |
| `type_hierarchy` | what a type inherits and implements, and what derives from it |
| `code_action` | the server's quick fixes and refactorings — add a missing import, and so on |

`blast_radius` and `check` are the two that exist specifically because agents
edit code they haven't read: one tells you the cost of a change before you make
it, the other verifies it afterwards without a full build.

`rename_symbol` is a dry run by default and lists every file it would touch.
Pass `apply=true` to write; it reports how many files actually changed on disk.
`code_action` works the same way: without a title it lists what is available;
with one it shows the diff; `apply=true` writes it.

Edit files however you like. Before every question the server checks the disk
for files that changed since it last looked and tells the language server, so
an agent's ordinary file edits are seen by the next query. Nothing has to be
routed through this server to keep its answers current.

Roslyn has neither a call hierarchy nor a type hierarchy. For C#,
`call_hierarchy` answers incoming calls from references (which also include
non-call references) and declines outgoing calls; `type_hierarchy` finds
subtypes through `textDocument/implementation` and shows supertypes from the
declaration line. Each answer says which of these it is.

Tools depend on what the language server behind them implements. `pyright`, for
instance, does not serve `textDocument/implementation`, so `find_implementations`
reports that rather than pretending the answer is "none" — a distinction that
matters more to an agent than to a person.

## Configuring it

**One entry, for everything.** One server serves one repository and every
language that repository contains. A second *repository* needs a second entry; a
second *language* does not.

```bash
claude mcp add lodesman --scope user -- uvx lodesman-mcp
```

That is the whole setup, and it works in every project, because with no path
argument the server binds to whatever directory the client launches it in.

At startup it counts source files to work out which languages the repository
actually contains, and starts a language server for each one **individually, on
the first question that needs it**. A language server costs hundreds of megabytes
and tens of seconds, so a repository containing four languages does not pay for
four of them to answer one question about one.

Detection covers 51 languages — everything SolidLSP supports, minus the
experimental servers and the ones with no real symbol structure (markdown, JSON,
YAML). That is deliberately wider than the set verified in CI below: trying an
untested language is strictly better than refusing to start, which is what the
old hand-written list did to ordinary React projects written in `.jsx`.

`project_info` reports which languages were found and which of their servers are
running. A language listed but not running has simply not been asked about.

### React, Vue, Svelte, Angular

React is not a separate thing to configure. `.tsx` and `.jsx` are TypeScript and
JavaScript with JSX syntax, and the TypeScript server handles them natively
alongside `.ts`, `.js`, `.mts`, `.mjs` and the rest — twelve extensions in total.
One server covers a mixed React codebase, and references resolve across the
boundary: a function declared in `api.ts` and used from both a `.tsx` and a
`.jsx` component comes back as one answer with all of them.

```bash
lodesman-mcp .    # React, plain TS, or both — nothing special needed
```

Vue and Svelte are genuinely different, because `.vue` and `.svelte` are
single-file-component formats that are not valid TypeScript. They have their own
servers, and those servers are **supersets** rather than alternatives: the Vue
server handles `.vue` *and* `.ts`/`.js`, Svelte handles `.svelte` *and* `.ts`.

Detection knows this. SolidLSP ranks Vue and Svelte below TypeScript precisely
because they are supersets, so a `.ts` file counts towards TypeScript while a
`.vue` file counts towards Vue — and a project with both gets both servers,
without either stealing the other's files.

Neither is verified in CI yet, so treat them as untested rather than supported.

### A repository with more than one language

Nothing to configure. A .NET solution with a React frontend, a Python service
with a TypeScript dashboard, a Go backend with a Vue admin panel — one entry
covers all of it.

```
solution/
  Solution.sln
  Web/            C#     ─┐
  Core/           C#      ├─ one server process, one MCP entry
  Infrastructure/ C#      │
  frontend/       React  ─┘
```

Measured on exactly that layout: `project_info` reports `csharp, typescript`,
questions about the C# projects are answered by Roslyn, and questions about the
frontend by tsserver — in the same process, each started only once something
asks for it.

Routing is by the question, not by guesswork:

- A tool naming a **file** goes to the server for that file's language. Asking
  Roslyn about a `.ts` file would produce a confusing error rather than an
  answer, so it is never asked.
- A tool naming a **symbol** is tried against each language in turn, so a symbol
  is found whichever half of the repository it lives in. Cross-language
  references do not exist at the language-server level — a C# symbol has no
  TypeScript references — so the first language that resolves a name owns it.

Warm servers are tried before cold ones, so answering a second question about a
language already in use costs nothing extra.

### Forcing a single language

`--language` overrides detection entirely and serves that language alone. Useful
when a repository contains something you specifically do not want a server
started for, or to name a language detection does not recognise:

```bash
lodesman-mcp . --language elixir
```

### The one case that still needs a second entry

Several projects **in the same language**, where that language's server only
searches one project at a time.

Roslyn loads sibling projects from a shared parent, so a .NET solution with
however many `.csproj` needs nothing. tsserver does not, and it is worse than
simply missing them. Measured on a repository holding a `web/` and an `admin/`,
each with its own `tsconfig.json`:

```
find_symbol WebWidget      → found
open a file in admin/      → (any tool call naming a file there)
find_symbol AdminWidget    → found
find_symbol WebWidget      → NOT FOUND
```

Its workspace symbol search follows whichever project it most recently saw a
file from, so the answer depends on what you asked previously. Holding a file
open in each project does not help — that was tried and measured.

Lodesman cannot fix this from the outside, so it declares it: when a symbol is
not found and the repository holds several projects in that language, the answer
says so instead of reporting an absence it cannot vouch for.

```
No symbol matching 'WebWidget'.

Treat this as inconclusive. This repository holds 2 separate typescript
projects (admin, web), and that language server searches one project at a
time, so a symbol in another project reports as missing. …
```

If that describes your repository, give each project its own entry:

```json
{
  "mcpServers": {
    "lodesman-web":   { "command": "uvx",
                        "args": ["lodesman-mcp", "web"] },
    "lodesman-admin": { "command": "uvx",
                        "args": ["lodesman-mcp", "admin"] }
  }
}
```

That is the only remaining reason to run more than one. Several *languages* in
one repository is not one of them.

Agents get a condensed version of all of this automatically: it is sent in the
MCP `initialize` response, so a coding agent can diagnose a misconfiguration and
propose the fix without being told any of it.

## Status

Alpha. The tool surface is still growing, and not every tool is finished.

Lodesman inherits SolidLSP's language coverage, and runs wherever its language
servers do. Not every language and platform combination has been exercised yet,
so if you try one and it breaks, please
[open an issue](https://github.com/florpan/lodesman/issues) — a report with the
language, the OS and the stderr output is the most useful thing you can send.
Testing help is very welcome.

### Known issues

None currently open.

Fixed in 0.3.1:

- `rename_symbol(apply=true)` reported success while writing nothing to disk.
  It now writes, preserves CRLF line endings, handles UTF-16 column offsets,
  and reports the number of files whose bytes actually changed.
- Language detection descended into dot-directories, so starting a server in a
  home directory could bind it to a "project" made of `.cache` and `.local`.
- Pointing the server at a directory with no recognized source files produced
  an unhandled traceback, which an MCP client renders as "failed to connect".
- Tools taking a `file` argument did not verify the path stayed inside the
  repository.
- The per-project cache key was case-folded on every platform, so on a
  case-sensitive filesystem two distinct repositories could share one cache.

## Development

```bash
git clone https://github.com/florpan/lodesman
cd lodesman
pip install -e .          # or: uv pip install -e .
python -m unittest discover -t . -s tests
```

Install before running the tests. Nothing in the suite asserts against a
third-party library, but importing the server pulls in the vendored SolidLSP
and therefore its dependencies, so on a bare interpreter the modules fail at
the loader rather than at an assertion.

That runs the unit tests and the integration tests that need no language
server — startup, language detection, the tool surface, and path containment,
which is enforced before any language server is contacted. A couple of seconds,
no downloads.

The rename tests drive a real language server and are opt-in, because a cold
machine has to download one first:

```bash
LODESMAN_INTEGRATION=1 python -m unittest discover -t . -s tests
```

They skip rather than fail if no working language server is available. Note
that "no language server" is narrower than it sounds: SolidLSP launches pyright
through `uvx`, which brings its own runtime, so the Python tests run on any
machine with uv even without node — only the TypeScript ones need node. Good
for coverage, but the two conditions are not the same claim.

### The language contract

`tests/integration/languages.py` holds one fixture project per language
lodesman auto-detects, all modelling the same thing — a `Record` type, a `Store`
interface, two implementations, and a second file that uses them — so the
assertions are identical across languages and only the syntax differs. Each
language then gets the same contract asserted against it: find a symbol, outline
a file, resolve a definition, find cross-file references, return a body, explain
a symbol, answer or decline implementations, and rename to disk without
disturbing line endings.

The set is tied to `EXTENSION_LANGUAGES` rather than to a popularity list, and a
test enforces that: adding a language to the detector without adding a fixture
fails the suite. The suite cannot fall behind what the server claims to do.

What actually passes, measured in CI on every push rather than claimed:

| language | contract | needs |
|---|---|---|
| C# | ✅ 8/8 | nothing — Roslyn fetches .NET and itself |
| TypeScript / JavaScript | ✅ 8/8 | node, npm |
| Python | ✅ 8/8 | uv |
| Go | ✅ 8/8 | go, and `go install golang.org/x/tools/gopls@latest` |
| Rust | ✅ 8/8 | rustup, and `rustup component add rust-analyzer` |
| Java | ✅ 8/8 | a JDK |
| C / C++ | ✅ 8/8 | clangd |
| Swift | ✅ 8/8 | a Swift toolchain; the package is built first |
| PHP | ✅ 8/8 | node, npm — intelephense analyses PHP from node. References and rename need `INTELEPHENSE_LICENSE_KEY`; without one they are refused rather than answered emptily |
| Ruby | ⚠️ 6/8 | ruby, bundler, and `gem install ruby-lsp` |
| Kotlin | ⚠️ blocked | a JDK — SolidLSP downloads its own server |

**Ruby** passes six of the eight. `find_references` and `rename_symbol` return
nothing, and it is not a capability gap — ruby-lsp advertises both
`referencesProvider` and `renameProvider` — nor timing, since raising its
internal cross-file wait from 500 ms to 8 s changed nothing. Unexplained.

**Kotlin** is blocked upstream, not by anything here. SolidLSP downloads
JetBrains `intellij-server` pinned at `262.9593.0`, and **that build has
expired**: it prints `This build of intellij-server has expired` to stdout and
exits, which surfaces as the server dying during the LSP handshake. Tracked as
[oraios/serena#2008](https://github.com/oraios/serena/issues/2008).

Pinning `263.4702.0` makes Kotlin pass 8/8, verified on Linux. It is not enabled
by default, because doing so moves an expiring EAP pin out of upstream and into
this repository: that build is on the same clock that killed the last one in
under two months, and an overridden version is downloaded without the hash
verification SolidLSP applies to its own defaults. The failure mode is a green
build turning red with no code change. To opt in locally, set it when
constructing `SolidLSPSettings`:

```python
ls_specific_settings={"kotlin": {"kotlin_lsp_version": "263.4702.0"}}
```

Use the string key, not `LanguageServerId.KOTLIN`. The vendored tree is imported
as a bare `solidlsp`, so an enum from a different import path fails an
`isinstance` check inside SolidLSP and the setting is silently ignored — no
error, no warning, and the old version downloads anyway.

A skip is not a pass, and CI enforces that: a language that runs no tests fails
the build unless `languages.py` records *why* it cannot run. A green tick that
might mean "ran eight assertions" or might mean "ran nothing" is worth nothing.

Anything unavailable skips with a reason naming the missing tool. CI runs the
full matrix, one job per language, so a per-language regression is caught even
though no single machine can run them all.

Not every server implements the whole protocol, and one gates parts of it
commercially. Where a capability is missing the contract does not go soft — it
asserts the opposite property, that lodesman *reports* the absence rather than
returning an empty result as though it were an answer. On unlicensed PHP,
`find_references` must say the result is inconclusive and `rename_symbol` must
refuse and leave the tree untouched. A confident wrong answer is the one
failure mode an agent cannot recover from.

To run them against a published release instead of the working tree:

```bash
LODESMAN_PKG=lodesman-mcp@0.3.1 UVX_FLAGS=--refresh \
  LODESMAN_INTEGRATION=1 python -m unittest discover -t . -s tests
```

`UVX_FLAGS` is separate because uv's own flags must precede the package name —
anything after it is forwarded to `lodesman-mcp` and rejected by its argument
parser.

There is also a standalone check against a repository of your own:

```bash
python scripts/smoke_test.py <repo> --language csharp
```

## Built on SolidLSP

The language-server client layer is [SolidLSP](https://github.com/oraios/serena),
MIT, vendored unmodified under `src/lodesman/_vendor/`. It is bundled rather
than depended on because it is not published to PyPI independently of the Serena
application, which is GPL and is **not** included here.

Everything Lodesman does lives outside that tree — the project anchoring, the
readiness gate, the cross-file indexing wait, symbol ranking, and the decision
to return code instead of coordinates were all solved by *calling* SolidLSP
differently, never by editing it. That rule is what keeps re-syncing with
upstream cheap.

See [NOTICE.md](NOTICE.md) for full attribution.

## License

MIT — see [LICENSE](LICENSE).

Bundled third-party code, its copyright holders and its license texts are listed
in [NOTICE.md](NOTICE.md). Everything bundled is MIT-licensed.
