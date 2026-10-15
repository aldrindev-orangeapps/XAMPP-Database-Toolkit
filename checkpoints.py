"""Persistent, file-identity-aware import checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass


APP_DIR = os.path.join(os.path.expanduser("~"), ".xampp_sql_importer")
CHECKPOINT_FILE = os.path.join(APP_DIR, "checkpoints.json")


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
    def _read(self) -> dict:
        try:
            with open(CHECKPOINT_FILE, "r", encoding="utf-8") as handle:
                value = json.load(handle)
                return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, key: str) -> ImportCheckpoint | None:
        raw = self._read().get(key)
        try:
            return ImportCheckpoint(**raw) if raw else None
        except (TypeError, ValueError):
            return None

    def save(self, checkpoint: ImportCheckpoint) -> None:
        os.makedirs(APP_DIR, exist_ok=True)
        data = self._read()
        data[checkpoint.key] = asdict(checkpoint)
        fd, temp_path = tempfile.mkstemp(prefix="checkpoints-", suffix=".json", dir=APP_DIR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
            os.replace(temp_path, CHECKPOINT_FILE)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def remove(self, key: str) -> None:
        data = self._read()
        if key in data:
            del data[key]
            os.makedirs(APP_DIR, exist_ok=True)
            with open(CHECKPOINT_FILE, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
