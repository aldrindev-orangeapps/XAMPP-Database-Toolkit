import os
import unittest


ROOT = os.path.dirname(os.path.dirname(__file__))


class PackagingTests(unittest.TestCase):
    def test_installer_is_per_user_and_has_uninstall_registration(self):
        path = os.path.join(ROOT, "installer", "XAMPP Database Toolkit.nsi")
        with open(path, encoding="utf-8") as handle:
            script = handle.read()

        self.assertIn("RequestExecutionLevel user", script)
        self.assertIn("$LOCALAPPDATA\\Programs", script)
        self.assertIn("WriteUninstaller", script)
        self.assertIn("WriteRegStr HKCU", script)
        self.assertIn("Desktop shortcut", script)

    def test_tag_release_builds_and_publishes_installer(self):
        path = os.path.join(ROOT, ".github", "workflows", "release.yml")
        with open(path, encoding="utf-8") as handle:
            workflow = handle.read()

        self.assertIn("build_installer.ps1", workflow)
        self.assertIn("INSTALLER_PATH", workflow)
        self.assertIn("$installer", workflow)


if __name__ == "__main__":
    unittest.main()
