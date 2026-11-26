"""Tkinter GUI for Android Kitchen."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .extractor import DEFAULT_CONFIG_PATTERNS, ExtractOptions
from .firmware import Scanner
from .pipeline import run
from .util import Cancelled, Job, KitchenError, human

APP_NAME = "Android Kitchen"
SETTINGS_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "AndroidKitchen")
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")

CHECK_ON, CHECK_OFF = "☑", "☐"

LOG_COLORS = {"info": None, "ok": "#1a7f37", "warn": "#b35900", "error": "#c62828",
              "head": "#0b57d0"}


def open_in_explorer(path: str, select: bool = False):
    if not os.path.exists(path):
        return
    if sys.platform == "win32":
        if select:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            os.startfile(path)  # noqa: S606 - opening a local folder chosen by the user
    else:
        subprocess.Popen(["xdg-open", path])


class GuiJob(Job):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q
        self._last = 0.0

    def log(self, msg, level="info"):
        self.q.put(("log", msg, level))

    def warn(self, msg):
        self.log(msg, "warn")

    def error(self, msg):
        self.log(msg, "error")

    def progress(self, cur, total, text=""):
        now = time.monotonic()
        if now - self._last > 0.1 or cur >= total:
            self._last = now
            self.q.put(("progress", cur, total, text))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {__version__} - stock firmware APK & config extractor")
        self.geometry("1180x800")
        self.minsize(900, 620)
        self.q: queue.Queue = queue.Queue()
        self.job: GuiJob | None = None
        self.worker: threading.Thread | None = None
        self.items = []
        self.apk_rows = []
        self.scanned_path = None
        self.ota_meta = {}
        self._init_style()
        self._build_vars()
        self._build_menu()
        self._build_ui()
        self._load_settings()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)
        self._log(f"{APP_NAME} {__version__} ready. Choose a firmware file or folder, then click "
                  "'Scan firmware'.", "head")

    # ------------------------------------------------------------------ setup
    def _init_style(self):
        style = ttk.Style(self)
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        style.configure("Treeview", rowheight=24)
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))
        style.configure("Title.TLabel", font=("Segoe UI", 11, "bold"))

    def _build_vars(self):
        self.v_fw = tk.StringVar()
        self.v_ws = tk.StringVar()
        self.v_apks = tk.BooleanVar(value=True)
        self.v_apex = tk.BooleanVar(value=False)
        self.v_jars = tk.BooleanVar(value=False)
        self.v_apkinfo = tk.BooleanVar(value=True)
        self.v_configs = tk.BooleanVar(value=True)
        self.v_full = tk.BooleanVar(value=False)
        self.v_meta = tk.BooleanVar(value=True)
        self.v_keep = tk.BooleanVar(value=False)
        self.v_imgonly = tk.BooleanVar(value=False)
        self.v_bothslots = tk.BooleanVar(value=True)
        self.v_status = tk.StringVar(value="Idle")
        self.v_filter = tk.StringVar()
        self.v_filter.trace_add("write", lambda *a: self._refresh_apks())

    def _build_menu(self):
        m = tk.Menu(self)
        fm = tk.Menu(m, tearoff=0)
        fm.add_command(label="Open firmware file...", command=self._pick_file, accelerator="Ctrl+O")
        fm.add_command(label="Open firmware folder...", command=self._pick_folder)
        fm.add_separator()
        fm.add_command(label="Open workspace folder", command=self._open_ws)
        fm.add_separator()
        fm.add_command(label="Exit", command=self._on_close)
        m.add_cascade(label="File", menu=fm)
        hm = tk.Menu(m, tearoff=0)
        hm.add_command(label="Supported formats", command=self._show_formats)
        hm.add_command(label="About", command=self._about)
        m.add_cascade(label="Help", menu=hm)
        self.config(menu=m)
        self.bind_all("<Control-o>", lambda e: self._pick_file())

    def _build_ui(self):
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)

        # --- source / workspace ---------------------------------------------------
        src = ttk.LabelFrame(outer, text=" Firmware ", padding=8)
        src.pack(fill="x")
        src.columnconfigure(1, weight=1)
        ttk.Label(src, text="Firmware:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(src, textvariable=self.v_fw).grid(row=0, column=1, sticky="ew")
        ttk.Button(src, text="File...", command=self._pick_file).grid(row=0, column=2, padx=(6, 0))
        ttk.Button(src, text="Folder...", command=self._pick_folder).grid(row=0, column=3, padx=(6, 0))
        ttk.Label(src, text="Workspace:").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=(6, 0))
        ttk.Entry(src, textvariable=self.v_ws).grid(row=1, column=1, sticky="ew", pady=(6, 0))
        ttk.Button(src, text="Browse...", command=self._pick_ws).grid(row=1, column=2, padx=(6, 0), pady=(6, 0))
        ttk.Button(src, text="Open", command=self._open_ws).grid(row=1, column=3, padx=(6, 0), pady=(6, 0))
        self.btn_scan = ttk.Button(src, text="Scan firmware", style="Accent.TButton",
                                   command=self._scan)
        self.btn_scan.grid(row=0, column=4, rowspan=2, padx=(12, 0), sticky="ns")

        # --- notebook + log in a vertical pane ---------------------------------------
        pane = ttk.Panedwindow(outer, orient="vertical")
        pane.pack(fill="both", expand=True, pady=(8, 0))
        self.nb = ttk.Notebook(pane)
        pane.add(self.nb, weight=3)
        self._build_partitions_tab()
        self._build_options_tab()
        self._build_apks_tab()

        logf = ttk.LabelFrame(pane, text=" Log ", padding=4)
        pane.add(logf, weight=2)
        self.log_text = tk.Text(logf, height=10, wrap="word", font=("Consolas", 9),
                                state="disabled", relief="flat", background="#fbfbfb")
        sb = ttk.Scrollbar(logf, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for lvl, color in LOG_COLORS.items():
            if color:
                self.log_text.tag_configure(lvl, foreground=color)
        self.log_text.tag_configure("head", foreground=LOG_COLORS["head"],
                                    font=("Consolas", 9, "bold"))

        # --- bottom: progress + actions --------------------------------------------
        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(8, 0))
        self.pbar = ttk.Progressbar(bottom, mode="determinate", maximum=1000)
        self.pbar.pack(side="left", fill="x", expand=True)
        ttk.Label(bottom, textvariable=self.v_status, width=48, anchor="w").pack(side="left", padx=8)
        self.btn_cancel = ttk.Button(bottom, text="Cancel", command=self._cancel, state="disabled")
        self.btn_cancel.pack(side="right")
        self.btn_start = ttk.Button(bottom, text="Extract selected", style="Accent.TButton",
                                    command=self._start, state="disabled")
        self.btn_start.pack(side="right", padx=(0, 6))
        self.btn_flash = ttk.Button(bottom, text="Build flash script", command=self._build_flash,
                                    state="disabled")
        self.btn_flash.pack(side="right", padx=(0, 6))

    def _build_partitions_tab(self):
        f = ttk.Frame(self.nb, padding=6)
        self.nb.add(f, text="  Partitions  ")
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="Partitions found in the firmware (click a row to toggle):",
                  style="Title.TLabel").pack(side="left")
        ttk.Button(bar, text="None", command=lambda: self._select("none")).pack(side="right")
        ttk.Button(bar, text="All", command=lambda: self._select("all")).pack(side="right", padx=4)
        ttk.Button(bar, text="Recommended", command=lambda: self._select("rec")).pack(side="right")
        cols = ("sel", "name", "kind", "size", "origin")
        tv = ttk.Treeview(f, columns=cols, show="headings", selectmode="browse")
        for c, t, w, anchor in [("sel", "", 36, "center"), ("name", "Partition", 160, "w"),
                                ("kind", "Type", 110, "w"), ("size", "Size", 100, "e"),
                                ("origin", "Source", 600, "w")]:
            tv.heading(c, text=t)
            tv.column(c, width=w, anchor=anchor, stretch=(c == "origin"))
        sb = ttk.Scrollbar(f, command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        tv.bind("<ButtonRelease-1>", self._toggle_row)
        tv.bind("<space>", self._toggle_row)
        tv.tag_configure("off", foreground="#777777")
        self.tv_parts = tv

    def _build_options_tab(self):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="  Options  ")
        f.columnconfigure(1, weight=1)
        f.rowconfigure(0, weight=1)

        left = ttk.Frame(f)
        left.grid(row=0, column=0, sticky="nw", padx=(0, 20))
        ttk.Label(left, text="What to extract", style="Title.TLabel").pack(anchor="w", pady=(0, 4))
        ttk.Checkbutton(left, text="APKs (*.apk)", variable=self.v_apks).pack(anchor="w")
        ttk.Checkbutton(left, text="   + APEX modules (*.apex, *.capex)",
                        variable=self.v_apex).pack(anchor="w")
        ttk.Checkbutton(left, text="   + Framework JARs (*.jar)", variable=self.v_jars).pack(anchor="w")
        ttk.Checkbutton(left, text="   Read package name / version from APKs",
                        variable=self.v_apkinfo).pack(anchor="w")
        ttk.Checkbutton(left, text="Config files (patterns on the right)",
                        variable=self.v_configs).pack(anchor="w", pady=(8, 0))
        ttk.Checkbutton(left, text="fs_config + file_contexts + symlink list",
                        variable=self.v_meta).pack(anchor="w", pady=(8, 0))
        ttk.Checkbutton(left, text="Full filesystem dump (every file)",
                        variable=self.v_full).pack(anchor="w", pady=(8, 0))
        ttk.Separator(left).pack(fill="x", pady=10)
        ttk.Label(left, text="Images", style="Title.TLabel").pack(anchor="w", pady=(0, 4))
        ttk.Checkbutton(left, text="Keep raw partition images (workspace\\images)",
                        variable=self.v_keep).pack(anchor="w")
        ttk.Checkbutton(left, text="Only unpack images (skip filesystem extraction)",
                        variable=self.v_imgonly).pack(anchor="w")
        ttk.Separator(left).pack(fill="x", pady=10)
        ttk.Label(left, text="Flash script", style="Title.TLabel").pack(anchor="w", pady=(0, 4))
        ttk.Checkbutton(left, text="Write firmware partitions to both slots (A and B)",
                        variable=self.v_bothslots).pack(anchor="w")
        ttk.Separator(left).pack(fill="x", pady=10)
        ttk.Label(left, justify="left", foreground="#555", font=("Consolas", 9), text=(
            "Output layout in the workspace:\n"
            "  apks\\<partition>\\...        collected APKs\n"
            "  configs\\<partition>\\...     collected config files\n"
            "  extracted\\<partition>\\...   full dump (if enabled)\n"
            "  config\\                     fs_config / file_contexts\n"
            "  images\\                     raw partition images\n"
            "  apk_list.csv                 APK index")).pack(anchor="w")

        right = ttk.Frame(f)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)
        hdr = ttk.Frame(right)
        hdr.grid(row=0, column=0, sticky="ew")
        ttk.Label(hdr, text="Config file patterns (one per line, matched against the path "
                            "inside the partition, * matches anything)",
                  style="Title.TLabel", wraplength=560, justify="left").pack(side="left")
        ttk.Button(hdr, text="Reset", command=self._reset_patterns).pack(side="right")
        self.txt_patterns = tk.Text(right, height=14, font=("Consolas", 10), wrap="none")
        self.txt_patterns.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        self._reset_patterns()

    def _build_apks_tab(self):
        f = ttk.Frame(self.nb, padding=6)
        self.nb.add(f, text="  APKs  ")
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="Filter:").pack(side="left")
        ttk.Entry(bar, textvariable=self.v_filter, width=40).pack(side="left", padx=6)
        self.lbl_apkcount = ttk.Label(bar, text="")
        self.lbl_apkcount.pack(side="left", padx=6)
        ttk.Button(bar, text="Open APK list (CSV)", command=self._open_csv).pack(side="right")
        ttk.Button(bar, text="Open APKs folder",
                   command=lambda: open_in_explorer(os.path.join(self.v_ws.get(), "apks"))
                   ).pack(side="right", padx=4)
        ttk.Button(bar, text="Open configs folder",
                   command=lambda: open_in_explorer(os.path.join(self.v_ws.get(), "configs"))
                   ).pack(side="right")
        cols = ("part", "package", "version", "sdk", "priv", "size", "path")
        tv = ttk.Treeview(f, columns=cols, show="headings")
        for c, t, w, anchor in [("part", "Partition", 90, "w"), ("package", "Package", 260, "w"),
                                ("version", "Version", 140, "w"), ("sdk", "min/target", 80, "center"),
                                ("priv", "Priv", 40, "center"), ("size", "Size", 80, "e"),
                                ("path", "Path in firmware", 420, "w")]:
            tv.heading(c, text=t, command=lambda c=c: self._sort_apks(c))
            tv.column(c, width=w, anchor=anchor, stretch=(c in ("path", "package")))
        sb = ttk.Scrollbar(f, command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        tv.bind("<Double-1>", self._apk_open)
        self.tv_apks = tv
        self._apk_sort = ("part", False)

    # ------------------------------------------------------------------ settings
    def _load_settings(self):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                s = json.load(f)
        except Exception:
            return
        for k, v in s.get("vars", {}).items():
            var = getattr(self, "v_" + k, None)
            if isinstance(var, tk.Variable):
                try:
                    var.set(v)
                except tk.TclError:
                    pass
        if s.get("patterns"):
            self.txt_patterns.delete("1.0", "end")
            self.txt_patterns.insert("1.0", s["patterns"])

    def _save_settings(self):
        names = ["fw", "ws", "apks", "apex", "jars", "apkinfo", "configs", "full", "meta",
                 "keep", "imgonly", "bothslots"]
        data = {"vars": {n: getattr(self, "v_" + n).get() for n in names},
                "patterns": self.txt_patterns.get("1.0", "end").strip()}
        try:
            os.makedirs(SETTINGS_DIR, exist_ok=True)
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
        except OSError:
            pass

    # ------------------------------------------------------------------ pickers
    def _default_ws(self, fw):
        base = fw.rstrip("\\/")
        stem = os.path.basename(base)
        for ext in (".tar.md5", ".tar.gz", ".tgz", ".zip", ".tar", ".img", ".bin", ".lz4"):
            if stem.lower().endswith(ext):
                stem = stem[:-len(ext)]
                break
        return os.path.join(os.path.dirname(base), (stem or "firmware") + "_kitchen")

    def _set_fw(self, path):
        if not path:
            return
        path = os.path.normpath(path)
        self.v_fw.set(path)
        self.v_ws.set(self._default_ws(path))

    def _pick_file(self):
        p = filedialog.askopenfilename(
            title="Select stock firmware",
            filetypes=[("Firmware packages", "*.zip *.tar *.md5 *.tgz *.gz *.xz *.img *.bin *.lz4 "
                                             "*.br *.ext4 *.dat"),
                       ("All files", "*.*")])
        self._set_fw(p)

    def _pick_folder(self):
        self._set_fw(filedialog.askdirectory(title="Select extracted firmware folder"))

    def _pick_ws(self):
        p = filedialog.askdirectory(title="Select workspace (output) folder")
        if p:
            self.v_ws.set(os.path.normpath(p))

    def _open_ws(self):
        ws = self.v_ws.get()
        if ws and os.path.isdir(ws):
            open_in_explorer(ws)
        else:
            messagebox.showinfo(APP_NAME, "The workspace folder does not exist yet.")

    def _reset_patterns(self):
        self.txt_patterns.delete("1.0", "end")
        self.txt_patterns.insert("1.0", "\n".join(DEFAULT_CONFIG_PATTERNS))

    # ------------------------------------------------------------------ partitions table
    def _fill_parts(self):
        tv = self.tv_parts
        tv.delete(*tv.get_children())
        for idx, it in enumerate(self.items):
            tv.insert("", "end", iid=str(idx), values=(
                CHECK_ON if it.selected else CHECK_OFF, it.name, it.kind, it.size_text, it.origin),
                tags=() if it.selected else ("off",))
        self._update_start()

    def _toggle_row(self, event):
        if self._busy():
            return
        if getattr(event, "x", None) is not None and event.type == tk.EventType.ButtonRelease:
            if self.tv_parts.identify_region(event.x, event.y) != "cell":
                return
            iid = self.tv_parts.identify_row(event.y)
        else:
            iid = self.tv_parts.focus()
        if not iid:
            return
        it = self.items[int(iid)]
        it.selected = not it.selected
        self.tv_parts.set(iid, "sel", CHECK_ON if it.selected else CHECK_OFF)
        self.tv_parts.item(iid, tags=() if it.selected else ("off",))
        self._update_start()

    def _select(self, mode):
        if self._busy():
            return
        from .firmware import FS_KINDS, is_interesting
        for it in self.items:
            if mode == "all":
                it.selected = True
            elif mode == "none":
                it.selected = False
            else:
                it.selected = is_interesting(it.name) and (
                    it.kind in FS_KINDS or it.kind in ("logical", "payload", "raw", "f2fs")
                    or it.kind.startswith("new.dat"))
        self._fill_parts()

    def _update_start(self):
        n = sum(1 for it in self.items if it.selected)
        busy = self._busy()
        self.btn_start.configure(state="normal" if n and not busy else "disabled",
                                 text=f"Extract selected ({n})" if n else "Extract selected")
        self.btn_flash.configure(state="normal" if self.items and not busy else "disabled")

    # ------------------------------------------------------------------ worker plumbing
    def _busy(self):
        return self.worker is not None and self.worker.is_alive()

    def _run_bg(self, fn, done):
        self.job = GuiJob(self.q)
        self.btn_scan.configure(state="disabled")
        self.btn_start.configure(state="disabled")
        self.btn_flash.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.pbar.configure(value=0)

        def target():
            try:
                res = fn(self.job)
                self.q.put(("done", done, res, None))
            except Cancelled:
                self.q.put(("done", done, None, "cancelled"))
            except KitchenError as e:
                self.q.put(("done", done, None, str(e)))
            except Exception as e:
                tb = traceback.format_exc()
                self.q.put(("log", tb, "error"))
                self.q.put(("done", done, None, f"{type(e).__name__}: {e}"))

        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._log(msg[1], msg[2])
                elif kind == "progress":
                    _, cur, total, text = msg
                    self.pbar.configure(value=(1000 * cur / total) if total else 0)
                    pct = f"{100 * cur / total:.0f}%" if total else ""
                    self.v_status.set(f"{pct}  {text}"[:70])
                elif kind == "done":
                    _, cb, res, err = msg
                    self.worker = None
                    self.btn_scan.configure(state="normal")
                    self.btn_cancel.configure(state="disabled")
                    if err == "cancelled":
                        self._log("Cancelled by user.", "warn")
                        self.v_status.set("Cancelled")
                    elif err:
                        self._log(f"Error: {err}", "error")
                        self.v_status.set("Error")
                        messagebox.showerror(APP_NAME, err)
                    else:
                        cb(res)
                    self._update_start()
        except queue.Empty:
            pass
        self.after(80, self._poll)

    def _log(self, msg, level="info"):
        t = self.log_text
        t.configure(state="normal")
        stamp = time.strftime("%H:%M:%S")
        t.insert("end", f"{stamp}  {msg}\n", (level,) if level in LOG_COLORS else ())
        t.see("end")
        t.configure(state="disabled")

    def _cancel(self):
        if self.job:
            self.job.cancel()
            self.v_status.set("Cancelling...")

    # ------------------------------------------------------------------ actions
    def _check_paths(self, need_items=False):
        fw, ws = self.v_fw.get().strip(), self.v_ws.get().strip()
        if not fw or not os.path.exists(fw):
            messagebox.showwarning(APP_NAME, "Please choose an existing firmware file or folder.")
            return None
        if not ws:
            ws = self._default_ws(fw)
            self.v_ws.set(ws)
        return fw, ws

    def _scan(self):
        paths = self._check_paths()
        if not paths:
            return
        fw, ws = paths
        self._save_settings()
        self._log(f"Scanning {fw}", "head")
        self.items = []
        self._fill_parts()

        def work(job):
            os.makedirs(ws, exist_ok=True)
            sc = Scanner(ws, job)
            items = sc.scan(fw)
            return items, sc.ota_metadata

        def done(res):
            items, self.ota_meta = res
            self.items = items
            self.scanned_path = (fw, ws)
            self._fill_parts()
            self.nb.select(0)
            if not items:
                self._log("No partition images found in this firmware.", "warn")
                self.v_status.set("Nothing found")
                return
            sel = sum(1 for i in items if i.selected)
            self._log(f"Found {len(items)} partition images ({sel} pre-selected). "
                      "Adjust the selection and click 'Extract selected'.", "ok")
            self.v_status.set(f"Scan complete: {len(items)} partitions")
            self.pbar.configure(value=1000)

        self._run_bg(work, done)

    def _extract_options(self) -> ExtractOptions:
        pats = [l.strip() for l in self.txt_patterns.get("1.0", "end").splitlines() if l.strip()]
        return ExtractOptions(full=self.v_full.get(), apks=self.v_apks.get(),
                              apex=self.v_apex.get(), jars=self.v_jars.get(),
                              configs=self.v_configs.get(), metadata=self.v_meta.get(),
                              apk_info=self.v_apkinfo.get(), config_patterns=pats)

    def _start(self):
        sel = [it for it in self.items if it.selected]
        if not sel:
            return
        ws = self.v_ws.get().strip()
        if self.scanned_path and os.path.normcase(ws) != os.path.normcase(self.scanned_path[1]):
            if not messagebox.askyesno(APP_NAME, "The workspace changed since the scan. "
                                                 "Continue with the new workspace?"):
                return
        opts = self._extract_options()
        if not self.v_imgonly.get() and not (opts.apks or opts.apex or opts.jars or opts.configs
                                             or opts.full or opts.metadata):
            messagebox.showwarning(APP_NAME, "Nothing to extract - enable at least one option.")
            return
        self._save_settings()
        try:
            free = __import__("shutil").disk_usage(os.path.splitdrive(os.path.abspath(ws))[0] + "\\"
                                                   if sys.platform == "win32" else ws).free
            need = sum(it.size or 0 for it in sel)
            if need and free < need * 1.2:
                if not messagebox.askyesno(
                        APP_NAME, f"The selected partitions need about {human(need)} of temporary "
                                  f"space but only {human(free)} is free on the workspace drive.\n\n"
                                  "Continue anyway?"):
                    return
        except Exception:
            pass
        self._log(f"Extracting {len(sel)} partitions: {', '.join(i.name for i in sel)}", "head")
        self.apk_rows = []
        self._refresh_apks()
        keep, imgonly = self.v_keep.get(), self.v_imgonly.get()

        def work(job):
            os.makedirs(ws, exist_ok=True)
            return run(sel, ws, opts, job, keep_images=keep or imgonly, images_only=imgonly)

        def done(results):
            self.apk_rows = [a for r in results or [] for a in r.apks]
            self._refresh_apks()
            self.pbar.configure(value=1000)
            self.v_status.set("Done")
            if self.apk_rows:
                self.nb.select(2)
            errors = sum(len(r.errors) for r in results or [])
            msg = (f"Finished.\n\n{len(self.apk_rows)} APK/APEX/JAR files and "
                   f"{sum(r.configs for r in results or [])} config files extracted to:\n{ws}")
            if errors:
                msg += f"\n\n{errors} files could not be extracted (see log)."
            if messagebox.askyesno(APP_NAME, msg + "\n\nOpen the workspace folder?"):
                open_in_explorer(ws)

        self._run_bg(work, done)

    def _build_flash(self):
        if not self.items or self._busy():
            return
        ws = self.v_ws.get().strip()
        bad = [i.name for i in self.items if "delta" in i.kind]
        if bad:
            messagebox.showerror(APP_NAME, "This is an incremental (delta) OTA. A flash script "
                                           "needs a FULL firmware package.")
            return
        unpackable = [i for i in self.items if i.kind in ("payload", "logical")]
        if not unpackable:
            if not messagebox.askyesno(
                    APP_NAME, "This firmware has no payload.bin or super image. The script will "
                              "be built from the images found, but please double-check "
                              "flash_plan.txt before using it.\n\nContinue?"):
                return
        need = sum(i.size or 0 for i in self.items)
        dev = self.ota_meta.get("pre-device", "")
        build = self.ota_meta.get("post-build", "")
        msg = (f"All {len(self.items)} partitions will be unpacked into\n"
               f"{os.path.join(ws, 'images')}\n(about {human(need)}), and flash_all.bat will "
               "be written next to them.\n\n")
        if build or dev:
            msg += f"Build: {build or '?'}\nMade for: {dev or '?'}\n\n"
        msg += ("Before flashing: make sure this firmware matches your model/region and is "
                "NOT older than the firmware on the phone (anti-rollback).\n\nContinue?")
        if not messagebox.askyesno(APP_NAME, msg):
            return
        self._save_settings()
        items = list(self.items)
        both = self.v_bothslots.get()
        info = dict(self.ota_meta)
        self._log(f"Building flash script from {len(items)} partitions", "head")

        def work(job):
            os.makedirs(ws, exist_ok=True)
            run(items, ws, ExtractOptions(), job, images_only=True,
                flash={"both_slots": both, "info": info, "all_names": [i.name for i in items]})
            return os.path.join(ws, "flash_all.bat")

        def done(script):
            self.pbar.configure(value=1000)
            self.v_status.set("Flash script ready")
            if messagebox.askyesno(
                    APP_NAME, f"Flash script created:\n{script}\n\nPut the phone in "
                              "bootloader (fastboot) mode and run flash_all.bat. Run "
                              "'flash_all.bat --dry-run' first to preview every command.\n\n"
                              "See flash_plan.txt for the partition list.\n\n"
                              "Open the workspace folder?"):
                open_in_explorer(script, select=True)

        self._run_bg(work, done)

    # ------------------------------------------------------------------ APK table
    def _refresh_apks(self):
        tv = self.tv_apks
        tv.delete(*tv.get_children())
        flt = self.v_filter.get().lower().strip()
        key, rev = self._apk_sort
        keyf = {"part": lambda r: r["partition"], "package": lambda r: r.get("package", ""),
                "version": lambda r: str(r.get("versionName", "")),
                "sdk": lambda r: str(r.get("targetSdkVersion", "")),
                "priv": lambda r: r["privileged"], "size": lambda r: r["size"],
                "path": lambda r: r["path"]}[key]
        rows = sorted(self.apk_rows, key=keyf, reverse=rev)
        shown = 0
        for idx, r in enumerate(rows):
            if flt and flt not in (r["path"] + " " + str(r.get("package", ""))).lower():
                continue
            ver = str(r.get("versionName", ""))
            if r.get("versionCode") not in (None, ""):
                ver += f" ({r['versionCode']})"
            sdk = f"{r.get('minSdkVersion', '')}/{r.get('targetSdkVersion', '')}".strip("/")
            tv.insert("", "end", iid=f"a{idx}", values=(
                r["partition"], r.get("package", "") or r.get("error", "")[:40], ver, sdk,
                "yes" if r["privileged"] else "", human(r["size"]), r["path"]),
                tags=(r["file"],))
            shown += 1
        total = len(self.apk_rows)
        self.lbl_apkcount.configure(text=f"{shown} of {total} shown" if total else "")

    def _sort_apks(self, col):
        key, rev = self._apk_sort
        self._apk_sort = (col, not rev if key == col else False)
        self._refresh_apks()

    def _apk_open(self, _event):
        iid = self.tv_apks.focus()
        if iid:
            tags = self.tv_apks.item(iid, "tags")
            if tags:
                open_in_explorer(tags[0], select=True)

    def _open_csv(self):
        p = os.path.join(self.v_ws.get(), "apk_list.csv")
        if os.path.exists(p):
            open_in_explorer(p)
        else:
            messagebox.showinfo(APP_NAME, "No APK list yet - run an extraction first.")

    # ------------------------------------------------------------------ misc
    def _show_formats(self):
        messagebox.showinfo(APP_NAME, (
            "Firmware packages\n"
            "  - Samsung: AP_*.tar.md5 / *.tar (+ .lz4 images)\n"
            "  - Xiaomi / Redmi fastboot ROM: .tgz / .tar.gz\n"
            "  - A/B OTA & recovery ROMs: .zip with payload.bin (full OTA)\n"
            "  - Older OTA ROMs: .zip with *.new.dat(.br) + *.transfer.list\n"
            "  - Pixel factory images: .zip (nested image-*.zip)\n"
            "  - Motorola: *_sparsechunk.N\n"
            "  - Any extracted folder with .img files\n\n"
            "Images: sparse, raw, lz4, super.img (dynamic partitions)\n"
            "Filesystems: ext4, EROFS (lz4 / lzma / deflate / zstd)\n\n"
            "Not supported: incremental OTAs, F2FS, Huawei UPDATE.APP, encrypted firmware."))

    def _about(self):
        messagebox.showinfo(APP_NAME, (
            f"{APP_NAME} {__version__}\n\n"
            "Unpacks stock Android firmware on Windows and extracts APKs, config files\n"
            "and filesystem metadata (fs_config / file_contexts).\n\n"
            "Pure Python - no WSL, Cygwin or root needed.\n"
            "Inspired by ravindu644/Android_Image_Tools."))

    def _on_close(self):
        if self._busy():
            if not messagebox.askyesno(APP_NAME, "A job is running. Cancel it and exit?"):
                return
            if self.job:
                self.job.cancel()
        self._save_settings()
        self.destroy()


def main():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    app = App()
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        app._set_fw(sys.argv[1])
    app.mainloop()


if __name__ == "__main__":
    main()
