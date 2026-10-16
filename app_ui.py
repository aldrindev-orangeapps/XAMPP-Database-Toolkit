"""Modern Tkinter user interface for XAMPP Database Toolkit."""

from __future__ import annotations

import configparser
import ctypes
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from tkinter import filedialog, simpledialog, ttk

from checkpoints import CheckpointStore, checkpoint_key
from importer import ResumableImporter
from local_auth import AuthError, LocalAuth
from mysql_service import MySQLConfig, MySQLService, SYSTEM_DATABASES
from sql_parser import is_read_only_sql, split_sql_text


CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".xampp_sql_importer.ini")
DEFAULT_MYSQL_PATHS = [r"C:\xampp\mysql\bin\mysql.exe", r"D:\xampp\mysql\bin\mysql.exe"]
DEFAULT_EMAIL_COLUMN = "email"
DEFAULT_PHONE_COLUMN = "phone"
DEFAULT_PASSWORD_COLUMN = "password"
DEFAULT_PASSWORD_HASH = "c4ca4238a0b923820dcc509a6f75849b"

BG, SURFACE, SIDEBAR, SIDEBAR_HOVER = "#F5F7FB", "#FFFFFF", "#111827", "#1F2937"
PRIMARY, PRIMARY_DARK = "#4F46E5", "#4338CA"
TEXT, MUTED, BORDER = "#111827", "#6B7280", "#E5E7EB"
SUCCESS, DANGER = "#059669", "#DC2626"
IDLE_LOGOUT_SECONDS = 15 * 60
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
XAMPP_SERVICES = {
    "apache": {"process": "httpd.exe", "label": "Apache"},
    "mysql": {"process": "mysqld.exe", "label": "MySQL"},
}
NAVIGATION_PAGES = ("Databases", "Import", "Sanitizer", "SQL Console", "Logs", "User Manual")

SQL_KEYWORDS = (
    "ADD|ALL|ALTER|AND|AS|ASC|BETWEEN|BY|CASE|COLUMN|CONSTRAINT|CREATE|CROSS|"
    "DATABASE|DEFAULT|DELETE|DESC|DISTINCT|DROP|ELSE|END|EXISTS|FOREIGN|FROM|"
    "FULL|GROUP|HAVING|IN|INDEX|INNER|INSERT|INTO|IS|JOIN|KEY|LEFT|LIKE|LIMIT|"
    "NOT|NULL|OFFSET|ON|OR|ORDER|OUTER|PRIMARY|PROCEDURE|REFERENCES|RENAME|"
    "REPLACE|RIGHT|SELECT|SET|SHOW|TABLE|THEN|TRUNCATE|UNION|UNIQUE|UPDATE|"
    "USE|VALUES|VIEW|WHEN|WHERE|WITH"
)
SQL_TOKEN_PATTERN = re.compile(
    rf"(?P<comment>--[^\n]*|\#[^\n]*|/\*[\s\S]*?\*/)"
    rf"|(?P<string>'(?:''|\\.|[^'\\])*'|\"(?:\"\"|\\.|[^\"\\])*\")"
    rf"|(?P<identifier>`(?:``|[^`])*`)"
    rf"|(?P<number>\b(?:0x[0-9a-f]+|\d+(?:\.\d+)?)\b)"
    rf"|(?P<keyword>\b(?:{SQL_KEYWORDS})\b)",
    re.IGNORECASE,
)


def xampp_root_from_mysql(mysql_executable: str) -> str:
    """Return the XAMPP root for .../mysql/bin/mysql.exe or raise a clear error."""
    path = os.path.abspath(os.path.expanduser(mysql_executable.strip()))
    bin_dir = os.path.dirname(path)
    mysql_dir = os.path.dirname(bin_dir)
    root = os.path.dirname(mysql_dir)
    if (
        os.path.basename(path).casefold() != "mysql.exe"
        or os.path.basename(bin_dir).casefold() != "bin"
        or os.path.basename(mysql_dir).casefold() != "mysql"
        or not os.path.isfile(path)
    ):
        raise FileNotFoundError("Select XAMPP's mysql\\bin\\mysql.exe in Connection settings.")
    return root


def xampp_service_running(service: str, root: str | None = None) -> bool:
    details = XAMPP_SERVICES[service]
    if root:
        relative_executable = (
            os.path.join("apache", "bin", "httpd.exe")
            if service == "apache"
            else os.path.join("mysql", "bin", "mysqld.exe")
        )
        expected_path = os.path.normcase(os.path.abspath(os.path.join(root, relative_executable)))
        if not os.path.isfile(expected_path):
            return False
        environment = os.environ.copy()
        environment["XAMPP_SERVICE_TARGET"] = expected_path
        process_name = details["process"].replace("'", "''")
        command = (
            "$target=[IO.Path]::GetFullPath($env:XAMPP_SERVICE_TARGET);"
            f"$match=Get-CimInstance Win32_Process -Filter \"Name='{process_name}'\" "
            "-ErrorAction Stop | Where-Object { $_.ExecutablePath -and "
            "([IO.Path]::GetFullPath($_.ExecutablePath) -ieq $target) };"
            "if ($match) { exit 0 } else { exit 3 }"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=12, env=environment,
            creationflags=CREATE_NO_WINDOW,
        )
        if result.returncode == 0:
            return True
        if result.returncode == 3:
            return False
        raise RuntimeError((result.stderr or "Could not check the selected XAMPP process.").strip())
    result = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {details['process']}", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, timeout=8, creationflags=CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "Could not check Windows process status.").strip())
    return f'"{details["process"]}"'.casefold() in result.stdout.casefold()


def launch_xampp_service_script(root: str, service: str, action: str) -> None:
    if service not in XAMPP_SERVICES or action not in {"start", "stop"}:
        raise ValueError("Unsupported XAMPP service action.")
    script = os.path.join(root, f"{service}_{action}.bat")
    if not os.path.isfile(script):
        raise FileNotFoundError(f"XAMPP script was not found: {script}")
    command_shell = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
    subprocess.Popen(
        [command_shell, "/D", "/C", "call", script], cwd=root,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
    )


def xampp_web_urls(root: str) -> tuple[str, str]:
    """Build local-site URLs from Apache's first active Listen directive."""
    port = "80"
    config_path = os.path.join(root, "apache", "conf", "httpd.conf")
    try:
        with open(config_path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = re.match(r"^\s*Listen\s+(?:\[[^]]+\]|[^\s:]+:)?(\d+)\s*(?:#.*)?$", line)
                if match:
                    port = match.group(1)
                    break
    except OSError:
        pass
    base = "http://localhost" + ("" if port == "80" else f":{port}") + "/"
    return base, base + "phpmyadmin/"


def parse_column_names(value: str) -> list[str]:
    """Parse comma/newline-separated exact names, preserving order."""
    names: list[str] = []
    seen: set[str] = set()
    for item in value.replace("\n", ",").split(","):
        name = item.strip()
        normalized = name.casefold()
        if name and normalized not in seen:
            names.append(name)
            seen.add(normalized)
    return names


def sanitizer_kind(
    column: str,
    email_column: str = DEFAULT_EMAIL_COLUMN,
    phone_column: str = DEFAULT_PHONE_COLUMN,
    password_column: str = DEFAULT_PASSWORD_COLUMN,
) -> str | None:
    lowered = column.casefold()
    for kind, configured_names in (
        ("password", password_column),
        ("email", email_column),
        ("phone", phone_column),
    ):
        if lowered in {name.casefold() for name in parse_column_names(configured_names)}:
            return kind
    return None


def build_sanitize_query(table: str, column: str, kind: str, value: str) -> str:
    table_q, column_q = table.replace("`", "``"), column.replace("`", "``")
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    where = "" if kind == "password" else f" WHERE `{column_q}` IS NOT NULL AND `{column_q}`<>''"
    return f"UPDATE `{table_q}` SET `{column_q}`='{escaped}'{where};"


def resource_path(relative: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


def register_poppins() -> str:
    if os.name == "nt":
        loaded = 0
        for filename in ("Poppins-Regular.ttf", "Poppins-Medium.ttf", "Poppins-SemiBold.ttf"):
            path = resource_path(os.path.join("assets", "fonts", filename))
            if os.path.isfile(path):
                try:
                    loaded += bool(ctypes.windll.gdi32.AddFontResourceExW(path, 0x10, 0))
                except (AttributeError, OSError):
                    pass
        if loaded:
            return "Poppins"
    return "Segoe UI"


class ModernApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.font_family = register_poppins()
        root.title("XAMPP Database Toolkit")
        root.geometry("1120x740")
        root.minsize(960, 640)
        root.configure(bg=BG)
        self._set_window_icon()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.mysql_path, self.host, self.port = tk.StringVar(), tk.StringVar(value="127.0.0.1"), tk.StringVar(value="3306")
        self.user, self.password = tk.StringVar(value="root"), tk.StringVar()
        self.selected_db, self.sql_file = tk.StringVar(), tk.StringVar()
        self.continue_on_error = tk.BooleanVar(value=True)
        self.connection_text, self.search_db = tk.StringVar(value="Not connected"), tk.StringVar()
        self.database_status = tk.StringVar(value="Ready")
        self.apache_service_status = tk.StringVar(value="Not checked")
        self.mysql_service_status = tk.StringVar(value="Not checked")
        self.local_site_url = tk.StringVar(value="http://localhost/")
        self.phpmyadmin_url = tk.StringVar(value="http://localhost/phpmyadmin/")
        self.xampp_service_states = {"apache": False, "mysql": False}
        self.import_status, self.import_detail = tk.StringVar(value="Ready to import"), tk.StringVar(value="Select a SQL dump to begin.")
        self.email_replacement, self.phone_replacement = tk.StringVar(value="test@example.com"), tk.StringVar(value="09170000000")
        self.password_replacement = tk.StringVar(value=DEFAULT_PASSWORD_HASH)
        self.email_column_name = tk.StringVar(value=DEFAULT_EMAIL_COLUMN)
        self.phone_column_name = tk.StringVar(value=DEFAULT_PHONE_COLUMN)
        self.password_column_name = tk.StringVar(value=DEFAULT_PASSWORD_COLUMN)
        self.query_status = tk.StringVar(value="Ready")
        self.sanitize_status = tk.StringVar(value="Scan a database to find personal data columns.")
        self.databases, self.sanitize_columns = [], []
        self.importer = None
        self.import_cancel = threading.Event()
        self.import_started = 0.0
        self.after_import_action = None
        self.active_page = "Databases"
        self.page_canvases = {}
        self.auth = LocalAuth()
        self.authenticated = False
        self.last_activity = time.monotonic()
        self.query_highlight_job = None

        self._load_config()
        if not self.mysql_path.get():
            self.mysql_path.set(next((p for p in DEFAULT_MYSQL_PATHS if os.path.isfile(p)), ""))
        self._config_value = MySQLConfig("", "127.0.0.1", "3306", "root", "")
        self._sync_config()
        self.service = MySQLService(self._mysql_config)
        self.checkpoints = CheckpointStore()
        self._configure_style()
        self._build_shell()
        self.app_shell.grid_remove()
        self._show_auth_screen()
        root.bind_all("<KeyPress>", self._record_activity, add="+")
        root.bind_all("<ButtonPress>", self._record_activity, add="+")
        root.bind_all("<MouseWheel>", self._scroll_active_page, add="+")
        root.after(30_000, self._check_idle_logout)

    def _set_window_icon(self):
        ico_path = resource_path(os.path.join("assets", "icons", "app-icon.ico"))
        png_path = resource_path(os.path.join("assets", "icons", "app-icon.png"))
        try:
            if os.path.isfile(ico_path):
                self.root.iconbitmap(default=ico_path)
        except tk.TclError:
            pass
        try:
            if os.path.isfile(png_path):
                self.app_icon_image = tk.PhotoImage(file=png_path)
                self.root.iconphoto(True, self.app_icon_image)
        except tk.TclError:
            self.app_icon_image = None

    def _configure_style(self):
        named = ("TkDefaultFont", "TkTextFont", "TkFixedFont", "TkMenuFont", "TkHeadingFont",
                 "TkCaptionFont", "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont")
        for name in named:
            try:
                tkfont.nametofont(name).configure(family=self.font_family, size=10)
            except tk.TclError:
                pass
        self.root.option_add("*Font", (self.font_family, 10))
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=SURFACE)
        style.configure("TLabel", background=BG, foreground=TEXT, font=(self.font_family, 10))
        style.configure("Card.TLabel", background=SURFACE, foreground=TEXT, font=(self.font_family, 10))
        style.configure("Muted.TLabel", background=SURFACE, foreground=MUTED, font=(self.font_family, 9))
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=(self.font_family, 20, "bold"))
        style.configure("Heading.TLabel", background=SURFACE, foreground=TEXT, font=(self.font_family, 12, "bold"))
        style.configure("TEntry", fieldbackground="#FAFBFC", bordercolor=BORDER, padding=8, font=(self.font_family, 10))
        style.configure("TCombobox", fieldbackground="#FAFBFC", bordercolor=BORDER, padding=7)
        style.configure("TCheckbutton", background=SURFACE, foreground=TEXT, font=(self.font_family, 9))
        style.configure("TButton", padding=(14, 8), background="#EEF2F7", foreground=TEXT, borderwidth=0,
                        font=(self.font_family, 9, "bold"))
        style.map("TButton", background=[("active", "#E5E7EB"), ("disabled", "#F3F4F6")])
        style.configure("Primary.TButton", background=PRIMARY, foreground="white")
        style.map("Primary.TButton", background=[("active", PRIMARY_DARK), ("disabled", "#A5B4FC")])
        style.configure("Danger.TButton", background=DANGER, foreground="white")
        style.map("Danger.TButton", background=[("active", "#B91C1C")])
        style.configure("Treeview", background=SURFACE, fieldbackground=SURFACE, foreground=TEXT,
                        rowheight=30, bordercolor=BORDER, font=(self.font_family, 9))
        style.configure("Treeview.Heading", background="#F9FAFB", foreground=TEXT, bordercolor=BORDER,
                        font=(self.font_family, 9, "bold"), padding=6)
        style.map("Treeview", background=[("selected", "#E0E7FF")], foreground=[("selected", TEXT)])
        style.configure("Horizontal.TProgressbar", background=PRIMARY, troughcolor="#E5E7EB", borderwidth=0)

    def _show_auth_screen(self, notice=""):
        if hasattr(self, "auth_screen") and self.auth_screen.winfo_exists():
            self.auth_screen.destroy()
        self.auth_screen = tk.Frame(self.root, bg=BG)
        self.auth_screen.grid(row=0, column=0, sticky="nsew")
        self.auth_screen.grid_rowconfigure(0, weight=1)
        self.auth_screen.grid_columnconfigure(0, weight=1)

        card = tk.Frame(self.auth_screen, bg=SURFACE, highlightthickness=1, highlightbackground=BORDER,
                        padx=38, pady=34)
        card.grid(row=0, column=0)
        auth_error = ""
        try:
            configured = self.auth.is_configured
        except AuthError as exc:
            configured = True
            auth_error = str(exc)
        tk.Label(card, text="XAMPP DATABASE TOOLKIT", bg=SURFACE, fg=PRIMARY,
                 font=(self.font_family, 10, "bold")).pack(anchor="w")
        tk.Label(card, text="Welcome back" if configured else "Secure this application",
                 bg=SURFACE, fg=TEXT, font=(self.font_family, 20, "bold")).pack(anchor="w", pady=(8, 4))
        description = ("Enter the local administrator password to continue."
                       if configured else
                       "Create one administrator password.")
        tk.Label(card, text=description, bg=SURFACE, fg=MUTED, justify="left", wraplength=350,
                 font=(self.font_family, 9)).pack(anchor="w", pady=(0, 22))

        self.auth_password = tk.StringVar()
        self.auth_confirm = tk.StringVar()
        tk.Label(card, text="Administrator password", bg=SURFACE, fg=MUTED,
                 font=(self.font_family, 9)).pack(anchor="w")
        password_row = tk.Frame(card, bg=SURFACE)
        password_row.pack(fill="x", pady=(6, 13))
        self.auth_password_visible = False
        password_entry = ttk.Entry(password_row, textvariable=self.auth_password, show="•")
        password_entry.pack(side="left", fill="x", expand=True)
        password_toggle = tk.Canvas(
            password_row, width=34, height=34, bg="#FAFBFC", highlightthickness=1,
            highlightbackground=BORDER, cursor="hand2",
        )
        password_toggle.pack(side="right", padx=(6, 0))
        password_toggle.bind(
            "<Button-1>",
            lambda _event: self._toggle_auth_password(password_entry, password_toggle),
        )
        self._draw_password_icon(password_toggle, visible=False)
        if not configured:
            tk.Label(card, text="Confirm password", bg=SURFACE, fg=MUTED,
                     font=(self.font_family, 9)).pack(anchor="w")
            confirm_entry = ttk.Entry(card, textvariable=self.auth_confirm, show="•")
            confirm_entry.pack(fill="x", pady=(6, 13))
            confirm_entry.bind("<Return>", lambda _event: self._submit_auth())
        password_entry.bind("<Return>", lambda _event: self._submit_auth())
        self.auth_message = tk.StringVar(value=auth_error or notice)
        tk.Label(card, textvariable=self.auth_message, bg=SURFACE,
                 fg=DANGER if notice else MUTED, justify="left", wraplength=350,
                 font=(self.font_family, 8)).pack(anchor="w", pady=(0, 10))
        self.auth_button = ttk.Button(card, text="Login" if configured else "Create password",
                                      style="Primary.TButton", command=self._submit_auth)
        self.auth_button.pack(fill="x")
        if auth_error:
            self.auth_button.configure(state="disabled")
        if not configured:
            tk.Label(card, text="Minimum 4 characters", bg=SURFACE, fg=MUTED,
                     font=(self.font_family, 8)).pack(anchor="center", pady=(12, 0))
        password_entry.focus_set()

    def _draw_password_icon(self, canvas, visible):
        canvas.delete("all")
        color = PRIMARY if visible else MUTED
        canvas.create_arc(5, 8, 29, 26, start=20, extent=140, style="arc", width=2, outline=color)
        canvas.create_arc(5, 8, 29, 26, start=200, extent=140, style="arc", width=2, outline=color)
        canvas.create_oval(14, 14, 20, 20, fill=color, outline=color)
        if visible:
            canvas.create_line(7, 27, 28, 6, fill=DANGER, width=2)

    def _toggle_auth_password(self, entry, canvas):
        self.auth_password_visible = not self.auth_password_visible
        entry.configure(show="" if self.auth_password_visible else "•")
        self._draw_password_icon(canvas, self.auth_password_visible)
        entry.focus_set()
        entry.icursor("end")

    def _submit_auth(self):
        password = self.auth_password.get()
        try:
            if not self.auth.is_configured:
                if password != self.auth_confirm.get():
                    self.auth_message.set("The passwords do not match.")
                    return
                self.auth.setup(password)
            else:
                valid, wait = self.auth.verify(password)
                if not valid:
                    self.auth_password.set("")
                    self.auth_message.set(
                        f"Too many failed attempts. Try again in {wait} seconds."
                        if wait else "Incorrect administrator password."
                    )
                    return
        except AuthError as exc:
            self.auth_message.set(str(exc))
            return
        self.auth_password.set("")
        self.auth_confirm.set("")
        self.authenticated = True
        self.last_activity = time.monotonic()
        self.auth_screen.grid_remove()
        self.app_shell.grid()
        self.show_page("Databases")
        self.root.after(150, self.refresh_databases)
        self.root.after(200, self.refresh_xampp_services)

    def logout(self, automatic=False):
        if not self.authenticated:
            return
        if self.importer:
            if automatic:
                self.last_activity = time.monotonic()
                return
            if not self._confirm(
                "Import in progress",
                "The current statement will finish and be checkpointed before logout. Continue?",
                True,
            ):
                return
            self.after_import_action = "logout"
            self.import_status.set("Finishing current statement before logout…")
            self.cancel_button.configure(state="disabled")
            self.importer.cancel()
            return
        self._finish_logout(automatic)

    def _finish_logout(self, automatic=False):
        self.authenticated = False
        self.password.set("")
        self.selected_db.set("")
        self._sync_config()
        self.connection_text.set("Not connected")
        self.connection_badge.configure(bg="#F3F4F6", fg=MUTED)
        self.app_shell.grid_remove()
        self._show_auth_screen("Session locked due to inactivity." if automatic else "You have logged out safely.")

    def _record_activity(self, _event=None):
        if self.authenticated:
            self.last_activity = time.monotonic()

    def _check_idle_logout(self):
        if self.authenticated and time.monotonic() - self.last_activity >= IDLE_LOGOUT_SECONDS:
            self.logout(automatic=True)
        self.root.after(30_000, self._check_idle_logout)

    def _build_shell(self):
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(0, weight=1)
        self.app_shell = tk.Frame(self.root, bg=BG)
        self.app_shell.grid(row=0, column=0, sticky="nsew")
        self.app_shell.grid_rowconfigure(0, weight=1)
        self.app_shell.grid_columnconfigure(1, weight=1)
        sidebar = tk.Frame(self.app_shell, bg=SIDEBAR, width=210)
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        tk.Label(sidebar, text="DB TOOLKIT", bg=SIDEBAR, fg="white", font=(self.font_family, 14, "bold"), anchor="w").pack(fill="x", padx=24, pady=(28, 4))
        tk.Label(sidebar, text="Database | Import | Sanitize", bg=SIDEBAR, fg="#9CA3AF", font=(self.font_family, 9), anchor="w").pack(fill="x", padx=24, pady=(0, 28))
        self.nav_buttons = {}
        for page in NAVIGATION_PAGES:
            button = tk.Button(sidebar, text=page, command=lambda value=page: self.show_page(value), bg=SIDEBAR,
                               fg="#D1D5DB", activebackground=SIDEBAR_HOVER, activeforeground="white", bd=0,
                               relief="flat", anchor="w", padx=24, pady=11, font=(self.font_family, 10), cursor="hand2")
            button.pack(fill="x", padx=10, pady=2)
            self.nav_buttons[page] = button
        tk.Label(sidebar, text="THIS IS NOT FOR SALE", bg=SIDEBAR, fg="#6B7280",
                 justify="left", font=(self.font_family, 8)).pack(side="bottom", anchor="w", padx=24, pady=22)

        main = tk.Frame(self.app_shell, bg=BG)
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_rowconfigure(1, weight=1)
        main.grid_columnconfigure(0, weight=1)
        header = tk.Frame(main, bg=SURFACE, height=72, highlightthickness=1, highlightbackground=BORDER)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        self.page_title = tk.Label(header, text="Databases", bg=SURFACE, fg=TEXT, font=(self.font_family, 14, "bold"))
        self.page_title.pack(side="left", padx=28)
        ttk.Button(header, text="Logout", command=self.logout).pack(side="right", padx=(8, 28), pady=17)
        self.connection_badge = tk.Label(header, textvariable=self.connection_text, bg="#F3F4F6", fg=MUTED,
                                         font=(self.font_family, 9, "bold"), padx=12, pady=6)
        self.connection_badge.pack(side="right", padx=(8, 0))
        self.database_badge = tk.Label(header, text="No database selected", bg="#EEF2FF", fg=PRIMARY,
                                       font=(self.font_family, 9, "bold"), padx=12, pady=6)
        self.database_badge.pack(side="right")
        self.page_host = tk.Frame(main, bg=BG)
        self.page_host.grid(row=1, column=0, sticky="nsew")
        self.page_host.grid_rowconfigure(0, weight=1)
        self.page_host.grid_columnconfigure(0, weight=1)
        footer = tk.Frame(main, bg=SURFACE, height=38, highlightthickness=1, highlightbackground=BORDER)
        footer.grid(row=2, column=0, sticky="ew")
        footer.grid_propagate(False)
        tk.Label(
            footer,
            text="Author: Aldrin Caballero",
            bg=SURFACE,
            fg=MUTED,
            font=(self.font_family, 8),
        ).pack(side="right", padx=28, pady=9)
        self.pages = {}
        self._build_import_page()
        self._build_databases_page()
        self._build_query_page()
        self._build_sanitize_page()
        self._build_logs_page()
        self._build_manual_page()
        self.selected_db.trace_add("write", lambda *_: self.database_badge.configure(text=self.selected_db.get() or "No database selected"))

    def _new_page(self, name):
        page = tk.Frame(self.page_host, bg=BG)
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_rowconfigure(0, weight=1)
        page.grid_columnconfigure(0, weight=1)
        canvas = tk.Canvas(page, bg=BG, bd=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        content = tk.Frame(canvas, bg=BG)
        content_window = canvas.create_window((0, 0), window=content, anchor="nw")

        def resize_content(event):
            canvas.itemconfigure(content_window, width=event.width)
            canvas.itemconfigure(content_window, height=max(event.height, content.winfo_reqheight()))
            canvas.configure(scrollregion=canvas.bbox("all"))

        def update_content(_event=None):
            target_height = max(canvas.winfo_height(), content.winfo_reqheight())
            canvas.itemconfigure(content_window, height=target_height)
            canvas.configure(scrollregion=canvas.bbox("all"))

        canvas.bind("<Configure>", resize_content)
        content.bind("<Configure>", update_content)
        self.pages[name] = page
        self.page_canvases[name] = canvas
        return content

    def _scroll_active_page(self, event):
        if not self.authenticated or isinstance(event.widget, (tk.Text, tk.Listbox, ttk.Treeview)):
            return None
        canvas = self.page_canvases.get(self.active_page)
        if not canvas or not canvas.winfo_ismapped():
            return None
        region = canvas.bbox("all")
        if not region or region[3] <= canvas.winfo_height():
            return None
        canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def _card(self, parent, title, subtitle=None, padding=22, subtitle_gap=16, spacer_height=10):
        card = ttk.Frame(parent, style="Card.TFrame", padding=padding)
        ttk.Label(card, text=title, style="Heading.TLabel").pack(anchor="w")
        if subtitle:
            ttk.Label(card, text=subtitle, style="Muted.TLabel").pack(anchor="w", pady=(3, subtitle_gap))
        else:
            ttk.Frame(card, style="Card.TFrame", height=spacer_height).pack()
        return card

    def _build_import_page(self):
        page = self._new_page("Import")
        body = tk.Frame(page, bg=BG)
        body.pack(fill="both", expand=True, padx=28, pady=18)
        ttk.Label(body, text="Import large SQL dumps", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text="Checkpointed streaming keeps memory use low and preserves confirmed progress.", foreground=MUTED).pack(anchor="w", pady=(2, 12))
        card = self._card(body, "SQL dump", "Choose a standard phpMyAdmin or mysqldump file.", padding=(16, 8), subtitle_gap=2)
        card.pack(fill="x")
        file_row = ttk.Frame(card, style="Card.TFrame")
        file_row.pack(fill="x")
        file_row.columnconfigure(0, weight=1)
        ttk.Entry(file_row, textvariable=self.sql_file).grid(row=0, column=0, sticky="ew", padx=(0, 10))
        ttk.Button(file_row, text="Browse file", command=self.browse_sql).grid(row=0, column=1)
        options = ttk.Frame(card, style="Card.TFrame")
        options.pack(fill="x", pady=(4, 0))
        ttk.Checkbutton(options, text="Continue after ordinary SQL errors", variable=self.continue_on_error).pack(side="left")
        self.import_button = ttk.Button(options, text="Start import", style="Primary.TButton", command=self.start_import)
        self.import_button.pack(side="right")
        self.cancel_button = ttk.Button(options, text="Cancel", command=self.cancel_import, state="disabled")
        self.cancel_button.pack(side="right", padx=8)
        progress = self._card(body, "Progress", padding=(16, 8), spacer_height=2)
        progress.pack(fill="x", pady=8)
        top = ttk.Frame(progress, style="Card.TFrame")
        top.pack(fill="x")
        ttk.Label(top, textvariable=self.import_status, style="Card.TLabel", font=(self.font_family, 11, "bold")).pack(side="left")
        self.progress_percent = ttk.Label(top, text="0%", style="Card.TLabel")
        self.progress_percent.pack(side="right")
        self.import_progress = ttk.Progressbar(progress, mode="determinate")
        self.import_progress.pack(fill="x", pady=(6, 3))
        self.import_detail_label = ttk.Label(
            progress, textvariable=self.import_detail, style="Muted.TLabel", wraplength=620, justify="left"
        )
        self.import_detail_label.pack(anchor="w")
        note = ttk.Frame(body, style="Card.TFrame", padding=(16, 12))
        note.pack(fill="x")
        self.safe_recovery_card = note
        ttk.Label(note, text="Safe recovery", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(
            note,
            text=("Progress advances only after MySQL confirms a complete statement. "
                  "If a connection drops at an uncertain commit point, the app pauses so you can avoid duplicate rows."),
            style="Card.TLabel",
            wraplength=850,
        ).pack(anchor="w", pady=(4, 0))

    def _build_databases_page(self):
        page = self._new_page("Databases")
        body = tk.Frame(page, bg=BG)
        body.pack(fill="both", expand=True, padx=28, pady=24)
        ttk.Label(body, text="Databases & connection", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text="Manage the server connection and choose the active database.", foreground=MUTED).pack(anchor="w", pady=(2, 18))
        conn = ttk.Frame(body, style="Card.TFrame", padding=16)
        conn.pack(fill="x")
        ttk.Label(conn, text="Connection", style="Heading.TLabel").pack(anchor="w")
        grid = ttk.Frame(conn, style="Card.TFrame")
        grid.pack(fill="x", pady=(10, 0))
        for column in (1, 3, 5, 7):
            grid.columnconfigure(column, weight=1)
        ttk.Label(grid, text="mysql.exe", style="Muted.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(grid, textvariable=self.mysql_path).grid(row=0, column=1, columnspan=6, sticky="ew", padx=(0, 8), pady=4)
        ttk.Button(grid, text="Browse", command=self.browse_mysql).grid(row=0, column=7, sticky="e", pady=4)
        compact_fields = (("Host", self.host, ""), ("Port", self.port, ""),
                          ("User", self.user, ""), ("Password", self.password, "*"))
        for index, (label, variable, mask) in enumerate(compact_fields):
            column = index * 2
            ttk.Label(grid, text=label, style="Muted.TLabel").grid(row=1, column=column, sticky="w", padx=(0, 6), pady=4)
            ttk.Entry(grid, textvariable=variable, show=mask).grid(row=1, column=column + 1, sticky="ew", padx=(0, 12), pady=4)
        actions = ttk.Frame(conn, style="Card.TFrame")
        actions.pack(fill="x", pady=(8, 0))
        ttk.Button(actions, text="Change app password", command=self.change_app_password).pack(side="left")
        ttk.Button(actions, text="Save settings", command=self.save_config).pack(side="right")
        ttk.Button(actions, text="Test connection", style="Primary.TButton", command=self.test_connection).pack(side="right", padx=8)
        services = self._card(
            body, "XAMPP services",
            "Start or stop the local services using the official scripts in the selected XAMPP folder.",
            padding=16, subtitle_gap=8,
        )
        services.pack(fill="x", pady=(14, 0))
        service_grid = tk.Frame(services, bg=SURFACE)
        service_grid.pack(fill="x")
        service_grid.columnconfigure(0, weight=1)
        service_grid.columnconfigure(1, weight=1)
        self.xampp_service_buttons = {}
        for column, (service, label, status_var) in enumerate((
            ("apache", "Apache web server", self.apache_service_status),
            ("mysql", "MySQL database", self.mysql_service_status),
        )):
            panel = tk.Frame(
                service_grid, bg="#F9FAFB", padx=14, pady=10,
                highlightthickness=1, highlightbackground=BORDER,
            )
            panel.grid(row=0, column=column, sticky="ew", padx=(0, 6) if column == 0 else (6, 0))
            heading = tk.Frame(panel, bg="#F9FAFB")
            heading.pack(fill="x")
            tk.Label(heading, text=label, bg="#F9FAFB", fg=TEXT,
                     font=(self.font_family, 10, "bold")).pack(side="left")
            tk.Label(heading, textvariable=status_var, bg="#F9FAFB", fg=MUTED,
                     font=(self.font_family, 9)).pack(side="right")
            controls = tk.Frame(panel, bg="#F9FAFB")
            controls.pack(fill="x", pady=(8, 0))
            start_button = ttk.Button(
                controls, text="Start", style="Primary.TButton",
                command=lambda name=service: self.control_xampp_service(name, "start"),
            )
            stop_button = ttk.Button(
                controls, text="Stop",
                command=lambda name=service: self.control_xampp_service(name, "stop"),
            )
            start_button.pack(side="left")
            stop_button.pack(side="left", padx=(8, 0))
            self.xampp_service_buttons[service] = {"start": start_button, "stop": stop_button}
        service_footer = tk.Frame(services, bg=SURFACE)
        service_footer.pack(fill="x", pady=(10, 0))
        tk.Label(service_footer, text="Quick links:", bg=SURFACE, fg=MUTED,
                 font=(self.font_family, 9, "bold")).pack(side="left")
        self.xampp_link_labels = {}
        for kind, variable in (("site", self.local_site_url), ("phpmyadmin", self.phpmyadmin_url)):
            link = tk.Label(
                service_footer, textvariable=variable, bg=SURFACE, fg=MUTED,
                font=(self.font_family, 9, "underline"), cursor="arrow",
            )
            link.pack(side="left", padx=(10, 0))
            link.bind("<Button-1>", lambda _event, name=kind: self._open_xampp_url(name))
            self.xampp_link_labels[kind] = link
        ttk.Button(service_footer, text="Refresh status", command=self.refresh_xampp_services).pack(side="right")
        dbcard = ttk.Frame(body, style="Card.TFrame", padding=16)
        dbcard.pack(fill="both", expand=True, pady=(14, 0))
        db_heading = ttk.Frame(dbcard, style="Card.TFrame")
        db_heading.pack(fill="x", pady=(0, 10))
        ttk.Label(db_heading, text="Available databases", style="Heading.TLabel").pack(side="left")
        ttk.Label(db_heading, textvariable=self.database_status, style="Muted.TLabel").pack(side="right")
        toolbar = ttk.Frame(dbcard, style="Card.TFrame")
        toolbar.pack(fill="x", pady=(0, 10))
        ttk.Entry(toolbar, textvariable=self.search_db, width=32).pack(side="left")
        self.search_db.trace_add("write", lambda *_: self._filter_databases())
        self.refresh_db_button = ttk.Button(toolbar, text="Refresh", command=self.refresh_databases)
        self.refresh_db_button.pack(side="left", padx=8)
        self.create_db_button = ttk.Button(toolbar, text="New database", style="Primary.TButton", command=self.create_database)
        self.create_db_button.pack(side="right")
        self.delete_db_button = ttk.Button(toolbar, text="Delete selected", style="Danger.TButton", command=self.delete_database)
        self.delete_db_button.pack(side="right", padx=8)
        self.database_busy_bar = ttk.Progressbar(dbcard, mode="indeterminate")
        list_wrap = ttk.Frame(dbcard, style="Card.TFrame")
        list_wrap.pack(fill="both", expand=True)
        self.db_list_wrap = list_wrap
        self.db_list = tk.Listbox(list_wrap, height=8, bg=SURFACE, fg=TEXT, bd=0, highlightthickness=1,
                                  highlightbackground=BORDER, selectbackground="#E0E7FF", selectforeground=TEXT,
                                  font=(self.font_family, 10), activestyle="none",
                                  yscrollcommand=lambda first, last: db_scroll.set(first, last))
        db_scroll = ttk.Scrollbar(list_wrap, orient="vertical", command=self.db_list.yview)
        self.db_list.pack(side="left", fill="both", expand=True)
        db_scroll.pack(side="right", fill="y", padx=(6, 0))
        self.db_list.bind("<<ListboxSelect>>", self._select_database)

    def _build_query_page(self):
        page = self._new_page("SQL Console")
        body = tk.Frame(page, bg=BG)
        body.pack(fill="both", expand=True, padx=28, pady=24)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(1, weight=2)
        body.grid_rowconfigure(2, weight=2)
        ttk.Label(body, text="SQL Console", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        editor = self._card(body, "Query editor", "Run selected text, or the entire editor when nothing is selected.")
        editor.grid(row=1, column=0, sticky="nsew", pady=(16, 10))
        actions = ttk.Frame(editor, style="Card.TFrame")
        actions.pack(side="bottom", fill="x", pady=(10, 0))
        ttk.Label(actions, textvariable=self.query_status, style="Muted.TLabel").pack(side="left")
        ttk.Label(actions, text="Ctrl+Enter", style="Muted.TLabel").pack(side="right", padx=(0, 10))
        self.run_query_button = ttk.Button(actions, text="Execute SQL", style="Primary.TButton", command=self.run_query)
        self.run_query_button.pack(side="right")
        ttk.Button(actions, text="Clear editor", command=self.clear_query_editor).pack(side="right", padx=(0, 8))
        editor_wrap = tk.Frame(editor, bg=SURFACE)
        editor_wrap.pack(fill="both", expand=True)
        editor_wrap.grid_rowconfigure(0, weight=1)
        editor_wrap.grid_columnconfigure(0, weight=1)
        self.query_editor = tk.Text(
            editor_wrap, height=8, wrap="none", undo=True, bg="#FBFCFE", fg=TEXT,
            insertbackground=TEXT, bd=0, highlightthickness=1, highlightbackground=BORDER,
            font=("Consolas", 10), padx=12, pady=12,
        )
        query_y_scroll = ttk.Scrollbar(editor_wrap, orient="vertical", command=self.query_editor.yview)
        query_x_scroll = ttk.Scrollbar(editor_wrap, orient="horizontal", command=self.query_editor.xview)
        self.query_editor.configure(
            yscrollcommand=query_y_scroll.set,
            xscrollcommand=query_x_scroll.set,
        )
        self.query_editor.grid(row=0, column=0, sticky="nsew")
        query_y_scroll.grid(row=0, column=1, sticky="ns")
        query_x_scroll.grid(row=1, column=0, sticky="ew")
        self.query_editor.tag_configure("sql_keyword", foreground="#4F46E5", font=("Consolas", 10, "bold"))
        self.query_editor.tag_configure("sql_string", foreground="#047857")
        self.query_editor.tag_configure("sql_comment", foreground="#6B7280", font=("Consolas", 10, "italic"))
        self.query_editor.tag_configure("sql_number", foreground="#B45309")
        self.query_editor.tag_configure("sql_identifier", foreground="#0369A1")
        self.query_editor.bind("<<Modified>>", self._on_query_editor_modified)
        self.query_editor.edit_modified(False)
        self.query_editor.bind("<Control-Return>", lambda _event: (self.run_query(), "break")[1])
        results = self._card(body, "Results")
        results.grid(row=2, column=0, sticky="nsew")
        wrap = ttk.Frame(results, style="Card.TFrame")
        wrap.pack(fill="both", expand=True)
        self.result_tree = ttk.Treeview(wrap, show="headings")
        yscroll, xscroll = ttk.Scrollbar(wrap, orient="vertical", command=self.result_tree.yview), ttk.Scrollbar(wrap, orient="horizontal", command=self.result_tree.xview)
        self.result_tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.result_tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        wrap.grid_rowconfigure(0, weight=1)
        wrap.grid_columnconfigure(0, weight=1)

    def _on_query_editor_modified(self, _event=None):
        if not self.query_editor.edit_modified():
            return
        self.query_editor.edit_modified(False)
        if self.query_highlight_job is not None:
            self.root.after_cancel(self.query_highlight_job)
        self.query_highlight_job = self.root.after(80, self._highlight_query_editor)

    def _highlight_query_editor(self):
        self.query_highlight_job = None
        text = self.query_editor.get("1.0", "end-1c")
        for tag in ("sql_keyword", "sql_string", "sql_comment", "sql_number", "sql_identifier"):
            self.query_editor.tag_remove(tag, "1.0", "end")
        tag_names = {
            "keyword": "sql_keyword",
            "string": "sql_string",
            "comment": "sql_comment",
            "number": "sql_number",
            "identifier": "sql_identifier",
        }
        for match in SQL_TOKEN_PATTERN.finditer(text):
            tag = tag_names[match.lastgroup]
            self.query_editor.tag_add(tag, f"1.0+{match.start()}c", f"1.0+{match.end()}c")

    def clear_query_editor(self):
        self.query_editor.delete("1.0", "end")
        self.query_status.set("Ready")
        self.query_editor.focus_set()

    def _build_sanitize_page(self):
        page = self._new_page("Sanitizer")
        body = tk.Frame(page, bg=BG)
        body.pack(fill="both", expand=True, padx=28, pady=24)
        ttk.Label(body, text="Data sanitizer", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text="Find multiple exact email, phone, and password columns, then replace their values.", foreground=MUTED).pack(anchor="w", pady=(2, 18))
        exact_card = self._card(
            body,
            "Sanitizer rules",
            "Enter one or more exact column names separated by commas. Partial matches are excluded.",
            padding=16,
            subtitle_gap=10,
        )
        exact_card.pack(fill="x")
        match_fields = tk.Frame(exact_card, bg=SURFACE)
        match_fields.pack(fill="x")
        for column in range(3):
            match_fields.columnconfigure(column, weight=1)
        for column, (title, variable, replacement, replacement_label, readonly) in enumerate((
            ("Email", self.email_column_name, self.email_replacement, "Replacement email", False),
            ("Phone number", self.phone_column_name, self.phone_replacement, "Replacement phone number", False),
            ("Password", self.password_column_name, self.password_replacement, "Replacement password (login: 1)", True),
        )):
            group = tk.Frame(
                match_fields, bg="#F9FAFB", padx=14, pady=12,
                highlightthickness=1, highlightbackground=BORDER,
            )
            group.grid(
                row=0, column=column, sticky="nsew",
                padx=(0 if column == 0 else 6, 0 if column == 2 else 6),
            )
            tk.Label(group, text=title, bg="#F9FAFB", fg=TEXT,
                     font=(self.font_family, 10, "bold"), anchor="w").pack(fill="x")
            tk.Label(group, text="Exact column names (comma-separated)", bg="#F9FAFB", fg=MUTED,
                     font=(self.font_family, 8), anchor="w").pack(fill="x", pady=(9, 3))
            ttk.Entry(group, textvariable=variable).pack(fill="x")
            tk.Label(group, text=replacement_label, bg="#F9FAFB", fg=MUTED,
                     font=(self.font_family, 8), anchor="w").pack(fill="x", pady=(10, 3))
            ttk.Entry(
                group, textvariable=replacement,
                state="readonly" if readonly else "normal",
            ).pack(fill="x")

        card = self._card(body, "Detected columns", "Exact full-name matches are selected automatically.")
        card.pack(fill="both", expand=True, pady=(14, 0))
        top = ttk.Frame(card, style="Card.TFrame")
        top.pack(fill="x", pady=(0, 10))
        ttk.Label(top, textvariable=self.sanitize_status, style="Muted.TLabel").pack(side="left")
        self.scan_sanitize_button = ttk.Button(top, text="Scan database", style="Primary.TButton", command=self.scan_columns)
        self.scan_sanitize_button.pack(side="right")
        self.sanitize_list = tk.Listbox(card, selectmode="extended", exportselection=False, bg=SURFACE, fg=TEXT,
                                        bd=0, highlightthickness=1, highlightbackground=BORDER, selectbackground="#E0E7FF",
                                        selectforeground=TEXT, font=(self.font_family, 9), activestyle="none")
        self.sanitize_list.pack(fill="both", expand=True)
        actions = ttk.Frame(card, style="Card.TFrame")
        actions.pack(fill="x", pady=(14, 0))
        ttk.Label(actions, text="Review the selected columns before applying permanent replacements.",
                  style="Muted.TLabel").pack(side="left")
        self.sanitize_button = ttk.Button(actions, text="Replace selected", style="Danger.TButton", command=self.start_sanitize)
        self.sanitize_button.pack(side="right")

    def _build_logs_page(self):
        page = self._new_page("Logs")
        body = tk.Frame(page, bg=BG)
        body.pack(fill="both", expand=True, padx=28, pady=24)
        top = tk.Frame(body, bg=BG)
        top.pack(fill="x")
        ttk.Label(top, text="Activity log", style="Title.TLabel").pack(side="left")
        ttk.Button(top, text="Clear", command=lambda: self._clear_log()).pack(side="right")
        card = self._card(body, "Session output")
        card.pack(fill="both", expand=True, pady=(18, 0))
        self.log_text = tk.Text(card, wrap="word", state="disabled", bg="#0F172A", fg="#D1FAE5",
                                insertbackground="white", bd=0, padx=14, pady=14, font=(self.font_family, 9))
        self.log_text.pack(fill="both", expand=True)

    def _build_manual_page(self):
        page = self._new_page("User Manual")
        body = tk.Frame(page, bg=BG)
        body.pack(fill="both", expand=True, padx=28, pady=24)
        ttk.Label(body, text="User manual", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            body,
            text="A practical guide to setting up and safely using every feature in the toolkit.",
            foreground=MUTED,
        ).pack(anchor="w", pady=(2, 18))

        sections = (
            (
                "Quick start",
                "1. Open Databases and select C:\\xampp\\mysql\\bin\\mysql.exe.\n"
                "2. Start MySQL if its status is Stopped. Start Apache if your local website needs it.\n"
                "3. Enter the host, port, MySQL user, and password, then click Test connection.\n"
                "4. Select an existing database or create a new one.\n"
                "5. Continue to Import, Sanitizer, or SQL Console.",
            ),
            (
                "Databases and XAMPP services",
                "Use Start and Stop to control Apache and MySQL. Refresh status checks whether httpd.exe and "
                "mysqld.exe are running. Save settings stores the MySQL path, host, port, and user, but never "
                "the MySQL password. Quick links open the local index when Apache is running and phpMyAdmin when "
                "both Apache and MySQL are running. If multiple XAMPP versions are installed, the service status "
                "and Start/Stop actions apply to the installation containing the selected mysql.exe. Each running "
                "installation must use its own Apache and MySQL ports.\n\n"
                "Refresh loads databases from the server. New database creates an empty UTF-8 database. "
                "Delete selected permanently removes the selected database after exact-name confirmation. "
                "MySQL cannot be stopped while an import is active.",
            ),
            (
                "Import SQL dumps",
                "Select the target database first, open Import, choose a .sql file, and click Start import. "
                "The progress bar advances only after MySQL confirms complete statements.\n\n"
                "If an import is cancelled or interrupted, its confirmed checkpoint is preserved. When the same "
                "file and database are used again, choose Resume to continue, Start fresh to restart, or Cancel. "
                "Keep the app open while a large current statement finishes during a safe cancellation.",
            ),
            (
                "Sanitize development data",
                "Configure the exact email, phone, and password column names, separated by commas when needed. "
                "Select a database, click Scan database, review the detected columns, then click Replace selected.\n\n"
                "Email and phone values are replaced only when non-empty. Password matches are reset on every row "
                "to the displayed hash whose login value is 1. Sanitizer changes are permanent, so retain the "
                "original SQL dump as your backup.",
            ),
            (
                "SQL Console",
                "Enter SQL in the syntax-highlighted editor. Select part of the SQL to run only that selection, "
                "or leave nothing selected to run the complete editor. Click Execute SQL or press Ctrl+Enter.\n\n"
                "Read-only queries run immediately. Statements that may change data or schema require confirmation. "
                "Results show up to 1,000 rows. Clear editor removes the query text and can be reversed with Ctrl+Z.",
            ),
            (
                "Logs, login, and safety",
                "Logs records connection, import, SQL, sanitizer, and service activity for the current session. "
                "Use Change app password under Databases to update the administrator password; the minimum is four "
                "characters. Five failed attempts trigger a timed lockout, and an inactive session locks after 15 minutes.\n\n"
                "Always keep a backup before deleting databases or sanitizing data. Do not close XAMPP or stop MySQL "
                "during an active import.",
            ),
            (
                "Common problems",
                "MySQL not found: Browse to C:\\xampp\\mysql\\bin\\mysql.exe.\n"
                "Connection failed: Start MySQL and verify the host, port, user, and password.\n"
                "Apache will not start: Another application may already be using port 80 or 443; check Apache logs.\n"
                "Import cannot use DELIMITER: Export ordinary schema/data SQL without stored routines or triggers.\n"
                "More details: Review the Logs page for the exact error message.",
            ),
        )
        for index, (title, content) in enumerate(sections):
            card = self._card(body, title, padding=18, spacer_height=4)
            card.pack(fill="x", pady=(0 if index == 0 else 10, 0))
            ttk.Label(
                card, text=content, style="Card.TLabel", justify="left", wraplength=840,
            ).pack(anchor="w", fill="x")

    def show_page(self, name):
        self.pages[name].tkraise()
        self.active_page = name
        self.page_title.configure(text=name)
        for page, button in self.nav_buttons.items():
            button.configure(bg=PRIMARY if page == name else SIDEBAR, fg="white" if page == name else "#D1D5DB",
                             font=(self.font_family, 10, "bold" if page == name else "normal"))

    def _mysql_config(self):
        return self._config_value

    def _sync_config(self):
        """Snapshot Tk variables on the UI thread before a worker starts."""
        self._config_value = MySQLConfig(
            self.mysql_path.get().strip(), self.host.get().strip(), self.port.get().strip(),
            self.user.get().strip(), self.password.get(),
        )

    def _thread(self, target):
        threading.Thread(target=target, daemon=True).start()

    def _ui(self, callback, *args):
        self.root.after(0, callback, *args)

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _dialog(self, title, message, buttons, danger=False):
        """Poppins-styled modal used instead of platform message boxes."""
        window = tk.Toplevel(self.root)
        window.title(title)
        window.configure(bg=SURFACE)
        window.resizable(False, False)
        window.transient(self.root)
        result = {"value": None}

        accent = DANGER if danger else PRIMARY
        tk.Frame(window, bg=accent, height=5).pack(fill="x")
        content = tk.Frame(window, bg=SURFACE, padx=26, pady=22)
        content.pack(fill="both", expand=True)
        tk.Label(content, text=title, bg=SURFACE, fg=TEXT, anchor="w",
                 font=(self.font_family, 13, "bold")).pack(fill="x")
        tk.Label(content, text=message, bg=SURFACE, fg=MUTED, justify="left", anchor="w",
                 wraplength=540, font=(self.font_family, 9)).pack(fill="x", pady=(10, 22))
        actions = tk.Frame(content, bg=SURFACE)
        actions.pack(fill="x")

        def choose(value):
            result["value"] = value
            window.destroy()

        for label, value, style in reversed(buttons):
            ttk.Button(actions, text=label, style=style, command=lambda v=value: choose(v)).pack(side="right", padx=(8, 0))
        window.protocol("WM_DELETE_WINDOW", lambda: choose(None))
        window.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - window.winfo_reqwidth()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - window.winfo_reqheight()) // 2
        window.geometry(f"+{max(0, x)}+{max(0, y)}")
        window.grab_set()
        window.focus_force()
        self.root.wait_window(window)
        return result["value"]

    def _alert(self, title, message, danger=False):
        self._dialog(title, message, [("OK", True, "Danger.TButton" if danger else "Primary.TButton")], danger)

    def _confirm(self, title, message, danger=False):
        return bool(self._dialog(title, message, [
            ("Cancel", False, "TButton"),
            ("Continue", True, "Danger.TButton" if danger else "Primary.TButton"),
        ], danger))

    def change_app_password(self):
        window = tk.Toplevel(self.root)
        window.title("Change app password")
        window.configure(bg=SURFACE)
        window.resizable(False, False)
        window.transient(self.root)
        tk.Frame(window, bg=PRIMARY, height=5).pack(fill="x")
        content = tk.Frame(window, bg=SURFACE, padx=28, pady=24)
        content.pack(fill="both", expand=True)
        tk.Label(content, text="Change administrator password", bg=SURFACE, fg=TEXT,
                 font=(self.font_family, 13, "bold")).pack(anchor="w", pady=(0, 14))
        current, new, confirm = tk.StringVar(), tk.StringVar(), tk.StringVar()
        for label, variable in (("Current password", current), ("New password", new), ("Confirm new password", confirm)):
            tk.Label(content, text=label, bg=SURFACE, fg=MUTED,
                     font=(self.font_family, 9)).pack(anchor="w")
            ttk.Entry(content, textvariable=variable, show="•", width=42).pack(fill="x", pady=(5, 11))
        message = tk.StringVar()
        tk.Label(content, textvariable=message, bg=SURFACE, fg=DANGER,
                 font=(self.font_family, 8)).pack(anchor="w")
        actions = tk.Frame(content, bg=SURFACE)
        actions.pack(fill="x", pady=(12, 0))

        def save():
            if new.get() != confirm.get():
                message.set("The new passwords do not match.")
                return
            try:
                self.auth.change_password(current.get(), new.get())
            except AuthError as exc:
                message.set(str(exc))
                return
            window.destroy()
            self._alert("Password changed", "The administrator password was updated successfully.")

        ttk.Button(actions, text="Cancel", command=window.destroy).pack(side="right")
        ttk.Button(actions, text="Update password", style="Primary.TButton", command=save).pack(side="right", padx=8)
        window.grab_set()
        window.focus_force()

    def log(self, message):
        def append():
            self.log_text.configure(state="normal")
            self.log_text.insert("end", f"[{time.strftime('%H:%M:%S')}] {message}\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self._ui(append)

    def _set_connection(self, connected, detail=None):
        self.connection_text.set(detail or ("Connected" if connected else "Not connected"))
        self.connection_badge.configure(bg="#D1FAE5" if connected else "#FEE2E2", fg=SUCCESS if connected else DANGER)

    def browse_sql(self):
        path = filedialog.askopenfilename(title="Select SQL dump", filetypes=[("SQL files", "*.sql"), ("All files", "*.*")])
        if path:
            self.sql_file.set(path)
            self.import_detail.set(f"{os.path.basename(path)} • {self._format_bytes(os.path.getsize(path))}")

    def browse_mysql(self):
        path = filedialog.askopenfilename(title="Select mysql.exe", filetypes=[("MySQL client", "mysql.exe"), ("All files", "*.*")])
        if path:
            self.mysql_path.set(path)
            self.refresh_xampp_services()

    def refresh_xampp_services(self, notify=False):
        try:
            root = xampp_root_from_mysql(self.mysql_path.get())
        except Exception as exc:
            self.xampp_service_states = {"apache": False, "mysql": False}
            self._update_xampp_quick_links()
            for service, status_var in (("apache", self.apache_service_status), ("mysql", self.mysql_service_status)):
                status_var.set("Setup required")
                for button in self.xampp_service_buttons[service].values():
                    button.configure(state="disabled")
            if notify:
                self._alert("XAMPP not found", str(exc), True)
            return

        for service, status_var in (("apache", self.apache_service_status), ("mysql", self.mysql_service_status)):
            status_var.set("Checking...")
            for button in self.xampp_service_buttons[service].values():
                button.configure(state="disabled")

        def worker():
            try:
                statuses = {service: xampp_service_running(service, root) for service in XAMPP_SERVICES}
                self._ui(self._apply_xampp_service_statuses, statuses)
            except Exception as exc:
                self._ui(self._xampp_status_failed, str(exc), notify)
        self._thread(worker)

    def _apply_xampp_service_statuses(self, statuses):
        self.xampp_service_states = dict(statuses)
        for service, running in statuses.items():
            status_var = self.apache_service_status if service == "apache" else self.mysql_service_status
            status_var.set("Running" if running else "Stopped")
            self.xampp_service_buttons[service]["start"].configure(state="disabled" if running else "normal")
            self.xampp_service_buttons[service]["stop"].configure(state="normal" if running else "disabled")
        self._update_xampp_quick_links()

    def _xampp_status_failed(self, error, notify=False):
        self.xampp_service_states = {"apache": False, "mysql": False}
        self._update_xampp_quick_links()
        self.apache_service_status.set("Status unavailable")
        self.mysql_service_status.set("Status unavailable")
        for buttons in self.xampp_service_buttons.values():
            for button in buttons.values():
                button.configure(state="disabled")
        self.log(f"XAMPP status check failed: {error}")
        if notify:
            self._alert("Status check failed", error, True)

    def _update_xampp_quick_links(self):
        try:
            root = xampp_root_from_mysql(self.mysql_path.get())
            site_url, phpmyadmin_url = xampp_web_urls(root)
        except Exception:
            site_url, phpmyadmin_url = "http://localhost/", "http://localhost/phpmyadmin/"
        self.local_site_url.set(site_url)
        self.phpmyadmin_url.set(phpmyadmin_url)
        availability = {
            "site": self.xampp_service_states.get("apache", False),
            "phpmyadmin": (
                self.xampp_service_states.get("apache", False)
                and self.xampp_service_states.get("mysql", False)
            ),
        }
        for kind, available in availability.items():
            self.xampp_link_labels[kind].configure(
                fg=PRIMARY if available else MUTED,
                cursor="hand2" if available else "arrow",
            )

    def _open_xampp_url(self, kind):
        apache_running = self.xampp_service_states.get("apache", False)
        mysql_running = self.xampp_service_states.get("mysql", False)
        if not apache_running:
            self._alert("Apache is stopped", "Start Apache before opening this local URL.")
            return
        if kind == "phpmyadmin" and not mysql_running:
            self._alert("MySQL is stopped", "Start MySQL before opening phpMyAdmin.")
            return
        url = self.phpmyadmin_url.get() if kind == "phpmyadmin" else self.local_site_url.get()
        if not webbrowser.open_new_tab(url):
            self._alert("Could not open browser", f"Open this URL manually:\n\n{url}", True)

    def control_xampp_service(self, service, action):
        label = XAMPP_SERVICES[service]["label"]
        if action == "stop" and service == "mysql" and self.importer:
            self._alert("Import in progress", "Stop the active import before stopping MySQL.", True)
            return
        if action == "stop" and not self._confirm(
            f"Stop {label}",
            f"Stop the local {label} service? Applications currently using it may be interrupted.",
            True,
        ):
            return
        try:
            root = xampp_root_from_mysql(self.mysql_path.get())
        except Exception as exc:
            self._alert("XAMPP not found", str(exc), True)
            return

        status_var = self.apache_service_status if service == "apache" else self.mysql_service_status
        status_var.set("Starting..." if action == "start" else "Stopping...")
        for button in self.xampp_service_buttons[service].values():
            button.configure(state="disabled")

        def worker():
            error = None
            desired_running = action == "start"
            try:
                if xampp_service_running(service, root) != desired_running:
                    launch_xampp_service_script(root, service, action)
                    for _ in range(15):
                        time.sleep(1)
                        if xampp_service_running(service, root) == desired_running:
                            break
                    else:
                        verb = "start" if desired_running else "stop"
                        raise RuntimeError(
                            f"{label} did not {verb} within 15 seconds. Check the XAMPP logs for details."
                        )
            except Exception as exc:
                error = str(exc)
            self._ui(self._xampp_action_finished, service, action, error)
        self._thread(worker)

    def _xampp_action_finished(self, service, action, error):
        label = XAMPP_SERVICES[service]["label"]
        if error:
            self.log(f"Could not {action} {label}: {error}")
            self._alert(f"{label} {action} failed", error, True)
        else:
            self.log(f"{label} {action} completed.")
            if service == "mysql":
                if action == "start":
                    self.root.after(250, self.refresh_databases)
                else:
                    self._set_connection(False, "MySQL stopped")
        self.refresh_xampp_services()

    @staticmethod
    def _format_bytes(value):
        number = float(value)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if number < 1024 or unit == "TB":
                return f"{number:.1f} {unit}"
            number /= 1024

    @staticmethod
    def _format_duration(value):
        seconds = max(0, int(value))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def test_connection(self):
        self._sync_config()
        self.connection_text.set("Connecting…")
        def worker():
            try:
                elapsed = self.service.ping()
                self._ui(self._set_connection, True, f"Connected • {elapsed * 1000:.0f} ms")
                self.log("Connection test succeeded.")
            except Exception as exc:
                self._ui(self._set_connection, False, "Connection failed")
                self._ui(self._alert, "Connection failed", str(exc), True)
        self._thread(worker)

    def refresh_databases(self):
        self._sync_config()
        self.database_status.set("Loading databases…")
        self.connection_text.set("Connecting…")
        def retry(attempt, delay):
            self._ui(self.connection_text.set, f"Retry {attempt}/5 in {delay}s")
        def worker():
            try:
                self.databases = self.service.databases(retry_callback=retry)
                self._ui(self._filter_databases)
                self._ui(self._set_connection, True, "Connected")
                self._ui(self.database_status.set, f"{len(self.databases)} database(s)")
                if self.databases and self.selected_db.get() not in self.databases:
                    self._ui(self.selected_db.set, self.databases[0])
                self.log(f"Loaded {len(self.databases)} database(s).")
            except Exception as exc:
                self._ui(self._set_connection, False, "Connection failed")
                self._ui(self.database_status.set, "Refresh failed")
                self.log(f"Database refresh failed: {exc}")
        self._thread(worker)

    def _set_database_busy(self, busy, message="Ready"):
        state = "disabled" if busy else "normal"
        self.refresh_db_button.configure(state=state)
        self.create_db_button.configure(state=state)
        self.delete_db_button.configure(state=state, text="Deleting…" if busy else "Delete selected")
        self.db_list.configure(state=state)
        self.database_status.set(message)
        if busy:
            self.database_busy_bar.pack(fill="x", pady=(0, 10), before=self.db_list_wrap)
            self.database_busy_bar.start(12)
        else:
            self.database_busy_bar.stop()
            self.database_busy_bar.pack_forget()

    def _filter_databases(self):
        query = self.search_db.get().strip().lower()
        self.db_list.delete(0, "end")
        for name in (item for item in self.databases if query in item.lower()):
            self.db_list.insert("end", name)
            if name == self.selected_db.get():
                self.db_list.selection_set("end")

    def _select_database(self, _event=None):
        selection = self.db_list.curselection()
        if selection:
            self.selected_db.set(self.db_list.get(selection[0]))

    def create_database(self):
        name = simpledialog.askstring("New database", "Database name:", parent=self.root)
        if not name:
            return
        if not all(char.isalnum() or char in "_$" for char in name):
            self._alert("Invalid name", "Use letters, numbers, underscore, or dollar sign only.")
            return
        self._sync_config()
        def worker():
            try:
                self.service.run(["-e", f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4;"])
                self.log(f"Created database: {name}")
                self._ui(self.selected_db.set, name)
                self._ui(self.refresh_databases)
            except Exception as exc:
                self._ui(self._alert, "Create failed", str(exc), True)
        self._thread(worker)

    def delete_database(self):
        if self.importer:
            self._alert("Import in progress", "Stop the active import before deleting a database.", True)
            return
        name = self.selected_db.get()
        if not name:
            self._alert("No database", "Select a database first.")
            return
        if name.lower() in SYSTEM_DATABASES:
            self._alert("Protected database", "MySQL system databases cannot be deleted here.", True)
            return
        typed = simpledialog.askstring("Delete database", f"Type {name} to permanently delete it:", parent=self.root)
        if typed != name:
            if typed is not None:
                self._alert("Name did not match", "The database was not deleted.")
            return
        self._sync_config()
        config = self._mysql_config()
        self._set_database_busy(True, f"Deleting {name}…")
        def worker():
            try:
                self.service.run(["-e", f"DROP DATABASE `{name}`;"])
            except Exception as exc:
                self._ui(self._delete_database_finished, name, str(exc), 0, None)
                return
            removed, checkpoint_warning = 0, None
            try:
                removed = self.checkpoints.remove_for_database(config.host, config.port, name)
            except Exception as exc:
                checkpoint_warning = str(exc)
                self.log(f"Database deleted, but its checkpoint could not be removed: {exc}")
            self.log(f"Deleted database: {name}")
            if removed:
                self.log(f"Removed {removed} stale import checkpoint(s) for {name}.")
            self._ui(self._delete_database_finished, name, None, removed, checkpoint_warning)
        self._thread(worker)

    def _delete_database_finished(self, name, error, removed_checkpoints=0, checkpoint_warning=None):
        if error:
            self._set_database_busy(False, "Delete failed")
            self._alert("Delete failed", error, True)
            return
        self.databases = [database for database in self.databases if database != name]
        self.selected_db.set(self.databases[0] if self.databases else "")
        self._filter_databases()
        self._set_database_busy(False, f"Deleted {name}")
        checkpoint_note = (
            f"\n\nRemoved {removed_checkpoints} saved import checkpoint(s). The next import will start fresh."
            if removed_checkpoints else ""
        )
        if checkpoint_warning:
            checkpoint_note += "\n\nThe saved checkpoint could not be cleared. Choose Start fresh when importing again."
        self._alert("Database deleted", f"{name} was deleted successfully.{checkpoint_note}")
        self.refresh_databases()

    def start_import(self):
        path, database = self.sql_file.get(), self.selected_db.get()
        if not database or not path or not os.path.isfile(path):
            self._alert("Missing information", "Select a database and a valid SQL file first.")
            return
        if not self._confirm("Confirm import", f"Import\n{path}\n\ninto database\n{database}?"):
            return
        self._sync_config()
        config = self._mysql_config()
        import_key = checkpoint_key(path, config.host, config.port, database)
        saved_checkpoint = self.checkpoints.get(import_key)
        if saved_checkpoint and saved_checkpoint.offset > 0:
            file_size = max(1, os.path.getsize(path))
            percent = min(100, saved_checkpoint.offset / file_size * 100)
            choice = self._dialog(
                "Existing import checkpoint",
                (f"A saved checkpoint exists at {percent:.1f}% ({self._format_bytes(saved_checkpoint.offset)}).\n\n"
                 "Resume only if this database still contains the previously imported tables and rows. "
                 "Choose Start fresh after deleting or recreating the database."),
                [
                    ("Cancel", "cancel", "TButton"),
                    ("Start fresh", "fresh", "Danger.TButton"),
                    ("Resume", "resume", "Primary.TButton"),
                ],
                True,
            )
            if not choice or choice == "cancel":
                return
            if choice == "fresh":
                self.checkpoints.remove(import_key)
                self.log(f"Discarded the saved checkpoint for {database}; starting from byte 0.")
        continue_on_error = self.continue_on_error.get()
        self.import_cancel.clear()
        self.import_started = time.monotonic()
        self.import_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.import_status.set("Preparing import…")
        self.import_progress["value"] = 0
        last_progress_update = [0.0]
        confirmed_at_start = [0]

        def progress(done, total, number, status):
            now = time.monotonic()
            # Keep statement-level confirmation while avoiding thousands of
            # unnecessary Tk redraws for dumps made of small INSERTs.
            if total and status == "Importing" and now - last_progress_update[0] < 0.1:
                return
            last_progress_update[0] = now
            if total and number == 0:
                confirmed_at_start[0] = done

            def update():
                if total:
                    pct = min(100, done / total * 100)
                    self.import_progress["value"] = pct
                    self.progress_percent.configure(text=f"{pct:.1f}%")
                    elapsed = max(0.01, time.monotonic() - self.import_started)
                    transferred = max(0, done - confirmed_at_start[0])
                    rate = transferred / elapsed
                    eta = (total - done) / rate if rate > 0 else None
                    eta_text = self._format_duration(eta) if eta is not None else "calculating"
                    self.import_detail.set(
                        f"Confirmed {self._format_bytes(done)} of {self._format_bytes(total)} • "
                        f"{self._format_bytes(rate)}/s • elapsed {self._format_duration(elapsed)} • "
                        f"ETA {eta_text} • statement {number:,}"
                    )
                self.import_status.set(status)
            self._ui(update)

        def ambiguous(preview, number, reason):
            answer, ready = {"value": "stop"}, threading.Event()
            def ask():
                if reason == "duplicate_on_resume":
                    result = self._dialog(
                        "Duplicate found after resume",
                        ("The first resumed statement reported duplicate key error 1062. This usually "
                         "means MySQL committed it before the previous app session closed, but its "
                         "checkpoint was not saved.\n\nChoose Assume executed only if this is the same, "
                         "partially imported database. If the database was recreated or changed, stop "
                         "and start a fresh import.\n\n" + preview[:700]),
                        [("Stop and preserve", "stop", "TButton"),
                         ("Assume executed and continue", "skip", "Primary.TButton")],
                        True,
                    )
                else:
                    result = self._dialog(
                        "Uncertain statement status",
                        f"Connection was lost during statement {number}. MySQL may have committed it.\n\n{preview[:700]}",
                        [("Stop and preserve", "stop", "TButton"),
                         ("Assume executed", "skip", "TButton"),
                         ("Retry statement", "retry", "Danger.TButton")],
                        True,
                    )
                answer["value"] = result or "stop"
                ready.set()
            self._ui(ask)
            ready.wait()
            return answer["value"]

        self.importer = ResumableImporter(self.service, self.checkpoints, progress, self.log, ambiguous, self.import_cancel)
        def worker():
            self._ui(self._import_finished, self.importer.run(path, database, continue_on_error))
        self._thread(worker)

    def cancel_import(self):
        if self.importer:
            self.import_status.set("Finishing current statement safely…")
            self.cancel_button.configure(state="disabled")
            self.importer.cancel()

    def _import_finished(self, result):
        pending_action = self.after_import_action
        self.after_import_action = None
        self.import_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self.importer = None
        if pending_action == "close":
            self.save_config(notify=False)
            self.root.destroy()
            return
        if pending_action == "logout":
            self._finish_logout()
            return
        if result.success:
            self.import_progress["value"] = 100
            self.progress_percent.configure(text="100%")
            self.import_status.set("Import complete")
            self.import_detail.set(f"Completed in {self._format_duration(time.monotonic() - self.import_started)}.")
            self.log(f"Import completed with {result.errors} SQL error(s).")
            self._alert("Import complete", f"Finished with {result.errors} SQL error(s). See Logs." if result.errors else "The SQL dump was imported successfully.")
        elif result.cancelled:
            self.import_status.set("Import stopped")
            self.import_detail.set("Confirmed progress was preserved and can be resumed.")
            self.log(result.message)
        else:
            self.import_status.set("Import failed")
            self.import_detail.set("The last confirmed checkpoint was preserved.")
            self.log(f"Import failed: {result.message}")
            self._alert("Import failed", result.message, True)

    def run_query(self):
        database = self.selected_db.get()
        try:
            text = self.query_editor.get("sel.first", "sel.last")
        except tk.TclError:
            text = self.query_editor.get("1.0", "end-1c")
        if not database or not text.strip():
            self._alert("Missing information", "Select a database and enter a SQL query.")
            return
        try:
            statements = split_sql_text(text)
        except Exception as exc:
            self._alert("Invalid SQL", str(exc), True)
            return
        destructive = [sql for sql in statements if not is_read_only_sql(sql)]
        if destructive and not self._confirm("Confirm database changes",
            f"{len(destructive)} statement(s) may change data or schema in {database}. Continue?", True):
            return
        self._sync_config()
        self.run_query_button.configure(state="disabled")
        self.query_status.set(f"Running {len(statements)} statement(s)…")
        def worker():
            last, summaries = None, []
            try:
                for index, statement in enumerate(statements, 1):
                    result = self.service.execute_query(statement, database, is_read_only_sql(statement))
                    last = result
                    affected = f", {result.affected_rows} affected" if result.affected_rows is not None and result.affected_rows >= 0 else ""
                    summaries.append(f"#{index} in {result.elapsed:.2f}s{affected}")
                self.log("SQL Console: " + "; ".join(summaries))
                self._ui(self._query_finished, last, len(statements), None)
            except Exception as exc:
                self.log(f"SQL Console error: {exc}")
                self._ui(self._query_finished, last, len(statements), str(exc))
        self._thread(worker)

    def _query_finished(self, result, count, error):
        self.run_query_button.configure(state="normal")
        if error:
            self.query_status.set("Query failed — see Logs")
            self._alert("Query failed", error, True)
            return
        self._show_result(result)
        suffix = " • showing first 1,000 rows" if result.truncated else ""
        affected = f" • {result.affected_rows} row(s) affected" if result.affected_rows is not None and result.affected_rows >= 0 else ""
        self.query_status.set(f"{count} statement(s) completed in {result.elapsed:.2f}s{affected}{suffix}")

    def _show_result(self, result):
        self.result_tree.delete(*self.result_tree.get_children())
        columns = result.columns or ["status"]
        self.result_tree["columns"] = columns
        for column in columns:
            self.result_tree.heading(column, text=column, command=lambda c=column: self._sort_tree(c, False))
            self.result_tree.column(column, width=150, minwidth=80, stretch=True)
        for row in result.rows or [["Query completed; no result rows."]]:
            self.result_tree.insert("", "end", values=row)

    def _sort_tree(self, column, reverse):
        values = [(self.result_tree.set(item, column), item) for item in self.result_tree.get_children("")]
        values.sort(reverse=reverse, key=lambda pair: pair[0].lower())
        for index, (_, item) in enumerate(values):
            self.result_tree.move(item, "", index)
        self.result_tree.heading(column, command=lambda: self._sort_tree(column, not reverse))

    def scan_columns(self):
        database = self.selected_db.get()
        if not database:
            self._alert("No database", "Select a database first.")
            return
        groups = [
            ("email", parse_column_names(self.email_column_name.get())),
            ("phone", parse_column_names(self.phone_column_name.get())),
            ("password", parse_column_names(self.password_column_name.get())),
        ]
        if any(not names for _, names in groups):
            self._alert(
                "Missing column name",
                "Enter at least one exact column name for email, phone number, and password.",
            )
            return
        kind_by_name = {}
        for kind, names in groups:
            for name in names:
                normalized = name.casefold()
                existing_kind = kind_by_name.get(normalized)
                if existing_kind and existing_kind != kind:
                    self._alert(
                        "Duplicate column name",
                        f'Column name "{name}" is assigned to both {existing_kind} and {kind}.',
                    )
                    return
                kind_by_name[normalized] = kind
        self._sync_config()
        self.sanitize_status.set("Scanning…")
        self.scan_sanitize_button.configure(state="disabled", text="Scanning…")
        sql_names = ", ".join(
            f"'{name.replace(chr(39), chr(39) * 2)}'" for name in kind_by_name
        )
        query = ("SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.columns "
                 f"WHERE TABLE_SCHEMA='{database.replace(chr(39), chr(39) * 2)}' "
                 f"AND LOWER(COLUMN_NAME) IN ({sql_names}) ORDER BY TABLE_NAME, COLUMN_NAME;")
        def worker():
            try:
                result = self.service.run(["-N", "-B", "-e", query], timeout=45, safe_retry=True)
                found = []
                for line in result.stdout.splitlines():
                    parts = line.split("\t")
                    if len(parts) == 2:
                        kind = kind_by_name.get(parts[1].casefold())
                        if kind:
                            found.append((parts[0], parts[1], kind))
                self._ui(self._show_sanitize_columns, found)
            except Exception as exc:
                self._ui(self.sanitize_status.set, "Scan failed — see Logs")
                self._ui(self._sanitize_scan_failed, str(exc))
                self.log(f"Sanitizer scan failed: {exc}")
        self._thread(worker)

    def _show_sanitize_columns(self, found):
        self.scan_sanitize_button.configure(state="normal", text="Scan database")
        self.sanitize_columns = found
        self.sanitize_list.delete(0, "end")
        for table, column, kind in found:
            self.sanitize_list.insert("end", f"{table}.{column}   [{kind}]")
        if found:
            self.sanitize_list.selection_set(0, "end")
        self.sanitize_status.set(f"Found {len(found)} matching column(s).")

    def _sanitize_scan_failed(self, error):
        self.scan_sanitize_button.configure(state="normal", text="Scan database")
        self.sanitize_status.set("Scan failed — see Logs")
        self._alert("Sanitizer scan failed", error, True)

    def start_sanitize(self):
        selected = [self.sanitize_columns[index] for index in self.sanitize_list.curselection()]
        database = self.selected_db.get()
        if not selected or not database:
            self._alert("Nothing selected", "Scan and select at least one column first.")
            return
        password_count = sum(kind == "password" for _, _, kind in selected)
        password_note = (
            f"\n\n{password_count} password column(s) will be set for every row to:\n"
            f"{DEFAULT_PASSWORD_HASH}\nDefault login password: 1"
            if password_count else ""
        )
        if not self._confirm(
            "Confirm permanent replacement",
            f"Permanently replace values in {len(selected)} column(s) in {database}?{password_note}",
            True,
        ):
            return
        self._sync_config()
        replacement_email = self.email_replacement.get()
        replacement_phone = self.phone_replacement.get()
        replacement_password = DEFAULT_PASSWORD_HASH
        self.sanitize_button.configure(state="disabled")
        self.sanitize_status.set("Replacing values…")
        def worker():
            errors, completed = [], 0
            for index, (table, column, kind) in enumerate(selected, 1):
                values = {"email": replacement_email, "phone": replacement_phone, "password": replacement_password}
                query = build_sanitize_query(table, column, kind, values[kind])
                try:
                    self.service.run(["-e", query], database=database, timeout=300)
                    completed += 1
                    self.log(f"Sanitized {table}.{column} ({index}/{len(selected)}).")
                except Exception as exc:
                    errors.append(f"{table}.{column}: {exc}")
                    self.log(f"Sanitizer stopped at {table}.{column}: {exc}")
                    break
                self._ui(self.sanitize_status.set, f"Completed {index} of {len(selected)} columns…")
            self._ui(self._sanitize_finished, errors, completed)
        self._thread(worker)

    def _sanitize_finished(self, errors, completed):
        self.sanitize_button.configure(state="normal")
        if errors:
            self.sanitize_status.set(f"Stopped after {completed} confirmed column(s). See Logs.")
            self._alert("Sanitizer stopped", errors[0], True)
        else:
            self.sanitize_status.set(f"Sanitized {completed} column(s).")
            self._alert("Sanitizer complete", "All selected columns were updated.")

    def _load_config(self):
        config = configparser.ConfigParser()
        if os.path.isfile(CONFIG_FILE):
            config.read(CONFIG_FILE)
            if "settings" in config:
                values = config["settings"]
                self.mysql_path.set(values.get("mysql_path", ""))
                self.host.set(values.get("host", "127.0.0.1"))
                self.port.set(values.get("port", "3306"))
                self.user.set(values.get("user", "root"))

    def save_config(self, notify=True):
        config = configparser.ConfigParser()
        config["settings"] = {"mysql_path": self.mysql_path.get(), "host": self.host.get(), "port": self.port.get(), "user": self.user.get()}
        with open(CONFIG_FILE, "w", encoding="utf-8") as handle:
            config.write(handle)
        if notify:
            self._alert("Settings saved", "Connection settings were saved. The password was not stored.")

    def on_close(self):
        if self.importer:
            if not self._confirm(
                "Import in progress",
                "The current statement will finish and be checkpointed before the app closes. Continue?",
                True,
            ):
                return
            self.after_import_action = "close"
            self.import_status.set("Finishing current statement before closing…")
            self.cancel_button.configure(state="disabled")
            self.importer.cancel()
            self.save_config(notify=False)
            return
        self.save_config(notify=False)
        self.root.destroy()


def run_app():
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "AldrinCaballero.XAMPPDatabaseToolkit"
            )
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    ModernApp(root)
    root.mainloop()
