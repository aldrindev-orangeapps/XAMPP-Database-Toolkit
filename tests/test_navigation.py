import unittest

from app_ui import NAVIGATION_PAGES


class NavigationTests(unittest.TestCase):
    def test_primary_workflow_order(self):
        self.assertEqual(
            ("Databases", "Import", "Sanitizer", "SQL Console"),
            NAVIGATION_PAGES[:4],
        )

    def test_user_manual_is_available(self):
        self.assertIn("User Manual", NAVIGATION_PAGES)


if __name__ == "__main__":
    unittest.main()
