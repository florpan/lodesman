# Third-party notices

Lodesman is MIT-licensed (see `LICENSE`). It bundles the following third-party
code, which is also MIT-licensed.

## SolidLSP

- **Location in this distribution:** `src/lodesman/_vendor/solidlsp/`
- **Upstream:** https://github.com/oraios/serena
- **License:** MIT — full text at `src/lodesman/_vendor/solidlsp/LICENSE`
- **Copyright:** © 2025–2026 Jain & Panchenko IT-Berater Partnerschaft
  (Oraios AI) and contributors

SolidLSP is the language-server client library Lodesman is built on. It is
vendored rather than declared as a dependency because it is not published to
PyPI independently of the Serena application.

The Serena repository is licensed per component: SolidLSP under MIT, the Serena
application under GPL-3.0-or-later. **Only SolidLSP is bundled here.** No part
of the Serena application is included or distributed, so no GPL obligations
attach to Lodesman. Upstream's own licensing overview states that the SolidLSP
files "remain MIT-licensed and may be obtained, extracted and used separately
under MIT terms."

The vendored tree is kept unmodified, with its per-file SPDX headers intact, so
that it can be re-synced with upstream. Lodesman's own behaviour lives entirely
outside it.

### OLSP

Some files under `_vendor/solidlsp/lsp_protocol_handler/` are derived from
[OLSP](https://github.com/predragnikolic/OLSP), © 2023 Предраг Николић, MIT.
The original notices are retained in those files.

## Runtime dependencies

Installed from PyPI rather than bundled; each carries its own license:
`pygls`, `lsprotocol`, `psutil`, `pathspec`, `overrides`, `sensai-utils`,
`filelock`, `requests`, `charset-normalizer`, `typing-extensions`.

## Language servers

Lodesman downloads language servers (Roslyn, typescript-language-server, and
others) on first use into `~/.solidlsp`. These are **not** bundled and are not
covered by this notice — each is obtained from its own publisher under its own
license.
