import os
import tempfile
import unittest
from unittest.mock import patch

from app_ui import launch_xampp_service_script, xampp_root_from_mysql, xampp_service_running, xampp_web_urls


class XamppServiceTests(unittest.TestCase):
    def test_xampp_root_is_derived_from_mysql_executable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bin_dir = os.path.join(temp_dir, "mysql", "bin")
            os.makedirs(bin_dir)
            executable = os.path.join(bin_dir, "mysql.exe")
            with open(executable, "wb"):
                pass
            self.assertEqual(os.path.abspath(temp_dir), xampp_root_from_mysql(executable))

    def test_non_xampp_mysql_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = os.path.join(temp_dir, "mysql.exe")
            with open(executable, "wb"):
                pass
            with self.assertRaises(FileNotFoundError):
                xampp_root_from_mysql(executable)

    def test_apache_listen_port_is_used_in_quick_links(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = os.path.join(temp_dir, "apache", "conf")
            os.makedirs(config_dir)
            config_path = os.path.join(config_dir, "httpd.conf")
            with open(config_path, "w", encoding="utf-8") as handle:
                handle.write("# Listen 80\nListen 127.0.0.1:8080\n")
            self.assertEqual(
                ("http://localhost:8080/", "http://localhost:8080/phpmyadmin/"),
                xampp_web_urls(temp_dir),
            )

    def test_default_quick_links_use_port_80(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertEqual(
                ("http://localhost/", "http://localhost/phpmyadmin/"),
                xampp_web_urls(temp_dir),
            )

    @patch("app_ui.subprocess.run")
    def test_running_process_is_detected(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = '"mysqld.exe","1234","Console","1","20,000 K"'
        run.return_value.stderr = ""
        self.assertTrue(xampp_service_running("mysql"))

    @patch("app_ui.subprocess.Popen")
    def test_official_script_is_launched_without_a_visible_window(self, popen):
        with tempfile.TemporaryDirectory() as temp_dir:
            script = os.path.join(temp_dir, "apache_start.bat")
            with open(script, "w", encoding="utf-8"):
                pass
            launch_xampp_service_script(temp_dir, "apache", "start")
        command = popen.call_args.args[0]
        self.assertEqual("call", command[-2])
        self.assertTrue(command[-1].endswith("apache_start.bat"))


if __name__ == "__main__":
    unittest.main()
