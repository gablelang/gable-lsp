"""Sanity checks over all captured compiler output.

These tests are intentionally tolerant: they pin the properties the server
relies on (paths, severities, spans) without freezing every message string, so
a compiler that improves its wording does not break the build.
"""

import glob
import os
import unittest

from support import CAPTURED, ensure_import_path

ensure_import_path()

from gable_lsp.diagnostics import parse_check_output  # noqa: E402


def fixture_paths():
    return sorted(glob.glob(os.path.join(CAPTURED, "*.txt")))


class CapturedFixturesTest(unittest.TestCase):
    def test_fixtures_exist(self):
        self.assertGreaterEqual(len(fixture_paths()), 9)

    def test_every_fixture_is_parseable(self):
        for path in fixture_paths():
            with self.subTest(fixture=os.path.basename(path)):
                with open(path, "r", encoding="utf-8") as handle:
                    parsed = parse_check_output(handle.read())
                self.assertEqual(parsed.unparsed, [], f"unparsed output in {path}")
                self.assertTrue(
                    parsed.diagnostics or parsed.input_error,
                    f"nothing was recognised in {path}",
                )

    def test_input_error_fixture_has_no_diagnostics(self):
        with open(os.path.join(CAPTURED, "input_error.txt"), "r", encoding="utf-8") as handle:
            parsed = parse_check_output(handle.read())
        self.assertEqual(parsed.diagnostics, [])
        self.assertIn("cannot read", parsed.input_error or "")

    def test_spans_are_recovered_from_carets(self):
        with open(os.path.join(CAPTURED, "caret_span.txt"), "r", encoding="utf-8") as handle:
            parsed = parse_check_output(handle.read())
        self.assertEqual(parsed.diagnostics[0].span_length, 4)
        with open(os.path.join(CAPTURED, "tsr_quote.txt"), "r", encoding="utf-8") as handle:
            parsed = parse_check_output(handle.read())
        self.assertEqual(parsed.diagnostics[0].span_length, 7)

    def test_imports_underline_the_whole_directive(self):
        with open(os.path.join(CAPTURED, "missing_import.txt"), "r", encoding="utf-8") as handle:
            parsed = parse_check_output(handle.read())
        error = parsed.diagnostics[0]
        self.assertIn("cannot import", error.message)
        self.assertGreater(error.span_length, len("import"))

    def test_one_based_positions(self):
        for path in fixture_paths():
            with open(path, "r", encoding="utf-8") as handle:
                parsed = parse_check_output(handle.read())
            for diagnostic in parsed.diagnostics:
                self.assertGreaterEqual(diagnostic.line, 1, path)
                self.assertGreaterEqual(diagnostic.column, 1, path)

    def test_synthetic_prelude_paths_are_recognised(self):
        with open(os.path.join(CAPTURED, "immut.txt"), "r", encoding="utf-8") as handle:
            parsed = parse_check_output(handle.read())
        synthetic = [d for d in parsed.diagnostics if d.path.startswith("<")]
        self.assertTrue(synthetic)
        self.assertEqual(synthetic[0].path, "<gable prelude>")


if __name__ == "__main__":
    unittest.main()
