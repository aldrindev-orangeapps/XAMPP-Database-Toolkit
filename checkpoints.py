"""Persistent, file-identity-aware import checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass


APP_DIR = os.path.join(os.path.expanduser("~"), ".xampp_sql_importer")
CHECKPOINT_FILE = os.path.join(APP_DIR, "checkpoints.json")
CHECKPOINT_JOURNAL = os.path.join(APP_DIR, "checkpoints.log")
COMPACT_EVERY = 10_000


@dataclass
class ImportCheckpoint:
    key: str
    path: str
    size: int
    mtime_ns: int
    host: str
    port: str
    database: str
    offset: int
    session_sql: list[str] | None = None


def checkpoint_key(path: str, host: str, port: str, database: str) -> str:
    stat = os.stat(path)
    identity = "\0".join([
        os.path.abspath(path), str(stat.st_size), str(stat.st_mtime_ns), host, port, database,
    ])
    return hashlib.sha256(identity.encode("utf-8", "surrogatepass")).hexdigest()


class CheckpointStore:
    """Durable checkpoints without rewriting the full JSON file per statement.

    Confirmed offsets are appended to a compact journal. A complete snapshot is
    written only occasionally, so imports with many small statements do not pay
    for a temporary-file creation and atomic replace after every acknowledgement.
    """

    def __init__(self, app_dir: str = APP_DIR):
        self.app_dir = app_dir
        self.checkpoint_file = os.path.join(app_dir, os.path.basename(CHECKPOINT_FILE))
        self.journal_file = os.path.join(app_dir, os.path.basename(CHECKPOINT_JOURNAL))
        self._data: dict | None = None
        self._journal_entries = 0
        self._journal_handle = None

    def __del__(self):
        self._close_journal()

    def close(self) -> None:
        """Flush and release the journal handle."""
        self._close_journal()

    def _close_journal(self) -> None:
        handle = self._journal_handle
        self._journal_handle = None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    def _read_snapshot(self) -> dict:
        try:
            with open(self.checkpoint_file, "r", encoding="utf-8") as handle:
                value = json.load(handle)
                return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _apply_record(data: dict, record: dict) -> None:
        operation = record.get("op")
        if operation == "save" and isinstance(record.get("checkpoint"), dict):
            checkpoint = record["checkpoint"]
            key = checkpoint.get("key")
            if isinstance(key, str):
                data[key] = checkpoint
        elif operation == "progress":
            key = record.get("key")
            if key in data and isinstance(record.get("offset"), int):
                data[key]["offset"] = record["offset"]
                if "session_sql" in record:
                    data[key]["session_sql"] = record["session_sql"]
        elif operation == "remove" and isinstance(record.get("key"), str):
            data.pop(record["key"], None)

    def _load(self) -> dict:
        if self._data is not None:
            return self._data
        data = self._read_snapshot()
        entries = 0
        try:
            with open(self.journal_file, "r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except (TypeError, ValueError):
                        continue  # A crash may leave one incomplete final record.
                    if isinstance(record, dict):
                        self._apply_record(data, record)
                        entries += 1
        except OSError:
            pass
        self._data = data
        self._journal_entries = entries
        return data

    def _append(self, record: dict) -> None:
        os.makedirs(self.app_dir, exist_ok=True)
        if self._journal_handle is None:
            # If a prior process died during its final write, separate that
            # partial JSON so recovery can safely ignore it.
            needs_newline = False
            try:
                if os.path.getsize(self.journal_file):
                    with open(self.journal_file, "rb") as existing:
                        existing.seek(-1, os.SEEK_END)
                        needs_newline = existing.read(1) != b"\n"
            except OSError:
                pass
            self._journal_handle = open(self.journal_file, "a", encoding="utf-8", newline="\n")
            if needs_newline:
                self._journal_handle.write("\n")
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        self._journal_handle.write(line + "\n")
        self._journal_handle.flush()
        self._journal_entries += 1

    def _compact(self) -> None:
        self._close_journal()
        os.makedirs(self.app_dir, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix="checkpoints-", suffix=".json", dir=self.app_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._load(), handle, indent=2)
            os.replace(temp_path, self.checkpoint_file)
            with open(self.journal_file, "w", encoding="utf-8"):
                pass
            self._journal_entries = 0
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def get(self, key: str) -> ImportCheckpoint | None:
        raw = self._load().get(key)
        try:
            return ImportCheckpoint(**raw) if raw else None
        except (TypeError, ValueError):
            return None

    def save(self, checkpoint: ImportCheckpoint) -> None:
        data = self._load()
        raw = asdict(checkpoint)
        previous = data.get(checkpoint.key)
        identity_fields = ("path", "size", "mtime_ns", "host", "port", "database")
        if not previous or any(previous.get(field) != raw[field] for field in identity_fields):
            record = {"op": "save", "checkpoint": raw}
        else:
            record = {"op": "progress", "key": checkpoint.key, "offset": checkpoint.offset}
            if previous.get("session_sql") != checkpoint.session_sql:
                record["session_sql"] = checkpoint.session_sql
        self._append(record)
        data[checkpoint.key] = raw
        if self._journal_entries >= COMPACT_EVERY:
            self._compact()

    def remove(self, key: str) -> None:
        data = self._load()
        if key in data:
            self._append({"op": "remove", "key": key})
            del data[key]
            self._compact()
