import unittest

from support import ensure_import_path

ensure_import_path()

from gable_lsp.settings import Settings  # noqa: E402


class SettingsTest(unittest.TestCase):
    def test_defaults(self):
        settings = Settings()
        self.assertEqual(settings.gable_path, "gable")
        self.assertEqual(settings.debounce_ms, 150)
        self.assertEqual(settings.check_timeout_ms, 10_000)
        self.assertEqual(settings.format_timeout_ms, 5_000)

    def test_nested_initialization_options(self):
        settings = Settings.from_mapping(
            {"gable": {"path": "/opt/gable", "check.debounceMs": 25, "check.timeoutMs": 2000}}
        )
        self.assertEqual(settings.gable_path, "/opt/gable")
        self.assertEqual(settings.debounce_ms, 25)
        self.assertEqual(settings.check_timeout_ms, 2000)

    def test_flat_dotted_keys(self):
        settings = Settings.from_mapping({"gable.path": "/bin/gable", "gable.format.timeoutMs": 900})
        self.assertEqual(settings.gable_path, "/bin/gable")
        self.assertEqual(settings.format_timeout_ms, 900)

    def test_garbage_is_ignored(self):
        settings = Settings.from_mapping(
            {"gable": {"path": 42, "check.debounceMs": "fast", "check.timeoutMs": None}}
        )
        self.assertEqual(settings, Settings())
        self.assertEqual(Settings.from_mapping(None), Settings())

    def test_booleans_are_not_ints(self):
        self.assertEqual(Settings.from_mapping({"gable": {"check.debounceMs": True}}).debounce_ms, 150)

    def test_values_are_clamped(self):
        settings = Settings.from_mapping(
            {"gable": {"check.debounceMs": -5, "check.timeoutMs": 10**9, "format.timeoutMs": 1}}
        )
        self.assertEqual(settings.debounce_ms, 0)
        self.assertEqual(settings.check_timeout_ms, 120_000)
        self.assertEqual(settings.format_timeout_ms, 100)

    def test_blank_path_falls_back(self):
        self.assertEqual(Settings.from_mapping({"gable": {"path": "   "}}).gable_path, "gable")

    def test_merge_keeps_unset_values(self):
        base = Settings(gable_path="/old", debounce_ms=10)
        merged = Settings.from_mapping({"gable": {"path": "/new"}}, base=base)
        self.assertEqual(merged.gable_path, "/new")
        self.assertEqual(merged.debounce_ms, 10)


if __name__ == "__main__":
    unittest.main()
