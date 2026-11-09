# gable-lsp

A Language Server Protocol implementation for [Gable](https://github.com/gablelang/gable),
a statically typed, natively compiled language.

It is deliberately small: the server owns the protocol, document
synchronisation and scheduling, and delegates every bit of language knowledge
to the `gable` compiler CLI. Diagnostics come from `gable check`, formatting
from `gable fmt` — the same tools you already use at the terminal.

```
.editor ── LSP (stdio or TCP) ──► gable-lsp ──► gable check / gable fmt
   ▲                                  │
   └────── publishDiagnostics ◄───────┘
```

* **Live diagnostics** on open, change and save, from the compiler's own
  line/column diagnostics — including errors in imported files and the
  compiler's notes, mapped to `relatedInformation`.
* **Formatting** (`textDocument/formatting`) via `gable fmt`, returned as
  `TextEdit`s so the editor stays in charge of the buffer.
* **Both transports**: stdio (what every editor launches) and a TCP socket.
* **No dependencies** beyond the Python 3.9+ standard library.
* **Unicode-correct**: negotiates `utf-8`, `utf-16` or `utf-32` position
  encodings, CRLF/LF line endings, and computes spans from the compiler's
  carets.
* **Works against the real compiler**: verified with Gable 0.3.0 — including
  its `<gable prelude>` synthetic diagnostics and cross-file errors.

## Install

```sh
pipx install .            # from a checkout, isolated
# or
python3 -m pip install --user .
```

This installs a single `gable-lsp` executable. It needs a `gable` binary on
`PATH` (or `--gable-path`/`gable.path` configuration). Nothing else.

Verify:

```sh
gable-lsp --version
gable-lsp --help
```

## Configure your editor

All four sections below assume Gable files are associated with a `gable`
filetype and that diagnostics appear after the first edit or save. The server
also supports TCP for editors that prefer a socket (see the end of this file).

### Neovim (0.11+ built-in LSP)

`~/.config/nvim/after/ftplugin/gable.lua` (plus a filetype registration):

```lua
vim.filetype.add({ extension = { gable = "gable" } })

vim.lsp.config("gable_lsp", {
  cmd = { "gable-lsp", "--stdio" },
  filetypes = { "gable" },
  root_markers = { ".git" },
  settings = {
    gable = { path = "gable", ["check.debounceMs"] = 150 },
  },
})
vim.lsp.enable("gable_lsp")
```

`vim.lsp.config`/`vim.lsp.enable` need Neovim 0.11 or newer. For Neovim
0.9/0.10 use the `nvim-lspconfig` variant in `editors/nvim/lspconfig-gable.lua`,
which registers the same command as a custom server.

Formatting is opt-in: `:lua vim.lsp.buf.format()` or a `gq` mapping. Nothing
is changed automatically unless you add an autocmd.

### Helix

`~/.config/helix/languages.toml`:

```toml
[language-server.gable-lsp]
command = "gable-lsp"
args = ["--stdio"]

[[language]]
name = "gable"
scope = "source.gable"
file-types = ["gable"]
comment-token = "//"
language-servers = ["gable-lsp"]
auto-format = false
```

`hx --health gable` should list `gable-lsp` as configured. Format the buffer
with `:format` (`:fmt` in older Helix builds).

### VS Code

`editors/vscode/` contains a minimal client extension (it only launches the
server and forwards settings):

```sh
cd editors/vscode && npm install
code --extensionDevelopmentPath="$PWD"
```

See `editors/vscode/README.md` for packaging a `.vsix`. Or point any generic
LSP client extension at the command `gable-lsp` with argument `--stdio` —
nothing in the server depends on this extension.

### Sublime Text

With the [LSP](https://packagecontrol.io/packages/LSP) package, add to
`Preferences: LSP Settings`:

```json
{
  "clients": {
    "gable-lsp": {
      "enabled": true,
      "command": ["gable-lsp", "--stdio"],
      "selector": "source.gable"
    }
  }
}
```

A copy is in `editors/sublime/LSP-gable-lsp.sublime-settings`.

## Configuration

Command line flags:

| flag | default | meaning |
| ---- | ------- | ------- |
| `--stdio` | (default) | speak LSP on stdin/stdout |
| `--tcp` / `--host` / `--port` | off / `0.0.0.0` / `9257` | listen on a socket instead |
| `--tcp-listen` | off | keep accepting clients after the first disconnects |
| `--gable-path PATH` | `gable` | compiler binary |
| `--check-debounce-ms MS` | `150` | quiet period before checking a changed buffer |
| `--check-timeout-ms MS` | `10000` | per-check compiler timeout |
| `--format-timeout-ms MS` | `5000` | per-formatting compiler timeout |
| `--log-level LEVEL` | `warning` | `debug`…`critical`, always to stderr or a file |
| `--log-file PATH` | unset | write logs to a file (useful for stdio sessions) |

The same knobs are honoured from `initializationOptions` and from
`workspace/didChangeConfiguration` notifications, so an editor can re-point the
server at a freshly built compiler without restarting it:

```json
{
  "gable": {
    "path": "/home/me/gable/build/gable",
    "check.debounceMs": 150,
    "check.timeoutMs": 10000,
    "format.timeoutMs": 5000
  }
}
```

Nothing is ever written to stdout except protocol frames; a misconfigured
compiler path produces one `window/showMessage` and a log line, not a broken
session.

## TCP transport

```sh
gable-lsp --tcp --port 9257
```

Then point the client at the socket. For Neovim:

```lua
vim.lsp.config("gable_lsp", { cmd = function() return vim.lsp.rpc.connect("127.0.0.1", 9257) end, ... })
```

One client is served per process, and the process exits when that client
disconnects — the same lifecycle as stdio. Use `--tcp-listen` to keep serving
reconnects, or `--port 0` to let the OS choose (the chosen port is logged to
stderr). TCP mode is also the easiest way to drive the server by hand:

```sh
printf 'Content-Length: 91\r\n\r\n{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"capabilities":{}}}' | nc localhost 9257
```

## How diagnostics work

On open/change/save the server snapshots the buffer, writes it to a hidden
sibling file (`.name.gable.lsp-check-<pid>-<n>`), runs `gable check` on that
file on a worker thread, parses stderr, maps the result to LSP diagnostics,
publishes them, and deletes the temp file. The sibling location keeps
`import` resolution identical to checking the real file; the `O_EXCL`/`0600`
creation and unconditional cleanup keep it safe. Results for a buffer that has
changed in the meantime are discarded, so diagnostics always describe the text
the user is looking at.

See **[docs/diagnostics-integration.md](docs/diagnostics-integration.md)** for
the full guide: the exact compiler output format, the regexes, the field
mapping (severities, spans, notes → `relatedInformation`, synthetic
`<gable prelude>` paths, absolute vs relative paths), multi-file publishing and
retraction, staleness rules, the temp-file strategy, and how to move to a
structured `gable check --format=json` or an in-process compiler call later.

## Formatting

`textDocument/formatting` copies the buffer to a temp file next to the
document, runs `gable fmt` on the copy, diffs copy-before against copy-after
and returns the difference as `TextEdit`s. Your file and your buffer are only
ever changed by your editor. A buffer that does not parse yields no edits
(`gable fmt` refuses to rewrite it), which is the CLI's own contract.

## Layout

```
src/gable_lsp/
  __init__.py       package version
  __main__.py       python -m gable_lsp
  main.py           CLI parsing, signal handling, transport selection
  transport.py      stdio and TCP endpoints
  server.py         protocol loop, capability exchange, handlers
  rpc.py            JSON-RPC framing, message reader/writer, error codes
  documents.py      open-document store, incremental sync
  encoding.py       line splitting, utf-8/16/32 position math
  diagnostics.py    compiler output parser + LSP mapping
  checker.py        worker thread: debounce, coalesce, compile, publish
  formatting.py     gable fmt via a temp copy, diff → TextEdits
  settings.py       flags / initializationOptions / didChangeConfiguration
  paths.py          path & file URI helpers
  log.py            stderr/file logging (never stdout)
tests/              145 stdlib unittest tests (unit + end-to-end)
  support.py        LSP client harness for child-process tests
  fixtures/bin/     deterministic fake `gable` CLI for protocol tests
  fixtures/captured real compiler output, nine representative cases
tools/capture_fixtures.sh   re-capture fixtures against a new compiler
docs/diagnostics-integration.md   the integration guide
editors/            Neovim, Helix, VS Code, Sublime configs
```

The modules have no cycles: `rpc` → `documents` → `encoding` at the bottom,
`diagnostics`/`formatting`/`checker` in the middle, `server`/`transport` on top.

## Development

```sh
python3 -m unittest discover -s tests -t tests -v
```

The suite needs only the standard library and runs in a few seconds. It spins
up real `python -m gable_lsp` child processes for the end-to-end cases, using a
deterministic fake compiler, so it works on a machine without Gable installed.
When a real binary is available (set `GABLE_BIN=/path/to/gable`, or build the
compiler at its default checkout path), additional tests run against it:
related-information from real notes, imported-file diagnostics, and a
formatting comparison with `gable fmt` itself.

Regenerate the captured fixtures after a compiler upgrade:

```sh
GABLE=/path/to/gable tools/capture_fixtures.sh
```

## Compatibility

* LSP 3.17 (`positionEncoding`), works with older clients (UTF-16 default).
* Python 3.9+.
* Gable compiler 0.3.x CLI (`gable check`, `gable fmt`).
* Tested here with Gable 0.3.0 built from source with g++ 12.

If the compiler later grows structured diagnostics, the parser is the only
module that changes; see §7 of the integration guide.

## License

Apache-2.0.
