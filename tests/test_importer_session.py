import os
import tempfile
import unittest

from importer import dump_preamble_session_sql, merge_session_sql, remember_session_sql


FOREIGN_KEYS_OFF = "/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;"


class ImporterSessionTests(unittest.TestCase):
    def test_critical_setting_is_not_evicted_by_recent_set_statements(self):
        remembered = remember_session_sql([], FOREIGN_KEYS_OFF)
        for number in range(250):
            remembered = remember_session_sql(
                remembered,
                f"SET @temporary_{number}={number};",
            )
        self.assertIn(FOREIGN_KEYS_OFF, remembered)
        self.assertLessEqual(len(remembered), 101)

    def test_old_checkpoint_recovers_critical_dump_preamble(self):
        sql = (
            "SET NAMES utf8mb4;\n"
            + FOREIGN_KEYS_OFF
            + "\nSET UNIQUE_CHECKS=0;\nCREATE TABLE t (id int);\n"
        )
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(sql.encode("utf-8"))
            path = handle.name
        try:
            recovered = dump_preamble_session_sql(path)
        finally:
            os.unlink(path)
        self.assertTrue(any("FOREIGN_KEY_CHECKS" in item for item in recovered))
        self.assertTrue(any("UNIQUE_CHECKS" in item for item in recovered))

    def test_recovered_preamble_precedes_saved_session_state(self):
        restored = "SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS;"
        merged = merge_session_sql([FOREIGN_KEYS_OFF], [restored])
        self.assertEqual([FOREIGN_KEYS_OFF, restored], merged)


if __name__ == "__main__":
    unittest.main()
