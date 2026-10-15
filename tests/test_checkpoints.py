import json
import os
import tempfile
import unittest

from checkpoints import CheckpointStore, ImportCheckpoint


def make_checkpoint(offset=10, session_sql=None):
    return ImportCheckpoint(
        key="test-key",
        path=r"C:\dump.sql",
        size=1_000,
        mtime_ns=123,
        host="127.0.0.1",
        port="3306",
        database="sample",
        offset=offset,
        session_sql=session_sql or [],
    )


class CheckpointStoreTests(unittest.TestCase):
    def test_progress_survives_new_store_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(directory)
            store.save(make_checkpoint(10))
            store.save(make_checkpoint(900, ["SET NAMES utf8mb4;"]))

            restored_store = CheckpointStore(directory)
            restored = restored_store.get("test-key")
            self.assertIsNotNone(restored)
            self.assertEqual(900, restored.offset)
            self.assertEqual(["SET NAMES utf8mb4;"], restored.session_sql)
            store.close()
            restored_store.close()

    def test_incomplete_journal_tail_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(directory)
            store.save(make_checkpoint(100))
            with open(store.journal_file, "a", encoding="utf-8") as handle:
                handle.write('{"op":"progress"')

            recovered = CheckpointStore(directory)
            self.assertEqual(100, recovered.get("test-key").offset)
            recovered.save(make_checkpoint(200))
            final_store = CheckpointStore(directory)
            self.assertEqual(200, final_store.get("test-key").offset)
            store.close()
            recovered.close()
            final_store.close()

    def test_legacy_snapshot_and_remove(self):
        with tempfile.TemporaryDirectory() as directory:
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, "checkpoints.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"test-key": make_checkpoint(55).__dict__}, handle)

            store = CheckpointStore(directory)
            self.assertEqual(55, store.get("test-key").offset)
            store.remove("test-key")
            self.assertIsNone(CheckpointStore(directory).get("test-key"))

    def test_database_deletion_removes_only_matching_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CheckpointStore(directory)
            first = make_checkpoint(100)
            second = make_checkpoint(200)
            second.key = "other-key"
            second.database = "other_database"
            store.save(first)
            store.save(second)

            removed = store.remove_for_database("127.0.0.1", "3306", "sample")
            self.assertEqual(1, removed)
            self.assertIsNone(store.get("test-key"))
            self.assertIsNotNone(store.get("other-key"))
            store.close()


if __name__ == "__main__":
    unittest.main()
