import configparser
import unittest

from connection_profiles import ConnectionProfile, add_profiles_to_config, profiles_from_config


class ConnectionProfileTests(unittest.TestCase):
    def test_profiles_round_trip_without_password_field(self):
        config = configparser.ConfigParser()
        profiles = {
            "XAMPP 5.6": ConnectionProfile(
                "XAMPP 5.6", r"C:\xampp-5.6\mysql\bin\mysql.exe", "127.0.0.1", "3307", "root",
            ),
        }

        add_profiles_to_config(config, profiles)
        loaded = profiles_from_config(config)

        self.assertEqual(profiles, loaded)
        self.assertNotIn("password", config["profile:XAMPP 5.6"])

    def test_non_profile_sections_are_ignored(self):
        config = configparser.ConfigParser()
        config["settings"] = {"mysql_path": "mysql.exe"}

        self.assertEqual({}, profiles_from_config(config))


if __name__ == "__main__":
    unittest.main()
