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
lodesman-mcp /path/to/repo --language csharp
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

`blast_radius` and `check` are the two that exist specifically because agents
edit code they haven't read: one tells you the cost of a change before you make
it, the other verifies it afterwards without a full build.

`rename_symbol` is a dry run by default and lists every file it would touch.
Pass `apply=true` to write; it reports how many files actually changed on disk.

Tools depend on what the language server behind them implements. `pyright`, for
instance, does not serve `textDocument/implementation`, so `find_implementations`
reports that rather than pretending the answer is "none" — a distinction that
matters more to an agent than to a person.

## Configuring it

**One server serves one repository, in one language, fixed at startup.** Nothing
changes that at runtime — not a tool call, not the agent. A second repository or
a second language means a second entry in your MCP configuration. That one
sentence answers most of the questions people have about how to set this up.

### A single-language project

Put it in your user configuration once and it works everywhere, because with no
path argument the server binds to whatever directory the client launches it in:

```bash
claude mcp add lodesman --scope user -- uvx lodesman-mcp
```

The language is detected by counting source files under the root and taking the
majority. Auto-detected: **C#, TypeScript/JavaScript, Python, Go, Rust, Java,
Kotlin, Ruby, PHP, Swift, C/C++**.

### A language it does not auto-detect

SolidLSP ships servers for far more languages than the eleven above. Any of them
can be used by naming it — there is just no detection for it, so it has to be
explicit:

```bash
lodesman-mcp . --language elixir
```

### A repository with more than one language

This is the common case — a backend and a frontend in one repo — and detection
handles it badly on purpose: it picks the majority language and then knows
nothing about the other. Give each one its own server, in a **project-scoped**
`.mcp.json` committed alongside the code:

```json
{
  "mcpServers": {
    "lodesman-backend": {
      "command": "uvx",
      "args": ["lodesman-mcp", "backend", "--language", "csharp"]
    },
    "lodesman-frontend": {
      "command": "uvx",
      "args": ["lodesman-mcp", "frontend", "--language", "typescript"]
    }
  }
}
```

Paths are relative to the directory the client launches in, so this file is
portable between machines. MCP namespaces tools per server, so the agent sees
`lodesman-backend`'s tools and `lodesman-frontend`'s tools as distinct and picks
by name — which also makes the choice legible in a transcript.

### Several projects in the same language

Binding one server to a shared parent sometimes works and sometimes quietly
under-reports, and it depends on the language server rather than on Lodesman.
Measured on a repository holding two C# projects and two TypeScript projects in
sibling folders, each server rooted at the repository root:

| | result |
|---|---|
| C# | found symbols in **both** projects — Roslyn loads siblings |
| TypeScript | found **one** — tsserver loaded only the project it was anchored in |

So the reliable configuration is **one server per project root**, not one per
language. Point servers at the folders that actually contain the projects. A
shared root may work for your language, but when it does not, the symptom is an
empty answer that looks exactly like a symbol having no references — which is
the one result you should never take at face value.

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

How much of it runs depends on what is installed, which varies a lot:

| language | needs |
|---|---|
| C# | nothing — Roslyn fetches .NET and itself |
| Python | uv |
| TypeScript / JavaScript | node, npm |
| PHP | node, npm — intelephense analyses PHP from node |
| | *references and rename need `INTELEPHENSE_LICENSE_KEY`* |
| Go | go, and `go install golang.org/x/tools/gopls@latest` |
| Rust | rustup, and `rustup component add rust-analyzer` |
| Java | a JDK |
| Ruby | ruby, and `gem install ruby-lsp` |
| C / C++ | clangd |
| Kotlin | a JDK and kotlin-language-server |
| Swift | a Swift toolchain |

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
