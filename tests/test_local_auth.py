import json
import os
import tempfile
import unittest
from unittest.mock import patch

from local_auth import AuthError, LocalAuth


class LocalAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.auth = LocalAuth(os.path.join(self.temp_dir.name, "auth.json"))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_setup_stores_hash_not_password(self):
        self.auth.setup("correct horse")
        with open(self.auth.path, encoding="utf-8") as handle:
            raw = handle.read()
        self.assertNotIn("correct horse", raw)
        self.assertTrue(self.auth.is_configured)

    def test_verify_and_change_password(self):
        self.auth.setup("first password")
        self.assertEqual((True, 0), self.auth.verify("first password"))
        self.auth.change_password("first password", "second password")
        self.assertEqual((True, 0), self.auth.verify("second password"))

    def test_minimum_password_length(self):
        with self.assertRaises(AuthError):
            self.auth.setup("short")

    def test_five_failures_trigger_persistent_lockout(self):
        self.auth.setup("correct password")
        with patch("local_auth.time.time", return_value=1000):
            for _ in range(4):
                self.assertEqual((False, 0), self.auth.verify("wrong password"))
            valid, wait = self.auth.verify("wrong password")
        self.assertFalse(valid)
        self.assertEqual(30, wait)
        with open(self.auth.path, encoding="utf-8") as handle:
            self.assertEqual(1030, json.load(handle)["locked_until"])


if __name__ == "__main__":
    unittest.main()
