import io
import os
import unittest
from unittest.mock import Mock, patch

from mysql_service import MySQLConfig, MySQLService


class FakeProcess:
    def __init__(self, return_code=0, error=b"", with_stdout=False):
        self.return_code = return_code
        self.stdout = io.BytesIO() if with_stdout else None
        self.stderr = io.BytesIO(error)
        self.kill = Mock()

    def wait(self):
        return self.return_code


class MySQLServiceCopyTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
