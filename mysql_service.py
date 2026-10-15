"""MySQL command-line client integration used by the desktop UI."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable


CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
SYSTEM_DATABASES = {"information_schema", "performance_schema", "mysql", "sys"}
CONNECTION_MARKERS = (
    "server has gone away", "lost connection", "can't connect", "cannot connect",
    "connection refused", "connection timed out", "query timed out", "error 2002", "error 2003",
    "error 2006", "error 2013", "broken pipe",
)


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


class MySQLError(RuntimeError):
    def __init__(self, message: str, connection_error: bool = False):
        super().__init__(message)
        self.connection_error = connection_error


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
