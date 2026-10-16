#!/usr/bin/env python3
"""Desktop window for organize.py — pick source and destination folders,
scan (in chunks, with a Stop button) to preview every file's properties in a
sortable/searchable table, check/uncheck exactly which files to include,
then organize. Built to stay responsive on very large libraries.

Run with: python gui.py
"""

import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import organize

# ---------- palette (light, card-based) ----------

PAGE_BG = "#eef1f6"
CARD_BG = "#ffffff"
CARD_BORDER = "#e2e5ec"
ROW_ALT_BG = "#f6f7fb"
TEXT = "#1f2430"
MUTED = "#6b7280"
ACCENT = "#4f6df5"
ACCENT_ACTIVE = "#3d59d6"
ACCENT_DISABLED = "#c3c9ee"
DANGER = "#e5484d"
DANGER_ACTIVE = "#c93a3f"
DANGER_DISABLED = "#f0c4c6"
CONSOLE_BG = "#1e1f2b"
CONSOLE_FG = "#d7dae0"

# The table only draws this many rows (a table with 100,000+ rows is slow to
# build and scroll). Every scanned file is still tracked, searchable, sortable
# and included in Organize when checked.
DISPLAY_LIMIT = 5000

GROUP_LABELS = {
    "Year": "year",
    "File size": "size",
    "Camera model": "camera",
    "No grouping": "none",
}

KEEP_LABELS = {
    "Oldest": "oldest",
    "Largest / best quality": "largest",
    "First scanned": "first",
}

CHECK_ON, CHECK_OFF = "☑", "☐"

TREE_COLUMNS = (
    ("selected", "", 34),
    ("name", "Name", 210),
    ("duplicate", "Duplicate?", 170),
    ("category", "Category", 105),
    ("size", "Size", 80),
    ("modified", "Modified", 130),
    ("taken", "Taken", 130),
    ("dimensions", "Dimensions", 95),
    ("camera", "Camera", 130),
    ("duration", "Duration", 75),
    ("source_folder", "Source Folder", 220),
)

SORT_KEYS = {
    "name": lambda m: m["name"].lower(),
    "duplicate": lambda m: m["duplicate_label"],
    "category": lambda m: m["category"].lower(),
    "size": lambda m: m["size_bytes"],
    "modified": lambda m: m["modified"],
    "taken": lambda m: (m["taken"] is None, m["taken"] or datetime.min),
    "dimensions": lambda m: (m["width"] or 0) * (m["height"] or 0),
    "camera": lambda m: (m["camera"] is None, (m["camera"] or "").lower()),
    "duration": lambda m: (m["duration_seconds"] is None, m["duration_seconds"] or 0),
    "source_folder": lambda m: m["source_folder"].lower(),
}


def _line_pixels(x0, y0, x1, y1):
    steps = max(abs(x1 - x0), abs(y1 - y0)) or 1
    for i in range(steps + 1):
        yield round(x0 + (x1 - x0) * i / steps), round(y0 + (y1 - y0) * i / steps)


def _checkbox_image(checked, enabled=True, size=18):
    """A checkbox indicator drawn pixel by pixel: rounded box, and a white
    tick on an accent fill when checked (the stock theme draws an X).
    """
    border = ACCENT if (checked and enabled) else ("#a9afbd" if enabled else "#d5d8e0")
    fill = (ACCENT if enabled else "#c3c9ee") if checked else "#ffffff"
    grid = [["" for _ in range(size)] for _ in range(size)]
    corner = {(0, 0), (size - 1, 0), (0, size - 1), (size - 1, size - 1)}
    for y in range(size):
        for x in range(size):
            if (x, y) in corner:
                continue
            edge = x in (0, size - 1) or y in (0, size - 1)
            grid[y][x] = border if edge else fill
    if checked:
        for a, b in (((4, 9), (7, 12)), ((7, 12), (13, 5))):
            for px, py in _line_pixels(*a, *b):
                for dx, dy in ((0, 0), (1, 0), (0, 1)):
                    grid[py + dy][px + dx] = "#ffffff"
    image = tk.PhotoImage(width=size, height=size)
    image.put(" ".join("{" + " ".join(c or "#eef1f6" for c in row) + "}" for row in grid))
    return image


def _install_tick_checkbuttons(style, root):
    root._tick_images = {
        "off": _checkbox_image(False),
        "on": _checkbox_image(True),
        "off_disabled": _checkbox_image(False, enabled=False),
        "on_disabled": _checkbox_image(True, enabled=False),
    }
    images = root._tick_images
    style.element_create(
        "Tick.indicator", "image", images["off"],
        ("disabled", "selected", images["on_disabled"]),
        ("disabled", images["off_disabled"]),
        ("selected", images["on"]),
    )
    style.layout("TCheckbutton", [
        ("Checkbutton.padding", {"sticky": "nswe", "children": [
            ("Tick.indicator", {"side": "left", "sticky": ""}),
            ("Checkbutton.focus", {"side": "left", "sticky": "", "children": [
                ("Checkbutton.label", {"sticky": "nswe"}),
            ]}),
        ]}),
    ])


def apply_style(root):
    root.configure(bg=PAGE_BG)
    style = ttk.Style(root)
    style.theme_use("clam")
    _install_tick_checkbuttons(style, root)

    style.configure("TFrame", background=PAGE_BG)
    style.configure("Card.TFrame", background=CARD_BG)

    style.configure("TLabel", background=PAGE_BG, foreground=TEXT, font=("Segoe UI", 10))
    style.configure("Card.TLabel", background=CARD_BG, foreground=TEXT, font=("Segoe UI", 10))
    style.configure("Muted.TLabel", background=PAGE_BG, foreground=MUTED, font=("Segoe UI", 9))
    style.configure("CardMuted.TLabel", background=CARD_BG, foreground=MUTED, font=("Segoe UI", 9))
    style.configure("Title.TLabel", background=PAGE_BG, foreground=TEXT, font=("Segoe UI", 16, "bold"))
    style.configure("CardTitle.TLabel", background=CARD_BG, foreground=TEXT, font=("Segoe UI", 11, "bold"))

    style.configure(
        "TButton", font=("Segoe UI", 10), padding=(12, 7), background="#ffffff",
        foreground=TEXT, bordercolor=CARD_BORDER, relief="flat",
    )
    style.map("TButton", background=[("active", "#eef0f5")])

    style.configure("TCheckbutton", background=CARD_BG, foreground=TEXT, font=("Segoe UI", 10))
    style.configure("TCombobox", font=("Segoe UI", 10), padding=4)
    style.configure("TEntry", font=("Segoe UI", 10), padding=5, fieldbackground="white")

    style.configure(
        "Accent.Horizontal.TProgressbar", troughcolor=CARD_BORDER, background=ACCENT,
        bordercolor=CARD_BORDER, lightcolor=ACCENT, darkcolor=ACCENT, thickness=10,
    )

    style.configure(
        "Treeview", font=("Segoe UI", 9), rowheight=25, background="white",
        fieldbackground="white", foreground=TEXT, borderwidth=0,
    )
    style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"), background="#eef0f5", relief="flat")
    style.map("Treeview.Heading", background=[("active", "#e4e7f0")])
    style.map("Treeview", background=[("selected", "#dfe6ff")], foreground=[("selected", TEXT)])


def card(parent, title=None):
    """A flat white 'card' panel (border-trick, no native rounding) on the
    light page background. Returns (outer_widget_to_pack, content_frame).
    """
    outer = tk.Frame(parent, bg=CARD_BORDER)
    inner = tk.Frame(outer, bg=CARD_BG)
    inner.pack(fill="both", expand=True, padx=1, pady=1)
    content = ttk.Frame(inner, padding=14, style="Card.TFrame")
    content.pack(fill="both", expand=True)

    if title:
        header = ttk.Frame(content, style="Card.TFrame")
        header.pack(fill="x", pady=(0, 10))
        accent = tk.Frame(header, bg=ACCENT, width=4)
        accent.pack(side="left", fill="y", padx=(0, 8))
        ttk.Label(header, text=title, style="CardTitle.TLabel").pack(side="left")

    return outer, content


class RoundedButton(tk.Canvas):
    """A small canvas-drawn pill button, since ttk buttons can't do rounded
    corners. Used for the main actions (Organize, Stop).
    """

    def __init__(self, parent, text, command, bg_color=PAGE_BG, width=150, height=40, radius=12,
                 color=ACCENT, hover=ACCENT_ACTIVE, disabled=ACCENT_DISABLED):
        super().__init__(parent, width=width, height=height, bg=bg_color, highlightthickness=0)
        self.command = command
        self.width, self.height, self.radius = width, height, radius
        self.text = text
        self.color, self.hover, self.disabled = color, hover, disabled
        self.enabled = True
        self._draw(color)
        self.bind("<Enter>", lambda e: self._draw(self.hover if self.enabled else self.disabled))
        self.bind("<Leave>", lambda e: self._draw(self.color if self.enabled else self.disabled))
        self.bind("<Button-1>", self._on_click)

    def _round_rect(self, x1, y1, x2, y2, r, **kwargs):
        points = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        self.create_polygon(points, smooth=True, **kwargs)

    def _draw(self, color):
        self.delete("all")
        self._round_rect(2, 2, self.width - 2, self.height - 2, self.radius, fill=color, outline="")
        self.create_text(
            self.width / 2, self.height / 2, text=self.text, fill="white",
            font=("Segoe UI", 10, "bold"),
        )

    def _on_click(self, event):
        if self.enabled and self.command:
            self.command()

    def set_enabled(self, enabled):
        self.enabled = enabled
        self._draw(self.color if enabled else self.disabled)
        self.configure(cursor="hand2" if enabled else "arrow")


class OrganizerApp:
    def __init__(self, root):
        self.root = root
        root.title("File Organizer")
        root.geometry("1100x760")
        root.minsize(940, 620)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        apply_style(root)

        self.log_queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.scanned_meta = []
        self.meta_by_path = {}
        self.filtered = []
        self.checked = {}
        self.dup_info = {}
        self.find_dups_used = False
        self.scan_checked_duplicates = False
        self.sort_column = None
        self.sort_reverse = False
        self.running = False
        self._filter_job = None
        self._bar_indeterminate = False

        self._build_layout()
        self.root.after(100, self._drain_log_queue)

    def _on_close(self):
        self.cancel_event.set()
        self.root.destroy()

    # ---------- layout ----------

    def _build_layout(self):
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="File Organizer", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="Add folders, scan to see every file's properties, pick what to keep, then organize.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(2, 12))

        top = ttk.Frame(outer)
        top.pack(fill="x")
        top.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=1)

        src_outer, src_content = card(top, "Messy Folders")
        src_outer.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self._build_sources(src_content)

        dest_outer, dest_content = card(top, "Destination & Options")
        dest_outer.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self._build_destination_and_options(dest_content)

        self._build_actions(outer)
        self._build_tables(outer)

    def _build_sources(self, parent):
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="both", expand=True)

        self.source_list = tk.Listbox(
            row, height=6, bg="white", fg=TEXT, relief="flat",
            highlightthickness=1, highlightbackground=CARD_BORDER, font=("Segoe UI", 9),
            selectbackground=ACCENT, selectforeground="white",
        )
        self.source_list.pack(side="left", fill="both", expand=True)

        btns = ttk.Frame(row, style="Card.TFrame")
        btns.pack(side="left", fill="y", padx=(10, 0))
        ttk.Button(btns, text="Add Folder...", command=self.add_source).pack(fill="x", pady=(0, 4))
        ttk.Button(btns, text="Remove Selected", command=self.remove_source).pack(fill="x")

    def _build_destination_and_options(self, parent):
        dest_row = ttk.Frame(parent, style="Card.TFrame")
        dest_row.pack(fill="x")
        self.dest_var = tk.StringVar()
        ttk.Entry(dest_row, textvariable=self.dest_var).pack(side="left", fill="x", expand=True)
        ttk.Button(dest_row, text="Browse...", command=self.pick_destination).pack(side="left", padx=(8, 0))

        row_a = ttk.Frame(parent, style="Card.TFrame")
        row_a.pack(fill="x", pady=(12, 0))
        ttk.Label(row_a, text="Group by:", style="Card.TLabel").pack(side="left")
        self.group_by_var = tk.StringVar(value="Year")
        ttk.Combobox(
            row_a, textvariable=self.group_by_var, values=list(GROUP_LABELS.keys()),
            state="readonly", width=13,
        ).pack(side="left", padx=(6, 16))
        ttk.Label(row_a, text="Keep:", style="Card.TLabel").pack(side="left")
        self.keep_var = tk.StringVar(value="Oldest")
        ttk.Combobox(
            row_a, textvariable=self.keep_var, values=list(KEEP_LABELS.keys()),
            state="readonly", width=20,
        ).pack(side="left", padx=(6, 0))

        row_b = ttk.Frame(parent, style="Card.TFrame")
        row_b.pack(fill="x", pady=(8, 0))
        self.dry_run_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row_b, text="Dry run (preview only)", variable=self.dry_run_var).pack(side="left")
        self.move_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row_b, text="Move instead of copy", variable=self.move_var).pack(side="left", padx=(16, 0))

        row_c = ttk.Frame(parent, style="Card.TFrame")
        row_c.pack(fill="x", pady=(4, 0))
        self.find_dups_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row_c, text="Find duplicates when scanning", variable=self.find_dups_var).pack(side="left")
        self.similar_var = tk.BooleanVar(value=False)
        self.similar_check = ttk.Checkbutton(row_c, text="Also similar photos & videos", variable=self.similar_var)
        self.similar_check.pack(side="left", padx=(16, 0))
        if not (organize.HAS_PIL or organize.HAS_VIDEO):
            self.similar_check.configure(state="disabled")

        row_d = ttk.Frame(parent, style="Card.TFrame")
        row_d.pack(fill="x", pady=(4, 0))
        self.details_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            row_d, text="Read photo details (date taken, camera) - untick for a faster scan",
            variable=self.details_var,
        ).pack(side="left")

    def _build_actions(self, parent):
        frame = ttk.Frame(parent)
        frame.pack(fill="x", pady=(12, 6))

        self.scan_btn = ttk.Button(frame, text="Scan Files", command=self.start_scan)
        self.scan_btn.pack(side="left")

        self.select_all_btn = ttk.Button(frame, text="Select All", command=lambda: self._set_all_checked(True), state="disabled")
        self.select_all_btn.pack(side="left", padx=(8, 0))
        self.select_none_btn = ttk.Button(frame, text="Select None", command=lambda: self._set_all_checked(False), state="disabled")
        self.select_none_btn.pack(side="left", padx=(4, 0))

        self.separate_btn = ttk.Button(frame, text="Separate Duplicates", command=self.start_separate, state="disabled")
        self.separate_btn.pack(side="left", padx=(12, 0))

        ttk.Label(frame, text="Search:").pack(side="left", padx=(16, 4))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self._schedule_filter())
        ttk.Entry(frame, textvariable=self.search_var, width=20).pack(side="left")

        self.run_btn = RoundedButton(frame, "Organize", self.start_organize, bg_color=PAGE_BG)
        self.run_btn.pack(side="right")
        self.run_btn.set_enabled(False)

        self.stop_btn = RoundedButton(
            frame, "Stop", self.stop, bg_color=PAGE_BG, width=90,
            color=DANGER, hover=DANGER_ACTIVE, disabled=DANGER_DISABLED,
        )
        self.stop_btn.pack(side="right", padx=(0, 8))
        self.stop_btn.set_enabled(False)

        status_row = ttk.Frame(parent)
        status_row.pack(fill="x", pady=(0, 8))
        self.status_var = tk.StringVar(value="Add folders, then Scan Files.")
        ttk.Label(status_row, textvariable=self.status_var, style="Muted.TLabel").pack(side="left")
        self.progress = ttk.Progressbar(
            status_row, style="Accent.Horizontal.TProgressbar", mode="determinate", length=280,
        )
        self.progress.pack(side="right")

    def _build_tables(self, parent):
        paned = ttk.Panedwindow(parent, orient="vertical")
        paned.pack(fill="both", expand=True)
        self.paned = paned
        paned.bind("<Map>", lambda e: self.root.after(50, self._place_sash))

        tree_outer, tree_content = card(paned, "Scanned Files (click a row's checkbox to include/exclude, click a heading to sort)")
        paned.add(tree_outer, weight=3)

        table_wrap = ttk.Frame(tree_content, style="Card.TFrame")
        table_wrap.pack(fill="both", expand=True)

        columns = [c[0] for c in TREE_COLUMNS]
        self.tree = ttk.Treeview(table_wrap, columns=columns, show="headings", selectmode="none", height=8)
        for key, label, width in TREE_COLUMNS:
            self.tree.heading(key, text=label, command=lambda k=key: self._sort_by(k))
            anchor = "center" if key == "selected" else "w"
            self.tree.column(key, width=width, anchor=anchor, stretch=(key != "selected"))

        vsb = ttk.Scrollbar(table_wrap, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(table_wrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        table_wrap.rowconfigure(0, weight=1)
        table_wrap.columnconfigure(0, weight=1)

        self.tree.tag_configure("even", background=CARD_BG)
        self.tree.tag_configure("odd", background=ROW_ALT_BG)
        self.tree.tag_configure("dup", foreground="#b45309")
        self.tree.bind("<Button-1>", self._on_tree_click)

        log_outer, log_content = card(paned, "Activity Log")
        paned.add(log_outer, weight=1)

        self.log_box = scrolledtext.ScrolledText(
            log_content, state="disabled", wrap="word", bg=CONSOLE_BG, fg=CONSOLE_FG,
            insertbackground=CONSOLE_FG, font=("Consolas", 9), relief="flat", padx=8, pady=8,
            height=6,
        )
        self.log_box.pack(fill="both", expand=True)

    def _place_sash(self):
        """Give the file table about two thirds of the space (it starts collapsed)."""
        height = self.paned.winfo_height()
        if height > 100:
            self.paned.sashpos(0, int(height * 0.66))

    # ---------- source / destination pickers ----------

    def add_source(self):
        folder = filedialog.askdirectory(title="Select a messy folder")
        if folder and folder not in self.source_list.get(0, "end"):
            self.source_list.insert("end", folder)

    def remove_source(self):
        for index in reversed(self.source_list.curselection()):
            self.source_list.delete(index)

    def pick_destination(self):
        folder = filedialog.askdirectory(title="Select destination folder")
        if folder:
            self.dest_var.set(folder)

    def _get_sources(self):
        return list(self.source_list.get(0, "end"))

    # ---------- logging, progress, stop ----------

    def append_log(self, message):
        self.log_queue.put(message)

    def _flush_lines(self, lines):
        if not lines:
            return
        self.log_box.configure(state="normal")
        self.log_box.insert("end", "\n".join(lines) + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        lines.clear()

    def _drain_log_queue(self):
        lines = []
        try:
            for _ in range(2000):
                message = self.log_queue.get_nowait()
                if callable(message):
                    self._flush_lines(lines)
                    message()
                else:
                    lines.append(message)
        except queue.Empty:
            pass
        self._flush_lines(lines)
        self.root.after(100, self._drain_log_queue)

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _make_progress(self):
        """A progress callback for worker threads: at most ~7 UI updates a second."""
        last = {"t": 0.0}

        def progress(stage, done, total):
            now = time.monotonic()
            if now - last["t"] < 0.15 and not (total and done >= total):
                return
            last["t"] = now
            self.log_queue.put(lambda: self._show_progress(stage, done, total))

        return progress

    def _show_progress(self, stage, done, total):
        if total:
            if self._bar_indeterminate:
                self.progress.stop()
                self._bar_indeterminate = False
            self.progress.configure(mode="determinate", maximum=max(total, 1), value=done)
            self.status_var.set(f"{stage}... {done:,} of {total:,}")
        else:
            if not self._bar_indeterminate:
                self.progress.configure(mode="indeterminate")
                self.progress.start(15)
                self._bar_indeterminate = True
            self.status_var.set(f"{stage}... {done:,} files found")

    def _reset_bar(self):
        self.progress.stop()
        self._bar_indeterminate = False
        self.progress.configure(mode="determinate", value=0)

    def stop(self):
        if not self.running:
            return
        self.cancel_event.set()
        self.stop_btn.set_enabled(False)
        self.status_var.set("Stopping... finishing the files already in progress")

    def _set_running(self, running, status):
        self.running = running
        has_files = bool(self.scanned_meta)
        state = "disabled" if running else "normal"
        self.scan_btn.configure(state=state)
        self.select_all_btn.configure(state="disabled" if running or not has_files else "normal")
        self.select_none_btn.configure(state="disabled" if running or not has_files else "normal")
        self.run_btn.set_enabled((not running) and has_files)
        has_dups = any(not i["keep"] for i in self.dup_info.values())
        self.separate_btn.configure(state="normal" if (not running and has_dups) else "disabled")
        self.stop_btn.set_enabled(running)
        if not running:
            self._reset_bar()
        self.status_var.set(status)

    def _begin_job(self, status):
        self.cancel_event.clear()
        self._set_running(True, status)
        self._clear_log()

    # ---------- scan ----------

    def start_scan(self):
        if self.running:
            return
        sources = self._get_sources()
        if not sources:
            messagebox.showwarning("Missing folders", "Add at least one messy folder to scan.")
            return

        self._begin_job("Scanning...")
        self.tree.delete(*self.tree.get_children())

        find_dups = self.find_dups_var.get()
        self.find_dups_used = find_dups
        similar = self.similar_var.get() and (organize.HAS_PIL or organize.HAS_VIDEO)
        keep = KEEP_LABELS[self.keep_var.get()]
        details = self.details_var.get()
        thread = threading.Thread(
            target=self._run_scan, args=(sources, find_dups, similar, keep, details), daemon=True
        )
        thread.start()

    def _run_scan(self, sources, find_dups, similar, keep, details):
        try:
            cancel = self.cancel_event.is_set
            progress = self._make_progress()
            started = time.monotonic()
            self.append_log(f"Scanning: {', '.join(sources)}\n")

            results = organize.scan_files(
                sources, log=self.append_log, cancel=cancel, details=details, progress=progress
            )
            self.append_log(f"Found {len(results):,} files in {time.monotonic() - started:.1f}s.")

            dup_info = {}
            if find_dups and results and not cancel():
                self.append_log(
                    "Looking for duplicates" + (" (including similar photos and videos)" if similar else "") + "..."
                )
                dup_info = organize.find_duplicates(
                    results, keep=keep, similar=similar, log=self.append_log,
                    cancel=cancel, progress=progress,
                )
                extras = sum(1 for i in dup_info.values() if not i["keep"])
                groups = len({i["group"] for i in dup_info.values()})
                self.append_log(f"Found {extras:,} duplicate file(s) in {groups:,} group(s).")

            prepared = self._prepare(results, dup_info)
            stopped = cancel()
            self.log_queue.put(lambda: self._on_scan_done(prepared, stopped))
        except Exception as e:
            self.append_log(f"\n! Unexpected error: {e}")
            self.log_queue.put(lambda: self._set_running(False, "Scan failed."))

    @staticmethod
    def _duplicate_label(info):
        if not info:
            return "-"
        if info["keep"]:
            return f"Keep ({info['copies']} copies)"
        prefix = "Duplicate of" if info["kind"] == "exact" else "Similar to"
        return f"{prefix} {info['keeper']}"

    def _prepare(self, results, dup_info):
        """Per-file bookkeeping done on the worker thread so the window stays
        smooth: labels, lower-cased search text, lookup and checkbox maps.
        Duplicates start unchecked so Organize never copies them again.
        """
        meta_by_path, checked = {}, {}
        for m in results:
            key = str(m["path"])
            info = dup_info.get(key)
            m["duplicate_label"] = self._duplicate_label(info)
            m["search"] = " ".join(
                (m["name"], m["category"], m["camera"] or "", m["source_folder"], m["ext"])
            ).lower()
            meta_by_path[key] = m
            checked[key] = info["keep"] if info else True
        return results, dup_info, meta_by_path, checked

    def _on_scan_done(self, prepared, stopped):
        self.scanned_meta, self.dup_info, self.meta_by_path, self.checked = prepared
        self.scan_checked_duplicates = self.find_dups_used
        self.sort_column = None
        self.sort_reverse = False
        self._refresh_headings()
        self._apply_filter()

        total = len(self.scanned_meta)
        extras = sum(1 for i in self.dup_info.values() if not i["keep"])
        message = f"Scanned {total:,} files"
        if stopped:
            message = f"Scan stopped - {total:,} files found so far (partial). Scan again for the rest"
        if extras:
            message += f"; {extras:,} duplicate(s) left unchecked"
        message += ". Review, then Organize." if not stopped else "."
        self._set_running(False, message)

    # ---------- table: filter / sort / checkboxes ----------

    def _schedule_filter(self):
        if self._filter_job is not None:
            self.root.after_cancel(self._filter_job)
        self._filter_job = self.root.after(250, self._apply_filter)

    def _apply_filter(self):
        self._filter_job = None
        query = self.search_var.get().strip().lower()
        if query:
            rows = [m for m in self.scanned_meta if query in m["search"]]
        else:
            rows = list(self.scanned_meta)

        if self.sort_column:
            keyfunc = SORT_KEYS.get(self.sort_column)
            if keyfunc:
                rows.sort(key=keyfunc, reverse=self.sort_reverse)

        self.filtered = rows
        shown = rows[:DISPLAY_LIMIT]
        self._populate_tree(shown)

        if self.scanned_meta and not self.running:
            text = f"Showing {len(shown):,} of {len(rows):,} matching files"
            if len(rows) > DISPLAY_LIMIT:
                text += f" (first {DISPLAY_LIMIT:,} drawn - search to narrow; checked files are organized even if not drawn)"
            self.status_var.set(text)

    def _populate_tree(self, rows):
        self.tree.delete(*self.tree.get_children())
        for i, meta in enumerate(rows):
            iid = str(meta["path"])
            tag = "even" if i % 2 == 0 else "odd"
            is_dup = not self.dup_info.get(iid, {"keep": True})["keep"]
            self.tree.insert("", "end", iid=iid, tags=(tag, "dup") if is_dup else (tag,), values=(
                CHECK_ON if self.checked.get(iid, True) else CHECK_OFF,
                meta["name"],
                meta["duplicate_label"],
                meta["category"],
                organize.human_size(meta["size_bytes"]),
                meta["modified"].strftime("%Y-%m-%d %H:%M"),
                meta["taken"].strftime("%Y-%m-%d %H:%M") if meta["taken"] else "-",
                f"{meta['width']}x{meta['height']}" if meta["width"] else "-",
                meta["camera"] or "-",
                meta["duration"] or "-",
                meta["source_folder"],
            ))

    def _sort_by(self, col):
        if col == "selected" or col not in SORT_KEYS:
            return
        self.sort_reverse = (not self.sort_reverse) if self.sort_column == col else False
        self.sort_column = col
        self._refresh_headings()
        self._apply_filter()

    def _refresh_headings(self):
        for key, label, _ in TREE_COLUMNS:
            text = label
            if key == self.sort_column:
                text += " ▲" if not self.sort_reverse else " ▼"
            self.tree.heading(key, text=text, command=lambda k=key: self._sort_by(k))

    def _on_tree_click(self, event):
        if self.tree.identify("region", event.x, event.y) != "cell":
            return
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.checked[row] = not self.checked.get(row, True)
        self.tree.set(row, "selected", CHECK_ON if self.checked[row] else CHECK_OFF)

    def _set_all_checked(self, value):
        for meta in self.filtered:
            self.checked[str(meta["path"])] = value
        for iid in self.tree.get_children():
            self.tree.set(iid, "selected", CHECK_ON if value else CHECK_OFF)
        word = "checked" if value else "unchecked"
        self.status_var.set(f"{len(self.filtered):,} file(s) {word}.")

    # ---------- separate duplicates in place ----------

    def start_separate(self):
        if self.running:
            return
        items = [
            (self.meta_by_path[p]["path"], self.meta_by_path[p]["root"])
            for p, info in self.dup_info.items()
            if not info["keep"] and p in self.meta_by_path
        ]
        if not items:
            messagebox.showinfo("No duplicates", "No duplicates were found in the last scan.")
            return

        dry_run = self.dry_run_var.get()
        if not dry_run:
            confirmed = messagebox.askyesno(
                "Separate duplicates",
                f"This will move {len(items):,} duplicate file(s) into a '_Duplicates' folder inside "
                "their own source folder. Nothing is copied or deleted, and the file marked "
                "Keep stays where it is. Continue?",
            )
            if not confirmed:
                return

        self._begin_job("Separating duplicates...")
        thread = threading.Thread(target=self._run_separate, args=(items, dry_run), daemon=True)
        thread.start()

    def _run_separate(self, items, dry_run):
        try:
            mode = "DRY RUN" if dry_run else "MOVE"
            self.append_log(f"Separating {len(items):,} duplicate file(s) ({mode})")
            moved, errors = organize.separate_duplicates(
                items, dry_run=dry_run, log=self.append_log,
                cancel=self.cancel_event.is_set, progress=self._make_progress(),
            )
            self.append_log(f"\nDone. Moved: {len(moved):,}  Errors: {errors}")
            self.log_queue.put(lambda: self._after_separate(moved, dry_run))
        except Exception as e:
            self.append_log(f"\n! Unexpected error: {e}")
            self.log_queue.put(lambda: self._set_running(False, "Failed."))

    def _after_separate(self, moved, dry_run):
        if dry_run:
            self._set_running(False, "Dry run finished - nothing was moved. Untick 'Dry run' to do it for real.")
            return
        gone = {str(p) for p in moved}
        self.scanned_meta = [m for m in self.scanned_meta if str(m["path"]) not in gone]
        for p in gone:
            self.dup_info.pop(p, None)
            self.checked.pop(p, None)
            self.meta_by_path.pop(p, None)
        self._apply_filter()
        self._set_running(False, f"Moved {len(gone):,} duplicate(s) into _Duplicates folders.")

    # ---------- organize ----------

    def start_organize(self):
        if self.running:
            return
        if not self.scanned_meta:
            messagebox.showwarning("Scan first", "Click 'Scan Files' to preview files before organizing.")
            return

        dest = self.dest_var.get().strip()
        if not dest:
            messagebox.showwarning("Missing destination", "Choose a destination folder.")
            return

        selected_paths = [p for p, ok in self.checked.items() if ok and p in self.meta_by_path]
        if not selected_paths:
            messagebox.showwarning("Nothing selected", "Check at least one file in the table to organize.")
            return

        dry_run = self.dry_run_var.get()
        move = self.move_var.get()
        group_by = GROUP_LABELS[self.group_by_var.get()]

        if not dry_run:
            if move:
                question = (
                    f"This will MOVE {len(selected_paths):,} selected file(s) into the destination folder "
                    "(they will no longer be in their original folders). Continue?"
                )
            else:
                question = (
                    f"This will copy {len(selected_paths):,} selected file(s) into the destination folder "
                    "(originals are left untouched). You can press Stop at any time and continue later. Continue?"
                )
            if not messagebox.askyesno("Confirm", question):
                return

        self._begin_job("Organizing...")
        known_duplicates = None
        if self.scan_checked_duplicates:
            known_duplicates = {p for p, i in self.dup_info.items() if not i["keep"]}
        thread = threading.Thread(
            target=self._run_organize,
            args=(selected_paths, dest, dry_run, group_by, move, self.meta_by_path, known_duplicates),
            daemon=True,
        )
        thread.start()

    def _run_organize(self, paths, dest, dry_run, group_by, move, metas, known_duplicates):
        try:
            started = time.monotonic()
            self.append_log(f"Organizing {len(paths):,} selected file(s)")
            self.append_log(f"Destination: {dest}")
            self.append_log(f"Group by: {group_by}")
            self.append_log(f"Mode: {'DRY RUN' if dry_run else ('MOVE' if move else 'COPY')}\n")

            stats = organize.organize_files(
                paths, Path(dest), dry_run, group_by=group_by, log=self.append_log, move=move,
                metas=metas, known_duplicates=known_duplicates, dedupe=False,
                cancel=self.cancel_event.is_set,
                progress=self._make_progress(),
            )

            elapsed = time.monotonic() - started
            self.append_log(f"\n{'Stopped' if stats['cancelled'] else 'Done'} in {elapsed:.1f}s.")
            self.append_log(f"  Organized: {stats['organized']:,}")
            self.append_log(f"  Duplicates set aside: {stats['duplicates']:,}")
            self.append_log(f"  Skipped (already at destination): {stats['skipped']:,}")
            self.append_log(f"  Errors: {stats['errors']:,}")

            if stats["cancelled"]:
                message = (
                    "Stopped. Click Organize again to continue - files already done are skipped."
                )
            elif dry_run:
                message = "Dry run finished - nothing was changed. Untick 'Dry run' to do it for real."
            else:
                message = f"Done in {elapsed:.1f}s."
            self.log_queue.put(lambda: self._set_running(False, message))
        except Exception as e:
            self.append_log(f"\n! Unexpected error: {e}")
            self.log_queue.put(lambda: self._set_running(False, "Failed."))


def main():
    root = tk.Tk()
    OrganizerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
