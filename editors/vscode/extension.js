// Minimal VS Code client for gable-lsp.
//
// The server is a plain LSP process: this extension only starts it over stdio,
// forwards the `gable.*` settings and registers the `.gable` language. Any
// generic LSP client extension configured with `gable-lsp --stdio` gives the
// same result; this exists so the repository ships a working VS Code setup.

const { workspace, window } = require("vscode");

let LanguageClient;
let TransportKind;
try {
  ({ LanguageClient, TransportKind } = require("vscode-languageclient/node"));
} catch (error) {
  window.showErrorMessage(
    "gable-lsp: the extension is not built. Run `npm install` inside editors/vscode, " +
    "or use any generic LSP client extension with `gable-lsp --stdio`."
  );
  throw error;
}

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
  // LSP message tracing, controlled by the standard `gable_lsp.trace.server`
  // setting that language clients create for themselves.
  const trace = workspace.getConfiguration("gable_lsp").get("trace.server", "off");
  if (trace !== "off") {
    clientOptions.traceOutputChannel = window.createOutputChannel("Gable LSP Trace");
  }
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
