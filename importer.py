"""Checkpointed, acknowledgement-driven SQL dump importer."""

from __future__ import annotations

import os
import queue
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from checkpoints import CheckpointStore, ImportCheckpoint, checkpoint_key
from mysql_service import CREATE_NO_WINDOW, MySQLError, MySQLService, is_connection_error
from sql_parser import SqlStatement, UnsupportedSqlError, iter_sql_statements


CRITICAL_SESSION_MARKERS = (
    "FOREIGN_KEY_CHECKS",
    "UNIQUE_CHECKS",
    "SQL_MODE",
    "TIME_ZONE",
    "SQL_NOTES",
    "SET NAMES",
    "@OLD_CHARACTER_SET_CLIENT",
    "@OLD_CHARACTER_SET_RESULTS",
    "@OLD_COLLATION_CONNECTION",
)


def is_critical_session_sql(sql: str) -> bool:
    normalized = re.sub(r"\s+", " ", sql).upper()
    return any(marker in normalized for marker in CRITICAL_SESSION_MARKERS)


def remember_session_sql(current: list[str], sql: str, recent_limit: int = 100) -> list[str]:
    """Keep recent SET statements without evicting critical dump state."""
    combined = current + [sql]
    noncritical_indexes = [
        index for index, statement in enumerate(combined)
        if not is_critical_session_sql(statement)
    ]
    recent_noncritical = set(noncritical_indexes[-recent_limit:])
    return [
        statement for index, statement in enumerate(combined)
        if is_critical_session_sql(statement) or index in recent_noncritical
    ]


def dump_preamble_session_sql(path: str, scan_limit: int = 1024 * 1024) -> list[str]:
    """Recover critical mysqldump settings needed by older checkpoints."""
    recovered: list[str] = []
    for statement in iter_sql_statements(path):
        try:
            if statement.keyword == "SET" and statement.size <= 64 * 1024:
                statement.stream.seek(0)
                sql = statement.stream.read().decode("utf-8", "replace")
                if is_critical_session_sql(sql):
                    recovered.append(sql)
            end = statement.end
        finally:
            statement.close()
        if end >= scan_limit:
            break
    return recovered


def merge_session_sql(preamble: list[str], saved: list[str]) -> list[str]:
    merged = list(preamble)
    for statement in saved:
        if statement not in merged:
            merged.append(statement)
    return merged


def is_duplicate_key_error(message: str) -> bool:
    """Return whether mysql reported duplicate-key error 1062."""
    return bool(re.search(r"\bERROR\s+1062\b", message, re.IGNORECASE))


class ImportCancelled(RuntimeError):
    pass


class SqlImportError(RuntimeError):
    pass


@dataclass
class ImportResult:
    success: bool
    cancelled: bool = False
    errors: int = 0
    message: str = ""


class _Client:
    def __init__(self, service: MySQLService, database: str, force: bool):
        args = ["--batch", "--raw", "--skip-column-names", "--unbuffered", "--binary-mode"]
        if force:
            args.append("--force")
        cmd, env = service.command(database, args)
        self.process = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, creationflags=CREATE_NO_WINDOW,
        )
        self.stdout: queue.Queue[bytes | None] = queue.Queue()
        self.stderr: queue.Queue[bytes] = queue.Queue()
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self):
        assert self.process.stdout is not None
        for line in iter(self.process.stdout.readline, b""):
            self.stdout.put(line)
        self.stdout.put(None)

    def _read_stderr(self):
        assert self.process.stderr is not None
        for line in iter(self.process.stderr.readline, b""):
            self.stderr.put(line)

    def errors(self) -> str:
        lines = []
        while True:
            try:
                lines.append(self.stderr.get_nowait().decode("utf-8", "replace").rstrip())
            except queue.Empty:
                return "\n".join(item for item in lines if item)

    def stop(self):
        if self.process.poll() is None:
            self.process.kill()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


class ResumableImporter:
    def __init__(
        self,
        service: MySQLService,
        checkpoints: CheckpointStore,
        progress: Callable[[int, int, int, str], None],
        log: Callable[[str], None],
        ambiguous: Callable[[str, int, str], str],
        cancelled: threading.Event,
    ):
        self.service = service
        self.checkpoints = checkpoints
        self.progress = progress
        self.log = log
        self.ambiguous = ambiguous
        self.cancelled = cancelled
        self.client: _Client | None = None

    def cancel(self):
        """Request a stop at the next confirmed statement boundary.

        Do not kill mysql.exe here. The statement currently in flight may have
        committed already; waiting for its acknowledgement lets us persist the
        correct checkpoint and avoids replaying it on resume.
        """
        self.cancelled.set()

    def _connect(self, database: str, force: bool, session_sql: list[str], announce=True) -> _Client:
        delays = [0, 1, 2, 4, 8, 16]
        last_error = ""
        for attempt, delay in enumerate(delays):
            if self.cancelled.is_set():
                raise ImportCancelled()
            if delay:
                self.progress(0, 0, 0, f"Connection lost — retry {attempt}/5 in {delay}s")
                time.sleep(delay)
            try:
                client = _Client(self.service, database, force)
                time.sleep(0.08)
                if client.process.poll() is not None:
                    last_error = client.errors() or "mysql.exe closed while connecting."
                    client.stop()
                    continue
                self.client = client
                if session_sql:
                    for sql in session_sql:
                        self._send_bytes(sql.encode("utf-8", "replace"), "session-restore")
                if announce:
                    self.log("Connected to MySQL.")
                return client
            except (OSError, MySQLError) as exc:
                last_error = str(exc)
        raise SqlImportError(last_error or "Could not reconnect to MySQL after five attempts.")

    def _send_bytes(self, payload: bytes, marker_suffix: str, statement: SqlStatement | None = None) -> str:
        if not self.client or not self.client.process.stdin:
            raise BrokenPipeError("MySQL client is not connected.")
        marker = f"__XSI_ACK_{marker_suffix}_{uuid.uuid4().hex}__"
        stdin = self.client.process.stdin
        if statement is None:
            stdin.write(payload)
        else:
            statement.stream.seek(0)
            while True:
                chunk = statement.stream.read(1024 * 1024)
                if not chunk:
                    break
                stdin.write(chunk)
        stdin.write(f"\nSELECT '{marker}';\n".encode("ascii"))
        stdin.flush()

        while True:
            try:
                line = self.client.stdout.get(timeout=0.2)
            except queue.Empty:
                if self.client.process.poll() is not None:
                    line = None
                else:
                    continue
            if line is None:
                time.sleep(0.05)
                error = self.client.errors() or f"mysql.exe exited with code {self.client.process.poll()}."
                if is_connection_error(error):
                    raise BrokenPipeError(error)
                raise SqlImportError(error)
            if line.decode("utf-8", "replace").strip() == marker:
                return self.client.errors()

    def _ensure_packet(self, required: int, current: int) -> int:
        if required <= current:
            return current
        target = min(1024 * 1024 * 1024, max(64 * 1024 * 1024, 1 << (required - 1).bit_length()))
        if required > target:
            raise SqlImportError(f"A single SQL statement is {required:,} bytes, above MySQL's 1 GB limit.")
        self.log(f"Increasing server max_allowed_packet to {target // 1048576} MB...")
        try:
            self.service.run(["-e", f"SET GLOBAL max_allowed_packet={target};"], timeout=20)
        except Exception as exc:
            raise SqlImportError(
                f"A statement needs {target // 1048576} MB, but the server packet limit is "
                f"{current // 1048576} MB. Set max_allowed_packet={target // 1048576}M under "
                f"[mysqld] in XAMPP's my.ini, restart MySQL, then resume.\n\n{exc}"
            ) from exc
        return target

    def run(self, path: str, database: str, continue_on_error: bool) -> ImportResult:
        size = os.path.getsize(path)
        cfg = self.service.config()
        key = checkpoint_key(path, cfg.host, cfg.port, database)
        saved = self.checkpoints.get(key)
        offset = saved.offset if saved else 0
        resumed_from_checkpoint = offset > 0
        session_sql = list(saved.session_sql or []) if saved else []
        if offset:
            self.log(f"Resuming from confirmed checkpoint at byte {offset:,} ({offset / size * 100:.1f}%).")
            recovered = dump_preamble_session_sql(path)
            session_sql = merge_session_sql(recovered, session_sql)
            if recovered:
                self.log("Recovered critical mysqldump session settings for safe resume.")

        stat = os.stat(path)
        checkpoint = ImportCheckpoint(
            key, os.path.abspath(path), stat.st_size, stat.st_mtime_ns,
            cfg.host, cfg.port, database, offset, session_sql,
        )
        self.progress(offset, size, 0, "Preparing import")
        errors = 0
        statement_number = 0
        in_transaction = False
        transaction_start = offset
        result = self.service.run(
            ["-N", "-B", "-e", "SELECT @@GLOBAL.max_allowed_packet;"],
            timeout=15, safe_retry=True,
        )
        packet_limit = int(result.stdout.strip().splitlines()[-1])
        self._connect(database, continue_on_error, session_sql)

        try:
            while offset < size:
                restart_from: int | None = None
                yielded = False
                for statement in iter_sql_statements(path, offset):
                    yielded = True
                    if self.cancelled.is_set():
                        statement.close()
                        raise ImportCancelled()
                    statement_number += 1

                    keyword = statement.keyword
                    normalized_preview = re.sub(r"\s+", "", statement.preview).lower()
                    disables_autocommit = keyword == "SET" and "autocommit=0" in normalized_preview
                    enables_autocommit = keyword == "SET" and "autocommit=1" in normalized_preview
                    starts_transaction = keyword in {"BEGIN", "START"} or disables_autocommit
                    ends_transaction = keyword in {"COMMIT", "ROLLBACK"} or enables_autocommit
                    ddl_in_transaction = in_transaction and keyword in {
                        "CREATE", "ALTER", "DROP", "TRUNCATE", "RENAME",
                    }
                    if starts_transaction and not in_transaction:
                        in_transaction = True
                        transaction_start = statement.start

                    if statement.size > packet_limit:
                        packet_limit = self._ensure_packet(statement.size, packet_limit)
                        self.client.stop()
                        self._connect(database, continue_on_error, session_sql)

                    if self.client and self.client.process.poll() is not None:
                        self.client.stop()
                        self._connect(database, continue_on_error, session_sql)

                    try:
                        statement_errors = self._send_bytes(b"", str(statement_number), statement)
                    except BrokenPipeError as exc:
                        detail = str(exc)
                        self.log(f"Connection interrupted at statement {statement_number}: {detail}")
                        if self.client:
                            self.client.stop()
                        if in_transaction and not ddl_in_transaction:
                            self.log("The open transaction was rolled back; reconnecting from its start.")
                            restart_from = transaction_start
                            in_transaction = False
                            self._connect(database, continue_on_error, session_sql)
                        else:
                            choice = self.ambiguous(statement.preview, statement_number, "connection_lost")
                            if choice == "retry":
                                restart_from = statement.start
                                self._connect(database, continue_on_error, session_sql)
                            elif choice == "skip":
                                offset = statement.end
                                checkpoint.offset = offset
                                self.checkpoints.save(checkpoint)
                                self._connect(database, continue_on_error, session_sql)
                                restart_from = offset
                            else:
                                raise ImportCancelled()
                        statement.close()
                        break
                    except SqlImportError as exc:
                        if (
                            resumed_from_checkpoint
                            and statement_number == 1
                            and is_duplicate_key_error(str(exc))
                        ):
                            choice = self.ambiguous(
                                statement.preview, statement_number, "duplicate_on_resume"
                            )
                            if choice == "skip":
                                offset = statement.end
                                checkpoint.offset = offset
                                self.checkpoints.save(checkpoint)
                                statement.close()
                                if self.client:
                                    self.client.stop()
                                self._connect(database, continue_on_error, session_sql)
                                restart_from = offset
                                break
                            statement.close()
                            raise ImportCancelled()
                        statement.close()
                        raise

                    if statement_errors:
                        if (
                            resumed_from_checkpoint
                            and statement_number == 1
                            and is_duplicate_key_error(statement_errors)
                        ):
                            choice = self.ambiguous(
                                statement.preview, statement_number, "duplicate_on_resume"
                            )
                            if choice == "skip":
                                self.log(
                                    "The first resumed statement was marked as already executed; "
                                    "its checkpoint is now confirmed."
                                )
                                statement_errors = ""
                            else:
                                statement.close()
                                raise ImportCancelled()
                        else:
                            errors += 1
                            self.log(f"Statement {statement_number} reported an error:\n{statement_errors}")
                            if not continue_on_error:
                                statement.close()
                                raise SqlImportError(statement_errors)

                    if keyword == "SET" and statement.size <= 64 * 1024:
                        statement.stream.seek(0)
                        sql = statement.stream.read().decode("utf-8", "replace")
                        session_sql = remember_session_sql(session_sql, sql)
                        checkpoint.session_sql = session_sql

                    offset = statement.end
                    if ends_transaction:
                        in_transaction = False
                    if not in_transaction:
                        checkpoint.offset = offset
                        self.checkpoints.save(checkpoint)
                    self.progress(offset, size, statement_number, "Importing")
                    statement.close()

                if restart_from is not None:
                    offset = restart_from
                    continue
                if not yielded or offset >= size:
                    break

            if self.client:
                self.client.stop()
            self.checkpoints.remove(key)
            return ImportResult(True, errors=errors, message="Import completed.")
        except ImportCancelled:
            if self.client:
                self.client.stop()
            return ImportResult(False, cancelled=True, errors=errors, message="Import stopped; checkpoint preserved.")
        except (UnsupportedSqlError, SqlImportError, OSError) as exc:
            if self.client:
                self.client.stop()
            return ImportResult(False, errors=errors, message=str(exc))
