// Minimal VS Code client for gable-lsp.
//
// The server is a plain LSP process: this extension only starts it over stdio,
// forwards the `gable.*` settings and registers the `.gable` language. Any
// generic LSP client extension configured with `gable-lsp --stdio` gives the
// same result; this exists so the repository ships a working VS Code setup.

const { workspace } = require("vscode");
const { LanguageClient, TransportKind } = require("vscode-languageclient/node");

let client;

function settings() {
  const config = workspace.getConfiguration("gable");
  return {
    gable: {
      path: config.get("path", "gable"),
      "check.debounceMs": config.get("check.debounceMs", 150),
    },
  };
}

async function activate(context) {
  const serverPath = workspace.getConfiguration("gable").get("serverPath", "gable-lsp");
  const serverOptions = {
    run: { command: serverPath, args: ["--stdio"], transport: TransportKind.stdio },
    debug: { command: serverPath, args: ["--stdio"], transport: TransportKind.stdio },
  };
  const clientOptions = {
    documentSelector: [{ scheme: "file", language: "gable" }],
    initializationOptions: settings(),
    synchronize: { configurationSection: "gable" },
  };
  client = new LanguageClient("gable-lsp", "Gable Language Server", serverOptions, clientOptions);
  context.subscriptions.push(client.start());
}

async function deactivate() {
  if (client) {
    await client.stop();
    client = undefined;
  }
}

module.exports = { activate, deactivate };
