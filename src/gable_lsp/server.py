"""The Gable language server: protocol state machine and request handlers.

Design notes
------------

* **The protocol loop never compiles.** ``gable check`` runs on a dedicated
  worker thread (:mod:`gable_lsp.checker`); the loop only applies document
  edits and schedules (debounced, coalesced) checks.
* **Diagnostics are always published, never waited for.** ``didOpen`` /
  ``didChange`` / ``didSave`` return immediately; results arrive later through
  ``textDocument/publishDiagnostics`` carrying the document version they were
  produced from.
* **All language knowledge lives in the compiler CLI.** This file knows about
  LSP payloads, not about Gable syntax.
* **A broken request must not kill the session.** Handler failures become
  JSON-RPC error responses; only framing errors terminate the loop.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from . import __version__
from .checker import CheckerConfig, CheckSnapshot, DiagnosticChecker
from .documents import DocumentStore, DocumentSyncError
from .encoding import pick_encoding
from .formatting import FormatterConfig, format_buffer
from .log import get_logger
from .paths import uri_to_path
from .rpc import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    SERVER_NOT_INITIALIZED,
    Endpoint,
    Message,
    RpcError,
)
from .settings import Settings

logger = get_logger(__name__)

SERVER_NAME = "gable-lsp"


class Server:
    """A single LSP session on one endpoint."""

    def __init__(self, endpoint: Endpoint, settings: Settings) -> None:
        self.endpoint = endpoint
        self.settings = settings.normalized()
        self.documents = DocumentStore()
        self.encoding = "utf-16"
        self.client_capabilities: Dict[str, Any] = {}
        self.workspace_folders: List[str] = []
        self.root_uri: Optional[str] = None
        self.exit_code: Optional[int] = None
        self._initialized = False
        self._shutdown_requested = False
        self._config_error_shown = False
        self._checker: Optional[DiagnosticChecker] = None
        self._handlers: Dict[str, Callable[[Message], Any]] = {
            "initialize": self._on_initialize,
            "initialized": self._on_initialized,
            "shutdown": self._on_shutdown,
            "exit": self._on_exit,
            "$/cancelRequest": self._on_cancel,
            "$/setTrace": self._on_set_trace,
            "textDocument/didOpen": self._on_did_open,
            "textDocument/didChange": self._on_did_change,
            "textDocument/didSave": self._on_did_save,
            "textDocument/didClose": self._on_did_close,
            "textDocument/formatting": self._on_formatting,
            "workspace/didChangeConfiguration": self._on_did_change_configuration,
        }
        self._checker = DiagnosticChecker(
            snapshot_provider=self._snapshot_for,
            publish=self._publish_diagnostics,
            config=self._checker_config(),
            notify_config_error=self._show_config_error,
        )

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------
    def run(self) -> int:
        """Serve until ``exit``/EOF. Returns the process exit code."""

        self._checker.start()
        try:
            while self.exit_code is None:
                try:
                    payload = self.endpoint.reader.read_message()
                except RpcError as exc:
                    logger.error("framing error: %s", exc.message)
                    self._send(
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {"code": exc.code, "message": exc.message},
                        }
                    )
                    if exc.fatal:
                        self.exit_code = 1
                        break
                    continue
                if payload is None:
                    logger.info("client closed the stream")
                    self.exit_code = 0 if self._shutdown_requested else 1
                    break
                self._dispatch(Message(payload))
        finally:
            self._checker.stop()
        if self.exit_code is None:
            return 1
        return self.exit_code

    def _dispatch(self, message: Message) -> None:
        method = message.method
        if method is None:
            # A response to something we sent (we send no requests) or junk.
            if message.is_request:
                self._error_response(message.id, INVALID_REQUEST, "missing method")
            return
        handler = self._handlers.get(method)
        if handler is None:
            if message.is_request:
                self._error_response(message.id, METHOD_NOT_FOUND, f"unsupported method {method!r}")
            else:
                logger.debug("ignoring notification %s", method)
            return
        if not self._initialized and method not in _PRE_INITIALIZE_ALLOWED:
            if message.is_request:
                self._error_response(message.id, SERVER_NOT_INITIALIZED, "server not initialized")
            return
        if self._shutdown_requested and method not in _POST_SHUTDOWN_ALLOWED:
            if message.is_request:
                self._error_response(message.id, INVALID_REQUEST, "server is shutting down")
            return
        try:
            result = handler(message)
        except RpcError as exc:
            if message.is_request:
                error: Dict[str, Any] = {"code": exc.code, "message": exc.message}
                if exc.data is not None:
                    error["data"] = exc.data
                self._send({"jsonrpc": "2.0", "id": message.id, "error": error})
            else:
                logger.warning("%s failed: %s", method, exc.message)
            return
        except Exception:  # noqa: BLE001 - never let a handler kill the server
            logger.exception("unhandled error in %s", method)
            if message.is_request:
                self._error_response(message.id, INTERNAL_ERROR, f"internal error handling {method}")
            return
        if message.is_request:
            self._send({"jsonrpc": "2.0", "id": message.id, "result": result})

    # ------------------------------------------------------------------
    # Low level send helpers
    # ------------------------------------------------------------------
    def _send(self, payload: Dict[str, Any]) -> bool:
        ok = self.endpoint.writer.write_message(payload)
        if not ok:
            logger.warning("could not write a message; the client has gone away")
            if self.exit_code is None:
                self.exit_code = 0 if self._shutdown_requested else 1
        return ok

    def _error_response(self, request_id: Any, code: int, message: str) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    def _notify(self, method: str, params: Dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _show_message(self, kind: int, message: str) -> None:
        self._notify("window/showMessage", {"type": kind, "message": message})

    def _show_config_error(self, message: str) -> None:
        if self._config_error_shown:
            return
        self._config_error_shown = True
        self._show_message(1, f"gable-lsp: {message}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _on_initialize(self, message: Message) -> Dict[str, Any]:
        if self._initialized:
            logger.warning("ignoring a duplicate initialize request")
            return {
                "capabilities": self._capabilities(),
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
            }
        params = message.params if isinstance(message.params, dict) else {}
        self.client_capabilities = params.get("capabilities") or {}
        general = self.client_capabilities.get("general") or {}
        self.encoding = pick_encoding(general.get("positionEncodings"))
        self.documents.set_encoding(self.encoding)
        root_uri = params.get("rootUri")
        if isinstance(root_uri, str):
            self.root_uri = root_uri
        folders = params.get("workspaceFolders")
        if isinstance(folders, list):
            self.workspace_folders = [
                f["uri"] for f in folders if isinstance(f, dict) and isinstance(f.get("uri"), str)
            ]
        self.settings = Settings.from_mapping(params.get("initializationOptions"), base=self.settings)
        self._apply_settings()
        # From here on the client may send requests; `initialized` is only a
        # notification and some clients delay it.
        self._initialized = True
        logger.info(
            "initialize: client=%r encoding=%s root=%r",
            (self.client_capabilities.get("clientInfo") or {}).get("name"),
            self.encoding,
            self.root_uri,
        )
        return {
            "capabilities": self._capabilities(),
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
        }

    def _on_initialized(self, message: Message) -> None:
        logger.info("initialized")

    def _on_shutdown(self, message: Message) -> None:
        logger.info("shutdown requested")
        self._shutdown_requested = True
        self._checker.cancel_all()
        return None

    def _on_exit(self, message: Message) -> None:
        logger.info("exit notification (shutdown=%s)", self._shutdown_requested)
        self.exit_code = 0 if self._shutdown_requested else 1

    def _capabilities(self) -> Dict[str, Any]:
        capabilities: Dict[str, Any] = {
            "positionEncoding": self.encoding,
            "textDocumentSync": {
                "openClose": True,
                "change": 2,  # incremental
                "save": {"includeText": False},
            },
            "documentFormattingProvider": True,
        }
        workspace = self.client_capabilities.get("workspace") or {}
        if workspace.get("workspaceFolders") or self.workspace_folders:
            capabilities["workspace"] = {"workspaceFolders": {"supported": True, "changeNotifications": False}}
        return capabilities

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    def _checker_config(self) -> CheckerConfig:
        return CheckerConfig(
            gable_path=self.settings.gable_path,
            debounce_ms=self.settings.debounce_ms,
            timeout_ms=self.settings.check_timeout_ms,
        )

    def _formatter_config(self) -> FormatterConfig:
        return FormatterConfig(
            gable_path=self.settings.gable_path,
            timeout_ms=self.settings.format_timeout_ms,
        )

    def _apply_settings(self) -> None:
        self._checker.config = self._checker_config()

    def _on_did_change_configuration(self, message: Message) -> None:
        params = message.params if isinstance(message.params, dict) else {}
        incoming = params.get("settings")
        if not isinstance(incoming, dict):
            incoming = params
        previous = self.settings
        self.settings = Settings.from_mapping(incoming, base=self.settings)
        self._apply_settings()
        logger.info("configuration changed: gable.path=%r", self.settings.gable_path)
        if self.settings.gable_path != previous.gable_path or self.settings != previous:
            # A new binary may fix long-standing errors: re-check every buffer.
            for uri in self.documents.open_uris():
                self._checker.schedule(uri, debounce=False)

    # ------------------------------------------------------------------
    # Documents and diagnostics
    # ------------------------------------------------------------------
    def _document_params(self, message: Message) -> Dict[str, Any]:
        params = message.params
        if not isinstance(params, dict):
            raise RpcError(INVALID_PARAMS, "missing params")
        document = params.get("textDocument")
        if not isinstance(document, dict) or not isinstance(document.get("uri"), str):
            raise RpcError(INVALID_PARAMS, "missing textDocument.uri")
        return params

    def _on_did_open(self, message: Message) -> None:
        params = self._document_params(message)
        document = params["textDocument"]
        uri = document["uri"]
        text = document.get("text")
        if not isinstance(text, str):
            raise RpcError(INVALID_PARAMS, "textDocument.text must be a string")
        version = document.get("version")
        self.documents.open(
            uri,
            document.get("languageId") or "gable",
            int(version) if isinstance(version, int) else 0,
            text,
        )
        self._checker.schedule(uri, debounce=False)

    def _on_did_change(self, message: Message) -> None:
        params = self._document_params(message)
        document = params["textDocument"]
        uri = document["uri"]
        changes = params.get("contentChanges")
        if not isinstance(changes, list):
            raise RpcError(INVALID_PARAMS, "contentChanges must be an array")
        version = document.get("version")
        try:
            updated = self.documents.change(uri, changes, version if isinstance(version, int) else None)
        except DocumentSyncError as exc:
            logger.warning("could not apply change to %s: %s", uri, exc)
            return
        if updated is None:
            logger.warning("didChange for unopened document %s", uri)
            return
        self._checker.schedule(uri)

    def _on_did_save(self, message: Message) -> None:
        params = self._document_params(message)
        uri = params["textDocument"]["uri"]
        text = params.get("text")
        document = self.documents.save(uri, text if isinstance(text, str) else None)
        if document is None:
            return
        self._checker.schedule(uri, debounce=False)

    def _on_did_close(self, message: Message) -> None:
        params = self._document_params(message)
        uri = params["textDocument"]["uri"]
        self.documents.close(uri)
        self._checker.clear(uri)

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------
    def _on_formatting(self, message: Message) -> List[dict]:
        params = self._document_params(message)
        uri = params["textDocument"]["uri"]
        document = self.documents.get(uri)
        if document is None:
            raise RpcError(INVALID_PARAMS, f"document {uri} is not open")
        options = params.get("options") or {}
        if options.get("insertSpaces") is False:
            logger.debug("client prefers tabs; gable fmt emits 4 spaces regardless")
        path = self._path_for_uri(uri)
        started = time.monotonic()
        edits = format_buffer(
            document.text,
            real_path=path,
            config=self._formatter_config(),
            encoding=self.encoding,
        )
        logger.info(
            "format %s: %d edit(s) in %.0f ms",
            uri,
            len(edits) if edits else 0,
            (time.monotonic() - started) * 1000,
        )
        return edits if edits is not None else []

    # ------------------------------------------------------------------
    # Checker plumbing
    # ------------------------------------------------------------------
    def _path_for_uri(self, uri: str) -> Optional[str]:
        if not uri.startswith("file:"):
            return None
        try:
            return uri_to_path(uri)
        except ValueError:
            return None

    def _snapshot_for(self, uri: str) -> Optional[CheckSnapshot]:
        document = self.documents.get(uri)
        if document is None:
            return None
        return CheckSnapshot(
            uri=uri,
            path=self._path_for_uri(uri),
            text=document.text,
            version=document.version,
            encoding=self.encoding,
        )

    def _publish_diagnostics(self, uri: str, diagnostics: List[dict], version: Optional[int]) -> None:
        params: Dict[str, Any] = {"uri": uri, "diagnostics": diagnostics}
        if version is not None:
            params["version"] = version
        self._notify("textDocument/publishDiagnostics", params)

    # ------------------------------------------------------------------
    # Miscellaneous notifications
    # ------------------------------------------------------------------
    def _on_cancel(self, message: Message) -> None:
        # Checks are cheap to let finish and edits are applied eagerly; there
        # is nothing cancellable in this server, so the request is a no-op.
        logger.debug("ignoring $/cancelRequest")

    def _on_set_trace(self, message: Message) -> None:
        params = message.params if isinstance(message.params, dict) else {}
        logger.debug("trace level set to %r", params.get("value"))


_PRE_INITIALIZE_ALLOWED = frozenset({"initialize", "initialized", "exit", "shutdown"})
_POST_SHUTDOWN_ALLOWED = frozenset({"exit", "shutdown", "$/setTrace"})


def serve(endpoint: Endpoint, settings: Settings) -> int:
    """Run one server session; returns the process exit code."""

    return Server(endpoint, settings).run()
