# XAMPP Database Toolkit

A modern Windows desktop tool for importing large SQL dumps, managing local XAMPP databases, running SQL, and sanitizing development data. The interface uses the bundled **Poppins** font and only requires `mysql.exe` at runtime.

## Features

- Checkpointed, bounded-memory import for 1 GB+ phpMyAdmin and mysqldump files
- Confirmed-statement progress instead of optimistic pipe progress
- Live transfer speed, elapsed time, and estimated remaining time during imports
- Safe reconnect attempts after MySQL connection failures
- Resume after reopening the app when the file and target connection have not changed
- Database creation, search, selection, and protected deletion
- Organized Apache and MySQL start/stop controls with live process status and clickable local URLs
- Syntax-highlighted SQL editor with selected-text execution, result tables, affected-row counts, and destructive-query confirmation
- Email, phone, and password column scanner/sanitizer
- Connection status, activity logs, cancellation, and clear error reporting
- Built-in scrollable user manual covering setup, features, safety, and troubleshooting
- Single-administrator login, logout, failed-attempt lockout, and idle auto-lock
- Original database-import security icon for the window, taskbar, and EXE
- Poppins Regular, Medium, and SemiBold bundled into the standalone EXE
- Vertically scrollable main pages for smaller windows and longer content

## Requirements

- Windows with XAMPP/MySQL or MariaDB installed
- `mysql.exe`, normally at `C:\xampp\mysql\bin\mysql.exe`
- Python 3 only when running from source or building the EXE

## Run from source

```powershell
python .\xampp_database_toolkit.py
```

## Build the standalone EXE

Install PyInstaller, then build with the checked-in spec file:

```powershell
python -m pip install pyinstaller
python -m PyInstaller --noconfirm ".\XAMPP Database Toolkit.spec"
```

The result is written to `dist\XAMPP Database Toolkit.exe`.

The spec includes the Poppins files from `assets\fonts`. To build without it:

```powershell
python -m PyInstaller --onefile --windowed --name "XAMPP Database Toolkit" --icon "assets\icons\app-icon.ico" --add-data "assets\fonts;assets\fonts" --add-data "assets\icons;assets\icons" .\xampp_database_toolkit.py
```

## Import and recovery

1. Open **Databases**, verify the connection, and select or create the target database.
2. Open **Import**, choose a standard `.sql` dump, and start the import.
3. Progress advances only after MySQL acknowledges a complete SQL statement.
4. Safe connection failures are retried five times with increasing delays.
5. If a non-transactional statement may have committed during a disconnect, the app asks whether to retry, skip, or stop. It never silently risks duplicating data.
6. Cancel, logout, and window close wait for the current statement acknowledgement before saving the checkpoint and stopping. For a very large statement, keep the app open while this safe stop finishes.

If the first statement after a resume reports duplicate-key error 1062, the app treats it as an uncertain previously committed statement. It asks whether to preserve the checkpoint and stop or explicitly assume that statement already executed and continue; it is never skipped silently.

Checkpoints are stored under `~/.xampp_sql_importer`. Confirmed offsets use a lightweight append-only journal and are periodically compacted into `checkpoints.json`; this preserves statement-level recovery without rewriting the complete checkpoint file after every statement. A checkpoint is accepted only when the file path, size, modification time, server, port, and database match. Successful imports remove their checkpoint.

Critical mysqldump session settings—including foreign-key checks, unique checks, SQL mode, and time zone—are pinned in the checkpoint and restored after reopening or reconnecting. Older checkpoints recover these settings from the dump preamble before resuming.

When a checkpoint already exists, the app asks whether to **Resume**, **Start fresh**, or **Cancel**. Deleting a database through the app also removes checkpoints tied to that server and database so a recreated database cannot accidentally resume from a stale offset.

The parser supports standard phpMyAdmin/mysqldump schema and data exports, including comments, quoted semicolons, multiline statements, and transactions. Scripts using custom `DELIMITER` blocks, stored procedures, functions, or triggers are rejected because arbitrary resume points are unsafe for them.

## Local administrator login

The first launch asks you to create one administrator password with at least four characters. The app stores only a randomly salted PBKDF2-SHA256 hash in `~/.xampp_sql_importer/auth.json`; it never stores the original password.

- Five failed attempts trigger a persistent timed lockout.
- **Logout** is always available in the header.
- An inactive session automatically locks after 15 minutes. An active import is allowed to reach a safe state before auto-locking.
- Logging out clears the in-memory MySQL password and selected database.
- The administrator password can be changed from **Databases → Change app password**.

This local login protects against casual access to the application. The Windows account and its filesystem permissions remain the security boundary: anyone who controls that Windows account can modify the application or its local authentication file.

## Database tools

- **XAMPP services** uses the official `apache_start.bat`, `apache_stop.bat`, `mysql_start.bat`, and `mysql_stop.bat` scripts found beside the configured `mysql\bin\mysql.exe`. Stopping MySQL is blocked while an import is active. Quick links open the local site and phpMyAdmin when their required services are running; a custom Apache `Listen` port is detected automatically.
- **Delete selected** requires the exact database name. MySQL system databases are hidden and protected.
- **SQL Console** runs selected text, or the complete editor if nothing is selected. Writes and schema changes require confirmation. Up to 1,000 result rows are displayed.
- **Sanitizer** accepts multiple comma-separated column names for email, phone, and password (for example, `email, email_address, work_email`). Only exact full-name, case-insensitive matches are listed and updated, and every match in a category receives that category's single replacement value. Replacements are permanent, so retain the original dump.
- Detected password columns can be reset across every row to `c4ca4238a0b923820dcc509a6f75849b`, whose default login value is `1`.

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
