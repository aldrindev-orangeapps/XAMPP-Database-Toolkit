import os
import tempfile
import unittest

from sql_parser import UnsupportedSqlError, is_read_only_sql, iter_sql_statements, split_sql_text


class SqlParserTests(unittest.TestCase):
    def test_quotes_comments_and_multiline(self):
        sql = "-- header; ignored\nCREATE TABLE `t` (`v` text);\nINSERT INTO t VALUES ('a;b'), (\"c;d\"); # end;\n"
        statements = split_sql_text(sql)
        self.assertEqual(2, len(statements))
        self.assertIn("'a;b'", statements[1])

    def test_offsets_can_resume(self):
        sql = b"CREATE TABLE t (id int);\nINSERT INTO t VALUES (1);\n"
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(sql)
            path = handle.name
        try:
            first_pass = list(iter_sql_statements(path))
            resume = first_pass[0].end
            for item in first_pass:
                item.close()
            second_pass = list(iter_sql_statements(path, resume))
            self.assertEqual("INSERT", second_pass[0].keyword)
            for item in second_pass:
                item.close()
        finally:
            os.unlink(path)

    def test_delimiter_is_rejected(self):
        with self.assertRaises(UnsupportedSqlError):
            split_sql_text("DELIMITER //\nCREATE PROCEDURE p() SELECT 1//")

    def test_read_only_classification(self):
        self.assertTrue(is_read_only_sql("/* x */ SELECT 1"))
        self.assertTrue(is_read_only_sql("SHOW TABLES"))
        self.assertFalse(is_read_only_sql("UPDATE t SET x=1"))

    def test_parser_state_beyond_preview_buffer(self):
        long_value = "x" * 10_000 + ";still quoted"
        statements = split_sql_text(f"INSERT INTO t VALUES ('{long_value}'); SELECT 2;")
        self.assertEqual(2, len(statements))
        self.assertTrue(statements[0].startswith("INSERT"))

    def test_mysqldump_executable_comments_are_preserved(self):
        statements = split_sql_text("/*!40101 SET NAMES utf8mb4 */;\n/*!40000 ALTER TABLE `t` DISABLE KEYS */;")
        self.assertEqual(2, len(statements))
        self.assertIn("SET NAMES", statements[0])


if __name__ == "__main__":
    unittest.main()
