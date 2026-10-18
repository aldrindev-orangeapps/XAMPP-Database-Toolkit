import os
import unittest
from unittest.mock import Mock, patch

from app_ui import IDLE_LOGOUT_SECONDS, NAVIGATION_PAGES, ModernApp
from mysql_service import MySQLConfig


class AppUiWorkflowTests(unittest.TestCase):
    def test_backup_restore_navigation_follows_import(self):
        self.assertEqual(("Databases", "Import", "Backup & Restore"), NAVIGATION_PAGES[:3])

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

    def test_database_copy_streams_into_selected_empty_destination(self):
        app = ModernApp.__new__(ModernApp)
        app.importer = None
        app.import_active = False
        app.database_copy_active = False
        app.databases = ["source", "destination"]
        app.selected_db = Mock()
        app.selected_db.get.return_value = "source"
        app._choose_copy_destination = Mock(return_value="destination")
        app._confirm = Mock(return_value=True)
        app._sync_config = Mock()
        app._set_database_busy = Mock()
        app.service = Mock()
        app.service.database_is_empty.return_value = True
        app.log = Mock()
        app._copy_database_finished = Mock()
        app._ui = lambda callback, *args: callback(*args)
        app._thread = lambda worker: worker()

        app.copy_database()

        app.service.copy_database.assert_called_once_with("source", "destination")
        app._copy_database_finished.assert_called_once_with(
            "source", "destination", None, False,
        )

    def test_database_copy_never_overwrites_nonempty_destination(self):
        app = ModernApp.__new__(ModernApp)
        app.importer = None
        app.import_active = False
        app.database_copy_active = False
        app.databases = ["source", "destination"]
        app.selected_db = Mock()
        app.selected_db.get.return_value = "source"
        app._choose_copy_destination = Mock(return_value="destination")
        app._confirm = Mock(return_value=True)
        app._sync_config = Mock()
        app._set_database_busy = Mock()
        app.service = Mock()
        app.service.database_is_empty.return_value = False
        app.log = Mock()
        app._copy_database_finished = Mock()
        app._ui = lambda callback, *args: callback(*args)
        app._thread = lambda worker: worker()

        app.copy_database()

        app.service.copy_database.assert_not_called()
        app._copy_database_finished.assert_called_once_with(
            "source",
            "destination",
            "The destination database is not empty. Choose an empty database.",
            False,
        )

    def test_failed_database_copy_warns_that_destination_may_be_partial(self):
        app = ModernApp.__new__(ModernApp)
        app.importer = None
        app.import_active = False
        app.database_copy_active = False
        app.databases = ["source", "destination"]
        app.selected_db = Mock()
        app.selected_db.get.return_value = "source"
        app._choose_copy_destination = Mock(return_value="destination")
        app._confirm = Mock(return_value=True)
        app._sync_config = Mock()
        app._set_database_busy = Mock()
        app.service = Mock()
        app.service.database_is_empty.return_value = True
        app.service.copy_database.side_effect = RuntimeError("dump failed")
        app.log = Mock()
        app._copy_database_finished = Mock()
        app._ui = lambda callback, *args: callback(*args)
        app._thread = lambda worker: worker()

        app.copy_database()

        app._copy_database_finished.assert_called_once_with(
            "source", "destination", "dump failed", True,
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

    @patch("app_ui.time.monotonic", return_value=IDLE_LOGOUT_SECONDS + 10)
    def test_idle_logout_is_suspended_during_backup_or_restore(self, _monotonic):
        for operation in ("backup", "restore"):
            app = ModernApp.__new__(ModernApp)
            app.authenticated = True
            app.import_active = False
            app.database_copy_active = False
            app.importer = None
            app.maintenance_operation = operation
            app.last_activity = 0
            app.logout = Mock()
            app.root = Mock()

            app._check_idle_logout()

            app.logout.assert_not_called()
            self.assertEqual(IDLE_LOGOUT_SECONDS + 10, app.last_activity)

    def test_active_backup_blocks_conflicting_database_work(self):
        app = ModernApp.__new__(ModernApp)
        app.import_active = False
        app.importer = None
        app.database_copy_active = False
        app.maintenance_operation = "backup"
        app.sanitize_active = False
        app.write_query_active = False
        app._alert = Mock()

        self.assertTrue(app._reject_conflicting_work("a restore"))
        app._alert.assert_called_once_with(
            "Database work in progress",
            "Wait for the active backup before starting a restore.",
            True,
        )

    def test_restore_cancellation_warns_destination_is_partial(self):
        app = ModernApp.__new__(ModernApp)
        app.maintenance_started = 0
        app.maintenance_operation = "restore"
        app.maintenance_cancel = Mock()
        app.last_activity = 0
        app._set_maintenance_controls = Mock()
        app.restore_status = Mock()
        app.restore_detail = Mock()
        app.log = Mock()
        app._alert = Mock()
        app.refresh_databases = Mock()

        app._restore_finished("backup.sql", "target_db", None, True, None)

        self.assertIn("partial", app.restore_detail.set.call_args.args[0])
        self.assertIn("Delete and recreate", app._alert.call_args.args[1])
        app.refresh_databases.assert_called_once_with()

    def test_backup_progress_is_empty_after_cancellation(self):
        app = ModernApp.__new__(ModernApp)
        app.maintenance_started = 0
        app.maintenance_operation = "backup"
        app.maintenance_cancel = Mock()
        app.last_activity = 0
        app.backup_progress = Mock()
        app._set_maintenance_controls = Mock()
        app.backup_status = Mock()
        app.backup_detail = Mock()
        app.log = Mock()

        app._backup_finished("source_db", "backup.sql", 0, True, None)

        app.backup_progress.stop.assert_called_once_with()
        app.backup_progress.configure.assert_called_once_with(mode="determinate", value=0)
        app.backup_detail.set.assert_called_once_with(
            "The temporary file was removed; any older backup was preserved."
        )

    @patch("app_ui.xampp_service_running", side_effect=[False, True])
    @patch("app_ui.xampp_root_from_mysql")
    def test_xampp_switch_is_cancelled_when_previous_mysql_is_running(self, root_from_mysql, service_running):
        root_from_mysql.side_effect = lambda path: "C:\\old-xampp" if "old" in path else "C:\\new-xampp"
        app = ModernApp.__new__(ModernApp)
        app.mysql_path = Mock()
        app.mysql_path.get.return_value = r"C:\old-xampp\mysql\bin\mysql.exe"
        app.accepted_mysql_path = app.mysql_path.get.return_value
        app.xampp_switch_pending = False
        app.import_active = False
        app.importer = None
        app.database_copy_active = False
        app.maintenance_operation = None
        app.sanitize_active = False
        app.write_query_active = False
        app.apache_service_status = Mock()
        app.mysql_service_status = Mock()
        app._confirm = Mock(return_value=False)
        app._alert = Mock()
        app.refresh_xampp_services = Mock()
        app._thread = lambda worker: worker()
        app._ui = lambda callback, *args: callback(*args)

        app._request_mysql_path_switch(r"C:\new-xampp\mysql\bin\mysql.exe")

        self.assertEqual(2, service_running.call_count)
        self.assertIn("MySQL", app._confirm.call_args.args[1])
        self.assertEqual(
            r"C:\old-xampp\mysql\bin\mysql.exe",
            app.mysql_path.set.call_args.args[0],
        )
        app.refresh_xampp_services.assert_called_once_with()

    @patch("app_ui.xampp_service_running", side_effect=[True, True])
    @patch("app_ui.xampp_root_from_mysql")
    def test_confirmed_xampp_switch_clears_stale_database_selection(self, root_from_mysql, _service_running):
        root_from_mysql.side_effect = lambda path: "C:\\old-xampp" if "old" in path else "C:\\new-xampp"
        app = ModernApp.__new__(ModernApp)
        app.mysql_path = Mock()
        app.mysql_path.get.return_value = r"C:\old-xampp\mysql\bin\mysql.exe"
        app.accepted_mysql_path = app.mysql_path.get.return_value
        app.xampp_switch_pending = False
        app.import_active = False
        app.importer = None
        app.database_copy_active = False
        app.maintenance_operation = None
        app.sanitize_active = False
        app.write_query_active = False
        app.apache_service_status = Mock()
        app.mysql_service_status = Mock()
        app.selected_db = Mock()
        app.databases = ["old_database"]
        app.database_status = Mock()
        app._confirm = Mock(return_value=True)
        app._alert = Mock()
        app._filter_databases = Mock()
        app._set_connection = Mock()
        app.log = Mock()
        app.refresh_xampp_services = Mock()
        app._thread = lambda worker: worker()
        app._ui = lambda callback, *args: callback(*args)

        new_path = r"C:\new-xampp\mysql\bin\mysql.exe"
        app._request_mysql_path_switch(new_path)

        self.assertEqual(os.path.abspath(new_path), app.accepted_mysql_path)
        self.assertEqual([], app.databases)
        app.selected_db.set.assert_called_once_with("")
        app._set_connection.assert_called_once_with(False, "Verify host and port")
        app.refresh_xampp_services.assert_called_once_with()

    def test_xampp_switch_is_blocked_during_database_work(self):
        app = ModernApp.__new__(ModernApp)
        app.mysql_path = Mock()
        app.mysql_path.get.return_value = r"C:\old-xampp\mysql\bin\mysql.exe"
        app.accepted_mysql_path = app.mysql_path.get.return_value
        app.xampp_switch_pending = False
        app.import_active = True
        app.importer = None
        app.database_copy_active = False
        app.maintenance_operation = None
        app.sanitize_active = False
        app.write_query_active = False
        app._alert = Mock()

        app._request_mysql_path_switch(r"C:\new-xampp\mysql\bin\mysql.exe")

        self.assertIn("active import", app._alert.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
