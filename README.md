# XAMPP SQL Importer & Data Sanitizer

A modern Windows desktop tool for importing large SQL dumps, managing local XAMPP databases, running SQL, and sanitizing development data. The interface uses the bundled **Poppins** font and only requires `mysql.exe` at runtime.

## Features

- Checkpointed, bounded-memory import for 1 GB+ phpMyAdmin and mysqldump files
- Confirmed-statement progress instead of optimistic pipe progress
- Safe reconnect attempts after MySQL connection failures
- Resume after reopening the app when the file and target connection have not changed
- Database creation, search, selection, and protected deletion
- SQL editor with selected-text execution, result tables, affected-row counts, and destructive-query confirmation
- Email and phone column scanner/sanitizer
- Connection status, activity logs, cancellation, and clear error reporting
- Poppins Regular, Medium, and SemiBold bundled into the standalone EXE

## Requirements

- Windows with XAMPP/MySQL or MariaDB installed
- `mysql.exe`, normally at `C:\xampp\mysql\bin\mysql.exe`
- Python 3 only when running from source or building the EXE

## Run from source

```powershell
python .\xampp_sql_importer.py
```

## Build the standalone EXE

Install PyInstaller, then build with the checked-in spec file:

```powershell
python -m pip install pyinstaller
python -m PyInstaller --noconfirm ".\XAMPP SQL Importer.spec"
```

The result is written to `dist\XAMPP SQL Importer.exe`.

The spec includes the Poppins files from `assets\fonts`. To build without it:

```powershell
python -m PyInstaller --onefile --windowed --name "XAMPP SQL Importer" --add-data "assets\fonts;assets\fonts" .\xampp_sql_importer.py
```

## Import and recovery

1. Open **Databases**, verify the connection, and select or create the target database.
2. Open **Import**, choose a standard `.sql` dump, and start the import.
3. Progress advances only after MySQL acknowledges a complete SQL statement.
4. Safe connection failures are retried five times with increasing delays.
5. If a non-transactional statement may have committed during a disconnect, the app asks whether to retry, skip, or stop. It never silently risks duplicating data.

Checkpoints are stored in `~/.xampp_sql_importer/checkpoints.json`. A checkpoint is accepted only when the file path, size, modification time, server, port, and database match. Successful imports remove their checkpoint.

The parser supports standard phpMyAdmin/mysqldump schema and data exports, including comments, quoted semicolons, multiline statements, and transactions. Scripts using custom `DELIMITER` blocks, stored procedures, functions, or triggers are rejected because arbitrary resume points are unsafe for them.

## Database tools

- **Delete selected** requires the exact database name. MySQL system databases are hidden and protected.
- **SQL Console** runs selected text, or the complete editor if nothing is selected. Writes and schema changes require confirmation. Up to 1,000 result rows are displayed.
- **Sanitizer** scans the selected database for common email and phone column names. Replacements are permanent, so retain the original dump.

## Settings and security

Connection settings are saved in `~/.xampp_sql_importer.ini`; the password is never saved. The password is passed to `mysql.exe` through its child-process environment instead of appearing in command-line arguments.

## Troubleshooting

| Problem | Resolution |
|---|---|
| App cannot find `mysql.exe` | Open **Databases**, browse to the XAMPP MySQL client, and save settings. |
| Connection fails | Start MySQL in XAMPP and verify host, port, user, and password. |
| A statement exceeds `max_allowed_packet` | Follow the displayed `my.ini` instruction, restart MySQL, then resume. |
| Import pauses after a disconnect | Review the shown statement. Retry may duplicate it; skip only if you know it committed. |
| Dump uses `DELIMITER` | Export ordinary schema/data SQL without stored routines, or use a specialized client. |
| Font falls back to Segoe UI | Rebuild with the spec so `assets\fonts` is included. |

## Font license

Poppins is distributed under the SIL Open Font License. The license is included at `assets\fonts\OFL.txt`.
