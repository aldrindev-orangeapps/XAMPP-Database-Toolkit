"""Modern Tkinter user interface for XAMPP SQL Importer."""

from __future__ import annotations

import configparser
import ctypes
import os
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, simpledialog, ttk

from checkpoints import CheckpointStore
from importer import ResumableImporter
from local_auth import AuthError, LocalAuth
from mysql_service import MySQLConfig, MySQLService, SYSTEM_DATABASES
from sql_parser import is_read_only_sql, split_sql_text


CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".xampp_sql_importer.ini")
DEFAULT_MYSQL_PATHS = [r"C:\xampp\mysql\bin\mysql.exe", r"D:\xampp\mysql\bin\mysql.exe"]
EMAIL_KEYWORDS = ["email"]
PHONE_KEYWORDS = [
    "phone", "mobile", "phone_number", "contact_number", "contactnumber", "contact_no",
    "cellphone", "cell_number", "cellnumber", "telephone", "mobile_number", "mobilenumber",
    "tel_no", "telno",
]

BG, SURFACE, SIDEBAR, SIDEBAR_HOVER = "#F5F7FB", "#FFFFFF", "#111827", "#1F2937"
PRIMARY, PRIMARY_DARK = "#4F46E5", "#4338CA"
TEXT, MUTED, BORDER = "#111827", "#6B7280", "#E5E7EB"
SUCCESS, DANGER = "#059669", "#DC2626"
IDLE_LOGOUT_SECONDS = 15 * 60


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
        root.title("XAMPP SQL Importer")
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
        self.import_status, self.import_detail = tk.StringVar(value="Ready to import"), tk.StringVar(value="Select a SQL dump to begin.")
        self.email_replacement, self.phone_replacement = tk.StringVar(value="test@example.com"), tk.StringVar(value="09170000000")
        self.query_status = tk.StringVar(value="Ready")
        self.sanitize_status = tk.StringVar(value="Scan a database to find personal data columns.")
        self.databases, self.sanitize_columns = [], []
        self.importer = None
        self.import_cancel = threading.Event()
        self.import_started = 0.0
        self.auth = LocalAuth()
        self.authenticated = False
        self.last_activity = time.monotonic()

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
        tk.Label(card, text="SQL IMPORTER", bg=SURFACE, fg=PRIMARY,
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
            tk.Label(card, text="Minimum 8 characters", bg=SURFACE, fg=MUTED,
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
        self.show_page("Import")
        self.root.after(150, self.refresh_databases)

    def logout(self, automatic=False):
        if not self.authenticated:
            return
        if self.importer:
            if automatic:
                self.last_activity = time.monotonic()
                return
            if not self._confirm(
                "Import in progress",
                "Logging out will stop the import at its last confirmed checkpoint. Continue?",
                True,
            ):
                return
            self.importer.cancel()
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
        tk.Label(sidebar, text="SQL IMPORTER", bg=SIDEBAR, fg="white", font=(self.font_family, 14, "bold"), anchor="w").pack(fill="x", padx=24, pady=(28, 4))
        tk.Label(sidebar, text="XAMPP database tools", bg=SIDEBAR, fg="#9CA3AF", font=(self.font_family, 9), anchor="w").pack(fill="x", padx=24, pady=(0, 28))
        self.nav_buttons = {}
        for page in ("Import", "Databases", "SQL Console", "Sanitizer", "Logs"):
            button = tk.Button(sidebar, text=page, command=lambda value=page: self.show_page(value), bg=SIDEBAR,
                               fg="#D1D5DB", activebackground=SIDEBAR_HOVER, activeforeground="white", bd=0,
                               relief="flat", anchor="w", padx=24, pady=11, font=(self.font_family, 10), cursor="hand2")
            button.pack(fill="x", padx=10, pady=2)
            self.nav_buttons[page] = button
        tk.Label(sidebar, text="Settings are saved without\nyour password.", bg=SIDEBAR, fg="#6B7280",
                 justify="left", font=(self.font_family, 8)).pack(side="bottom", anchor="w", padx=24, pady=22)

        main = tk.Frame(self.app_shell, bg=BG)
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_rowconfigure(1, weight=1)
        main.grid_columnconfigure(0, weight=1)
        header = tk.Frame(main, bg=SURFACE, height=72, highlightthickness=1, highlightbackground=BORDER)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        self.page_title = tk.Label(header, text="Import", bg=SURFACE, fg=TEXT, font=(self.font_family, 14, "bold"))
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
        self.selected_db.trace_add("write", lambda *_: self.database_badge.configure(text=self.selected_db.get() or "No database selected"))

    def _new_page(self, name):
        page = tk.Frame(self.page_host, bg=BG)
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        self.pages[name] = page
        return page

    def _card(self, parent, title, subtitle=None):
        card = ttk.Frame(parent, style="Card.TFrame", padding=22)
        ttk.Label(card, text=title, style="Heading.TLabel").pack(anchor="w")
        if subtitle:
            ttk.Label(card, text=subtitle, style="Muted.TLabel").pack(anchor="w", pady=(3, 16))
        else:
            ttk.Frame(card, style="Card.TFrame", height=10).pack()
        return card

    def _build_import_page(self):
        page = self._new_page("Import")
        body = tk.Frame(page, bg=BG)
        body.pack(fill="both", expand=True, padx=28, pady=24)
        ttk.Label(body, text="Import large SQL dumps", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text="Checkpointed streaming keeps memory use low and preserves confirmed progress.", foreground=MUTED).pack(anchor="w", pady=(2, 18))
        card = self._card(body, "SQL dump", "Choose a standard phpMyAdmin or mysqldump file.")
        card.pack(fill="x")
        file_row = ttk.Frame(card, style="Card.TFrame")
        file_row.pack(fill="x")
        file_row.columnconfigure(0, weight=1)
        ttk.Entry(file_row, textvariable=self.sql_file).grid(row=0, column=0, sticky="ew", padx=(0, 10))
        ttk.Button(file_row, text="Browse file", command=self.browse_sql).grid(row=0, column=1)
        options = ttk.Frame(card, style="Card.TFrame")
        options.pack(fill="x", pady=(14, 0))
        ttk.Checkbutton(options, text="Continue after ordinary SQL errors", variable=self.continue_on_error).pack(side="left")
        self.import_button = ttk.Button(options, text="Start import", style="Primary.TButton", command=self.start_import)
        self.import_button.pack(side="right")
        self.cancel_button = ttk.Button(options, text="Cancel", command=self.cancel_import, state="disabled")
        self.cancel_button.pack(side="right", padx=8)
        progress = self._card(body, "Progress")
        progress.pack(fill="x", pady=18)
        top = ttk.Frame(progress, style="Card.TFrame")
        top.pack(fill="x")
        ttk.Label(top, textvariable=self.import_status, style="Card.TLabel", font=(self.font_family, 11, "bold")).pack(side="left")
        self.progress_percent = ttk.Label(top, text="0%", style="Card.TLabel")
        self.progress_percent.pack(side="right")
        self.import_progress = ttk.Progressbar(progress, mode="determinate")
        self.import_progress.pack(fill="x", pady=(14, 9))
        ttk.Label(progress, textvariable=self.import_detail, style="Muted.TLabel").pack(anchor="w")
        note = self._card(body, "Safe recovery", "Progress advances only after MySQL confirms a complete statement.")
        note.pack(fill="x")
        ttk.Label(note, text="If a connection drops at an uncertain commit point, the app pauses so you can avoid duplicate rows.",
                  style="Card.TLabel", wraplength=750).pack(anchor="w")

    def _build_databases_page(self):
        page = self._new_page("Databases")
        body = tk.Frame(page, bg=BG)
        body.pack(fill="both", expand=True, padx=28, pady=24)
        ttk.Label(body, text="Databases & connection", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text="Manage the server connection and choose the active database.", foreground=MUTED).pack(anchor="w", pady=(2, 18))
        conn = self._card(body, "Connection")
        conn.pack(fill="x")
        grid = ttk.Frame(conn, style="Card.TFrame")
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)
        fields = [("mysql.exe", self.mysql_path), ("Host", self.host), ("Port", self.port), ("User", self.user), ("Password", self.password)]
        for index, (label, variable) in enumerate(fields):
            row, pair = divmod(index, 2)
            ttk.Label(grid, text=label, style="Muted.TLabel").grid(row=row, column=pair * 2, sticky="w", padx=(0, 8), pady=6)
            ttk.Entry(grid, textvariable=variable, show="*" if label == "Password" else "").grid(row=row, column=pair * 2 + 1, sticky="ew", padx=(0, 18), pady=6)
        ttk.Button(grid, text="Browse", command=self.browse_mysql).grid(row=0, column=4, padx=(0, 8))
        actions = ttk.Frame(conn, style="Card.TFrame")
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="Change app password", command=self.change_app_password).pack(side="left")
        ttk.Button(actions, text="Save settings", command=self.save_config).pack(side="right")
        ttk.Button(actions, text="Test connection", style="Primary.TButton", command=self.test_connection).pack(side="right", padx=8)
        dbcard = self._card(body, "Available databases")
        dbcard.pack(fill="both", expand=True, pady=(18, 0))
        toolbar = ttk.Frame(dbcard, style="Card.TFrame")
        toolbar.pack(fill="x", pady=(0, 10))
        ttk.Entry(toolbar, textvariable=self.search_db, width=32).pack(side="left")
        self.search_db.trace_add("write", lambda *_: self._filter_databases())
        ttk.Button(toolbar, text="Refresh", command=self.refresh_databases).pack(side="left", padx=8)
        ttk.Button(toolbar, text="New database", style="Primary.TButton", command=self.create_database).pack(side="right")
        ttk.Button(toolbar, text="Delete selected", style="Danger.TButton", command=self.delete_database).pack(side="right", padx=8)
        self.db_list = tk.Listbox(dbcard, bg=SURFACE, fg=TEXT, bd=0, highlightthickness=1, highlightbackground=BORDER,
                                  selectbackground="#E0E7FF", selectforeground=TEXT, font=(self.font_family, 10), activestyle="none")
        self.db_list.pack(fill="both", expand=True)
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
        self.query_editor = tk.Text(editor, height=8, wrap="none", undo=True, bg="#FBFCFE", fg=TEXT,
                                    insertbackground=TEXT, bd=0, highlightthickness=1, highlightbackground=BORDER,
                                    font=(self.font_family, 10), padx=12, pady=12)
        self.query_editor.pack(fill="both", expand=True)
        actions = ttk.Frame(editor, style="Card.TFrame")
        actions.pack(fill="x", pady=(10, 0))
        ttk.Label(actions, textvariable=self.query_status, style="Muted.TLabel").pack(side="left")
        self.run_query_button = ttk.Button(actions, text="Run query", style="Primary.TButton", command=self.run_query)
        self.run_query_button.pack(side="right")
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

    def _build_sanitize_page(self):
        page = self._new_page("Sanitizer")
        body = tk.Frame(page, bg=BG)
        body.pack(fill="both", expand=True, padx=28, pady=24)
        ttk.Label(body, text="Data sanitizer", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text="Find and replace email and phone values in the selected database.", foreground=MUTED).pack(anchor="w", pady=(2, 18))
        card = self._card(body, "Detected columns", "Select the columns you want to overwrite.")
        card.pack(fill="both", expand=True)
        top = ttk.Frame(card, style="Card.TFrame")
        top.pack(fill="x", pady=(0, 10))
        ttk.Label(top, textvariable=self.sanitize_status, style="Muted.TLabel").pack(side="left")
        ttk.Button(top, text="Scan database", style="Primary.TButton", command=self.scan_columns).pack(side="right")
        self.sanitize_list = tk.Listbox(card, selectmode="extended", exportselection=False, bg=SURFACE, fg=TEXT,
                                        bd=0, highlightthickness=1, highlightbackground=BORDER, selectbackground="#E0E7FF",
                                        selectforeground=TEXT, font=(self.font_family, 9), activestyle="none")
        self.sanitize_list.pack(fill="both", expand=True)
        fields = ttk.Frame(card, style="Card.TFrame")
        fields.pack(fill="x", pady=(14, 0))
        fields.columnconfigure(1, weight=1)
        fields.columnconfigure(3, weight=1)
        ttk.Label(fields, text="Replacement email", style="Muted.TLabel").grid(row=0, column=0, padx=(0, 8))
        ttk.Entry(fields, textvariable=self.email_replacement).grid(row=0, column=1, sticky="ew", padx=(0, 18))
        ttk.Label(fields, text="Replacement phone", style="Muted.TLabel").grid(row=0, column=2, padx=(0, 8))
        ttk.Entry(fields, textvariable=self.phone_replacement).grid(row=0, column=3, sticky="ew")
        self.sanitize_button = ttk.Button(fields, text="Replace selected", style="Danger.TButton", command=self.start_sanitize)
        self.sanitize_button.grid(row=0, column=4, padx=(14, 0))

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

    def show_page(self, name):
        self.pages[name].tkraise()
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

    @staticmethod
    def _format_bytes(value):
        number = float(value)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if number < 1024 or unit == "TB":
                return f"{number:.1f} {unit}"
            number /= 1024

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
        self.connection_text.set("Connecting…")
        def retry(attempt, delay):
            self._ui(self.connection_text.set, f"Retry {attempt}/5 in {delay}s")
        def worker():
            try:
                self.databases = self.service.databases(retry_callback=retry)
                self._ui(self._filter_databases)
                self._ui(self._set_connection, True, "Connected")
                if self.databases and self.selected_db.get() not in self.databases:
                    self._ui(self.selected_db.set, self.databases[0])
                self.log(f"Loaded {len(self.databases)} database(s).")
            except Exception as exc:
                self._ui(self._set_connection, False, "Connection failed")
                self.log(f"Database refresh failed: {exc}")
        self._thread(worker)

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
        def worker():
            try:
                self.service.run(["-e", f"DROP DATABASE `{name}`;"])
                self.log(f"Deleted database: {name}")
                self._ui(self.selected_db.set, "")
                self._ui(self.refresh_databases)
            except Exception as exc:
                self._ui(self._alert, "Delete failed", str(exc), True)
        self._thread(worker)

    def start_import(self):
        path, database = self.sql_file.get(), self.selected_db.get()
        if not database or not path or not os.path.isfile(path):
            self._alert("Missing information", "Select a database and a valid SQL file first.")
            return
        if not self._confirm("Confirm import", f"Import\n{path}\n\ninto database\n{database}?"):
            return
        self._sync_config()
        continue_on_error = self.continue_on_error.get()
        self.import_cancel.clear()
        self.import_started = time.monotonic()
        self.import_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.import_status.set("Preparing import…")
        self.import_progress["value"] = 0

        def progress(done, total, number, status):
            def update():
                if total:
                    pct = min(100, done / total * 100)
                    self.import_progress["value"] = pct
                    self.progress_percent.configure(text=f"{pct:.1f}%")
                    rate = done / max(0.01, time.monotonic() - self.import_started)
                    self.import_detail.set(f"Confirmed {self._format_bytes(done)} of {self._format_bytes(total)} • {self._format_bytes(rate)}/s • statement {number:,}")
                self.import_status.set(status)
            self._ui(update)

        def ambiguous(preview, number):
            answer, ready = {"value": "stop"}, threading.Event()
            def ask():
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
            self.import_status.set("Stopping safely…")
            self.importer.cancel()

    def _import_finished(self, result):
        self.import_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self.importer = None
        if result.success:
            self.import_progress["value"] = 100
            self.progress_percent.configure(text="100%")
            self.import_status.set("Import complete")
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
        self._sync_config()
        self.sanitize_status.set("Scanning…")
        clauses = " OR ".join(f"LOWER(COLUMN_NAME) LIKE '%{word}%'" for word in EMAIL_KEYWORDS + PHONE_KEYWORDS)
        query = ("SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.columns "
                 f"WHERE TABLE_SCHEMA='{database.replace(chr(39), chr(39) * 2)}' AND ({clauses}) ORDER BY TABLE_NAME, COLUMN_NAME;")
        def worker():
            try:
                result = self.service.run(["-N", "-B", "-e", query], timeout=45, safe_retry=True)
                found = []
                for line in result.stdout.splitlines():
                    parts = line.split("\t")
                    if len(parts) == 2:
                        kind = "email" if any(word in parts[1].lower() for word in EMAIL_KEYWORDS) else "phone"
                        found.append((parts[0], parts[1], kind))
                self._ui(self._show_sanitize_columns, found)
            except Exception as exc:
                self._ui(self.sanitize_status.set, "Scan failed — see Logs")
                self.log(f"Sanitizer scan failed: {exc}")
        self._thread(worker)

    def _show_sanitize_columns(self, found):
        self.sanitize_columns = found
        self.sanitize_list.delete(0, "end")
        for table, column, kind in found:
            self.sanitize_list.insert("end", f"{table}.{column}   [{kind}]")
        if found:
            self.sanitize_list.selection_set(0, "end")
        self.sanitize_status.set(f"Found {len(found)} matching column(s).")

    def start_sanitize(self):
        selected = [self.sanitize_columns[index] for index in self.sanitize_list.curselection()]
        database = self.selected_db.get()
        if not selected or not database:
            self._alert("Nothing selected", "Scan and select at least one column first.")
            return
        if not self._confirm("Confirm permanent replacement", f"Permanently replace values in {len(selected)} column(s) in {database}?", True):
            return
        self._sync_config()
        replacement_email = self.email_replacement.get()
        replacement_phone = self.phone_replacement.get()
        self.sanitize_button.configure(state="disabled")
        self.sanitize_status.set("Replacing values…")
        def esc(value): return value.replace("\\", "\\\\").replace("'", "\\'")
        def worker():
            errors, completed = [], 0
            for index, (table, column, kind) in enumerate(selected, 1):
                value = replacement_email if kind == "email" else replacement_phone
                table_q, column_q = table.replace("`", "``"), column.replace("`", "``")
                query = f"UPDATE `{table_q}` SET `{column_q}`='{esc(value)}' WHERE `{column_q}` IS NOT NULL AND `{column_q}`<>'';"
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
        if self.importer and not self._confirm("Import in progress", "Stop the import and close the app?", True):
            return
        if self.importer:
            self.importer.cancel()
        self.save_config(notify=False)
        self.root.destroy()


def run_app():
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "AldrinCaballero.XAMMPSQLImporter"
            )
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    ModernApp(root)
    root.mainloop()
