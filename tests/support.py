"""Helpers shared by the test suite (stdlib only)."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_ROOT = os.path.join(REPO_ROOT, "src")
FIXTURE_BIN = os.path.join(REPO_ROOT, "tests", "fixtures", "bin", "gable")
CAPTURED = os.path.join(REPO_ROOT, "tests", "fixtures", "captured")

def _find_real_compiler() -> str:
    """Locate a real ``gable`` binary for the optional integration tests.

    Order: ``GABLE_BIN``, then ``gable`` on PATH, then a few conventional
    build locations (a sibling checkout, ``build/``, ``target/``). The tests
    that use it skip when nothing is found.
    """

    explicit = os.environ.get("GABLE_BIN")
    if explicit:
        return explicit
    from shutil import which

    on_path = which("gable")
    if on_path:
        return on_path
    candidates = [
        os.path.join(os.path.dirname(REPO_ROOT), "gable", "build", "gable"),
        os.path.join(REPO_ROOT, "build", "gable"),
        os.path.join(REPO_ROOT, "..", "gable-ref", "gable-main", "build", "gable"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return os.path.abspath(candidate)
    return ""


REAL_GABLE = _find_real_compiler()


def ensure_import_path() -> None:
    if SRC_ROOT not in sys.path:
        sys.path.insert(0, SRC_ROOT)


class ServerProcess:
    """A ``python -m gable_lsp`` child speaking LSP over stdio."""

    def __init__(self, gable_path: str = FIXTURE_BIN, extra_args=None, env=None):
        ensure_import_path()
        command = [sys.executable, "-m", "gable_lsp", "--gable-path", gable_path]
        command.extend(extra_args or [])
        environment = dict(os.environ)
        environment["PYTHONPATH"] = SRC_ROOT
        environment.pop("GABLE_LSP_LOG", None)
        if env:
            environment.update(env)
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=REPO_ROOT,
            env=environment,
        )
        self._queue: "queue.Queue" = queue.Queue()
        self._stderr_lines = []
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self._next_id = 0

    # -- plumbing ------------------------------------------------------
    def _read_stdout(self):
        stream = self.proc.stdout
        assert stream is not None
        while True:
            headers = {}
            while True:
                line = stream.readline()
                if not line:
                    self._queue.put(None)
                    return
                if line.strip() == b"":
                    break
                name, _, value = line.strip().partition(b":")
                headers[name.strip().lower()] = value.strip()
            length = int(headers[b"content-length"])
            body = stream.read(length)
            if body is None or len(body) != length:
                self._queue.put(None)
                return
            self._queue.put(json.loads(body.decode("utf-8")))

    def _read_stderr(self):
        stream = self.proc.stderr
        assert stream is not None
        for line in stream:
            self._stderr_lines.append(line.decode("utf-8", errors="replace"))

    def send(self, payload):
        body = json.dumps(payload).encode("utf-8")
        stream = self.proc.stdin
        assert stream is not None
        stream.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        stream.flush()

    def notify(self, method, params=None):
        self.send({"jsonrpc": "2.0", "method": method, "params": {} if params is None else params})

    def request(self, method, params=None, timeout=30):
        self._next_id += 1
        request_id = self._next_id
        self.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": {} if params is None else params,
            }
        )
        message = self._wait_for(
            lambda m: m.get("id") == request_id,
            timeout=timeout,
            description=f"response to {method}",
        )
        return message

    def wait_for_notification(self, predicate, timeout=30, description="notification"):
        return self._wait_for(
            lambda m: m.get("method") is not None and predicate(m),
            timeout=timeout,
            description=description,
        )

    def wait_for_diagnostics(self, uri, timeout=30):
        return self.wait_for_notification(
            lambda m: m.get("method") == "textDocument/publishDiagnostics"
            and m.get("params", {}).get("uri") == uri,
            timeout=timeout,
            description=f"diagnostics for {uri}",
        )["params"]

    def _wait_for(self, predicate, timeout, description):
        deadline = time.monotonic() + timeout
        deferred = []
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"timed out waiting for {description}; stderr:\n"
                        + "".join(self._stderr_lines[-40:])
                    )
                try:
                    message = self._queue.get(timeout=min(0.25, remaining))
                except queue.Empty:
                    continue
                if message is None:
                    raise EOFError(
                        f"server exited while waiting for {description}; stderr:\n"
                        + "".join(self._stderr_lines[-40:])
                    )
                if predicate(message):
                    return message
                deferred.append(message)
        finally:
            for message in deferred:
                self._queue.put(message)

    # -- lifecycle -----------------------------------------------------
    def initialize(self, capabilities=None, initialization_options=None):
        params = {
            "processId": os.getpid(),
            "clientInfo": {"name": "gable-lsp-tests", "version": "1"},
            "rootUri": None,
            "capabilities": capabilities or {},
        }
        if initialization_options is not None:
            params["initializationOptions"] = initialization_options
        response = self.request("initialize", params)
        self.notify("initialized", {})
        return response

    def shutdown(self):
        self.request("shutdown", None)
        self.notify("exit", None)
        try:
            return self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - failure path
            self.proc.kill()
            raise
        finally:
            self._close_pipes()

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            pass
        self._close_pipes()

    def _close_pipes(self):
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):  # pragma: no cover - already closed
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        if self.proc.poll() is None:
            self.kill()
        else:
            self._close_pipes()


def real_compiler_available() -> bool:
    return os.path.isfile(REAL_GABLE) and os.access(REAL_GABLE, os.X_OK)
