import os
import unittest

from support import ensure_import_path

ensure_import_path()

from gable_lsp.paths import (  # noqa: E402
    absolute_path,
    normalize_path,
    path_to_uri,
    same_path,
    uri_to_path,
)


class UriTest(unittest.TestCase):
    def test_plain_path(self):
        self.assertEqual(path_to_uri("/tmp/a.gable"), "file:///tmp/a.gable")

    def test_spaces_are_escaped(self):
        self.assertEqual(
            path_to_uri("/tmp/dir with spaces/t.gable"),
            "file:///tmp/dir%20with%20spaces/t.gable",
        )

    def test_non_ascii_is_escaped(self):
        self.assertEqual(path_to_uri("/proj/é.gable"), "file:///proj/%C3%A9.gable")

    def test_roundtrip(self):
        for path in ("/tmp/a.gable", "/tmp/dir with spaces/é.gable", "/tmp/a+b&c.gable"):
            self.assertEqual(uri_to_path(path_to_uri(path)), path)

    def test_relative_path_becomes_absolute(self):
        uri = path_to_uri("relative.gable")
        self.assertTrue(uri.startswith("file:///"))
        self.assertTrue(uri.endswith("/relative.gable"))

    def test_uri_to_path_accepts_bare_paths(self):
        self.assertEqual(uri_to_path("/tmp/a.gable"), "/tmp/a.gable")

    def test_unicode_uri_is_decoded(self):
        self.assertEqual(uri_to_path("file:///proj/%C3%A9.gable"), "/proj/é.gable")


class PathTest(unittest.TestCase):
    def test_absolute_path_normalises(self):
        self.assertEqual(absolute_path("/tmp/./a/../b.gable"), "/tmp/b.gable")

    def test_same_path_follows_symlinks(self):
        directory = os.path.dirname(os.path.abspath(__file__))
        self.assertTrue(same_path(directory, directory + "/."))
        self.assertFalse(same_path(directory, directory + "/nope"))

    def test_normalize_path_is_absolute(self):
        self.assertTrue(os.path.isabs(normalize_path("a.gable")))


if __name__ == "__main__":
    unittest.main()
