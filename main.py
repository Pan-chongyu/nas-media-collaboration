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

from PIL import Image, ImageTk, ImageOps, ImageChops

from library import LibraryService
from media import _ffmpeg
from player import MediaPlayer, PlayerError
from ui_controls import TimelineScale


APP_NAME = "素材协作"
APP_VERSION = "0.3.2"
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
BG = "#f1f4f7"
FG = "#172738"
MUTED = "#748293"
ACCENT = "#087f72"
PREVIEW = "#142131"
STAGE = "#090f18"


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
        self._player_dragging = False
        self._player_seek_job = None
        self._fullscreen = None
        self._player_error = ""
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
        self.card_badges = {}
        self._preview_source = None
        self._preview_path = None
        self._split_fraction = 0.60
        self._split_ready = False
        self._pending_preview = None
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
        style.configure("TButton", bordercolor="#d8e1e7", lightcolor="#ffffff", darkcolor="#ffffff", focuscolor=BG)
        style.map("TButton", background=[("active", "#e8efec"), ("disabled", "#ededed")])
        style.configure("Primary.TButton", background=ACCENT, foreground="white", borderwidth=0)
        style.map("Primary.TButton", background=[("active", "#086452"), ("disabled", "#c2cdc8")])
        style.configure("Selected.TButton", background="#dfeee8", foreground=ACCENT)
        style.configure("TEntry", padding=7)
        style.configure("Treeview", rowheight=46, background="white", fieldbackground="white", foreground=FG, borderwidth=0)
        style.configure("Treeview.Heading", padding=(8, 9), background="#edf0f2", font=("Microsoft YaHei UI", 9, "bold"))
        style.map("Treeview", background=[("selected", "#dcefe7")], foreground=[("selected", "#125f50")])
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor="#e4e9e7", borderwidth=0)
        style.configure("Preview.TFrame", background=PREVIEW)
        style.configure("Preview.TLabel", background=PREVIEW, foreground="#e3ebf5")
        style.configure("PreviewMuted.TLabel", background=PREVIEW, foreground="#91a4b9", font=("Microsoft YaHei UI", 9))
        style.configure("Preview.TButton", background="#233449", foreground="#e3ebf5", bordercolor="#33465c", lightcolor="#233449", darkcolor="#233449", focuscolor="#233449", padding=(10, 7))
        style.map("Preview.TButton", background=[("active", "#314a62"), ("disabled", "#1b2b3c")], foreground=[("disabled", "#586c80")])
        style.configure("Play.TButton", background="#38c9ac", foreground="#092c28", borderwidth=0, font=("Microsoft YaHei UI", 10, "bold"), padding=(14, 7))
        style.map("Play.TButton", background=[("active", "#6ee0c7"), ("disabled", "#23473f")], foreground=[("disabled", "#78978f")])
        style.configure("Compact.TButton", padding=(9, 5))
        style.configure("TCheckbutton", background=BG, foreground=MUTED)
        style.configure("Vertical.TScrollbar", troughcolor=BG, background="#c6d1d9", borderwidth=0, arrowsize=12)

    def _build_shell(self):
        sidebar = tk.Frame(self, bg="#182532", width=164)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text="▣  素材协作", bg="#182532", fg="white", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w", padx=16, pady=(25, 4))
        tk.Label(sidebar, text="MEDIA WORKSPACE", bg="#182532", fg="#7995a8", font=("Segoe UI", 8)).pack(anchor="w", padx=19, pady=(0, 28))
        self.nav = {}
        for name in ("素材库", "任务中心", "工单", "脚本", "设置"):
            button = tk.Button(sidebar, text=name, anchor="w", bg="#182532", fg="#b2c0cf", activebackground="#284253",
                               activeforeground="white", bd=0, padx=18, pady=13, command=lambda page=name: self.show_page(page))
            button.pack(fill="x", padx=8, pady=3)
            self.nav[name] = button
        tk.Frame(sidebar, bg="#182532").pack(fill="both", expand=True)
        tk.Label(sidebar, text="●  局域网协作", bg="#182532", fg="#76c9b7", anchor="w", font=("Microsoft YaHei UI", 9)).pack(fill="x", padx=18)
        tk.Label(sidebar, text=f"v{APP_VERSION}", bg="#182532", fg="#7e96a9", anchor="w", font=("Microsoft YaHei UI", 9)).pack(fill="x", padx=18, pady=(7, 20))
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
        self.page.pack(fill="both", expand=True, padx=20, pady=(16, 0))
        self.bind_all("<MouseWheel>", self._mousewheel)
        self.bind("<Control-c>", lambda _event: self._copy_path() if self.active_page == "素材库" else None)
        self.bind("<KeyPress>", self._preview_shortcut, add="+")

    def show_page(self, name: str):
        self._stop_player()
        self.active_page = name
        self.query_generation += 1
        self.image_refs.clear()
        self.thumb_widgets.clear()
        self.card_widgets.clear()
        for child in self.page.winfo_children():
            child.destroy()
        for page, button in self.nav.items():
            button.configure(bg="#285046" if page == name else "#182532", fg="#a3f1db" if page == name else "#b2c0cf")
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
        titlebar.pack(fill="x", pady=(0, 12))
        ttk.Label(titlebar, text="素材库", font=("Microsoft YaHei UI", 20, "bold")).pack(side="left")
        self.count_label = ttk.Label(titlebar, text="读取本地索引...", style="Muted.TLabel")
        self.count_label.pack(side="left", padx=14)
        ttk.Button(titlebar, text="立即同步", style="Compact.TButton", command=self._sync).pack(side="right")
        searchbar = ttk.Frame(self.page)
        searchbar.pack(fill="x", pady=(0, 10))
        self.search_var = tk.StringVar()
        ttk.Label(searchbar, text="搜索素材", style="Muted.TLabel").pack(side="left", padx=(0, 10))
        entry = ttk.Entry(searchbar, textvariable=self.search_var, width=22)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda _event: self._request_page(reset=True))
        self.search_var.trace_add("write", self._search_changed)
        self._tooltip(entry, "输入文件名或路径，自动筛选素材")
        self.type_var = tk.StringVar(value="全部")
        types = ttk.Combobox(searchbar, textvariable=self.type_var, values=("全部", "视频", "图片", "音频"), state="readonly", width=6)
        types.pack(side="left", padx=10)
        types.bind("<<ComboboxSelected>>", lambda _event: self._request_page(reset=True))
        self.grid_button = ttk.Button(searchbar, text="网格", width=4, command=lambda: self._set_view("grid"))
        self.grid_button.pack(side="left")
        self.list_button = ttk.Button(searchbar, text="列表", width=4, command=lambda: self._set_view("list"))
        self.list_button.pack(side="left", padx=(4, 0))
        toolbar = ttk.Frame(self.page)
        toolbar.pack(fill="x", pady=(0, 12))
        ttk.Button(toolbar, text="扫描 NAS", style="Compact.TButton", command=self._scan_nas).pack(side="left")
        self.pause_button = ttk.Button(toolbar, text="暂停扫描", style="Compact.TButton", command=self._pause_scan)
        self.pause_button.pack(side="left", padx=5)
        self.pause_button.configure(state="normal" if self.scanning else "disabled")
        ttk.Button(toolbar, text="索引文件", style="Compact.TButton", command=self._import_files).pack(side="left", padx=(0, 5))
        ttk.Button(toolbar, text="批量缩略图", style="Compact.TButton", command=self._batch_thumbnails).pack(side="left")
        self.autoplay_preview = tk.BooleanVar(value=False)
        ttk.Checkbutton(toolbar, text="点选即播", variable=self.autoplay_preview).pack(side="right")
        self.work_split = tk.PanedWindow(self.page, orient="horizontal", sashwidth=12, sashrelief="flat", sashcursor="sb_h_double_arrow", bg=BG, bd=0, opaqueresize=True)
        self.work_split.pack(fill="both", expand=True)
        self.library_surface = ttk.Frame(self.work_split)
        self.preview_panel = tk.Frame(self.work_split, bg=PREVIEW, width=420)
        self.work_split.add(self.library_surface, minsize=240, stretch="always")
        self.work_split.add(self.preview_panel, minsize=360, stretch="always")
        self._split_ready = False
        self.work_split.bind("<Configure>", self._layout_split)
        self.work_split.bind("<ButtonRelease-1>", self._remember_split)
        preview = self.preview_panel
        preview.columnconfigure(0, weight=1)
        preview.rowconfigure(2, weight=1, minsize=140)
        header = tk.Frame(preview, bg=PREVIEW)
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(10, 0))
        tk.Label(header, text="素材预览", bg=PREVIEW, fg="#91a4b9", font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.preview_position = tk.Label(header, text="未选择", bg=PREVIEW, fg="#6ed9bd", font=("Segoe UI", 9))
        self.preview_position.pack(side="right")
        self.preview_title = tk.Label(preview, text="选择一个素材开始", bg=PREVIEW, fg="#f2f6fb", font=self.preview_font, anchor="w", justify="left")
        self.preview_title.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 8))
        self.player_surface = tk.Frame(preview, bg=STAGE, height=300)
        self.player_surface.grid(row=2, column=0, sticky="nsew", padx=12)
        self.player_surface.grid_propagate(False)
        self.preview_image_label = tk.Label(self.player_surface, bg=STAGE, fg="#9aafc3", compound="center", cursor="hand2", bd=0)
        self.preview_image_label.place(x=0, y=0, relwidth=1, relheight=1)
        self.player_surface.bind("<Configure>", self._resize_player)
        self.preview_image_label.bind("<Button-1>", lambda _event: self._toggle_player())
        self._tooltip(self.preview_image_label, "点击播放 · 空格暂停 · F 全屏")
        controls = ttk.Frame(preview, style="Preview.TFrame")
        controls.grid(row=3, column=0, sticky="ew", padx=18, pady=(7, 4))
        self.player_progress = tk.DoubleVar(value=0.0)
        self.player_progress_scale = TimelineScale(controls, from_=0, to=100, variable=self.player_progress,
                                                  command=self._player_progress_changed, bg=PREVIEW, accent="#38c9ac")
        self.player_progress_scale.pack(fill="x")
        self.player_progress_scale.state(["disabled"])
        self.player_progress_scale.bind("<ButtonPress-1>", self._player_drag_start)
        self.player_progress_scale.bind("<ButtonRelease-1>", self._player_drag_end)
        times = ttk.Frame(controls, style="Preview.TFrame")
        times.pack(fill="x", pady=(0, 8))
        self.player_hint = ttk.Label(times, text="点击画面开始预览", style="PreviewMuted.TLabel")
        self.player_hint.pack(side="left")
        self.player_elapsed = ttk.Label(times, text="00:00 / --:--", style="PreviewMuted.TLabel")
        self.player_elapsed.pack(side="right")
        player_bar = ttk.Frame(controls, style="Preview.TFrame")
        player_bar.pack(fill="x", pady=(0, 5))
        self.previous_asset_button = ttk.Button(player_bar, text="上一条", style="Preview.TButton", width=5, command=lambda: self._step_asset(-1))
        self.previous_asset_button.pack(side="left")
        self.play_button = ttk.Button(player_bar, text="▶ 播放", style="Play.TButton", width=7, command=self._toggle_player)
        self.play_button.pack(side="left", padx=5)
        self.next_asset_button = ttk.Button(player_bar, text="下一条", style="Preview.TButton", width=5, command=lambda: self._step_asset(1))
        self.next_asset_button.pack(side="left")
        self.fullscreen_button = ttk.Button(player_bar, text="全屏", width=4, style="Preview.TButton", command=self._fullscreen_player)
        self.fullscreen_button.pack(side="right")
        volume_bar = ttk.Frame(controls, style="Preview.TFrame")
        volume_bar.pack(fill="x", pady=(3, 0))
        self.stop_button = ttk.Button(volume_bar, text="停止", style="Preview.TButton", width=4, command=self._stop_player)
        self.stop_button.pack(side="left")
        self.player_volume = tk.IntVar(value=self.player.volume)
        ttk.Label(volume_bar, text="音量", style="PreviewMuted.TLabel").pack(side="left", padx=(12, 6))
        TimelineScale(volume_bar, from_=0, to=100, variable=self.player_volume, command=self._set_player_volume,
                      bg=PREVIEW, accent="#839bb3", width=90).pack(side="left")
        self.volume_label = ttk.Label(volume_bar, text=f"{self.player.volume}%", width=4, style="PreviewMuted.TLabel")
        self.volume_label.pack(side="left", padx=(6, 0))
        details = self.preview_details = tk.Frame(preview, bg=PREVIEW)
        details.grid(row=4, column=0, sticky="ew", padx=18, pady=(8, 6))
        tk.Frame(details, height=1, bg="#2b3b4d").pack(fill="x", pady=(0, 8))
        self.preview_meta = tk.Label(details, text="双击素材卡片，在这里直接播放", bg=PREVIEW, fg="#91a4b9", anchor="w", justify="left", font=("Microsoft YaHei UI", 9), wraplength=370)
        self.preview_meta.pack(fill="x")
        preview_actions = self.preview_actions = ttk.Frame(preview, style="Preview.TFrame")
        preview_actions.grid(row=5, column=0, sticky="ew", padx=18, pady=(4, 14))
        for label, tooltip, command in (("复制路径", "复制剪辑路径 Ctrl+C", self._copy_path), ("外部打开", "使用系统默认程序打开", self._open_asset), ("刷新封面", "重新生成缩略图", self._retry_thumbnail)):
            button = ttk.Button(preview_actions, text=label, style="Preview.TButton", command=command)
            button.pack(side="left", padx=(0, 6))
            self._tooltip(button, tooltip)
        self.preview_panel.bind("<Configure>", self._preview_panel_resized)
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
        self.asset_tree.bind("<Double-1>", lambda _event: self._play_asset(self.selected_id))
        bottom = ttk.Frame(self.page)
        bottom.pack(side="bottom", fill="x", pady=(10, 0), before=self.work_split)
        self.previous_button = ttk.Button(bottom, text="上一页", style="Compact.TButton", command=lambda: self._turn_page(-1))
        self.previous_button.pack(side="left")
        self.next_button = ttk.Button(bottom, text="下一页", style="Compact.TButton", command=lambda: self._turn_page(1))
        self.next_button.pack(side="left", padx=6)
        self.page_label = ttk.Label(bottom, text="", style="Muted.TLabel")
        self.page_label.pack(side="left", padx=8)
        ttk.Label(bottom, text="双击播放 · 空格暂停 · F 全屏", style="Muted.TLabel").pack(side="right")
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
        self._preview_source = self._preview_path = None
        self.image_refs.pop("preview", None)
        self.preview_image_label.configure(image="", text="▶\n\n选择素材，点击播放", font=("Microsoft YaHei UI", 13), bg=STAGE)

    def _layout_split(self, event):
        if event.width < 650 or self.active_page != "素材库":
            return
        width = event.width
        if getattr(self, "_split_last_width", 0) != width or not self._split_ready:
            self._split_last_width = width
            target = max(240, min(width - 372, round(width * self._split_fraction)))
            self.work_split.sash_place(0, target, 0)
            self._split_ready = True

    def _remember_split(self, _event=None):
        if self.work_split.winfo_width() > 1:
            self._split_fraction = self.work_split.sash_coord(0)[0] / self.work_split.winfo_width()

    def _preview_panel_resized(self, event):
        item = self._selected()
        width = max(220, event.width - 36)
        self.preview_title.configure(text=self._fit_text(item["name"], width, 1, self.preview_font) if item else "选择一个素材开始")
        self.preview_meta.configure(wraplength=width)
        if event.height < 500:
            self.preview_details.grid_remove()
        else:
            self.preview_details.grid()

    def _preview_shortcut(self, event):
        if self.active_page != "素材库" or self._fullscreen:
            return
        widget = self.focus_get()
        if widget and widget.winfo_class() in {"Entry", "TEntry", "Text", "TCombobox", "TScale", "TButton", "Button", "TCheckbutton"}:
            return
        if event.keysym == "space":
            self._toggle_player()
        elif event.keysym.lower() == "f":
            self._fullscreen_player()
        elif event.keysym in {"Left", "Right"}:
            self.player.seek(self.player.elapsed + (-5 if event.keysym == "Left" else 5))
        else:
            return
        return "break"

    def _play_asset(self, identity):
        if not identity:
            return
        self._select(identity, autoplay=False)
        self.focus_set()
        item = self._selected()
        if item and item["media_type"] in {"video", "audio"} and not self.player.playing:
            self._toggle_player()

    def _step_asset(self, step):
        index = next((index for index, item in enumerate(self.records) if item["asset_id"] == self.selected_id), -1)
        target = index + step if index >= 0 else 0
        if not 0 <= target < len(self.records):
            page = self.page_number + step
            if 0 <= page < math.ceil(self.total / PAGE_SIZE):
                self._turn_page(step)
                self._pending_preview = (self.query_generation, 0 if step > 0 else -1)
            return
        item = self.records[target]
        self._play_asset(item["asset_id"])
        if self.view_mode == "list":
            self.asset_tree.selection_set(item["asset_id"])
            self.asset_tree.see(item["asset_id"])
        else:
            card = self.card_widgets.get(item["asset_id"])
            if card:
                self.asset_canvas.yview_moveto(card.winfo_y() / max(1, self.asset_grid.winfo_height()))

    def _mousewheel(self, event):
        if self.active_page == "素材库" and self.view_mode == "grid" and self.asset_canvas.winfo_exists() and str(event.widget).startswith(str(self.library_surface)):
            self.asset_canvas.yview_scroll(int(-event.delta / 120), "units")

    def _canvas_resize(self, event):
        if self.active_page != "素材库":
            return
        self.asset_canvas.itemconfigure(self.asset_window, width=event.width)
        columns = max(1, round(event.width / 226))
        width = max(170, event.width // columns - 12)
        if columns != self.columns or abs(width - self.card_width) > 8:
            self.columns, self.card_width = columns, width
            if self._resize_after:
                self.after_cancel(self._resize_after)
            self._resize_after = self.after_idle(self._render_current)

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
        preview_ref = self.image_refs.get("preview")
        self.image_refs.clear()
        if preview_ref:
            self.image_refs["preview"] = preview_ref
        self.card_widgets.clear()
        self.thumb_widgets.clear()
        self.card_shapes.clear()
        self.card_badges.clear()
        for child in self.asset_grid.winfo_children():
            child.destroy()
        self.asset_tree.delete(*self.asset_tree.get_children())
        if self.selected_id not in {item["asset_id"] for item in self.records}:
            self._stop_player()
            self.selected_id = ""
            self.preview_title.configure(text="选择一个素材开始")
            self.preview_meta.configure(text="双击素材卡片，在这里直接播放")
            self.preview_position.configure(text="未选择")
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
                frame = tk.Frame(self.asset_grid, bg="white", width=width, height=height + 68,
                                 highlightthickness=2, highlightbackground=ACCENT if asset_id == self.selected_id else "#e0e6ec", cursor="hand2")
                frame.grid(row=index // max(self.columns, 1), column=index % max(self.columns, 1), padx=5, pady=6, sticky="n")
                frame.pack_propagate(False)
                image = self._placeholder(width - 4, height, item["media_type"])
                self.image_refs[asset_id] = image
                label = tk.Label(frame, image=image, width=width - 4, height=height, bd=0, compound="center", text=TYPE_NAMES.get(item["media_type"], "文件"), fg=MUTED)
                label.pack()
                title = tk.Label(frame, text=self._fit_text(item["name"], width - 20, 1, self.card_font), bg="white", fg=FG, anchor="w", justify="left", font=self.card_font)
                title.pack(fill="x", padx=10, pady=(7, 2))
                self._tooltip(title, item["relative_path"])
                meta = tk.Label(frame, text=f"{TYPE_NAMES.get(item['media_type'], '文件')}   {readable_size(item['size'])}", bg="white", fg=MUTED, anchor="w", font=("Microsoft YaHei UI", 8))
                meta.pack(fill="x", padx=10, pady=2)
                badge = tk.Label(label, text="正在预览" if asset_id == self.selected_id else TYPE_NAMES.get(item["media_type"], "文件"),
                                 bg=ACCENT if asset_id == self.selected_id else "#233449", fg="white", font=("Microsoft YaHei UI", 8), padx=7, pady=2)
                badge.place(x=8, y=8)
                self.card_badges[asset_id] = badge
                for widget in (frame, label, title, meta, badge):
                    widget.bind("<Button-1>", lambda _event, identity=asset_id: (self.focus_set(), self._select(identity)))
                    widget.bind("<Double-1>", lambda _event, identity=asset_id: self._play_asset(identity))
                self.thumb_widgets[asset_id] = label
                self.card_widgets[asset_id] = frame
                self.card_shapes[asset_id] = (width - 4, height)
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
            if self._preview_path != path:
                with Image.open(path) as source:
                    source = source.convert("RGB")
                    # Cached covers include a uniform canvas around portrait clips.
                    # Remove only that display padding before fitting the larger stage.
                    corner = source.getpixel((0, 0))
                    if source.getpixel((source.width - 1, 0)) == corner:
                        difference = ImageChops.difference(source, Image.new("RGB", source.size, corner))
                        bounds = difference.convert("L").point(lambda p: 255 if p > 18 else 0).getbbox()
                        if bounds and bounds[2] - bounds[0] > 30 and bounds[3] - bounds[1] > 30:
                            source = source.crop(bounds)
                    self._preview_source = source.copy()
                    self._preview_path = path
            self._draw_preview_cover()
        except (OSError, ValueError, tk.TclError):
            self._empty_preview()

    def _draw_preview_cover(self):
        if self._preview_source is None or self.player.playing:
            return
        size = (max(1, self.player_surface.winfo_width()), max(1, self.player_surface.winfo_height()))
        if min(size) < 5:
            return
        fitted = ImageOps.contain(self._preview_source, size, Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", size, STAGE)
        canvas.paste(fitted, ((size[0] - fitted.width) // 2, (size[1] - fitted.height) // 2))
        self.image_refs["preview"] = ImageTk.PhotoImage(canvas, master=self)
        self.preview_image_label.configure(image=self.image_refs["preview"], text="")

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

    def _select(self, identity, autoplay=True):
        item = next((item for item in self.records if item["asset_id"] == identity), None)
        if not item:
            return
        changed = self.selected_id != identity
        if changed:
            self._stop_player()
        self.selected_id = identity
        for key, card in self.card_widgets.items():
            card.configure(highlightbackground=ACCENT if key == identity else "#e0e6ec")
        for key, badge in self.card_badges.items():
            record = next((record for record in self.records if record["asset_id"] == key), None)
            badge.configure(text="正在预览" if key == identity else TYPE_NAMES.get(record["media_type"], "文件"), bg=ACCENT if key == identity else "#233449")
        self.preview_title.configure(text=self._fit_text(item["name"], max(240, self.preview_panel.winfo_width() - 36), 1, self.preview_font))
        self._tooltip(self.preview_title, item["relative_path"])
        timestamp = datetime.fromtimestamp(item["mtime_ns"] / 1_000_000_000).strftime("%Y-%m-%d %H:%M")
        self.preview_meta.configure(text=f"{TYPE_NAMES.get(item['media_type'], '文件')}   ·   {readable_size(item['size'])}   ·   {timestamp}\n{item['relative_path']}")
        index = next(i for i, record in enumerate(self.records) if record["asset_id"] == identity)
        self.preview_position.configure(text=f"{self.page_number * PAGE_SIZE + index + 1:02d} / {self.total:,}")
        self.previous_asset_button.configure(state="normal" if index > 0 or self.page_number > 0 else "disabled")
        self.next_asset_button.configure(state="normal" if self.page_number * PAGE_SIZE + index + 1 < self.total else "disabled")
        if item.get("thumbnail"):
            self._preview_thumbnail(item["thumbnail"])
        else:
            self._empty_preview()
        playable = item["media_type"] in {"video", "audio"}
        self.play_button.configure(state="normal" if playable else "disabled")
        self.fullscreen_button.configure(state="normal" if playable else "disabled")
        if changed and autoplay and self.autoplay_preview.get() and playable:
            self._toggle_player()
        elif not self.player.playing:
            self.player_hint.configure(text="点击画面或空格播放" if playable else "图片预览")

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
            if self.player.state.ended:
                self.player.seek(0)
                if self.player.state.paused:
                    self.player.pause()
            else:
                self.player.pause()
            return
        try:
            self._player_error = ""
            self.preview_image_label.place_forget()
            self.player_surface.update_idletasks()
            self.player.set_volume(self.player_volume.get())
            self.player.embed(item["path"], self.player_surface.winfo_id())
            self._resize_player()
            self.player_hint.configure(text="正在加载：" + item["name"])
            if self.player_job:
                self.after_cancel(self.player_job)
            self.player_job = self.after(100, self._player_tick)
        except PlayerError as exc:
            self.preview_image_label.place(x=0, y=0, relwidth=1, relheight=1)
            self._status.set(str(exc))

    def _player_tick(self):
        self.player_job = None
        if self.active_page != "素材库" or self.stop_event.is_set():
            return
        for event in self.player.drain_events():
            if event == "nas-exit-fullscreen":
                self._leave_fullscreen()
            elif event == "nas-toggle-fullscreen":
                self._fullscreen_player()
        state = self.player.state
        if not self._player_dragging:
            self.player_progress.set(min(100.0, state.elapsed / state.duration * 100.0) if state.duration else 0)
        self.player_progress_scale.state(["!disabled"] if state.active and state.duration > 0 else ["disabled"])
        total = self._format_clock(state.duration) if state.duration else "--:--"
        self.player_elapsed.configure(text=f"{self._format_clock(state.elapsed)} / {total}")
        label = "↻ 重播" if state.ended else "▶ 继续" if state.paused else "Ⅱ 暂停" if state.active else "▶ 播放"
        self.play_button.configure(text=label)
        hint = "播放完毕" if state.ended else "正在加载或缓冲…" if state.loading else "已暂停" if state.paused else "正在播放" if state.active else "选择视频或音频后播放"
        self.player_hint.configure(text=hint)
        if self._fullscreen:
            self.fullscreen_play.configure(text=label)
            self.fullscreen_time.configure(text=self.player_elapsed.cget("text"))
        if state.error and state.error != self._player_error:
            self._player_error = state.error
            self._status.set(state.error)
            self.player_hint.configure(text=state.error)
            self._leave_fullscreen()
            self.preview_image_label.place(x=0, y=0, relwidth=1, relheight=1)
            self._draw_preview_cover()
        if state.active:
            self.player_job = self.after(200, self._player_tick)

    def _format_clock(self, seconds):
        seconds = max(0, int(seconds))
        if seconds >= 3600:
            return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    def _player_drag_start(self, _event=None):
        self._player_dragging = True
        if self._player_seek_job:
            self.after_cancel(self._player_seek_job)
            self._player_seek_job = None

    def _player_drag_end(self, _event=None):
        self._player_dragging = False
        self._commit_player_seek()

    def _player_progress_changed(self, _value=None):
        if self._player_dragging:
            return
        if self._player_seek_job:
            self.after_cancel(self._player_seek_job)
        self._player_seek_job = self.after(120, self._commit_player_seek)

    def _commit_player_seek(self):
        if self._player_seek_job:
            self.after_cancel(self._player_seek_job)
            self._player_seek_job = None
        self.player.seek(self.player_progress.get() / 100 * self.player.state.duration)

    def _stop_player(self):
        self._leave_fullscreen()
        if self.player_job:
            self.after_cancel(self.player_job)
            self.player_job = None
        if self._player_seek_job:
            self.after_cancel(self._player_seek_job)
            self._player_seek_job = None
        self._player_dragging = False
        self.player.stop()
        if hasattr(self, "play_button") and self.play_button.winfo_exists():
            self.play_button.configure(text="▶ 播放")
            self.player_progress.set(0)
            self.player_progress_scale.state(["disabled"])
            self.player_elapsed.configure(text="00:00 / --:--")
            self.player_hint.configure(text="选择视频或音频后播放")
            self.preview_image_label.place(x=0, y=0, relwidth=1, relheight=1)
            self._draw_preview_cover()

    def _set_player_volume(self, value):
        try:
            self.player.set_volume(int(float(value)))
            if hasattr(self, "volume_label") and self.volume_label.winfo_exists():
                self.volume_label.configure(text=f"{int(float(value))}%")
        except (TypeError, ValueError):
            pass

    def _resize_player(self, _event=None):
        surface = self.fullscreen_surface if self._fullscreen else getattr(self, "player_surface", None)
        if surface and surface.winfo_exists():
            self.player.attach(surface.winfo_id(), surface.winfo_width(), surface.winfo_height())
            if not self._fullscreen:
                self._draw_preview_cover()

    def _fullscreen_player(self):
        if self._fullscreen:
            self._leave_fullscreen()
            return
        if not self.player.playing:
            self._toggle_player()
        if not self.player.playing:
            return
        window = tk.Toplevel(self)
        self._fullscreen = window
        window.title("素材协作 · 全屏播放")
        window.configure(bg="#121916")
        window.attributes("-fullscreen", True)
        window.protocol("WM_DELETE_WINDOW", self._leave_fullscreen)
        window.bind("<Escape>", lambda _event: self._leave_fullscreen())
        window.bind("<space>", lambda _event: self._toggle_player())
        controls = ttk.Frame(window, padding=10)
        controls.pack(side="bottom", fill="x")
        self.fullscreen_play = ttk.Button(controls, text=self.play_button.cget("text"), command=self._toggle_player)
        self.fullscreen_play.pack(side="left")
        self.fullscreen_time = ttk.Label(controls, text=self.player_elapsed.cget("text"))
        self.fullscreen_time.pack(side="left", padx=12)
        ttk.Button(controls, text="退出全屏 Esc", command=self._leave_fullscreen).pack(side="right")
        scale = TimelineScale(controls, from_=0, to=100, variable=self.player_progress, command=self._player_progress_changed, bg=BG, accent=ACCENT)
        scale.pack(side="left", fill="x", expand=True, padx=12)
        scale.bind("<ButtonPress-1>", self._player_drag_start)
        scale.bind("<ButtonRelease-1>", self._player_drag_end)
        self.fullscreen_surface = tk.Frame(window, bg="#121916")
        self.fullscreen_surface.pack(fill="both", expand=True)
        self.fullscreen_surface.bind("<Configure>", self._resize_player)
        window.update_idletasks()
        self._resize_player()
        window.focus_force()

    def _leave_fullscreen(self):
        window = self._fullscreen
        if not window:
            return
        self._fullscreen = None
        self._resize_player()
        window.destroy()

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
                    if self._pending_preview and self._pending_preview[0] == generation and self.records:
                        index = self._pending_preview[1]
                        self._pending_preview = None
                        self._play_asset(self.records[index]["asset_id"])
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
        self._stop_player()
        self.player.close()
        self.destroy()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--no-auto-sync", action="store_true")
    parser.add_argument("--smoke-test", type=Path)
    parser.add_argument("--player-smoke-test", type=Path)
    arguments = parser.parse_args()
    app = Workspace(data_dir=arguments.data_dir, auto_sync=not arguments.no_auto_sync and not arguments.smoke_test and not arguments.player_smoke_test)
    if arguments.player_smoke_test:
        from player_smoke import run_smoke
        run_smoke(app, arguments.player_smoke_test)
    elif arguments.smoke_test:
        def finish_smoke():
            report = dict(version=APP_VERSION, ffmpeg=_ffmpeg(), mpv=str(app.player.mpv or ""), sqlite=str(app.service.db_path), window=[app.winfo_width(), app.winfo_height()])
            arguments.smoke_test.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            app.close()
        app.after(1200, finish_smoke)
    app.mainloop()
