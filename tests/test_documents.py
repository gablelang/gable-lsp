import unittest

from support import ensure_import_path

ensure_import_path()

from gable_lsp.documents import DocumentStore, TextDocument  # noqa: E402


class FullSyncTest(unittest.TestCase):
    def test_open_and_full_replace(self):
        store = DocumentStore()
        document = store.open("file:///a.gable", "gable", 1, "one\ntwo")
        self.assertEqual(document.version, 1)
        store.change("file:///a.gable", [{"text": "replaced"}], 2)
        self.assertEqual(document.text, "replaced")
        self.assertEqual(document.version, 2)
        self.assertEqual(document.line_text(0), "replaced")

    def test_change_of_unknown_document_is_none(self):
        store = DocumentStore()
        self.assertIsNone(store.change("file:///nope.gable", [{"text": "x"}], 1))

    def test_close(self):
        store = DocumentStore()
        store.open("file:///a.gable", "gable", 1, "")
        self.assertIsNotNone(store.close("file:///a.gable"))
        self.assertIsNone(store.get("file:///a.gable"))
        self.assertEqual(len(store), 0)


class IncrementalSyncTest(unittest.TestCase):
    def setUp(self):
        self.document = TextDocument("file:///a.gable", "gable", 1, "abc\ndef\n")

    def change(self, start, end, text, version=None):
        self.document.apply_incremental(
            [
                {
                    "range": {"start": start, "end": end},
                    "text": text,
                }
            ],
            version,
        )

    def test_insert_within_line(self):
        self.change({"line": 0, "character": 1}, {"line": 0, "character": 1}, "XY")
        self.assertEqual(self.document.text, "aXYbc\ndef\n")

    def test_delete_within_line(self):
        self.change({"line": 0, "character": 1}, {"line": 0, "character": 3}, "")
        self.assertEqual(self.document.text, "a\ndef\n")

    def test_replace_across_lines(self):
        self.change({"line": 0, "character": 2}, {"line": 1, "character": 1}, "!")
        self.assertEqual(self.document.text, "ab!ef\n")

    def test_append_at_end(self):
        end = {"line": 2, "character": 0}
        self.change(end, end, "ghi\n")
        self.assertEqual(self.document.text, "abc\ndef\nghi\n")

    def test_multiple_changes_apply_in_order(self):
        self.document.apply_incremental(
            [
                {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 3}}, "text": "xyz"},
                {"range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 3}}, "text": "uvw"},
            ],
            5,
        )
        self.assertEqual(self.document.text, "xyz\nuvw\n")
        self.assertEqual(self.document.version, 5)

    def test_full_text_change_inside_incremental_list(self):
        self.document.apply_incremental([{"text": "brand new"}], 2)
        self.assertEqual(self.document.text, "brand new")
        self.assertEqual(self.document.version, 2)

    def test_astral_characters_use_negotiated_units(self):
        document = TextDocument("file:///a.gable", "gable", 1, "a😀b\n")
        # utf-16 default: the emoji is two units, so column 3 is after it.
        document.apply_incremental(
            [{"range": {"start": {"line": 0, "character": 3}, "end": {"line": 0, "character": 3}}, "text": "!"}],
            2,
        )
        self.assertEqual(document.text, "a😀!b\n")

        document = TextDocument("file:///b.gable", "gable", 1, "a😀b\n")
        document.set_encoding("utf-8")
        document.apply_incremental(
            [{"range": {"start": {"line": 0, "character": 5}, "end": {"line": 0, "character": 5}}, "text": "!"}],
            2,
        )
        self.assertEqual(document.text, "a😀!b\n")

    def test_crlf_lines(self):
        document = TextDocument("file:///a.gable", "gable", 1, "one\r\ntwo\r\n")
        document.apply_incremental(
            [{"range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 3}}, "text": "TWO"}],
            2,
        )
        self.assertEqual(document.text, "one\r\nTWO\r\n")

    def test_store_change_tracks_version(self):
        store = DocumentStore()
        store.open("file:///a.gable", "gable", 1, "x")
        document = store.change(
            "file:///a.gable",
            [{"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}, "text": "y"}],
            7,
        )
        assert document is not None
        self.assertEqual(document.text, "y")
        self.assertEqual(document.version, 7)


if __name__ == "__main__":
    unittest.main()
