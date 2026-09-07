"""Windows desktop client for a locally indexed, shared NAS media library."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.font import Font
import uuid

from PIL import Image, ImageTk

from library import LibraryService
from media import _ffmpeg
from player import MediaPlayer, PlayerError


APP_NAME = "素材协作"
APP_VERSION = "0.3.0"
DEFAULT_ROOT = r"\\SmartStorage\新媒体-137964276\素材库"
DEFAULT_SYNC_ROOT = r"\\SmartStorage\新媒体-137964276\素材协作数据"
DEFAULT_PUBLISH_ROOT = r"\\SmartStorage\新媒体-137964276\软件库\素材协作"
CONFIG_PATH = Path(os.getenv("APPDATA", str(Path.home()))) / APP_NAME / "settings.json"
LOCAL_DATA = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / APP_NAME
PAGE_SIZE = 24
TYPE_NAMES = {"video": "视频", "image": "图片", "audio": "音频"}
STATUS_NAMES = {"pending": "待生成", "ready": "已缓存", "failed": "生成失败", "skipped": "无画面"}
JOB_NAMES = {"scan": "素材扫描", "import": "索引文件", "thumbnails": "批量缩略图"}
JOB_STATUS = {"running": "处理中", "paused": "已暂停", "partial": "部分完成", "failed": "失败", "done": "已完成"}
BG = "#f6f7f8"
FG = "#25292d"
MUTED = "#687078"
ACCENT = "#107c69"


def readable_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return ""


def short_time(value: str) -> str:
    if not value:
        return "尚未同步"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%m-%d %H:%M:%S")
    except ValueError:
        return value


def load_settings(path: Path) -> dict:
    defaults = dict(nas_root=DEFAULT_ROOT, sync_root=DEFAULT_SYNC_ROOT, publish_root=DEFAULT_PUBLISH_ROOT,
                    nas_user="nas", auto_sync=True, device_id="node-" + uuid.uuid4().hex[:16])
    try:
        previous = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(previous, dict):
            defaults.update(previous)
    except (OSError, ValueError):
        pass
    if defaults.get("device_id") in {"", "device-001", None} or not re.fullmatch(r"[A-Za-z0-9_-]{3,80}", str(defaults.get("device_id", ""))):
        defaults["device_id"] = "node-" + uuid.uuid4().hex[:16]
    return defaults


def store_settings(path: Path, settings: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(settings, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Workspace(tk.Tk):
    def __init__(self, data_dir: Path | None = None, auto_sync: bool = True):
        super().__init__()
        self.title(f"{APP_NAME}  {APP_VERSION}")
        self.geometry("1360x850")
        self.minsize(1024, 700)
        self.configure(bg=BG)
        self.data_dir = Path(data_dir) if data_dir else LOCAL_DATA
        self.config_path = self.data_dir / "settings.json" if data_dir else CONFIG_PATH
        self.settings = load_settings(self.config_path)
        store_settings(self.config_path, self.settings)
        self.service = LibraryService(self.data_dir, self.settings["nas_root"], self.settings["sync_root"], self.settings["device_id"])
        self.messages: queue.Queue = queue.Queue()
        self.thumb_queue: queue.Queue = queue.Queue(maxsize=64)
        self.thumb_pending: set[tuple[str, str]] = set()
        self.thumb_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.player = MediaPlayer()
        self.player_job = None
        self.scan_stop = threading.Event()
        self.batch_stop = threading.Event()
        self.scanning = self.syncing = self.batching = False
        self.active_page = ""
        self.records: list[dict] = []
        self.total = 0
        self.page_number = 0
        self.query_generation = 0
        self.selected_id = ""
        self.view_mode = "grid"
        self.columns = 0
        self.card_width = 230
        self.image_refs = {}
        self.thumb_widgets = {}
        self.card_widgets = {}
        self.card_shapes = {}
        self._reload_after = self._resize_after = self._search_after = None
        self._status = tk.StringVar(value="本地索引就绪")
        self._scan_status = tk.StringVar(value="尚未扫描")
        self._sync_status = tk.StringVar(value="同步待检查")
        self._node_status = tk.StringVar(value=self.settings["device_id"])
        self._build_style()
        self.card_font = Font(self, family="Microsoft YaHei UI", size=9, weight="bold")
        self.preview_font = Font(self, family="Microsoft YaHei UI", size=12, weight="bold")
        self._build_shell()
        for _ in range(2):
            threading.Thread(target=self._thumbnail_loop, daemon=True, name="thumbnail-ui").start()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.show_page("素材库")
        self.after(60, self._drain_messages)
        self.auto_sync_allowed = auto_sync
        self.after(1200, self._auto_sync)

    def _build_style(self):
        self.option_add("*Font", ("Microsoft YaHei UI", 10))
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("Muted.TLabel", foreground=MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("TButton", padding=(12, 7), background="#ffffff", foreground=FG, borderwidth=1)
        style.map("TButton", background=[("active", "#e8efec"), ("disabled", "#ededed")])
        style.configure("Primary.TButton", background=ACCENT, foreground="white", borderwidth=0)
        style.map("Primary.TButton", background=[("active", "#086452"), ("disabled", "#c2cdc8")])
        style.configure("Selected.TButton", background="#dfeee8", foreground=ACCENT)
        style.configure("TEntry", padding=7)
        style.configure("Treeview", rowheight=46, background="white", fieldbackground="white", foreground=FG, borderwidth=0)
        style.configure("Treeview.Heading", padding=(8, 9), background="#edf0f2", font=("Microsoft YaHei UI", 9, "bold"))
        style.map("Treeview", background=[("selected", "#dcefe7")], foreground=[("selected", "#125f50")])
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor="#e4e9e7", borderwidth=0)

    def _build_shell(self):
        sidebar = tk.Frame(self, bg="#242729", width=184)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text=APP_NAME, bg="#242729", fg="white", font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w", padx=20, pady=(30, 28))
        self.nav = {}
        for name in ("素材库", "任务中心", "工单", "脚本", "设置"):
            button = tk.Button(sidebar, text=name, anchor="w", bg="#242729", fg="#d2d7da", activebackground="#38413d",
                               activeforeground="white", bd=0, padx=18, pady=13, command=lambda page=name: self.show_page(page))
            button.pack(fill="x", padx=8, pady=3)
            self.nav[name] = button
        tk.Frame(sidebar, bg="#242729").pack(fill="both", expand=True)
        tk.Label(sidebar, textvariable=self._node_status, bg="#242729", fg="#aeb7b5", wraplength=155, font=("Microsoft YaHei UI", 8), anchor="w").pack(fill="x", padx=16)
        tk.Label(sidebar, text=f"v{APP_VERSION}", bg="#242729", fg="#aeb7b5", anchor="w", font=("Microsoft YaHei UI", 9)).pack(fill="x", padx=16, pady=(7, 20))
        self.content = ttk.Frame(self)
        self.content.pack(side="left", fill="both", expand=True)
        status = ttk.Frame(self.content)
        status.pack(side="bottom", fill="x", padx=20, pady=12)
        status.columnconfigure(0, weight=1)
        message = ttk.Label(status, textvariable=self._status, style="Muted.TLabel", wraplength=500)
        message.grid(row=0, column=0, sticky="ew", padx=(0, 12))
        message.bind("<Configure>", lambda event: message.configure(wraplength=max(100, event.width)))
        ttk.Label(status, textvariable=self._sync_status, style="Muted.TLabel").grid(row=0, column=1, sticky="e")
        self.page = ttk.Frame(self.content)
        self.page.pack(fill="both", expand=True, padx=20, pady=(24, 0))
        self.bind_all("<MouseWheel>", self._mousewheel)
        self.bind("<Control-c>", lambda _event: self._copy_path() if self.active_page == "素材库" else None)

    def show_page(self, name: str):
        self.active_page = name
        self.query_generation += 1
        self.image_refs.clear()
        self.thumb_widgets.clear()
        self.card_widgets.clear()
        for child in self.page.winfo_children():
            child.destroy()
        for page, button in self.nav.items():
            button.configure(bg="#35463d" if page == name else "#242729", fg="white" if page == name else "#d2d7da")
        if name == "素材库":
            self._assets_page()
        elif name == "任务中心":
            self._tasks_page()
        elif name == "设置":
            self._settings_page()
        else:
            ttk.Label(self.page, text=name, style="Title.TLabel").pack(anchor="w", pady=(0, 20))
            ttk.Label(self.page, text="暂无记录", style="Muted.TLabel").pack(anchor="center", pady=80)
        self._request_overview()

    def _assets_page(self):
        titlebar = ttk.Frame(self.page)
        titlebar.pack(fill="x")
        ttk.Label(titlebar, text="素材库", style="Title.TLabel").pack(side="left")
        self.count_label = ttk.Label(titlebar, text="读取本地索引...", style="Muted.TLabel")
        self.count_label.pack(side="left", padx=16)
        ttk.Label(self.page, text=self.settings["nas_root"], style="Muted.TLabel", wraplength=750).pack(anchor="w", pady=(6, 14))
        searchbar = ttk.Frame(self.page)
        searchbar.pack(fill="x", pady=(0, 10))
        self.search_var = tk.StringVar()
        entry = ttk.Entry(searchbar, textvariable=self.search_var, width=28)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda _event: self._request_page(reset=True))
        self.search_var.trace_add("write", self._search_changed)
        ttk.Button(searchbar, text="搜索", command=lambda: self._request_page(reset=True)).pack(side="left", padx=(8, 12))
        self.type_var = tk.StringVar(value="全部")
        types = ttk.Combobox(searchbar, textvariable=self.type_var, values=("全部", "视频", "图片", "音频"), state="readonly", width=7)
        types.pack(side="left", padx=(0, 12))
        types.bind("<<ComboboxSelected>>", lambda _event: self._request_page(reset=True))
        self.grid_button = ttk.Button(searchbar, text="▦", width=3, command=lambda: self._set_view("grid"))
        self.grid_button.pack(side="left")
        self.list_button = ttk.Button(searchbar, text="☰", width=3, command=lambda: self._set_view("list"))
        self.list_button.pack(side="left", padx=(4, 0))
        self._tooltip(self.grid_button, "缩略图网格")
        self._tooltip(self.list_button, "详细列表")
        toolbar = ttk.Frame(self.page)
        toolbar.pack(fill="x", pady=(0, 12))
        ttk.Button(toolbar, text="扫描 NAS", style="Primary.TButton", command=self._scan_nas).pack(side="left")
        self.pause_button = ttk.Button(toolbar, text="暂停扫描", command=self._pause_scan)
        self.pause_button.pack(side="left", padx=6)
        self.pause_button.configure(state="normal" if self.scanning else "disabled")
        ttk.Button(toolbar, text="索引文件", command=self._import_files).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="批量缩略图", command=self._batch_thumbnails).pack(side="left")
        ttk.Button(toolbar, text="立即同步", command=self._sync).pack(side="right")
        ttk.Label(self.page, textvariable=self._scan_status, style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
        work = ttk.Frame(self.page)
        work.pack(fill="both", expand=True)
        preview = tk.Frame(work, width=252, bg=BG)
        preview.pack(side="right", fill="y", padx=(16, 0))
        preview.pack_propagate(False)
        self.preview_image_label = tk.Label(preview, bg="#e7ecea", fg=MUTED, width=240, height=135, compound="center")
        self.preview_image_label.pack(fill="x", pady=(0, 14))
        player_bar = ttk.Frame(preview)
        player_bar.pack(fill="x", pady=(0, 10))
        self.play_button = ttk.Button(player_bar, text="▶ 播放", command=self._toggle_player)
        self.play_button.pack(side="left")
        self.stop_button = ttk.Button(player_bar, text="■", width=3, command=self._stop_player)
        self.stop_button.pack(side="left", padx=4)
        self.fullscreen_button = ttk.Button(player_bar, text="全屏", command=self._fullscreen_player)
        self.fullscreen_button.pack(side="right")
        self.player_volume = tk.IntVar(value=80)
        ttk.Scale(preview, from_=0, to=100, variable=self.player_volume, command=self._set_player_volume).pack(fill="x", pady=(0, 10))
        self.player_hint = ttk.Label(preview, text="选择视频或音频后播放", style="Muted.TLabel")
        self.player_hint.pack(anchor="w", pady=(0, 10))
        preview_actions = ttk.Frame(preview)
        preview_actions.pack(fill="x", pady=(0, 12))
        for symbol, tooltip, command in (("↗", "打开素材", self._open_asset), ("⧉", "复制剪辑路径", self._copy_path), ("↻", "重新生成缩略图", self._retry_thumbnail)):
            button = ttk.Button(preview_actions, text=symbol, width=4, command=command)
            button.pack(side="left", padx=(0, 6))
            self._tooltip(button, tooltip)
        self.preview_title = ttk.Label(preview, text="尚未选择素材", wraplength=248, font=("Microsoft YaHei UI", 12, "bold"))
        self.preview_title.pack(anchor="w", fill="x", pady=(0, 12))
        self.preview_meta = ttk.Label(preview, text="", wraplength=245, justify="left", style="Muted.TLabel")
        self.preview_meta.pack(anchor="w", fill="x", pady=(0, 12))
        self.library_surface = ttk.Frame(work)
        self.library_surface.pack(side="left", fill="both", expand=True)
        self.asset_canvas = tk.Canvas(self.library_surface, bg=BG, highlightthickness=0)
        self.asset_scrollbar = ttk.Scrollbar(self.library_surface, orient="vertical", command=self.asset_canvas.yview)
        self.asset_canvas.configure(yscrollcommand=self.asset_scrollbar.set)
        self.asset_scrollbar.pack(side="right", fill="y")
        self.asset_horizontal = ttk.Scrollbar(self.library_surface, orient="horizontal")
        self.asset_canvas.pack(fill="both", expand=True)
        self.asset_grid = ttk.Frame(self.asset_canvas)
        self.asset_window = self.asset_canvas.create_window((0, 0), window=self.asset_grid, anchor="nw")
        self.asset_grid.bind("<Configure>", lambda _event: self.asset_canvas.configure(scrollregion=self.asset_canvas.bbox("all")))
        self.asset_canvas.bind("<Configure>", self._canvas_resize)
        self.asset_tree = ttk.Treeview(self.library_surface, columns=("name", "type", "size", "status"), show="tree headings", selectmode="browse")
        self.asset_tree.configure(yscrollcommand=self.asset_scrollbar.set, xscrollcommand=self.asset_horizontal.set)
        self.asset_horizontal.configure(command=self.asset_tree.xview)
        self.asset_tree.heading("#0", text="")
        self.asset_tree.column("#0", width=72, minwidth=72, stretch=False)
        for name, label, width in (("name", "名称", 250), ("type", "类型", 60), ("size", "大小", 85), ("status", "缩略图", 80)):
            self.asset_tree.heading(name, text=label)
            self.asset_tree.column(name, width=width, minwidth=width if name != "name" else 140, stretch=name == "name")
        self.asset_tree.bind("<<TreeviewSelect>>", self._list_selection)
        self.asset_tree.bind("<Double-1>", lambda _event: self._open_asset())
        bottom = ttk.Frame(self.page)
        bottom.pack(fill="x", pady=12)
        self.previous_button = ttk.Button(bottom, text="上一页", command=lambda: self._turn_page(-1))
        self.previous_button.pack(side="left")
        self.next_button = ttk.Button(bottom, text="下一页", command=lambda: self._turn_page(1))
        self.next_button.pack(side="left", padx=6)
        self.page_label = ttk.Label(bottom, text="", style="Muted.TLabel")
        self.page_label.pack(side="left", padx=10)
        self._empty_preview()
        self._set_view(self.view_mode)
        self._request_page()

    def _tooltip(self, widget, text):
        popup = []
        def leave(_event=None):
            if popup:
                popup.pop().destroy()
        def enter(_event=None):
            leave()
            tip = tk.Toplevel(self)
            tip.wm_overrideredirect(True)
            tip.geometry(f"+{widget.winfo_rootx()}+{widget.winfo_rooty() + widget.winfo_height() + 4}")
            tk.Label(tip, text=text() if callable(text) else text, bg="#303632", fg="white", padx=8, pady=4, wraplength=480).pack()
            popup.append(tip)
        widget.bind("<Enter>", enter)
        widget.bind("<Leave>", leave)
        widget.bind("<Destroy>", leave)

    def _fit_text(self, text, width, lines, font):
        result, line = [], ""
        for index, character in enumerate(text):
            if font.measure(line + character) > width and line:
                result.append(line)
                line = ""
                if len(result) == lines:
                    last = result[-1]
                    while last and font.measure(last + "…") > width:
                        last = last[:-1]
                    result[-1] = last + "…"
                    return "\n".join(result)
            line += character
        if line:
            result.append(line)
        return "\n".join(result)

    def _placeholder(self, width, height, kind):
        color = {"video": "#e2eae7", "image": "#e8eaf0", "audio": "#ece7de"}.get(kind, "#e9ebed")
        return ImageTk.PhotoImage(Image.new("RGB", (width, height), color), master=self)

    def _empty_preview(self):
        image = self._placeholder(248, 140, "")
        self.image_refs["preview"] = image
        self.preview_image_label.configure(image=image, text="", width=248, height=140)

    def _mousewheel(self, event):
        if self.active_page == "素材库" and self.view_mode == "grid" and self.asset_canvas.winfo_exists():
            self.asset_canvas.yview_scroll(int(-event.delta / 120), "units")

    def _canvas_resize(self, event):
        if self.active_page != "素材库":
            return
        self.asset_canvas.itemconfigure(self.asset_window, width=event.width)
        columns = max(1, event.width // 226)
        width = max(170, event.width // columns - 12)
        if columns != self.columns or abs(width - self.card_width) > 8:
            self.columns, self.card_width = columns, width
            if self._resize_after:
                self.after_cancel(self._resize_after)
            self._resize_after = self.after(120, self._render_current)

    def _set_view(self, mode):
        self.view_mode = mode
        if self.active_page != "素材库":
            return
        self.grid_button.configure(style="Selected.TButton" if mode == "grid" else "TButton")
        self.list_button.configure(style="Selected.TButton" if mode == "list" else "TButton")
        if mode == "grid":
            self.asset_tree.pack_forget()
            self.asset_horizontal.pack_forget()
            self.asset_scrollbar.configure(command=self.asset_canvas.yview)
            self.asset_canvas.pack(fill="both", expand=True)
        else:
            self.asset_canvas.pack_forget()
            self.asset_scrollbar.configure(command=self.asset_tree.yview)
            self.asset_horizontal.pack(side="bottom", fill="x")
            self.asset_tree.pack(fill="both", expand=True)
        self._render_current()

    def _render_current(self):
        self._resize_after = None
        if self.active_page != "素材库":
            return
        self.image_refs.clear()
        self.card_widgets.clear()
        self.thumb_widgets.clear()
        self.card_shapes.clear()
        for child in self.asset_grid.winfo_children():
            child.destroy()
        self.asset_tree.delete(*self.asset_tree.get_children())
        if self.selected_id not in {item["asset_id"] for item in self.records}:
            self.selected_id = ""
            self.preview_title.configure(text="尚未选择素材")
            self.preview_meta.configure(text="")
        self._empty_preview()
        if not self.records:
            ttk.Label(self.asset_grid, text="暂无素材", style="Muted.TLabel").pack(pady=70)
        for index, item in enumerate(self.records):
            asset_id = item["asset_id"]
            if self.view_mode == "list":
                self.asset_tree.insert("", "end", iid=asset_id, values=(item["name"], TYPE_NAMES.get(item["media_type"], "文件"), readable_size(item["size"]), STATUS_NAMES.get(item["thumbnail_status"], "")))
                self.card_shapes[asset_id] = (64, 36)
            else:
                width = self.card_width
                height = round(width * 9 / 16)
                frame = tk.Frame(self.asset_grid, bg="white", width=width, height=height + 94,
                                 highlightthickness=1, highlightbackground=ACCENT if asset_id == self.selected_id else "#dde2df", cursor="hand2")
                frame.grid(row=index // max(self.columns, 1), column=index % max(self.columns, 1), padx=5, pady=6, sticky="n")
                frame.pack_propagate(False)
                image = self._placeholder(width - 2, height, item["media_type"])
                self.image_refs[asset_id] = image
                label = tk.Label(frame, image=image, width=width - 2, height=height, bd=0, compound="center", text=TYPE_NAMES.get(item["media_type"], "文件"), fg=MUTED)
                label.pack()
                title = tk.Label(frame, text=self._fit_text(item["name"], width - 20, 2, self.card_font), bg="white", fg=FG, anchor="w", justify="left", font=self.card_font)
                title.pack(fill="x", padx=10, pady=(7, 2))
                self._tooltip(title, item["relative_path"])
                meta = tk.Label(frame, text=f"{TYPE_NAMES.get(item['media_type'], '文件')}   {readable_size(item['size'])}", bg="white", fg=MUTED, anchor="w", font=("Microsoft YaHei UI", 8))
                meta.pack(fill="x", padx=10, pady=2)
                for widget in (frame, label, title, meta):
                    widget.bind("<Button-1>", lambda _event, identity=asset_id: self._select(identity))
                    widget.bind("<Double-1>", lambda _event, identity=asset_id: self._open_asset(identity))
                self.thumb_widgets[asset_id] = label
                self.card_widgets[asset_id] = frame
                self.card_shapes[asset_id] = (width - 2, height)
            if item.get("thumbnail"):
                self._show_thumbnail(item, item["thumbnail"])
            elif item["media_type"] in {"video", "image"} and item["thumbnail_status"] == "pending":
                self._enqueue_thumbnail(item)
        if self.selected_id:
            self._select(self.selected_id)
        pages = max(1, math.ceil(self.total / PAGE_SIZE))
        self.count_label.configure(text=f"{self.total:,} 个素材")
        self.page_label.configure(text=f"{self.page_number + 1} / {pages} 页")
        self.previous_button.configure(state="normal" if self.page_number else "disabled")
        self.next_button.configure(state="normal" if self.page_number + 1 < pages else "disabled")

    def _show_thumbnail(self, item, path):
        identity = item["asset_id"]
        if identity not in self.card_shapes:
            return
        try:
            with Image.open(path) as source:
                image = ImageTk.PhotoImage(source.convert("RGB").resize(self.card_shapes[identity], Image.Resampling.LANCZOS), master=self)
        except (OSError, ValueError, tk.TclError):
            self._enqueue_thumbnail(item)
            return
        self.image_refs[identity] = image
        if self.view_mode == "list":
            if self.asset_tree.exists(identity):
                self.asset_tree.item(identity, image=image)
        elif identity in self.thumb_widgets:
            self.thumb_widgets[identity].configure(image=image, text="")
        if identity == self.selected_id:
            self._preview_thumbnail(path)

    def _preview_thumbnail(self, path):
        try:
            with Image.open(path) as source:
                image = ImageTk.PhotoImage(source.convert("RGB").resize((248, 140), Image.Resampling.LANCZOS), master=self)
            self.image_refs["preview"] = image
            self.preview_image_label.configure(image=image, text="")
        except (OSError, ValueError, tk.TclError):
            self._empty_preview()

    def _enqueue_thumbnail(self, item, force=False):
        if item["media_type"] not in {"image", "video"}:
            return
        key = (item["asset_id"], item["file_hash"])
        with self.thumb_lock:
            if key in self.thumb_pending:
                return
            try:
                self.thumb_queue.put_nowait((self.service, key))
                self.thumb_pending.add(key)
            except queue.Full:
                pass

    def _thumbnail_loop(self):
        while not self.stop_event.is_set():
            try:
                service, key = self.thumb_queue.get(timeout=0.3)
            except queue.Empty:
                continue
            try:
                result = service.thumbnail(*key)
                self.messages.put(("thumbnail", (service, result)))
            except Exception as exc:
                self.messages.put(("error", str(exc)))
            finally:
                with self.thumb_lock:
                    self.thumb_pending.discard(key)
                self.thumb_queue.task_done()

    def _select(self, identity):
        item = next((item for item in self.records if item["asset_id"] == identity), None)
        if not item:
            return
        self.selected_id = identity
        self._stop_player()
        for key, card in self.card_widgets.items():
            card.configure(highlightbackground=ACCENT if key == identity else "#dde2df")
        self.preview_title.configure(text=self._fit_text(item["name"], 240, 2, self.preview_font))
        self._tooltip(self.preview_title, item["relative_path"])
        timestamp = datetime.fromtimestamp(item["mtime_ns"] / 1_000_000_000).strftime("%Y-%m-%d %H:%M")
        self.preview_meta.configure(text=f"{TYPE_NAMES.get(item['media_type'], '文件')}  ·  {readable_size(item['size'])}\n\n{timestamp}\n\n{item['relative_path']}\n\n缩略图：{STATUS_NAMES.get(item['thumbnail_status'], '')}")
        if item.get("thumbnail"):
            self._preview_thumbnail(item["thumbnail"])
        else:
            self._empty_preview()

    def _list_selection(self, _event):
        if self.active_page == "素材库" and self.asset_tree.selection():
            self._select(self.asset_tree.selection()[0])

    def _selected(self):
        return next((item for item in self.records if item["asset_id"] == self.selected_id), None)

    def _copy_path(self):
        item = self._selected()
        if item:
            self.clipboard_clear()
            self.clipboard_append(item["path"])
            self._status.set("已复制：" + item["name"])

    def _open_asset(self, identity=None):
        if identity:
            self._select(identity)
        item = self._selected()
        if not item:
            return
        def open_file():
            if not Path(item["path"]).is_file():
                raise FileNotFoundError("素材不可访问：" + item["path"])
            os.startfile(item["path"])
            return item["name"]
        self._background("opened", open_file)

    def _toggle_player(self):
        item = self._selected()
        if not item or item["media_type"] not in {"video", "audio"}:
            self._status.set("请选择视频或音频素材")
            return
        if self.player.playing:
            self.player.pause()
            self.play_button.configure(text="▶ 播放")
            return
        try:
            self.player.open(item["path"])
            self.play_button.configure(text="Ⅱ 暂停")
            self.player_hint.configure(text=f"正在播放：{item['name']}")
            self.after(500, self._player_tick)
        except PlayerError as exc:
            self._status.set(str(exc))

    def _player_tick(self):
        if self.player.playing:
            self.after(500, self._player_tick)
        else:
            self.play_button.configure(text="▶ 播放")

    def _stop_player(self):
        self.player.stop()
        if hasattr(self, "play_button"):
            self.play_button.configure(text="▶ 播放")

    def _set_player_volume(self, value):
        try:
            self.player.set_volume(int(float(value)))
        except (TypeError, ValueError):
            pass

    def _fullscreen_player(self):
        item = self._selected()
        if item and item["media_type"] in {"video", "audio"}:
            self.player.open(item["path"])
            self._status.set("播放器已打开，可使用 ffplay 全屏快捷键")

    def _retry_thumbnail(self):
        item = self._selected()
        if item:
            self._enqueue_thumbnail(item, force=True)

    def _search_changed(self, *_):
        if self._search_after:
            self.after_cancel(self._search_after)
        self._search_after = self.after(300, lambda: self._request_page(reset=True))

    def _request_page(self, reset=False):
        self._search_after = None
        if self.active_page != "素材库":
            return
        if reset:
            self.page_number = 0
        self.query_generation += 1
        generation = self.query_generation
        query = self.search_var.get().strip()
        kind = next((key for key, value in TYPE_NAMES.items() if value == self.type_var.get()), None)
        offset = self.page_number * PAGE_SIZE
        service = self.service
        self._background("page", lambda: (generation, service.page(query, kind, offset, PAGE_SIZE)))

    def _turn_page(self, difference):
        self.page_number = max(0, self.page_number + difference)
        self._request_page()
        self.asset_canvas.yview_moveto(0)

    def _schedule_reload(self):
        if self._reload_after is None:
            self._reload_after = self.after(1200, self._reload)

    def _reload(self):
        self._reload_after = None
        self._request_page()
        self._request_overview()

    def _scan_nas(self):
        if self.scanning:
            self._status.set("扫描已在运行")
            return
        self.scanning = True
        self.scan_stop = threading.Event()
        self._scan_status.set("正在扫描...")
        if self.active_page == "素材库":
            self.pause_button.configure(state="normal")
        service = self.service
        self._background("scan_done", lambda: service.scan(self.scan_stop, lambda value: self.messages.put(("progress", value))))

    def _pause_scan(self):
        self.scan_stop.set()
        self._scan_status.set("正在保存扫描结果...")

    def _import_files(self):
        paths = filedialog.askopenfilenames(title="选择素材根目录内的文件", initialdir=self.settings["nas_root"])
        if paths:
            service = self.service
            self._background("import_done", lambda: service.import_files(list(paths)))

    def _batch_thumbnails(self):
        if self.batching:
            self.batch_stop.set()
            self._status.set("正在暂停缩略图批处理...")
            return
        self.batching = True
        self.batch_stop = threading.Event()
        service = self.service
        self._status.set("缩略图批处理已开始")
        self._background("batch_done", lambda: service.batch_thumbnails(self.batch_stop, lambda value: self.messages.put(("progress", value))))

    def _sync(self):
        if self.syncing:
            return
        self.syncing = True
        self._sync_status.set("正在同步")
        service = self.service
        self._background("sync_done", lambda: service.sync_once(self.stop_event))

    def _auto_sync(self):
        if self.stop_event.is_set():
            return
        if self.auto_sync_allowed and self.settings.get("auto_sync", True):
            self._sync()
        self.after(30000, self._auto_sync)

    def _request_overview(self):
        service = self.service
        self._background("overview", service.overview)

    def _background(self, kind, operation):
        def run():
            try:
                self.messages.put((kind, operation()))
            except Exception as exc:
                self.messages.put(("operation_error", (kind, str(exc))))
        threading.Thread(target=run, daemon=True, name=kind).start()

    def _tasks_page(self):
        ttk.Label(self.page, text="任务中心", style="Title.TLabel").pack(anchor="w", pady=(0, 18))
        bar = ttk.Frame(self.page)
        bar.pack(fill="x", pady=(0, 14))
        ttk.Button(bar, text="扫描 / 继续", command=self._scan_nas).pack(side="left")
        ttk.Button(bar, text="暂停扫描", command=self._pause_scan).pack(side="left", padx=6)
        ttk.Button(bar, text="批量缩略图 / 暂停", command=self._batch_thumbnails).pack(side="left")
        ttk.Button(bar, text="立即同步", command=self._sync).pack(side="right")
        ttk.Label(self.page, textvariable=self._scan_status, style="Muted.TLabel").pack(anchor="w", pady=(0, 12))
        self.jobs_tree = ttk.Treeview(self.page, columns=("kind", "status", "done", "changed", "errors", "updated"), show="headings")
        for name, label, width in (("kind", "任务", 150), ("status", "状态", 90), ("done", "已处理", 85), ("changed", "新增 / 更新", 100), ("errors", "错误", 75), ("updated", "更新时间", 140)):
            self.jobs_tree.heading(name, text=label)
            self.jobs_tree.column(name, width=width, minwidth=75)
        self.jobs_tree.pack(fill="both", expand=True)
        self.job_details = ttk.Label(self.page, text="", style="Muted.TLabel", wraplength=780)
        self.job_details.pack(fill="x", pady=14)
        self.jobs_tree.bind("<<TreeviewSelect>>", self._job_selected)
        self.jobs = {}

    def _job_selected(self, _event):
        if self.jobs_tree.selection():
            item = self.jobs.get(self.jobs_tree.selection()[0])
            if item:
                self.job_details.configure(text=item["message"])

    def _settings_page(self):
        ttk.Label(self.page, text="设置", style="Title.TLabel").pack(anchor="w", pady=(0, 22))
        self.setting_vars = {}
        form = ttk.Frame(self.page)
        form.pack(fill="x")
        for i, (key, label) in enumerate((("nas_root", "素材根目录"), ("sync_root", "协作数据目录"), ("publish_root", "软件发布目录"), ("device_id", "本机节点 ID"))):
            ttk.Label(form, text=label).grid(row=i * 2, column=0, sticky="w", pady=(12, 4))
            variable = tk.StringVar(value=self.settings.get(key, ""))
            self.setting_vars[key] = variable
            ttk.Entry(form, textvariable=variable, width=72).grid(row=i * 2 + 1, column=0, sticky="ew", ipady=3)
            if key != "device_id":
                ttk.Button(form, text="浏览", command=lambda value=variable: self._browse_directory(value)).grid(row=i * 2 + 1, column=1, padx=(8, 0))
        form.columnconfigure(0, weight=1)
        self.autosync_var = tk.BooleanVar(value=self.settings.get("auto_sync", True))
        ttk.Checkbutton(self.page, text="自动同步", variable=self.autosync_var).pack(anchor="w", pady=22)
        commands = ttk.Frame(self.page)
        commands.pack(fill="x")
        ttk.Button(commands, text="保存设置", style="Primary.TButton", command=self._save_settings).pack(side="left")
        ttk.Button(commands, text="测试连接", command=self._test_nas).pack(side="left", padx=8)
        ttk.Button(commands, text="检查更新", command=self._check_for_updates).pack(side="left")
        ttk.Label(self.page, text=f"本地索引：{self.service.db_path}", style="Muted.TLabel", wraplength=700).pack(anchor="w", pady=(25, 8))
        ttk.Label(self.page, text=f"当前版本：{APP_VERSION}", style="Muted.TLabel").pack(anchor="w")

    def _browse_directory(self, variable):
        path = filedialog.askdirectory(title="选择目录", initialdir=variable.get())
        if path:
            variable.set(path)

    def _save_settings(self):
        if self.scanning or self.syncing or self.batching:
            messagebox.showinfo(APP_NAME, "请先等待任务结束或暂停扫描、缩略图任务")
            return
        updated = dict(self.settings)
        updated.update({key: variable.get().strip() for key, variable in self.setting_vars.items()})
        if any(not updated[key] for key in ("nas_root", "sync_root", "publish_root")):
            messagebox.showerror(APP_NAME, "目录不能为空")
            return
        try:
            service = LibraryService(self.data_dir, updated["nas_root"], updated["sync_root"], updated["device_id"])
            updated["auto_sync"] = self.autosync_var.get()
            store_settings(self.config_path, updated)
        except (OSError, ValueError) as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self.settings, self.service = updated, service
        self.records, self.total, self.selected_id = [], 0, ""
        self.query_generation += 1
        self._node_status.set(updated["device_id"])
        self.page_number = 0
        self._status.set("设置已保存")

    def _test_nas(self):
        root = self.setting_vars["nas_root"].get().strip()
        def check():
            if not Path(root).is_dir():
                raise OSError("素材目录不可访问，请检查共享路径和 Windows 凭据")
            return "NAS 连接正常"
        self._background("connection", check)

    def _check_for_updates(self):
        publish = self.setting_vars["publish_root"].get().strip() if self.active_page == "设置" else self.settings["publish_root"]
        self._status.set("正在检查更新...")
        self._background("update", lambda: self._prepare_update(Path(publish)))

    def _prepare_update(self, publish):
        manifest = json.loads((publish / "manifest.json").read_text(encoding="utf-8-sig"))
        version = str(manifest.get("version", ""))
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise ValueError("更新版本格式错误")
        if tuple(map(int, version.split("."))) <= tuple(map(int, APP_VERSION.split("."))):
            return None
        filename = f"素材协作-{version}-setup.exe"
        expected = str(manifest.get("sha256", "")).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("更新包缺少 SHA-256 校验信息")
        source = publish / filename
        target_dir = self.data_dir / "updates"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / filename
        temporary = target.with_suffix("." + uuid.uuid4().hex + ".tmp")
        try:
            shutil.copyfile(source, temporary)
            with temporary.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != expected:
                raise ValueError("更新包校验失败")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return {"version": version, "installer": target, "notes": str(manifest.get("notes", ""))}

    def _drain_messages(self):
        if self.stop_event.is_set():
            return
        for _ in range(60):
            try:
                kind, value = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == "page":
                generation, (records, count) = value
                if self.active_page == "素材库" and generation == self.query_generation:
                    self.records, self.total = records, count
                    self._render_current()
            elif kind == "thumbnail":
                service, result = value
                if service is self.service and self.active_page == "素材库":
                    item = next((item for item in self.records if item["asset_id"] == result["asset_id"] and item["file_hash"] == result["file_hash"]), None)
                    if item:
                        item["thumbnail"], item["thumbnail_status"] = result["path"], result["status"]
                        if result["path"]:
                            self._show_thumbnail(item, result["path"])
                        if item["asset_id"] == self.selected_id:
                            self._select(self.selected_id)
            elif kind == "progress":
                self._status.set(f"{JOB_NAMES.get(value['kind'], value['kind'])}：{value['done']} 已处理，{value['changed']} 已更新，{value['errors']} 错误")
                if value["kind"] == "scan":
                    self._scan_status.set(f"扫描：{value['done']:,} 已发现 / {value['changed']:,} 新增或变更")
                self._schedule_reload()
            elif kind in {"scan_done", "import_done", "batch_done"}:
                if kind == "scan_done":
                    self.scanning = False
                    self._scan_status.set(f"{JOB_STATUS.get(value['status'], value['status'])}：{value['done']:,} 素材 / {value['changed']:,} 新增或变更 / {value['errors']} 错误")
                    if self.active_page == "素材库":
                        self.pause_button.configure(state="disabled")
                elif kind == "batch_done":
                    self.batching = False
                self._status.set(value["message"])
                self._reload()
                if kind != "batch_done" and self.auto_sync_allowed:
                    self._sync()
            elif kind == "sync_done":
                self.syncing = False
                if value["status"] == "offline":
                    self._sync_status.set("离线 · 待重试")
                    self._status.set(value.get("error", "NAS 暂不可用"))
                else:
                    self._sync_status.set("同步完成")
                    self._status.set(f"同步：发出 {value['sent']} 批 / 收到 {value['received']} 批 / 更新 {value['changed']} 个素材")
                    if value["changed"]:
                        self._request_page()
                self._request_overview()
            elif kind == "overview":
                pending = value.get("pending", 0)
                if not self.syncing:
                    deferred = value.get("deferred", 0)
                    extra = f" · 待重试 {deferred}" if deferred else ""
                    self._sync_status.set(f"{'离线' if value.get('sync_error') else short_time(value.get('last_sync', ''))} · 待发 {pending}{extra}")
                if self.active_page == "任务中心":
                    self.jobs = {item["job_id"]: item for item in value["jobs"]}
                    self.jobs_tree.delete(*self.jobs_tree.get_children())
                    for item in value["jobs"]:
                        self.jobs_tree.insert("", "end", iid=item["job_id"], values=(JOB_NAMES.get(item["kind"], item["kind"]), JOB_STATUS.get(item["status"], item["status"]), item["done"], item["changed"], item["errors"], short_time(item["updated_at"])))
            elif kind == "update":
                if value is None:
                    self._status.set("当前已是最新版本")
                elif messagebox.askyesno(APP_NAME, f"发现新版本 {value['version']}\n\n{value['notes']}\n\n现在安装？"):
                    os.startfile(str(value["installer"]))
                    self.close()
                    return
            elif kind == "operation_error":
                operation, error = value
                if operation == "scan_done":
                    self.scanning = False
                if operation == "batch_done":
                    self.batching = False
                if operation == "sync_done":
                    self.syncing = False
                self._status.set(error)
            elif kind in {"error", "connection", "opened"}:
                self._status.set(str(value))
        self.after(60, self._drain_messages)

    def close(self):
        self.stop_event.set()
        self.scan_stop.set()
        self.batch_stop.set()
        self.player.close()
        self.destroy()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--no-auto-sync", action="store_true")
    parser.add_argument("--smoke-test", type=Path)
    arguments = parser.parse_args()
    app = Workspace(data_dir=arguments.data_dir, auto_sync=not arguments.no_auto_sync and not arguments.smoke_test)
    if arguments.smoke_test:
        def finish_smoke():
            report = dict(version=APP_VERSION, ffmpeg=_ffmpeg(), sqlite=str(app.service.db_path), window=[app.winfo_width(), app.winfo_height()])
            arguments.smoke_test.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            app.close()
        app.after(1200, finish_smoke)
    app.mainloop()
