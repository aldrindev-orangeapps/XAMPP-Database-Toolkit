"""Local single-admin authentication for the desktop application."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time


AUTH_DIR = os.path.join(os.path.expanduser("~"), ".xampp_sql_importer")
AUTH_FILE = os.path.join(AUTH_DIR, "auth.json")
PBKDF2_ITERATIONS = 600_000
MIN_PASSWORD_LENGTH = 4


class AuthError(RuntimeError):
    pass


class LocalAuth:
    def __init__(self, path: str = AUTH_FILE):
        self.path = path

    @property
    def is_configured(self) -> bool:
        data = self._read()
        return bool(data.get("salt") and data.get("password_hash"))

    def setup(self, password: str) -> None:
        self._validate_password(password)
        salt = secrets.token_bytes(32)
        digest = self._derive(password, salt, PBKDF2_ITERATIONS)
        self._write({
            "version": 1,
            "algorithm": "pbkdf2_sha256",
            "iterations": PBKDF2_ITERATIONS,
            "salt": base64.b64encode(salt).decode("ascii"),
            "password_hash": base64.b64encode(digest).decode("ascii"),
            "failed_attempts": 0,
            "locked_until": 0,
        })

    def verify(self, password: str) -> tuple[bool, int]:
        data = self._read()
        if not self.is_configured:
            raise AuthError("The administrator password has not been configured.")
        now = int(time.time())
        locked_until = int(data.get("locked_until", 0))
        if locked_until > now:
            return False, locked_until - now
        try:
            salt = base64.b64decode(data["salt"], validate=True)
            expected = base64.b64decode(data["password_hash"], validate=True)
            iterations = int(data.get("iterations", PBKDF2_ITERATIONS))
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthError("The local authentication file is damaged.") from exc
        actual = self._derive(password, salt, iterations)
        if hmac.compare_digest(actual, expected):
            data["failed_attempts"] = 0
            data["locked_until"] = 0
            self._write(data)
            return True, 0

        attempts = int(data.get("failed_attempts", 0)) + 1
        data["failed_attempts"] = attempts
        wait = 0
        if attempts >= 5:
            wait = min(900, 30 * (2 ** min(5, (attempts - 5) // 5)))
            data["locked_until"] = now + wait
        self._write(data)
        return False, wait

    def change_password(self, current_password: str, new_password: str) -> None:
        valid, wait = self.verify(current_password)
        if not valid:
            if wait:
                raise AuthError(f"Too many failed attempts. Try again in {wait} seconds.")
            raise AuthError("The current password is incorrect.")
        self.setup(new_password)

    @staticmethod
    def _validate_password(password: str) -> None:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise AuthError(f"Use at least {MIN_PASSWORD_LENGTH} characters for the administrator password.")
        if password.isspace():
            raise AuthError("The administrator password cannot contain only spaces.")

    @staticmethod
    def _derive(password: str, salt: bytes, iterations: int) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=32)

    def _read(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
                return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise AuthError(f"Could not read local authentication settings: {exc}") from exc

    def _write(self, data: dict) -> None:
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix="auth-", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
