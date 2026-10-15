import unittest

from app_ui import DEFAULT_PASSWORD_HASH, build_sanitize_query, sanitizer_kind


class SanitizerTests(unittest.TestCase):
    def test_only_exact_column_names_are_detected(self):
        self.assertEqual("password", sanitizer_kind("password"))
        self.assertEqual("email", sanitizer_kind("EMAIL"))
        self.assertEqual("phone", sanitizer_kind("phone"))
        self.assertIsNone(sanitizer_kind("email_password_hash"))
        self.assertIsNone(sanitizer_kind("customer_email"))

    def test_custom_exact_names_are_supported(self):
        self.assertEqual("phone", sanitizer_kind("mobile_number", "email", "mobile_number", "passwd"))
        self.assertEqual("password", sanitizer_kind("PASSWD", "email", "mobile_number", "passwd"))

    def test_password_update_covers_every_row(self):
        query = build_sanitize_query("users", "password", "password", DEFAULT_PASSWORD_HASH)
        self.assertEqual(
            "UPDATE `users` SET `password`='c4ca4238a0b923820dcc509a6f75849b';",
            query,
        )
        self.assertNotIn("WHERE", query)

    def test_email_and_phone_updates_skip_empty_values(self):
        query = build_sanitize_query("users", "email", "email", "test@example.com")
        self.assertIn("WHERE `email` IS NOT NULL AND `email`<>''", query)


if __name__ == "__main__":
    unittest.main()
