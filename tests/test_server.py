"""End-to-end protocol tests: a real ``gable-lsp`` child process per test."""

import os
import shutil
import sys
import tempfile
import time
import unittest

from support import ServerProcess, ensure_import_path, real_compiler_available

ensure_import_path()


def doc(uri, text, version=1, language_id="gable"):
    return {
        "textDocument": {
            "uri": uri,
            "languageId": language_id,
            "version": version,
            "text": text,
        }
    }


def ranged(uri, version, start, end, text):
    return {
        "textDocument": {"uri": uri, "version": version},
        "contentChanges": [{"range": {"start": start, "end": end}, "text": text}],
    }


class ServerCase(unittest.TestCase):
    """Base class: temp workspace + a server child process."""

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="gable-lsp-e2e-")
        self.server = ServerProcess()
        self.server.initialize()

    def tearDown(self):
        self.server.kill()
        shutil.rmtree(self.directory, ignore_errors=True)

    def path(self, name="main.gable"):
        return os.path.join(self.directory, name)

    def uri(self, name="main.gable"):
        from gable_lsp.paths import path_to_uri

        return path_to_uri(self.path(name))

    def write(self, text, name="main.gable"):
        path = self.path(name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path


class LifecycleTest(unittest.TestCase):
    def test_initialize_declares_sync_capabilities(self):
        with ServerProcess() as server:
            response = server.initialize()
            result = response["result"]
            capabilities = result["capabilities"]
            self.assertEqual(capabilities["positionEncoding"], "utf-16")
            self.assertEqual(
                capabilities["textDocumentSync"],
                {"openClose": True, "change": 2, "save": {"includeText": False}},
            )
            self.assertTrue(capabilities["documentFormattingProvider"])
            self.assertEqual(result["serverInfo"]["name"], "gable-lsp")

    def test_position_encoding_is_negotiated(self):
        with ServerProcess() as server:
            response = server.initialize(
                capabilities={"general": {"positionEncodings": ["utf-8", "utf-16"]}}
            )
            self.assertEqual(response["result"]["capabilities"]["positionEncoding"], "utf-8")

    def test_shutdown_then_exit_is_clean(self):
        with ServerProcess() as server:
            server.initialize()
            response = server.request("shutdown", None)
            self.assertIn("result", response)
            server.notify("exit", None)
            self.assertEqual(server.proc.wait(timeout=10), 0)

    def test_exit_without_shutdown_is_an_error(self):
        with ServerProcess() as server:
            server.initialize()
            server.notify("exit", None)
            self.assertEqual(server.proc.wait(timeout=10), 1)

    def test_requests_before_initialize_are_rejected(self):
        with ServerProcess() as server:
            response = server.request("textDocument/formatting", {"textDocument": {"uri": "file:///x.gable"}})
            self.assertEqual(response["error"]["code"], -32002)

    def test_unknown_request_returns_method_not_found(self):
        with ServerProcess() as server:
            server.initialize()
            response = server.request("textDocument/hover", {"textDocument": {"uri": "file:///x.gable"}})
            self.assertEqual(response["error"]["code"], -32601)
            self.assertIn("hover", response["error"]["message"])

    def test_requests_are_allowed_before_initialized_notification(self):
        # LSP: the client may send requests as soon as initialize is answered,
        # even if it has not sent the initialized notification yet.
        with ServerProcess() as server:
            server.request("initialize", {"capabilities": {}})
            response = server.request("shutdown", None)
            self.assertIn("result", response)

    def test_requests_after_shutdown_are_rejected(self):
        with ServerProcess() as server:
            server.initialize()
            self.assertIn("result", server.request("shutdown", None))
            response = server.request(
                "textDocument/formatting", {"textDocument": {"uri": "file:///x.gable"}}
            )
            self.assertEqual(response["error"]["code"], -32600)
            server.notify("exit", None)
            self.assertEqual(server.proc.wait(timeout=10), 0)

    def test_eof_without_shutdown_is_an_error(self):
        with ServerProcess() as server:
            server.initialize()
            server.proc.stdin.close()
            self.assertEqual(server.proc.wait(timeout=10), 1)

    def test_unknown_notification_is_ignored(self):
        with ServerProcess() as server:
            server.initialize()
            server.notify("$/somethingNew", {"x": 1})
            # The session must still be alive and responsive.
            self.assertIn("result", server.request("shutdown", None))


class DiagnosticsTest(ServerCase):
    def test_open_publishes_errors_with_version(self):
        text = "fn main() {\n    BAD marker\n}\n"
        self.write(text)
        self.server.notify("textDocument/didOpen", doc(self.uri(), text))
        params = self.server.wait_for_diagnostics(self.uri())
        self.assertEqual(params["version"], 1)
        diagnostics = params["diagnostics"]
        self.assertEqual(len(diagnostics), 1)
        diagnostic = diagnostics[0]
        self.assertEqual(diagnostic["severity"], 1)
        self.assertEqual(diagnostic["source"], "gable")
        self.assertEqual(diagnostic["range"], {
            "start": {"line": 1, "character": 4},
            "end": {"line": 1, "character": 7},
        })
        self.assertEqual(diagnostic["message"], "marker on line 2")

    def test_clean_document_publishes_an_empty_list(self):
        text = "fn main() {\n}\n"
        self.write(text)
        self.server.notify("textDocument/didOpen", doc(self.uri(), text))
        self.assertEqual(self.server.wait_for_diagnostics(self.uri())["diagnostics"], [])

    def test_unsaved_edits_are_checked_from_the_buffer(self):
        self.write("fn main() {\n}\n")
        self.server.notify("textDocument/didOpen", doc(self.uri(), "fn main() {\n}\n"))
        self.assertEqual(self.server.wait_for_diagnostics(self.uri())["diagnostics"], [])
        # Insert a bad line without saving; the server must see it.
        self.server.notify(
            "textDocument/didChange",
            ranged(
                self.uri(),
                2,
                {"line": 1, "character": 0},
                {"line": 1, "character": 0},
                "    BAD typed\n",
            ),
        )
        params = self.server.wait_for_diagnostics(self.uri())
        self.assertEqual(params["version"], 2)
        self.assertEqual(len(params["diagnostics"]), 1)

    def test_fixing_the_buffer_retracts_diagnostics(self):
        self.write("fn main() {\n    BAD\n}\n")
        self.server.notify("textDocument/didOpen", doc(self.uri(), "fn main() {\n    BAD\n}\n"))
        self.assertEqual(len(self.server.wait_for_diagnostics(self.uri())["diagnostics"]), 1)
        self.server.notify(
            "textDocument/didChange",
            ranged(self.uri(), 2, {"line": 1, "character": 0}, {"line": 1, "character": 7}, ""),
        )
        self.assertEqual(self.server.wait_for_diagnostics(self.uri())["diagnostics"], [])

    def test_stale_results_are_never_published(self):
        text = "fn main() {\n    BAD\n}\n"
        self.write(text)
        self.server.notify("textDocument/didOpen", doc(self.uri(), text))
        # Immediately fix the document before the debounced check can run.
        self.server.notify(
            "textDocument/didChange",
            {
                "textDocument": {"uri": self.uri(), "version": 2},
                "contentChanges": [{"text": "fn main() {\n}\n"}],
            },
        )
        params = self.server.wait_for_diagnostics(self.uri())
        self.assertEqual(params["diagnostics"], [])
        self.assertEqual(params["version"], 2)

    def test_did_save_rechecks_immediately(self):
        self.write("fn main() {\n}\n")
        self.server.notify("textDocument/didOpen", doc(self.uri(), "fn main() {\n}\n"))
        self.assertEqual(self.server.wait_for_diagnostics(self.uri())["diagnostics"], [])
        # Make the buffer dirty, then save: the save-triggered check must run
        # without waiting for the debounce window.
        self.server.notify(
            "textDocument/didChange",
            ranged(self.uri(), 2, {"line": 1, "character": 0}, {"line": 1, "character": 0}, "    BAD\n"),
        )
        self.server.notify("textDocument/didSave", {"textDocument": {"uri": self.uri()}})
        params = self.server.wait_for_diagnostics(self.uri())
        self.assertEqual(len(params["diagnostics"]), 1)
        self.assertEqual(params["version"], 2)

    def test_disk_changes_are_ignored_while_the_buffer_is_open(self):
        # The editor's buffer is the source of truth: a check must never
        # resurrect diagnostics for a version the user has already fixed.
        self.write("fn main() {\n}\n")
        self.server.notify("textDocument/didOpen", doc(self.uri(), "fn main() {\n}\n"))
        self.assertEqual(self.server.wait_for_diagnostics(self.uri())["diagnostics"], [])
        self.write("fn main() {\n    BAD\n}\n")
        self.server.notify("textDocument/didSave", {"textDocument": {"uri": self.uri()}})
        self.assertEqual(self.server.wait_for_diagnostics(self.uri())["diagnostics"], [])

    def test_did_close_clears_diagnostics(self):
        text = "fn main() {\n    BAD\n}\n"
        self.write(text)
        self.server.notify("textDocument/didOpen", doc(self.uri(), text))
        self.assertEqual(len(self.server.wait_for_diagnostics(self.uri())["diagnostics"]), 1)
        self.server.notify("textDocument/didClose", {"textDocument": {"uri": self.uri()}})
        self.assertEqual(self.server.wait_for_diagnostics(self.uri())["diagnostics"], [])

    def test_no_temp_files_remain_after_checks(self):
        text = "fn main() {\n    BAD\n}\n"
        self.write(text)
        self.server.notify("textDocument/didOpen", doc(self.uri(), text))
        self.server.wait_for_diagnostics(self.uri())
        self.assertEqual(sorted(os.listdir(self.directory)), ["main.gable"])

    def test_missing_compiler_is_reported_once(self):
        with ServerProcess(gable_path=os.path.join(self.directory, "nope")) as server:
            server.initialize()
            server.notify("textDocument/didOpen", doc(self.uri(), "fn main() {\n}\n"))
            message = server.wait_for_notification(
                lambda m: m.get("method") == "window/showMessage",
                description="compiler-missing message",
            )
            self.assertEqual(message["params"]["type"], 1)
            self.assertIn("Gable compiler", message["params"]["message"])


class FormattingTest(ServerCase):
    def test_formatting_returns_edits_without_touching_disk(self):
        original = "fn main() {\nMESSY print(1)\n}\n"
        path = self.write(original)
        self.server.notify("textDocument/didOpen", doc(self.uri(), original))
        response = self.server.request(
            "textDocument/formatting",
            {"textDocument": {"uri": self.uri()}, "options": {"tabSize": 4, "insertSpaces": True}},
        )
        edits = response["result"]
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["newText"], "    print(1)\n")
        self.assertEqual(edits[0]["range"]["start"], {"line": 1, "character": 0})
        self.assertEqual(edits[0]["range"]["end"], {"line": 2, "character": 0})
        with open(path, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), original)
        self.assertEqual(sorted(os.listdir(self.directory)), ["main.gable"])

    def test_formatting_an_unopened_document_is_an_error(self):
        response = self.server.request(
            "textDocument/formatting",
            {"textDocument": {"uri": "file:///not-open.gable"}, "options": {}},
        )
        self.assertEqual(response["error"]["code"], -32602)

    def test_formatting_a_broken_document_returns_no_edits(self):
        text = "BAD\n"
        self.write(text)
        self.server.notify("textDocument/didOpen", doc(self.uri(), text))
        response = self.server.request(
            "textDocument/formatting",
            {"textDocument": {"uri": self.uri()}, "options": {}},
        )
        self.assertEqual(response["result"], [])


class ConfigurationTest(ServerCase):
    def test_initialization_options_set_the_compiler_path(self):
        with ServerProcess(gable_path=os.path.join(self.directory, "missing")) as server:
            from support import FIXTURE_BIN

            server.initialize(initialization_options={"gable": {"path": FIXTURE_BIN}})
            text = "fn main() {\n    BAD\n}\n"
            self.write(text)
            server.notify("textDocument/didOpen", doc(self.uri(), text))
            diagnostics = server.wait_for_diagnostics(self.uri())["diagnostics"]
            self.assertEqual(len(diagnostics), 1)

    def test_did_change_configuration_switches_the_binary(self):
        from support import FIXTURE_BIN

        self.server.notify(
            "workspace/didChangeConfiguration",
            {"settings": {"gable": {"path": FIXTURE_BIN, "check.debounceMs": 0}}},
        )
        text = "fn main() {\n    BAD\n}\n"
        self.write(text)
        self.server.notify("textDocument/didOpen", doc(self.uri(), text))
        self.assertEqual(len(self.server.wait_for_diagnostics(self.uri())["diagnostics"]), 1)


class TransportTest(unittest.TestCase):
    def test_tcp_transport(self):
        import json
        import socket
        import subprocess
        import threading

        from support import REPO_ROOT, SRC_ROOT

        env = dict(os.environ)
        env["PYTHONPATH"] = SRC_ROOT
        proc = subprocess.Popen(
            [sys.executable, "-m", "gable_lsp", "--tcp", "--host", "127.0.0.1", "--port", "0", "--log-level", "info"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=REPO_ROOT,
            env=env,
        )
        port_holder = {}
        ready = threading.Event()

        def watch():
            for line in proc.stderr:
                text = line.decode("utf-8", errors="replace")
                if "listening" in text and ":" in text:
                    try:
                        port_holder["port"] = int(text.rsplit(":", 1)[1].strip())
                    except ValueError:
                        pass
                    ready.set()

        threading.Thread(target=watch, daemon=True).start()
        try:
            self.assertTrue(ready.wait(20), "server never announced its port")
            connection = socket.create_connection(("127.0.0.1", port_holder["port"]), timeout=20)
            with connection:
                def send(payload):
                    body = json.dumps(payload).encode()
                    connection.sendall(b"Content-Length: %d\r\n\r\n" % len(body) + body)

                send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"capabilities": {}}})
                buffer = b""
                while b"\r\n\r\n" not in buffer:
                    buffer += connection.recv(4096)
                header, _, rest = buffer.partition(b"\r\n\r\n")
                length = int(header.split(b":")[1])
                while len(rest) < length:
                    rest += connection.recv(4096)
                response = json.loads(rest[:length])
                self.assertEqual(response["id"], 1)
                self.assertIn("capabilities", response["result"])
                send({"jsonrpc": "2.0", "id": 2, "method": "shutdown", "params": None})
        finally:
            try:
                connection.close()
            except Exception:
                pass
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except (OSError, ValueError):
                    pass


@unittest.skipUnless(real_compiler_available(), "real gable compiler not available")
class RealCompilerTest(ServerCase):
    def setUp(self):
        from support import REAL_GABLE

        self.directory = tempfile.mkdtemp(prefix="gable-lsp-real-")
        self.server = ServerProcess(gable_path=REAL_GABLE)
        self.server.initialize()

    def test_immutable_binding_has_note_as_related_information(self):
        text = "fn main() {\n    let x: int = 1\n    x = 2\n    print(x)\n}\n"
        self.write(text)
        self.server.notify("textDocument/didOpen", doc(self.uri(), text))
        diagnostics = self.server.wait_for_diagnostics(self.uri())["diagnostics"]
        self.assertEqual(len(diagnostics), 1)
        diagnostic = diagnostics[0]
        self.assertIn("immutable binding", diagnostic["message"])
        self.assertEqual(diagnostic["source"], "gable")
        related = diagnostic["relatedInformation"]
        self.assertEqual(len(related), 1)
        self.assertEqual(related[0]["message"], "binding declared here")
        self.assertEqual(related[0]["location"]["range"]["start"], {"line": 1, "character": 8})
        self.assertEqual(related[0]["location"]["uri"], self.uri())

    def test_formatting_matches_gable_fmt(self):
        original = 'fn main() {\nprint("hi")\n   let x: int = 1 + 2\n  print(x)\n}\n'
        path = self.write(original)
        self.server.notify("textDocument/didOpen", doc(self.uri(), original))
        response = self.server.request(
            "textDocument/formatting",
            {"textDocument": {"uri": self.uri()}, "options": {"tabSize": 4, "insertSpaces": True}},
        )
        edits = response["result"]
        from gable_lsp.paths import uri_to_path

        # Apply like an editor, then compare with what the CLI would produce.
        from test_formatting import apply_edits

        edited = apply_edits(original, edits)
        formatted = self.path("reference.gable")
        with open(formatted, "w", encoding="utf-8") as handle:
            handle.write(original)
        os.system(f"'{__import__('support').REAL_GABLE}' fmt '{formatted}'")
        with open(formatted, "r", encoding="utf-8") as handle:
            self.assertEqual(edited, handle.read())
        self.assertTrue(os.path.exists(path))

    def test_imported_file_errors_are_published_on_that_file(self):
        self.write('fn value() -> int {\n    return "not an int"\n}\n', "badtype.gable")
        root = 'import "badtype.gable"\n\nfn main() {\n    print(1)\n}\n'
        self.write(root, "root.gable")
        self.server.notify("textDocument/didOpen", doc(self.uri("root.gable"), root))
        imported_uri = self.uri("badtype.gable")
        params = self.server.wait_for_notification(
            lambda m: m.get("method") == "textDocument/publishDiagnostics"
            and m["params"]["uri"] == imported_uri
            and m["params"]["diagnostics"],
            description="diagnostics for the imported file",
        )["params"]
        self.assertEqual(len(params["diagnostics"]), 1)
        self.assertIn("type mismatch", params["diagnostics"][0]["message"])


if __name__ == "__main__":
    unittest.main()
