"""Streaming SQL statement parsing helpers.

The parser deliberately supports normal mysqldump/phpMyAdmin output.  Custom
DELIMITER scripts are rejected because splitting stored-program bodies without
a full SQL grammar would make resumable checkpoints unsafe.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from typing import BinaryIO, Iterator


class UnsupportedSqlError(ValueError):
    pass


@dataclass
class SqlStatement:
    stream: BinaryIO
    start: int
    end: int
    size: int
    preview: str
    keyword: str

    def close(self) -> None:
        self.stream.close()


_COMMENT_PREFIX = re.compile(
    rb"^(?:\s+|--[^\r\n]*(?:\r?\n|$)|\#[^\r\n]*(?:\r?\n|$)|/\*(?![!M]).*?\*/)*",
    re.DOTALL,
)


def statement_keyword(sample: bytes) -> str:
    cleaned = _COMMENT_PREFIX.sub(b"", sample, count=1).lstrip()
    executable = re.match(rb"/\*(?:!|M!)[0-9]*\s*([A-Za-z]+)", cleaned)
    if executable:
        return executable.group(1).decode("ascii", "ignore").upper()
    match = re.match(rb"([A-Za-z]+)", cleaned)
    return match.group(1).decode("ascii", "ignore").upper() if match else ""


def is_read_only_sql(sql: str) -> bool:
    keyword = statement_keyword(sql.encode("utf-8", "replace"))
    return keyword in {"SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN"}


def split_sql_text(sql: str) -> list[str]:
    """Split editor text with the same rules used by the file importer."""
    raw = sql.encode("utf-8")
    statements: list[str] = []
    with tempfile.NamedTemporaryFile(delete=False) as source:
        source.write(raw)
        path = source.name
    try:
        for item in iter_sql_statements(path):
            item.stream.seek(0)
            statements.append(item.stream.read().decode("utf-8", "replace").strip())
            item.close()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return [item for item in statements if item]


def iter_sql_statements(
    path: str,
    start_offset: int = 0,
    chunk_size: int = 1024 * 1024,
    spool_limit: int = 8 * 1024 * 1024,
) -> Iterator[SqlStatement]:
    """Yield semicolon-terminated statements without retaining the dump in RAM."""
    with open(path, "rb") as source:
        source.seek(start_offset)
        absolute = start_offset
        statement_start = start_offset
        output = tempfile.SpooledTemporaryFile(max_size=spool_limit, mode="w+b")
        sample = bytearray()
        pending = bytearray()
        previous_value = None
        state = "normal"
        escaped = False
        line_probe = bytearray()

        def append(value: int) -> None:
            pending.append(value)
            if len(pending) >= 64 * 1024:
                output.write(pending)
                pending.clear()
            if len(sample) < 8192:
                sample.append(value)

        try:
            while True:
                block = source.read(chunk_size)
                if not block:
                    break
                for value in block:
                    absolute += 1
                    append(value)
                    char = chr(value)
                    previous = previous_value
                    previous_value = value

                    if char not in "\r\n" and len(line_probe) < 128:
                        line_probe.append(value)
                        if line_probe.lstrip().upper().startswith(b"DELIMITER"):
                            raise UnsupportedSqlError(
                                "This dump uses DELIMITER (stored routines/triggers), which cannot be "
                                "checkpointed safely. Export it as a standard schema/data dump."
                            )
                    if char == "\n":
                        if line_probe.lstrip().upper().startswith(b"DELIMITER"):
                            raise UnsupportedSqlError(
                                "This dump uses DELIMITER (stored routines/triggers), which cannot be "
                                "checkpointed safely. Export it as a standard schema/data dump."
                            )
                        line_probe.clear()

                    if state in {"single", "double", "backtick"}:
                        if escaped:
                            escaped = False
                            continue
                        if value == 0x5C and state != "backtick":
                            escaped = True
                            continue
                        quote = {"single": "'", "double": '"', "backtick": "`"}[state]
                        if char == quote:
                            state = "normal"
                        continue

                    if state == "line_comment":
                        if char in "\r\n":
                            state = "normal"
                        continue
                    if state == "block_comment":
                        if char == "/" and len(sample) >= 2 and sample[-2] == ord("*"):
                            state = "normal"
                        continue

                    if char == "'":
                        state = "single"
                    elif char == '"':
                        state = "double"
                    elif char == "`":
                        state = "backtick"
                    elif char == "#":
                        state = "line_comment"
                    elif char == "*" and previous == ord("/"):
                        state = "block_comment"
                    elif char == "-" and previous == ord("-"):
                        state = "line_comment"
                    elif char == ";":
                        if pending:
                            output.write(pending)
                            pending.clear()
                        output.flush()
                        if statement_keyword(bytes(sample)):
                            output.seek(0)
                            preview = bytes(sample[:500]).decode("utf-8", "replace").strip()
                            yield SqlStatement(
                                output, statement_start, absolute, absolute - statement_start,
                                preview, statement_keyword(bytes(sample)),
                            )
                            output = tempfile.SpooledTemporaryFile(max_size=spool_limit, mode="w+b")
                            sample = bytearray()
                        else:
                            output.seek(0)
                            output.truncate(0)
                            sample.clear()
                        statement_start = absolute
                        previous_value = None

            if pending:
                output.write(pending)
                pending.clear()
            output.flush()
            if output.tell() and statement_keyword(bytes(sample)):
                output.seek(0)
                preview = bytes(sample[:500]).decode("utf-8", "replace").strip()
                yield SqlStatement(
                    output, statement_start, absolute, absolute - statement_start,
                    preview, statement_keyword(bytes(sample)),
                )
                output = None
        finally:
            if output is not None:
                output.close()
