import os
import unittest

from support import CAPTURED, ensure_import_path

ensure_import_path()

from gable_lsp.diagnostics import (  # noqa: E402
    LineProvider,
    diagnostic_range,
    map_to_lsp,
    parse_check_output,
)
from gable_lsp.paths import path_to_uri  # noqa: E402


def captured(name):
    with open(os.path.join(CAPTURED, name), "r", encoding="utf-8") as handle:
        return handle.read()


class ParseTest(unittest.TestCase):
    def test_immut_fixture(self):
        parsed = parse_check_output(captured("immut.txt"))
        self.assertEqual(
            [(d.line, d.column, d.severity) for d in parsed.diagnostics],
            [(3, 5, "error"), (2, 9, "note"), (4, 12, "error"), (4, 12, "note"), (1, 1, "error")],
        )
        # The last one is the compiler's synthetic "<gable prelude>" source:
        # an executable with no entry point.
        self.assertEqual(parsed.diagnostics[-1].path, "<gable prelude>")
        self.assertEqual(parsed.diagnostics[0].span_length, 1)
        self.assertEqual(parsed.diagnostics[1].span_length, 1)
        self.assertIn("immutable binding", parsed.diagnostics[0].message)
        self.assertIsNone(parsed.input_error)
        self.assertEqual(parsed.unparsed, [])

    def test_summary_line_is_not_a_diagnostic(self):
        parsed = parse_check_output("gable: 2 error(s) in /proj/a.gable\n")
        self.assertEqual(parsed.diagnostics, [])

    def test_input_error(self):
        parsed = parse_check_output(captured("input_error.txt"))
        self.assertEqual(parsed.diagnostics, [])
        self.assertIsNotNone(parsed.input_error)
        self.assertIn("cannot read", parsed.input_error)

    def test_span_from_caret_run(self):
        parsed = parse_check_output(captured("caret_span.txt"))
        self.assertTrue(parsed.diagnostics)
        self.assertEqual(parsed.diagnostics[0].span_length, 4)

    def test_quoted_string_span(self):
        parsed = parse_check_output(captured("tsr_quote.txt"))
        spans = {d.span_length for d in parsed.diagnostics if d.severity == "error"}
        self.assertIn(7, spans)

    def test_paths_may_contain_colons(self):
        parsed = parse_check_output(
            'C:\\proj\\a.gable:12:3: error: boom\n'
        )
        self.assertEqual(parsed.diagnostics[0].path, "C:\\proj\\a.gable")
        self.assertEqual((parsed.diagnostics[0].line, parsed.diagnostics[0].column), (12, 3))

    def test_unrecognised_output_is_collected(self):
        parsed = parse_check_output("gable: internal compiler error: kaput\nrandom noise\n")
        self.assertEqual(parsed.diagnostics, [])
        self.assertEqual(len(parsed.unparsed), 2)


class RangeTest(unittest.TestCase):
    def test_ascii_span(self):
        parsed = parse_check_output("a.gable:1:5: error: x\ngable: 1 error(s) in a.gable\n")
        raw = parsed.diagnostics[0]
        rng = diagnostic_range(raw, "    let a: int = 1;", "utf-16")
        self.assertEqual(rng["start"], {"line": 0, "character": 4})
        self.assertEqual(rng["end"], {"line": 0, "character": 5})

    def test_span_is_clamped_to_line(self):
        raw = parse_check_output("a.gable:1:1: error: x\n").diagnostics[0]
        raw.span_length = 40
        rng = diagnostic_range(raw, "abc", "utf-16")
        self.assertEqual(rng["end"], {"line": 0, "character": 3})

    def test_utf16_column_conversion(self):
        raw = parse_check_output("a.gable:1:2: error: x\n").diagnostics[0]
        # One astral character before the column: unit 1 -> utf-16 unit 2.
        rng = diagnostic_range(raw, "😀bad", "utf-16")
        self.assertEqual(rng["start"], {"line": 0, "character": 2})

    def test_utf8_column_conversion(self):
        raw = parse_check_output("a.gable:1:2: error: x\n").diagnostics[0]
        rng = diagnostic_range(raw, "ébad", "utf-8")
        self.assertEqual(rng["start"], {"line": 0, "character": 2})

    def test_missing_line_text(self):
        raw = parse_check_output("a.gable:9:3: error: x\n").diagnostics[0]
        rng = diagnostic_range(raw, None, "utf-16")
        self.assertEqual(rng["start"], {"line": 8, "character": 2})


class MapTest(unittest.TestCase):
    def mapping_for(self, text, root="/proj/main.gable"):
        parsed = parse_check_output(text)
        provider = LineProvider("utf-16")
        return parsed, provider, root

    def test_temp_path_is_remapped_to_document_uri(self):
        parsed, provider, root = self.mapping_for(
            "/proj/.main.gable.lsp-check-1-1:1:1: error: bad\n"
            "  1 | nope\n"
            "    | ^~~~\n"
        )
        temp = "/proj/.main.gable.lsp-check-1-1"
        provider.add_snapshot(temp, "nope!\n")
        mapped = map_to_lsp(
            parsed,
            root_checked_path=temp,
            root_uri=path_to_uri(root),
            uri_for_path=path_to_uri,
            line_provider=provider,
        )
        self.assertEqual(list(mapped), [path_to_uri(root)])
        diagnostic = mapped[path_to_uri(root)][0]
        self.assertEqual(diagnostic["range"]["end"], {"line": 0, "character": 4})
        self.assertEqual(diagnostic["severity"], 1)
        self.assertEqual(diagnostic["source"], "gable")

    def test_notes_become_related_information(self):
        root = "/proj/main.gable"
        parsed, provider, _ = self.mapping_for(
            captured("immut.txt").replace("/proj/tests/fixtures/src/immut.gable", root)
        )
        provider.add_snapshot(root, "fn f() {\n    let a: i32 = 1;\n    a = 2;\n    return a;\n}\n")
        mapped = map_to_lsp(
            parsed,
            root_checked_path=root,
            root_uri=path_to_uri(root),
            uri_for_path=path_to_uri,
            line_provider=provider,
        )
        diagnostics = mapped[path_to_uri(root)]
        # Two real diagnostics plus the synthetic "<gable prelude>" entry-point
        # error, which is attributed to the checked document.
        self.assertEqual([d["severity"] for d in diagnostics], [1, 1, 1])
        self.assertEqual(diagnostics[2]["range"]["start"], {"line": 0, "character": 0})
        first, second = diagnostics[:2]
        self.assertEqual(first["relatedInformation"][0]["message"], "binding declared here")
        self.assertEqual(
            first["relatedInformation"][0]["location"]["range"]["start"],
            {"line": 1, "character": 8},
        )
        self.assertIn("explicit conversion", second["relatedInformation"][0]["message"])
        self.assertEqual(second["relatedInformation"][0]["location"]["range"]["start"], {"line": 3, "character": 11})

    def test_note_without_anchor_is_standalone_information(self):
        root = "/proj/main.gable"
        parsed, provider, _ = self.mapping_for(f"{root}:1:1: note: standalone\n")
        provider.add_snapshot(root, "x\n")
        mapped = map_to_lsp(
            parsed,
            root_checked_path=root,
            root_uri=path_to_uri(root),
            uri_for_path=path_to_uri,
            line_provider=provider,
        )
        diagnostic = mapped[path_to_uri(root)][0]
        self.assertEqual(diagnostic["severity"], 3)
        self.assertTrue(diagnostic["message"].startswith("note: "))
        self.assertNotIn("relatedInformation", diagnostic)

    def test_cross_file_diagnostic_marks_imported_document(self):
        parsed = parse_check_output(
            "/proj/bad.gable:1:1: error: bad\n/proj/main.gable:1:1: error: other\n"
        )
        provider = LineProvider("utf-16")
        provider.add_snapshot("/proj/main.gable", "import\n")
        mapped = map_to_lsp(
            parsed,
            root_checked_path="/proj/main.gable",
            root_uri=path_to_uri("/proj/main.gable"),
            uri_for_path=path_to_uri,
            line_provider=provider,
        )
        self.assertEqual(
            set(mapped), {path_to_uri("/proj/bad.gable"), path_to_uri("/proj/main.gable")}
        )

    def test_input_error_becomes_diagnostic_at_origin(self):
        parsed = parse_check_output("gable: error: cannot read '/proj/main.gable': no such file\n")
        provider = LineProvider("utf-16")
        provider.add_snapshot("/proj/main.gable", "")
        mapped = map_to_lsp(
            parsed,
            root_checked_path="/proj/main.gable",
            root_uri=path_to_uri("/proj/main.gable"),
            uri_for_path=path_to_uri,
            line_provider=provider,
        )
        diagnostics = mapped[path_to_uri("/proj/main.gable")]
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["severity"], 1)
        self.assertEqual(diagnostics[0]["range"]["start"], {"line": 0, "character": 0})

    def test_import_cycle_reported_on_importing_file(self):
        parsed = parse_check_output(captured("import_cycle.txt"))
        provider = LineProvider("utf-16")
        for name in ("a", "b"):
            provider.add_snapshot(f"/proj/{name}.gable", 'import "x"\n\nfn main() {\n}\n')
        mapped = map_to_lsp(
            parsed,
            root_checked_path="/proj/a.gable",
            root_uri=path_to_uri("/proj/a.gable"),
            uri_for_path=path_to_uri,
            line_provider=provider,
        )
        self.assertTrue(mapped)
        self.assertIn(path_to_uri("/proj/b.gable"), mapped)


if __name__ == "__main__":
    unittest.main()
