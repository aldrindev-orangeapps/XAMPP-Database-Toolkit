import threading
import unittest

from importer import ResumableImporter, is_duplicate_key_error


class _ClientThatMustNotBeKilled:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class ImportResumeSafetyTests(unittest.TestCase):
    def test_duplicate_key_error_detection(self):
        self.assertTrue(
            is_duplicate_key_error(
                "ERROR 1062 (23000) at line 46: Duplicate entry 'abc' for key 'PRIMARY'"
            )
        )
        self.assertFalse(is_duplicate_key_error("ERROR 1452: foreign key constraint fails"))

    def test_cancel_waits_for_confirmed_statement_boundary(self):
        cancelled = threading.Event()
        importer = ResumableImporter(
            service=None,
            checkpoints=None,
            progress=lambda *_: None,
            log=lambda *_: None,
            ambiguous=lambda *_: "stop",
            cancelled=cancelled,
        )
        client = _ClientThatMustNotBeKilled()
        importer.client = client

        importer.cancel()

        self.assertTrue(cancelled.is_set())
        self.assertFalse(client.stopped)


if __name__ == "__main__":
    unittest.main()
