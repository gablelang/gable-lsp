import os
import shutil
import sys
import tempfile
import unittest

from support import FIXTURE_BIN, ensure_import_path

ensure_import_path()

from gable_lsp.encoding import LineIndex  # noqa: E402
from gable_lsp.formatting import FormatterConfig, diff_edits, format_buffer  # noqa: E402


def apply_edits(text, edits, encoding="utf-16"):
    """Apply LSP TextEdits the way an editor would (last edit first)."""

    index = LineIndex(text, encoding)
    for edit in sorted(
        edits,
        key=lambda e: (e["range"]["start"]["line"], e["range"]["start"]["character"]),
        reverse=True,
    ):
        start = index.offset_at(edit["range"]["start"]["line"], edit["range"]["start"]["character"])
        end = index.offset_at(edit["range"]["end"]["line"], edit["range"]["end"]["character"])
        text = text[:start] + edit["newText"] + text[end:]
        index = LineIndex(text, encoding)
    return text


class DiffTest(unittest.TestCase):
    def test_identical_text_has_no_edits(self):
        self.assertEqual(diff_edits("a\nb\n", "a\nb\n", "utf-16"), [])

    def test_line_indentation_roundtrip(self):
        old = "fn main() {\n  let x: int = 1\n   print(x)\n}\n"
        new = "fn main() {\n    let x: int = 1\n    print(x)\n}\n"
        edits = diff_edits(old, new, "utf-16")
        self.assertEqual(apply_edits(old, edits), new)

    def test_edits_are_ordered_and_disjoint(self):
        old = "a\nb\nc\nd\ne\n"
        new = "a\nB\nc\nD\ne\n"
        edits = diff_edits(old, new, "utf-16")
        starts = [e["range"]["start"] for e in edits]
        self.assertEqual(starts, sorted(starts, key=lambda p: (p["line"], p["character"])))
        self.assertEqual(apply_edits(old, edits), new)

    def test_crlf_is_preserved(self):
        old = "a\r\nb\r\n"
        new = "a\r\n  b\r\n"
        self.assertEqual(apply_edits(old, diff_edits(old, new, "utf-16")), new)
        # The compiler emits LF, so a CRLF buffer reformatted to LF text must
        # still produce exact edits (this is what the diff sees).
        self.assertEqual(apply_edits(old, diff_edits(old, "a\n  b\n", "utf-16")), "a\n  b\n")

    def test_missing_final_newline_is_added(self):
        self.assertEqual(apply_edits("abc", diff_edits("abc", "abc\n", "utf-16")), "abc\n")

    def test_final_newline_removed(self):
        self.assertEqual(apply_edits("abc\n", diff_edits("abc\n", "abc", "utf-16")), "abc")

    def test_empty_document(self):
        edits = diff_edits("", "fn main() {\n}\n", "utf-16")
        self.assertEqual(apply_edits("", edits), "fn main() {\n}\n")

    def test_utf16_columns_on_astral_lines(self):
        old = "print(\"😀\")\n"
        new = "print(\"😀😀\")\n"
        edits = diff_edits(old, new, "utf-16")
        self.assertEqual(apply_edits(old, edits), new)


class FormatBufferTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="gable-lsp-fmt-")

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_formats_through_the_cli(self):
        path = os.path.join(self.directory, "a.gable")
        original = "fn main() {\nMESSY print(1)\n}\n"
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(original)
        edits = format_buffer(
            original,
            real_path=path,
            config=FormatterConfig(gable_path=FIXTURE_BIN),
            encoding="utf-16",
        )
        assert edits is not None
        self.assertEqual(apply_edits(original, edits), "fn main() {\n    print(1)\n}\n")

    def test_the_real_file_is_never_modified(self):
        path = os.path.join(self.directory, "a.gable")
        original = "MESSY keep me\n"
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(original)
        format_buffer(
            original,
            real_path=path,
            config=FormatterConfig(gable_path=FIXTURE_BIN),
            encoding="utf-16",
        )
        with open(path, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), original)

    def test_no_temp_files_are_left_behind(self):
        path = os.path.join(self.directory, "a.gable")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("MESSY x\n")
        format_buffer(
            "MESSY x\n",
            real_path=path,
            config=FormatterConfig(gable_path=FIXTURE_BIN),
            encoding="utf-16",
        )
        self.assertEqual(os.listdir(self.directory), ["a.gable"])

    def test_unparsable_buffer_is_untouched(self):
        path = os.path.join(self.directory, "a.gable")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("BAD\n")
        self.assertIsNone(
            format_buffer(
                "BAD\n",
                real_path=path,
                config=FormatterConfig(gable_path=FIXTURE_BIN),
                encoding="utf-16",
            )
        )

    def test_missing_binary_returns_none(self):
        path = os.path.join(self.directory, "a.gable")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("MESSY x\n")
        self.assertIsNone(
            format_buffer(
                "MESSY x\n",
                real_path=path,
                config=FormatterConfig(gable_path=os.path.join(self.directory, "no-such-gable")),
                encoding="utf-16",
            )
        )

    def test_missing_directory_returns_none(self):
        self.assertIsNone(
            format_buffer(
                "MESSY x\n",
                real_path=os.path.join(self.directory, "gone", "a.gable"),
                config=FormatterConfig(gable_path=FIXTURE_BIN),
                encoding="utf-16",
            )
        )

    def test_unsaved_buffer_is_formatted_from_memory(self):
        path = os.path.join(self.directory, "a.gable")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("stale contents on disk\n")
        edits = format_buffer(
            "MESSY new\n",
            real_path=path,
            config=FormatterConfig(gable_path=FIXTURE_BIN),
            encoding="utf-16",
        )
        assert edits is not None
        self.assertEqual(apply_edits("MESSY new\n", edits), "    new\n")


if __name__ == "__main__":
    unittest.main()
