"""MySQL command-line client integration used by the desktop UI."""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, TypedDict


CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
SYSTEM_DATABASES = {"information_schema", "performance_schema", "mysql", "sys"}
CONNECTION_MARKERS = (
    "server has gone away", "lost connection", "can't connect", "cannot connect",
    "connection refused", "connection timed out", "query timed out", "error 2002", "error 2003",
    "error 2006", "error 2013", "broken pipe",
)
BACKUP_HEADER_PREFIX = b"-- XAMPP_DATABASE_TOOLKIT_BACKUP "
BACKUP_FORMAT_VERSION = 1
STREAM_CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True)
class MySQLConfig:
    executable: str
    host: str = "127.0.0.1"
    port: str = "3306"
    user: str = "root"
    password: str = ""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[str]]
    affected_rows: int | None
    elapsed: float
    truncated: bool = False
    stderr: str = ""


class BackupMetadata(TypedDict):
    format_version: int
    source_database: str
    created_at: str


class MySQLError(RuntimeError):
    def __init__(self, message: str, connection_error: bool = False):
        super().__init__(message)
        self.connection_error = connection_error


class DatabaseOperationCancelled(RuntimeError):
    """Raised after a streaming backup or restore is cancelled safely."""


def is_connection_error(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in CONNECTION_MARKERS)


class MySQLService:
    def __init__(self, config_provider: Callable[[], MySQLConfig]):
        self._config_provider = config_provider

    def config(self) -> MySQLConfig:
        cfg = self._config_provider()
        if not cfg.executable or not os.path.isfile(cfg.executable):
            raise FileNotFoundError("mysql.exe was not found. Select it in Connection settings.")
        return cfg

    def command(self, database: str | None = None, extra: list[str] | None = None) -> tuple[list[str], dict[str, str]]:
        cfg = self.config()
        cmd = [
            cfg.executable, "-h", cfg.host or "127.0.0.1", "-P", cfg.port or "3306",
            "-u", cfg.user or "root", "--default-character-set=utf8mb4",
            "--connect-timeout=10", "--max-allowed-packet=1G",
        ]
        if database:
            cmd.append(database)
        if extra:
            cmd.extend(extra)
        env = os.environ.copy()
        if cfg.password:
            env["MYSQL_PWD"] = cfg.password
        else:
            env.pop("MYSQL_PWD", None)
        return cmd, env

    def run(
        self,
        extra: list[str],
        database: str | None = None,
        timeout: int = 30,
        safe_retry: bool = False,
        retry_callback: Callable[[int, int], None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        delays = [1, 2, 4, 8, 16] if safe_retry else []
        attempt = 0
        while True:
            cmd, env = self.command(database, extra)
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout, env=env,
                    creationflags=CREATE_NO_WINDOW,
                )
            except subprocess.TimeoutExpired as exc:
                message = f"MySQL command timed out after {timeout} seconds."
                result = None
                connection_failure = True
            else:
                message = (result.stderr or result.stdout or "MySQL command failed.").strip()
                connection_failure = is_connection_error(message)
                if result.returncode == 0:
                    return result
                if not connection_failure:
                    raise MySQLError(message, False)

            if attempt >= len(delays):
                raise MySQLError(message, connection_failure)
            delay = delays[attempt]
            attempt += 1
            if retry_callback:
                retry_callback(attempt, delay)
            time.sleep(delay)

    def ping(self) -> float:
        started = time.monotonic()
        self.run(["-N", "-B", "-e", "SELECT 1;"], timeout=12, safe_retry=False)
        return time.monotonic() - started

    def databases(self, retry_callback=None) -> list[str]:
        result = self.run(
            ["-N", "-B", "-e", "SHOW DATABASES;"], timeout=15,
            safe_retry=True, retry_callback=retry_callback,
        )
        return [line.strip() for line in result.stdout.splitlines()
                if line.strip() and line.strip().lower() not in SYSTEM_DATABASES]

    def database_is_empty(self, database: str) -> bool:
        """Return whether a schema contains no tables, views, routines, or events."""
        schema_value = f"CONVERT(0x{database.encode('utf-8').hex()} USING utf8mb4)"
        query = (
            "SELECT "
            f"(SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA={schema_value}) + "
            f"(SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA={schema_value}) + "
            f"(SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA={schema_value});"
        )
        result = self.run(["-N", "-B", "-e", query], timeout=30)
        try:
            return int(result.stdout.strip()) == 0
        except ValueError as exc:
            raise MySQLError("MySQL did not return a valid destination object count.") from exc

    @staticmethod
    def _drain_binary_error(stream, output: bytearray) -> None:
        if stream is None:
            return
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            if len(output) < 65_536:
                output.extend(chunk[:65_536 - len(output)])

    @staticmethod
    def _stop_process(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    @classmethod
    def _watch_cancellation(
        cls,
        process: subprocess.Popen,
        cancelled: threading.Event,
        finished: threading.Event,
    ) -> None:
        while not finished.wait(0.1):
            if cancelled.is_set():
                cls._stop_process(process)
                return

    @staticmethod
    def _backup_header(database: str) -> bytes:
        metadata = {
            "format_version": BACKUP_FORMAT_VERSION,
            "source_database": database,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        return BACKUP_HEADER_PREFIX + json.dumps(
            metadata, ensure_ascii=True, separators=(",", ":"),
        ).encode("ascii") + b"\n"

    @staticmethod
    def _read_backup_header(stream) -> BackupMetadata:
        try:
            line = stream.readline(65_537)
        except (OSError, EOFError) as exc:
            raise MySQLError("The backup file is damaged or is not a valid toolkit backup.") from exc
        if len(line) > 65_536 or not line.startswith(BACKUP_HEADER_PREFIX):
            raise MySQLError("Only backups created by XAMPP Database Toolkit can be restored here.")
        try:
            metadata = json.loads(line[len(BACKUP_HEADER_PREFIX):].decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MySQLError("The toolkit backup metadata is damaged.") from exc
        if (
            not isinstance(metadata, dict)
            or metadata.get("format_version") != BACKUP_FORMAT_VERSION
            or not isinstance(metadata.get("source_database"), str)
            or not isinstance(metadata.get("created_at"), str)
        ):
            raise MySQLError("This toolkit backup format is unsupported or incomplete.")
        return BackupMetadata(
            format_version=metadata["format_version"],
            source_database=metadata["source_database"],
            created_at=metadata["created_at"],
        )

    def inspect_backup(self, path: str) -> BackupMetadata:
        """Validate a toolkit backup header and return its non-secret metadata."""
        lowered_path = path.casefold()
        if not (lowered_path.endswith(".sql") or lowered_path.endswith(".sql.gz")):
            raise MySQLError("Toolkit backups must use the .sql or .sql.gz file extension.")
        compressed = lowered_path.endswith(".sql.gz")
        try:
            with open(path, "rb") as raw:
                if compressed:
                    with gzip.GzipFile(fileobj=raw, mode="rb") as stream:
                        return self._read_backup_header(stream)
                return self._read_backup_header(raw)
        except FileNotFoundError:
            raise
        except (OSError, EOFError) as exc:
            raise MySQLError("The backup file is damaged or cannot be read.") from exc

    def backup_database(
        self,
        database: str,
        output_path: str,
        compress: bool,
        progress: Callable[[int], None] | None = None,
        cancelled: threading.Event | None = None,
    ) -> int:
        """Stream a complete logical backup to an atomically replaced file."""
        cfg = self.config()
        dump_executable = os.path.join(os.path.dirname(cfg.executable), "mysqldump.exe")
        if not os.path.isfile(dump_executable):
            raise FileNotFoundError(
                "mysqldump.exe was not found beside mysql.exe in the selected XAMPP folder."
            )
        output_path = os.path.abspath(output_path)
        output_directory = os.path.dirname(output_path)
        if not os.path.isdir(output_directory):
            raise FileNotFoundError(f"Backup folder was not found: {output_directory}")
        dump_command = [
            dump_executable,
            "--host", cfg.host or "127.0.0.1",
            "--port", cfg.port or "3306",
            "--user", cfg.user or "root",
            "--default-character-set=utf8mb4",
            "--single-transaction",
            "--quick",
            "--routines",
            "--events",
            "--triggers",
            "--quote-names",
            "--hex-blob",
            "--",
            database,
        ]
        environment = os.environ.copy()
        if cfg.password:
            environment["MYSQL_PWD"] = cfg.password
        else:
            environment.pop("MYSQL_PWD", None)

        temp_fd, temp_path = tempfile.mkstemp(
            prefix=".xampp-toolkit-backup-", suffix=".tmp", dir=output_directory,
        )
        os.close(temp_fd)
        process = None
        error_output = bytearray()
        cancellation = cancelled or threading.Event()
        finished = threading.Event()
        try:
            process = subprocess.Popen(
                dump_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                creationflags=CREATE_NO_WINDOW,
            )
            assert process.stdout is not None
            error_thread = threading.Thread(
                target=self._drain_binary_error,
                args=(process.stderr, error_output),
                daemon=True,
            )
            error_thread.start()
            threading.Thread(
                target=self._watch_cancellation,
                args=(process, cancellation, finished),
                daemon=True,
            ).start()
            with open(temp_path, "wb") as raw:
                destination = gzip.GzipFile(filename="", fileobj=raw, mode="wb") if compress else raw
                try:
                    destination.write(self._backup_header(database))
                    while True:
                        if cancellation.is_set():
                            raise DatabaseOperationCancelled("Backup cancelled.")
                        chunk = process.stdout.read(STREAM_CHUNK_SIZE)
                        if not chunk:
                            break
                        destination.write(chunk)
                        if progress:
                            progress(raw.tell())
                finally:
                    if compress:
                        destination.close()
                raw.flush()
                os.fsync(raw.fileno())
            return_code = process.wait()
            error_thread.join()
            if cancellation.is_set():
                raise DatabaseOperationCancelled("Backup cancelled.")
            if return_code:
                detail = error_output.decode("utf-8", "replace").strip()
                raise MySQLError(detail or f"mysqldump.exe exited with code {return_code}.")
            os.replace(temp_path, output_path)
            if progress:
                progress(os.path.getsize(output_path))
            return os.path.getsize(output_path)
        finally:
            finished.set()
            if process is not None:
                self._stop_process(process)
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass

    def restore_database(
        self,
        backup_path: str,
        destination: str,
        progress: Callable[[int, int], None] | None = None,
        cancelled: threading.Event | None = None,
    ) -> BackupMetadata:
        """Stream a validated toolkit backup into an existing empty database."""
        metadata = self.inspect_backup(backup_path)
        if not self.database_is_empty(destination):
            raise MySQLError("The destination database is not empty. Choose an empty database.")
        total = os.path.getsize(backup_path)
        compressed = backup_path.casefold().endswith(".gz")
        source_prefix = (
            "`" + metadata["source_database"].replace("`", "``") + "`."
        ).encode("utf-8")
        destination_prefix = (
            "`" + destination.replace("`", "``") + "`."
        ).encode("utf-8")
        command, environment = self.command(destination)
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=environment,
            creationflags=CREATE_NO_WINDOW,
        )
        assert process.stdin is not None
        error_output = bytearray()
        error_thread = threading.Thread(
            target=self._drain_binary_error,
            args=(process.stderr, error_output),
            daemon=True,
        )
        error_thread.start()
        cancellation = cancelled or threading.Event()
        finished = threading.Event()
        threading.Thread(
            target=self._watch_cancellation,
            args=(process, cancellation, finished),
            daemon=True,
        ).start()
        try:
            with open(backup_path, "rb") as raw:
                stream = gzip.GzipFile(fileobj=raw, mode="rb") if compressed else raw
                try:
                    self._read_backup_header(stream)
                    pending = b""
                    while True:
                        if cancellation.is_set():
                            raise DatabaseOperationCancelled("Restore cancelled.")
                        chunk = stream.read(STREAM_CHUNK_SIZE)
                        if not chunk:
                            break
                        pending += chunk
                        if source_prefix != destination_prefix:
                            safe_start = len(pending) - len(source_prefix)
                            if safe_start < 0:
                                continue
                            output = bytearray()
                            cursor = 0
                            while True:
                                match = pending.find(source_prefix, cursor)
                                if match < 0 or match > safe_start:
                                    break
                                output.extend(pending[cursor:match])
                                output.extend(destination_prefix)
                                cursor = match + len(source_prefix)
                            cutoff = max(cursor, safe_start + 1)
                            output.extend(pending[cursor:cutoff])
                            process.stdin.write(output)
                            pending = pending[cutoff:]
                        else:
                            process.stdin.write(pending)
                            pending = b""
                        if progress:
                            progress(min(raw.tell(), total), total)
                    if pending:
                        process.stdin.write(
                            pending.replace(source_prefix, destination_prefix)
                            if source_prefix != destination_prefix else pending
                        )
                finally:
                    if compressed:
                        stream.close()
            process.stdin.close()
            return_code = process.wait()
            error_thread.join()
            if cancellation.is_set():
                raise DatabaseOperationCancelled("Restore cancelled.")
            if return_code:
                detail = error_output.decode("utf-8", "replace").strip()
                raise MySQLError(detail or f"mysql.exe exited with code {return_code}.")
            if progress:
                progress(total, total)
            return metadata
        except (BrokenPipeError, OSError) as exc:
            if cancellation.is_set():
                raise DatabaseOperationCancelled("Restore cancelled.") from exc
            self._stop_process(process)
            error_thread.join()
            detail = error_output.decode("utf-8", "replace").strip()
            raise MySQLError(detail or "mysql.exe stopped before the backup was restored.") from exc
        finally:
            finished.set()
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            self._stop_process(process)

    def copy_database(self, source: str, destination: str) -> None:
        """Stream one database through mysqldump into another database."""
        cfg = self.config()
        dump_executable = os.path.join(os.path.dirname(cfg.executable), "mysqldump.exe")
        if not os.path.isfile(dump_executable):
            raise FileNotFoundError(
                "mysqldump.exe was not found beside mysql.exe in the selected XAMPP folder."
            )
        dump_command = [
            dump_executable,
            "--host", cfg.host or "127.0.0.1",
            "--port", cfg.port or "3306",
            "--user", cfg.user or "root",
            "--default-character-set=utf8mb4",
            "--single-transaction",
            "--quick",
            "--routines",
            "--events",
            "--triggers",
            "--hex-blob",
            "--",
            source,
        ]
        dump_environment = os.environ.copy()
        if cfg.password:
            dump_environment["MYSQL_PWD"] = cfg.password
        else:
            dump_environment.pop("MYSQL_PWD", None)
        restore_command, restore_environment = self.command(destination)

        dump_process = subprocess.Popen(
            dump_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dump_environment,
            creationflags=CREATE_NO_WINDOW,
        )
        assert dump_process.stdout is not None
        try:
            restore_process = subprocess.Popen(
                restore_command,
                stdin=dump_process.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=restore_environment,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception:
            dump_process.kill()
            dump_process.wait()
            raise
        finally:
            dump_process.stdout.close()

        def drain_error(stream, output: bytearray) -> None:
            if stream is None:
                return
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    return
                if len(output) < 65_536:
                    output.extend(chunk[:65_536 - len(output)])

        dump_error, restore_error = bytearray(), bytearray()
        dump_error_thread = threading.Thread(
            target=drain_error, args=(dump_process.stderr, dump_error), daemon=True,
        )
        restore_error_thread = threading.Thread(
            target=drain_error, args=(restore_process.stderr, restore_error), daemon=True,
        )
        dump_error_thread.start()
        restore_error_thread.start()
        dump_return_code = dump_process.wait()
        restore_return_code = restore_process.wait()
        dump_error_thread.join()
        restore_error_thread.join()

        if dump_return_code:
            detail = dump_error.decode("utf-8", "replace").strip()
            raise MySQLError(detail or f"mysqldump.exe exited with code {dump_return_code}.")
        if restore_return_code:
            detail = restore_error.decode("utf-8", "replace").strip()
            raise MySQLError(detail or f"mysql.exe exited with code {restore_return_code}.")

    def execute_query(self, sql: str, database: str, read_only: bool, max_rows: int = 1000) -> QueryResult:
        marker = "__xsi_affected_rows__"
        combined = f"{sql.rstrip().rstrip(';')};\nSELECT ROW_COUNT() AS `{marker}`;"
        delays = [0, 1, 2, 4, 8, 16] if read_only else [0]
        last_error = ""
        for delay in delays:
            if delay:
                time.sleep(delay)
            started = time.monotonic()
            cmd, env = self.command(database, ["--batch", "--raw", "--quick", "-e", combined])
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                creationflags=CREATE_NO_WINDOW, bufsize=1,
            )
            timed_out = threading.Event()

            def terminate_on_timeout():
                timed_out.set()
                if process.poll() is None:
                    process.kill()

            timeout_timer = threading.Timer(300, terminate_on_timeout)
            timeout_timer.daemon = True
            timeout_timer.start()
            error_lines: list[str] = []

            def drain_errors():
                assert process.stderr is not None
                error_lines.extend(process.stderr)

            error_thread = threading.Thread(target=drain_errors, daemon=True)
            error_thread.start()
            columns: list[str] = []
            rows: list[list[str]] = []
            affected = None
            truncated = False
            reading_affected = False
            assert process.stdout is not None
            for raw_line in process.stdout:
                line = raw_line.rstrip("\r\n")
                if line.strip() == marker:
                    reading_affected = True
                    continue
                if reading_affected:
                    try:
                        affected = int(line.strip())
                    except ValueError:
                        pass
                    reading_affected = False
                    continue
                if not columns:
                    columns = line.split("\t")
                elif len(rows) < max_rows:
                    rows.append(line.split("\t"))
                else:
                    truncated = True
            return_code = process.wait()
            timeout_timer.cancel()
            error_thread.join(timeout=2)
            error = "".join(error_lines).strip()
            if timed_out.is_set():
                error = "MySQL query timed out after 300 seconds."
                return_code = -1
            if return_code == 0:
                return QueryResult(columns, rows, affected, time.monotonic() - started, truncated, error)
            last_error = error or f"mysql.exe exited with code {return_code}."
            if not read_only or not is_connection_error(last_error):
                raise MySQLError(last_error, is_connection_error(last_error))
        raise MySQLError(last_error or "Query failed after five reconnect attempts.", True)
