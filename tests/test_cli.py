"""CLI surface: argument parsing and process-level behaviour."""

import json
import os
import subprocess
import sys
import unittest

from support import REPO_ROOT, SRC_ROOT, ServerProcess, ensure_import_path

ensure_import_path()

from gable_lsp.main import build_parser, settings_from_args  # noqa: E402


class ParserTest(unittest.TestCase):
    def test_defaults(self):
        args = build_parser().parse_args([])
        self.assertFalse(args.tcp)
        self.assertFalse(args.stdio)
        self.assertEqual(args.port, 9257)
        self.assertEqual(args.host, "0.0.0.0")
        settings = settings_from_args(args)
        self.assertEqual(settings.gable_path, "gable")
        self.assertEqual(settings.debounce_ms, 150)

    def test_overrides(self):
        args = build_parser().parse_args(
            [
                "--gable-path", "/opt/gable",
                "--check-debounce-ms", "0",
                "--check-timeout-ms", "2500",
                "--format-timeout-ms", "800",
            ]
        )
        settings = settings_from_args(args)
        self.assertEqual(settings.gable_path, "/opt/gable")
        self.assertEqual(settings.debounce_ms, 0)
        self.assertEqual(settings.check_timeout_ms, 2500)
        self.assertEqual(settings.format_timeout_ms, 800)

    def test_cli_values_are_clamped(self):
        args = build_parser().parse_args(["--check-debounce-ms", "-10", "--check-timeout-ms", "1"])
        settings = settings_from_args(args)
        self.assertEqual(settings.debounce_ms, 0)
        self.assertEqual(settings.check_timeout_ms, 100)

    def test_stdio_and_tcp_are_exclusive(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--stdio", "--tcp"])


class ProcessTest(unittest.TestCase):
    def run_cli(self, *arguments):
        env = dict(os.environ)
        env["PYTHONPATH"] = SRC_ROOT
        return subprocess.run(
            [sys.executable, "-m", "gable_lsp", *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=REPO_ROOT,
            env=env,
            timeout=30,
        )

    def test_version_goes_to_stdout(self):
        completed = self.run_cli("--version")
        self.assertEqual(completed.returncode, 0)
        self.assertTrue(completed.stdout.decode().startswith("gable-lsp "))

    def test_help_mentions_both_transports(self):
        completed = self.run_cli("--help")
        help_text = completed.stdout.decode()
        self.assertEqual(completed.returncode, 0)
        self.assertIn("--stdio", help_text)
        self.assertIn("--tcp", help_text)
        self.assertIn("--gable-path", help_text)

    def test_nothing_but_frames_on_stdout(self):
        # With --log-level debug, every log line must still go to stderr.
        server = ServerProcess(extra_args=["--log-level", "debug"])
        try:
            response = server.initialize()
            self.assertEqual(response["id"], 1)
            server.shutdown()
        finally:
            server.kill()
        self.assertEqual(server.proc.returncode, 0)

    def test_bad_port_is_rejected(self):
        # argparse rejects non-integers before the server starts.
        completed = self.run_cli("--tcp", "--port", "not-a-number")
        self.assertEqual(completed.returncode, 2)
        self.assertIn(b"invalid int value", completed.stderr)


if __name__ == "__main__":
    unittest.main()
