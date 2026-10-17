import unittest
from unittest.mock import Mock, call, patch

from app_ui import IDLE_LOGOUT_SECONDS, ModernApp
from mysql_service import MySQLConfig


class AppUiWorkflowTests(unittest.TestCase):
    def test_database_deletion_uses_confirmation_button_and_quotes_name(self):
        app = ModernApp.__new__(ModernApp)
        app.importer = None
        app.import_active = False
        app.selected_db = Mock()
        app.selected_db.get.return_value = "client`archive"
        app._confirm = Mock(return_value=True)
        app._sync_config = Mock()
        app._mysql_config = Mock(return_value=MySQLConfig("mysql.exe", "127.0.0.1", "3306"))
        app._set_database_busy = Mock()
        app.service = Mock()
        app.checkpoints = Mock()
        app.checkpoints.remove_for_database.return_value = 0
        app.log = Mock()
        app._delete_database_finished = Mock()
        app._ui = lambda callback, *args: callback(*args)
        app._thread = lambda worker: worker()

        app.delete_database()

        app._confirm.assert_called_once_with(
            "Delete database",
            "Permanently delete the database 'client`archive'?\n\nThis cannot be undone.",
            True,
            confirm_label="Delete database",
        )
        app.service.run.assert_called_once_with(["-e", "DROP DATABASE `client``archive`;"])

    def test_cancelled_database_confirmation_does_not_delete(self):
        app = ModernApp.__new__(ModernApp)
        app.importer = None
        app.import_active = False
        app.selected_db = Mock()
        app.selected_db.get.return_value = "customer_data"
        app._confirm = Mock(return_value=False)
        app._sync_config = Mock()

        app.delete_database()

        app._sync_config.assert_not_called()

    @patch("app_ui.simpledialog.askstring", return_value="source_copy")
    def test_database_copy_creates_new_destination_and_streams_data(self, _askstring):
        app = ModernApp.__new__(ModernApp)
        app.importer = None
        app.import_active = False
        app.database_copy_active = False
        app.databases = ["source"]
        app.selected_db = Mock()
        app.selected_db.get.return_value = "source"
        app.root = Mock()
        app._confirm = Mock(return_value=True)
        app._sync_config = Mock()
        app._set_database_busy = Mock()
        app.service = Mock()
        app.log = Mock()
        app._copy_database_finished = Mock()
        app._ui = lambda callback, *args: callback(*args)
        app._thread = lambda worker: worker()

        app.copy_database()

        app.service.run.assert_called_once_with([
            "-e", "CREATE DATABASE `source_copy` CHARACTER SET utf8mb4;",
        ])
        app.service.copy_database.assert_called_once_with("source", "source_copy")
        app._copy_database_finished.assert_called_once_with(
            "source", "source_copy", None, None,
        )

    @patch("app_ui.simpledialog.askstring", return_value="existing")
    def test_database_copy_never_overwrites_existing_destination(self, _askstring):
        app = ModernApp.__new__(ModernApp)
        app.importer = None
        app.import_active = False
        app.database_copy_active = False
        app.databases = ["source", "Existing"]
        app.selected_db = Mock()
        app.selected_db.get.return_value = "source"
        app.root = Mock()
        app._alert = Mock()
        app._confirm = Mock()

        app.copy_database()

        app._alert.assert_called_once()
        app._confirm.assert_not_called()

    @patch("app_ui.simpledialog.askstring", return_value="source_copy")
    def test_failed_database_copy_removes_partial_destination(self, _askstring):
        app = ModernApp.__new__(ModernApp)
        app.importer = None
        app.import_active = False
        app.database_copy_active = False
        app.databases = ["source"]
        app.selected_db = Mock()
        app.selected_db.get.return_value = "source"
        app.root = Mock()
        app._confirm = Mock(return_value=True)
        app._sync_config = Mock()
        app._set_database_busy = Mock()
        app.service = Mock()
        app.service.copy_database.side_effect = RuntimeError("dump failed")
        app.log = Mock()
        app._copy_database_finished = Mock()
        app._ui = lambda callback, *args: callback(*args)
        app._thread = lambda worker: worker()

        app.copy_database()

        self.assertEqual(
            [
                call([
                    "-e", "CREATE DATABASE `source_copy` CHARACTER SET utf8mb4;",
                ]),
                call(["-e", "DROP DATABASE `source_copy`;"]),
            ],
            app.service.run.call_args_list,
        )
        app._copy_database_finished.assert_called_once_with(
            "source", "source_copy", "dump failed", None,
        )

    @patch("app_ui.time.monotonic", return_value=IDLE_LOGOUT_SECONDS + 10)
    def test_idle_logout_is_suspended_during_import(self, _monotonic):
        app = ModernApp.__new__(ModernApp)
        app.authenticated = True
        app.import_active = True
        app.database_copy_active = False
        app.importer = None
        app.last_activity = 0
        app.logout = Mock()
        app.root = Mock()

        app._check_idle_logout()

        app.logout.assert_not_called()
        self.assertEqual(IDLE_LOGOUT_SECONDS + 10, app.last_activity)
        app.root.after.assert_called_once_with(30_000, app._check_idle_logout)

    @patch("app_ui.time.monotonic", return_value=IDLE_LOGOUT_SECONDS + 10)
    def test_idle_logout_still_applies_without_import(self, _monotonic):
        app = ModernApp.__new__(ModernApp)
        app.authenticated = True
        app.import_active = False
        app.database_copy_active = False
        app.importer = None
        app.last_activity = 0
        app.logout = Mock()
        app.root = Mock()

        app._check_idle_logout()

        app.logout.assert_called_once_with(automatic=True)


if __name__ == "__main__":
    unittest.main()
