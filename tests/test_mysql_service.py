import io
import gzip
import os
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from mysql_service import DatabaseOperationCancelled, MySQLConfig, MySQLError, MySQLService


class OpenBytesIO(io.BytesIO):
    def close(self):
        pass


class TrackingBytesIO(io.BytesIO):
    def __init__(self, value):
        super().__init__(value)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class FakeProcess:
    def __init__(self, return_code=0, error=b"", stdout=b"", with_stdout=False, with_stdin=False):
        self.return_code = return_code
        self.stdout = io.BytesIO(stdout) if with_stdout else None
        self.stdin = OpenBytesIO() if with_stdin else None
        self.stderr = io.BytesIO(error)
        self.kill = Mock()
        self.terminate = Mock()

    def wait(self, timeout=None):
        return self.return_code

    def poll(self):
        return self.return_code


class MySQLServiceCopyTests(unittest.TestCase):
    def test_database_is_empty_counts_all_schema_objects_with_hex_name(self):
        config = MySQLConfig("mysql.exe")
        service = MySQLService(lambda: config)
        service.run = Mock()
        service.run.return_value.stdout = "0\n"

        self.assertTrue(service.database_is_empty("client-db"))

        extra = service.run.call_args.args[0]
        self.assertEqual(["-N", "-B", "-e"], extra[:3])
        self.assertIn("0x636c69656e742d6462", extra[3])
        self.assertIn("information_schema.TABLES", extra[3])
        self.assertIn("information_schema.ROUTINES", extra[3])
        self.assertIn("information_schema.EVENTS", extra[3])

    @patch("mysql_service.subprocess.Popen")
    @patch("mysql_service.os.path.isfile", return_value=True)
    def test_copy_database_streams_dump_without_password_argument(self, _isfile, popen):
        dump_process = FakeProcess(with_stdout=True)
        restore_process = FakeProcess()
        popen.side_effect = [dump_process, restore_process]
        config = MySQLConfig(
            os.path.join("C:\\xampp", "mysql", "bin", "mysql.exe"),
            "127.0.0.1",
            "3307",
            "admin",
            "secret",
        )
        service = MySQLService(lambda: config)

        service.copy_database("source_db", "destination_db")

        dump_call, restore_call = popen.call_args_list
        dump_command = dump_call.args[0]
        restore_command = restore_call.args[0]
        self.assertTrue(dump_command[0].endswith("mysqldump.exe"))
        self.assertEqual(["--", "source_db"], dump_command[-2:])
        self.assertEqual("destination_db", restore_command[-1])
        self.assertNotIn("secret", dump_command)
        self.assertNotIn("secret", restore_command)
        self.assertEqual("secret", dump_call.kwargs["env"]["MYSQL_PWD"])
        self.assertEqual("secret", restore_call.kwargs["env"]["MYSQL_PWD"])
        self.assertIs(restore_call.kwargs["stdin"], dump_process.stdout)


class MySQLServiceBackupRestoreTests(unittest.TestCase):
    def setUp(self):
        self.config = MySQLConfig(
            os.path.join("C:\\xampp", "mysql", "bin", "mysql.exe"),
            "127.0.0.1", "3307", "admin", "secret-password",
        )
        self.service = MySQLService(lambda: self.config)

    @patch("mysql_service.subprocess.Popen")
    @patch("mysql_service.os.path.isfile", return_value=True)
    def test_plain_backup_has_full_objects_metadata_and_no_password_argument(self, _isfile, popen):
        popen.return_value = FakeProcess(stdout=b"CREATE TABLE t (id int);\n", with_stdout=True)
        with tempfile.TemporaryDirectory() as folder:
            output = os.path.join(folder, "backup.sql")
            size = self.service.backup_database("source_db", output, False)
            with open(output, "rb") as handle:
                content = handle.read()

        command = popen.call_args.args[0]
        self.assertEqual(size, len(content))
        self.assertIn(b"XAMPP_DATABASE_TOOLKIT_BACKUP", content)
        self.assertIn(b'"source_database":"source_db"', content)
        self.assertTrue(content.endswith(b"CREATE TABLE t (id int);\n"))
        for option in ("--routines", "--events", "--triggers", "--single-transaction", "--quick"):
            self.assertIn(option, command)
        self.assertNotIn("secret-password", command)
        self.assertEqual("secret-password", popen.call_args.kwargs["env"]["MYSQL_PWD"])
        self.assertNotIn(b"secret-password", content)

    @patch("mysql_service.subprocess.Popen")
    @patch("mysql_service.os.path.isfile", return_value=True)
    def test_gzip_backup_streams_and_validates_metadata(self, _isfile, popen):
        popen.return_value = FakeProcess(stdout=b"INSERT INTO t VALUES (1);\n", with_stdout=True)
        with tempfile.TemporaryDirectory() as folder:
            output = os.path.join(folder, "backup.sql.gz")
            self.service.backup_database("source_db", output, True)
            metadata = self.service.inspect_backup(output)
            with gzip.open(output, "rb") as handle:
                content = handle.read()

        self.assertEqual("source_db", metadata["source_database"])
        self.assertEqual(1, metadata["format_version"])
        self.assertTrue(content.endswith(b"INSERT INTO t VALUES (1);\n"))

    @patch("mysql_service.subprocess.Popen")
    @patch("mysql_service.os.path.isfile", return_value=True)
    def test_failed_or_cancelled_backup_preserves_existing_file(self, _isfile, popen):
        with tempfile.TemporaryDirectory() as folder:
            output = os.path.join(folder, "backup.sql")
            with open(output, "wb") as handle:
                handle.write(b"older backup")
            popen.return_value = FakeProcess(return_code=1, error=b"dump failed", with_stdout=True)
            with self.assertRaises(MySQLError):
                self.service.backup_database("source_db", output, False)
            with open(output, "rb") as handle:
                self.assertEqual(b"older backup", handle.read())

            popen.return_value = FakeProcess(stdout=b"new dump", with_stdout=True)
            cancelled = threading.Event()
            cancelled.set()
            with self.assertRaises(DatabaseOperationCancelled):
                self.service.backup_database("source_db", output, False, cancelled=cancelled)
            with open(output, "rb") as handle:
                self.assertEqual(b"older backup", handle.read())

    def test_inspect_backup_rejects_unmarked_damaged_and_unsupported_files(self):
        with tempfile.TemporaryDirectory() as folder:
            unmarked = os.path.join(folder, "unmarked.sql")
            with open(unmarked, "wb") as handle:
                handle.write(b"SELECT 1;\n")
            with self.assertRaises(MySQLError):
                self.service.inspect_backup(unmarked)

            damaged = os.path.join(folder, "damaged.sql.gz")
            with open(damaged, "wb") as handle:
                handle.write(b"not gzip")
            with self.assertRaises(MySQLError):
                self.service.inspect_backup(damaged)

            unsupported = os.path.join(folder, "unsupported.sql")
            with open(unsupported, "wb") as handle:
                handle.write(
                    b'-- XAMPP_DATABASE_TOOLKIT_BACKUP {"format_version":99,'
                    b'"source_database":"db","created_at":"now"}\n'
                )
            with self.assertRaises(MySQLError):
                self.service.inspect_backup(unsupported)

            wrong_extension = os.path.join(folder, "backup.dump")
            with open(wrong_extension, "wb") as handle:
                handle.write(self.service._backup_header("db"))
            with self.assertRaises(MySQLError):
                self.service.inspect_backup(wrong_extension)

    def _write_backup(self, path, compressed=False):
        payload = self.service._backup_header("source_db") + b"CREATE TABLE t (id int);\n"
        if compressed:
            with gzip.open(path, "wb") as handle:
                handle.write(payload)
        else:
            with open(path, "wb") as handle:
                handle.write(payload)
        return b"CREATE TABLE t (id int);\n"

    @patch("mysql_service.subprocess.Popen")
    def test_restore_streams_plain_and_gzip_without_checkpoint_or_password_argument(self, popen):
        self.service.database_is_empty = Mock(return_value=True)
        with tempfile.TemporaryDirectory() as folder:
            for filename, compressed in (("backup.sql", False), ("backup.sql.gz", True)):
                path = os.path.join(folder, filename)
                sql = self._write_backup(path, compressed)
                process = FakeProcess(with_stdin=True)
                popen.return_value = process

                metadata = self.service.restore_database(path, "empty_db")

                self.assertEqual("source_db", metadata["source_database"])
                self.assertEqual(sql, process.stdin.getvalue())
                command = popen.call_args.args[0]
                self.assertEqual("empty_db", command[-1])
                self.assertNotIn("secret-password", command)
                self.assertEqual("secret-password", popen.call_args.kwargs["env"]["MYSQL_PWD"])

    @patch("mysql_service.subprocess.Popen")
    def test_restore_rejects_nonempty_destination_before_starting_mysql(self, popen):
        self.service.database_is_empty = Mock(return_value=False)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "backup.sql")
            self._write_backup(path)
            with self.assertRaisesRegex(MySQLError, "not empty"):
                self.service.restore_database(path, "occupied_db")
        popen.assert_not_called()

    @patch("mysql_service.subprocess.Popen")
    def test_restore_cancellation_is_explicit_and_non_resumable(self, popen):
        self.service.database_is_empty = Mock(return_value=True)
        popen.return_value = FakeProcess(with_stdin=True)
        cancelled = threading.Event()
        cancelled.set()
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "backup.sql")
            self._write_backup(path)
            with self.assertRaises(DatabaseOperationCancelled):
                self.service.restore_database(path, "empty_db", cancelled=cancelled)

    @patch("mysql_service.subprocess.Popen")
    @patch("mysql_service.os.path.isfile", return_value=True)
    def test_backup_reads_dump_in_bounded_chunks(self, _isfile, popen):
        process = FakeProcess(with_stdout=True)
        process.stdout = TrackingBytesIO(b"x" * (150 * 1024))
        popen.return_value = process
        with tempfile.TemporaryDirectory() as folder:
            self.service.backup_database("source_db", os.path.join(folder, "backup.sql"), False)
        self.assertTrue(process.stdout.read_sizes)
        self.assertEqual({64 * 1024}, set(process.stdout.read_sizes))


if __name__ == "__main__":
    unittest.main()
