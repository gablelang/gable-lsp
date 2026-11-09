# Gable language server for VS Code

A thin client for `gable-lsp`: it starts the server over stdio for `.gable`
files and forwards the `gable.*` settings. All language features live in the
server, so this extension is deliberately tiny.

## Use it

```sh
cd editors/vscode
npm install                  # fetches vscode-languageclient
code --extensionDevelopmentPath="$PWD"   # run this copy
```

To install it permanently, package it first (needs `@vscode/vsce`):

```sh
npm install -g @vscode/vsce
vsce package                 # produces gable-lsp-0.1.0.vsix
code --install-extension gable-lsp-0.1.0.vsix
```

Requirements:

* `gable-lsp` on `PATH` (`pipx install .` from the repository root), or set
  `gable.serverPath` to its absolute path.
* `gable` on `PATH`, or set `gable.path`.

## Settings

| setting | default | meaning |
| ------- | ------- | ------- |
| `gable.serverPath` | `gable-lsp` | command that launches the server |
| `gable.path` | `gable` | compiler binary used for diagnostics and formatting |
| `gable.check.debounceMs` | `150` | quiet period before re-checking a buffer |

LSP tracing is available through the conventional `gable_lsp.trace.server`
setting that the language client registers.

## No extension needed

Any generic LSP client extension for VS Code works too; point it at:

```json
{ "gable-lsp": { "command": ["gable-lsp", "--stdio"], "selector": "source.gable" } }
```

