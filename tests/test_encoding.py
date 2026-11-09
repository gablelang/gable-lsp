import unittest

from support import ensure_import_path

ensure_import_path()

from gable_lsp.encoding import (  # noqa: E402
    LineIndex,
    pick_encoding,
    split_line_offsets,
    units_of,
)


class PickEncodingTest(unittest.TestCase):
    def test_defaults_to_utf16(self):
        self.assertEqual(pick_encoding(None), "utf-16")
        self.assertEqual(pick_encoding("utf-8"), "utf-16")
        self.assertEqual(pick_encoding([]), "utf-16")

    def test_prefers_first_supported_offer(self):
        self.assertEqual(pick_encoding(["utf-8", "utf-16"]), "utf-8")
        self.assertEqual(pick_encoding(["utf-32", "utf-8"]), "utf-32")

    def test_ignores_unknown_entries(self):
        self.assertEqual(pick_encoding(["latin-1", "utf-16"]), "utf-16")
        self.assertEqual(pick_encoding(["latin-1", "utf-16", "utf-8"]), "utf-16")
        self.assertEqual(pick_encoding(["latin-1"]), "utf-16")

    def test_case_insensitive(self):
        self.assertEqual(pick_encoding(["UTF-8"]), "utf-8")


class UnitsTest(unittest.TestCase):
    def test_ascii(self):
        for encoding in ("utf-8", "utf-16", "utf-32"):
            self.assertEqual(units_of("abc", encoding), 3)

    def test_bmp_and_astral(self):
        self.assertEqual(units_of("é", "utf-16"), 1)
        self.assertEqual(units_of("é", "utf-8"), 2)
        self.assertEqual(units_of("😀", "utf-16"), 2)
        self.assertEqual(units_of("😀", "utf-8"), 4)
        self.assertEqual(units_of("😀", "utf-32"), 1)


class LineIndexTest(unittest.TestCase):
    TEXT = "abc\r\ndefg\n\nh"

    def test_line_count_and_text(self):
        index = LineIndex(self.TEXT)
        self.assertEqual(index.line_count, 4)
        self.assertEqual(index.line_text(0), "abc")
        self.assertEqual(index.line_text(1), "defg")
        self.assertEqual(index.line_text(2), "")
        self.assertEqual(index.line_text(3), "h")
        self.assertEqual(index.line_text(99), "")

    def test_offsets_roundtrip(self):
        index = LineIndex(self.TEXT)
        for line in range(index.line_count):
            text = index.line_text(line)
            for character in range(len(text) + 1):
                offset = index.offset_at(line, character)
                self.assertEqual(index.position_at(offset), (line, character))

    def test_offset_at_clamps(self):
        index = LineIndex(self.TEXT)
        self.assertEqual(index.offset_at(99, 0), len(self.TEXT))
        self.assertEqual(index.offset_at(0, 99), 3)  # end of "abc", before CRLF

    def test_lone_carriage_return(self):
        index = LineIndex("a\rb")
        self.assertEqual(index.line_count, 2)
        self.assertEqual(index.position_at(2), (1, 0))

    def test_utf16_character_positions(self):
        index = LineIndex("a😀b")
        self.assertEqual(index.offset_at(0, 3), 2)   # after the surrogate pair
        self.assertEqual(index.offset_at(0, 2), 1)   # middle of the pair clamps
        index = LineIndex("a😀b", "utf-8")
        self.assertEqual(index.offset_at(0, 5), 2)
        index = LineIndex("a😀b", "utf-32")
        self.assertEqual(index.offset_at(0, 2), 2)


class SplitLineOffsetsTest(unittest.TestCase):
    def test_terminators(self):
        self.assertEqual(split_line_offsets("a\r\nb"), [(0, 1, 3), (3, 4, 4)])
        self.assertEqual(split_line_offsets("a\nb\n"), [(0, 1, 2), (2, 3, 4), (4, 4, 4)])
        self.assertEqual(split_line_offsets(""), [(0, 0, 0)])
        self.assertEqual(split_line_offsets("a\rb\nc"), [(0, 1, 2), (2, 3, 4), (4, 5, 5)])


if __name__ == "__main__":
    unittest.main()
