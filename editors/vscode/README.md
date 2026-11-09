# Gable language server for VS Code

No build step. The extension starts `gable-lsp --stdio` for `.gable` files.

```sh
code --install-extension editors/vscode
```

Requirements:

* `gable-lsp` on `PATH` (`pipx install .` from the repository root), or set
  `gable.serverPath` to its absolute path in VS Code settings.
* `gable` on `PATH`, or set `gable.path`.

`vscode-languageclient` is the only dependency; `npm install` inside this
folder is only needed if you want to debug the extension, not to use it with a
locally installed server.

Equivalent generic configuration (any LSP client extension):

```json
{ "gable-lsp": { "command": ["gable-lsp", "--stdio"], "selector": "source.gable" } }
```
