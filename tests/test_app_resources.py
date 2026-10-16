import unittest
from unittest.mock import patch

import app_ui


class FontResourceTests(unittest.TestCase):
    def tearDown(self):
        app_ui._registered_font_paths.clear()

    @patch("app_ui.os.path.isfile", return_value=True)
    @patch("app_ui.resource_path", side_effect=lambda path: path)
    def test_registered_private_fonts_are_released(self, _resource_path, _isfile):
        with (
            patch("app_ui.os.name", "nt"),
            patch.object(app_ui.ctypes.windll.gdi32, "AddFontResourceExW", return_value=1) as add,
            patch.object(app_ui.ctypes.windll.gdi32, "RemoveFontResourceExW", return_value=1) as remove,
        ):
            self.assertEqual("Poppins", app_ui.register_poppins())
            app_ui.unregister_poppins()

        self.assertEqual(3, add.call_count)
        self.assertEqual(3, remove.call_count)
        self.assertEqual([], app_ui._registered_font_paths)

    def test_unregister_is_safe_when_no_font_was_loaded(self):
        app_ui.unregister_poppins()
        self.assertEqual([], app_ui._registered_font_paths)


if __name__ == "__main__":
    unittest.main()
