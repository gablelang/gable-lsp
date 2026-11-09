"""Unit tests for the debounce/coalesce checker, without a real server."""

import os
import shutil
import tempfile
import threading
import time
import unittest

from support import FIXTURE_BIN, ensure_import_path

ensure_import_path()

from gable_lsp.checker import CheckSnapshot, CheckerConfig, DiagnosticChecker  # noqa: E402
from gable_lsp.paths import path_to_uri  # noqa: E402


class CheckerHarness:
    """Drives DiagnosticChecker with a dict of snapshots and a publish log."""

    def __init__(self, directory, gable_path=FIXTURE_BIN, debounce_ms=20):
        self.directory = directory
        self.snapshots = {}
        self.published = []
        self.lock = threading.Lock()
        self.checker = DiagnosticChecker(
            snapshot_provider=self.snapshot_for,
            publish=self.publish,
            config=CheckerConfig(
                gable_path=gable_path, debounce_ms=debounce_ms, timeout_ms=5_000
            ),
            notify_config_error=self.on_config_error,
        )
        self.config_errors = []
        self.checker.start()

    def snapshot_for(self, uri):
        return self.snapshots.get(uri)

    def publish(self, uri, diagnostics, version):
        with self.lock:
            self.published.append((uri, diagnostics, version))

    def on_config_error(self, message):
        with self.lock:
            self.config_errors.append(message)

    def set_text(self, uri, path, text, version=1):
        self.snapshots[uri] = CheckSnapshot(uri, path, text, version, "utf-16")

    def wait_for(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                for entry in self.published:
                    if predicate(*entry):
                        return entry
            time.sleep(0.01)
        raise TimeoutError(f"no publish matched; saw {self.published!r}")

    def stop(self):
        self.checker.stop()


class CheckerTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="gable-lsp-checker-")
        self.harness = CheckerHarness(self.directory)
        self.path = os.path.join(self.directory, "main.gable")
        self.uri = path_to_uri(self.path)

    def tearDown(self):
        self.harness.stop()
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_reports_errors_for_the_buffer(self):
        self.harness.set_text(self.uri, self.path, "fn main() {\n    BAD\n}\n")
        self.harness.checker.schedule(self.uri, debounce=False)
        uri, diagnostics, version = self.harness.wait_for(lambda u, d, v: u == self.uri)
        self.assertEqual(version, 1)
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["range"]["start"], {"line": 1, "character": 4})

    def test_clean_buffer_publishes_empty(self):
        self.harness.set_text(self.uri, self.path, "fn main() {\n}\n")
        self.harness.checker.schedule(self.uri, debounce=False)
        self.harness.wait_for(lambda u, d, v: u == self.uri)
        self.assertEqual(self.harness.published[-1][1], [])

    def test_stale_version_is_discarded(self):
        self.harness.set_text(self.uri, self.path, "fn main() {\n    BAD\n}\n", version=1)
        self.harness.checker.schedule(self.uri, debounce=False)
        # Change the buffer immediately: the compile in flight must be dropped.
        self.harness.set_text(self.uri, self.path, "fn main() {\n}\n", version=2)
        self.harness.wait_for(lambda u, d, v: u == self.uri and v == 2)
        versions = [v for _, _, v in self.harness.published]
        self.assertNotIn(1, versions)

    def test_debounce_coalesces_a_typing_burst(self):
        self.harness.snapshots[self.uri] = CheckSnapshot(
            self.uri, self.path, "fn main() {\n}\n", 0, "utf-16"
        )
        for version in range(1, 40):
            self.harness.set_text(self.uri, self.path, "fn main() {\n}\n", version=version)
            self.harness.checker.schedule(self.uri, debounce=True)
        self.harness.wait_for(lambda u, d, v: u == self.uri)
        time.sleep(0.3)
        self.assertLessEqual(len(self.harness.published), 3)

    def test_no_temp_files_survive_a_check(self):
        # The document does not exist on disk, so the checker must use a
        # sibling temp file — and delete it when the compile finishes.
        self.harness.set_text(self.uri, self.path, "fn main() {\n    BAD\n}\n")
        self.harness.checker.schedule(self.uri, debounce=False)
        uri, diagnostics, _ = self.harness.wait_for(lambda u, d, v: u == self.uri)
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(os.listdir(self.directory), [])

    def test_temp_path_is_remapped_onto_the_document_uri(self):
        # The fake compiler puts the path it checked in the message, so this
        # proves the temp path was rewritten to the buffer's URI.
        self.harness.set_text(self.uri, self.path, "fn main() {\n    BAD\n}\n")
        self.harness.checker.schedule(self.uri, debounce=False)
        uri, diagnostics, _ = self.harness.wait_for(lambda u, d, v: u == self.uri)
        self.assertEqual(uri, self.uri)
        self.assertNotIn(".lsp-check-", diagnostics[0]["message"])

    def test_missing_binary_reports_a_config_error_once(self):
        harness = CheckerHarness(self.directory, gable_path=os.path.join(self.directory, "nope"))
        try:
            harness.set_text(self.uri, self.path, "fn main() {\n}\n")
            harness.checker.schedule(self.uri, debounce=False)
            harness.checker.schedule(self.uri, debounce=False)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not harness.config_errors:
                time.sleep(0.01)
            self.assertEqual(len(harness.config_errors), 1)
            self.assertIn("Gable compiler", harness.config_errors[0])
        finally:
            harness.stop()

    def test_clear_publishes_empty_and_forgets_the_document(self):
        self.harness.set_text(self.uri, self.path, "fn main() {\n    BAD\n}\n")
        self.harness.checker.schedule(self.uri, debounce=False)
        self.harness.wait_for(lambda u, d, v: u == self.uri and d)
        self.harness.checker.clear(self.uri)
        self.harness.wait_for(lambda u, d, v: u == self.uri and not d)

    def test_document_without_a_path_is_skipped(self):
        self.harness.set_text(self.uri, None, "fn main() {\n    BAD\n}\n")
        self.harness.checker.schedule(self.uri, debounce=False)
        time.sleep(0.5)
        self.assertEqual(self.harness.published, [])
        self.assertEqual(self.harness.config_errors, [])

    def test_retraction_is_scoped_to_the_checking_root(self):
        # Two documents report on the same imported file. The most recent
        # publisher owns it; an older root going clean must not clear it.
        harness = self.harness
        diag = {
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
            "severity": 1,
            "source": "gable",
            "message": "boom",
        }
        a, b, shared = "file:///a.gable", "file:///b.gable", "file:///shared.gable"
        harness.checker._publish_map(a, {a: [], shared: [dict(diag)]}, 1)
        harness.checker._publish_map(b, {b: [], shared: [dict(diag)]}, 1)
        harness.checker._publish_map(a, {a: []}, 2)   # a.gable is clean again
        harness.checker._publish_map(b, {b: []}, 2)   # b.gable is clean again
        publications = [(uri, bool(diags)) for uri, diags, _ in harness.published]
        # a publishes [a: clean, shared: error]; b republishes shared as its own.
        self.assertEqual(publications[:2], [(a, False), (shared, True)])
        self.assertEqual(publications[2:4], [(b, False), (shared, True)])
        # a going clean must NOT retract shared, because b now owns it.
        self.assertEqual(publications[4], (a, False))
        self.assertNotIn((shared, False), publications[:5])
        # Only when b also goes clean does shared get retracted.
        self.assertEqual(publications[5:], [(b, False), (shared, False)])
        # Every root's record now lists only the root itself (empty result sets
        # are still published, which is how a clean file retracts its errors).
        self.assertEqual(harness.checker._published, {a: {a}, b: {b}})

    def test_stopping_is_idempotent(self):
        self.harness.checker.stop()
        self.harness.checker.stop()


if __name__ == "__main__":
    unittest.main()
