"""Script and work-order screens; all repository work stays off the Tk thread."""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
from datetime import datetime
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from collaboration_store import CollaborationConflictError
from indexer import get_records_by_ids, open_index, root_key


STATUSES = {
    "script": ("草稿", "待审核", "已定稿"),
    "work_order": ("待处理", "进行中", "待审核", "已完成", "已取消"),
}
NAMES = {"script": "脚本", "work_order": "工单"}
PAGE_SIZE = 30
BG = "#f1f4f7"
FG = "#172738"
MUTED = "#748293"


def _date(value):
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(value or "")


def _assets(service, identities):
    """Resolve names in the local index without stat-ing a media path."""
    with closing(open_index(service.db_path)) as db:
        found = get_records_by_ids(db, list(identities))
    key = root_key(service.root)
    return {identity: asdict(record) for identity, record in found.items() if record.root_key == key}


def _text(parent, *, height=8, readonly=False):
    frame = ttk.Frame(parent)
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(0, weight=1)
    widget = tk.Text(frame, wrap="word", height=height, undo=not readonly,
                     font=("Microsoft YaHei UI", 10), bg="white", fg=FG,
                     relief="flat", padx=12, pady=10, insertbackground="#087f72",
                     highlightthickness=1, highlightbackground="#d8e1e7",
                     highlightcolor="#087f72", spacing3=5)
    widget.grid(row=0, column=0, sticky="nsew")
    scroll = ttk.Scrollbar(frame, command=widget.yview)
    scroll.grid(row=0, column=1, sticky="ns")
    widget.configure(yscrollcommand=scroll.set)
    if readonly:
        widget.configure(state="disabled")
    return frame, widget


def _set_text(widget, value, readonly=False):
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", value or "")
    if readonly:
        widget.configure(state="disabled")


class _LocalJobs:
    """Workers never call Tk, including after(). Destroy drops late results."""

    def _init_jobs(self):
        self._results = queue.Queue()
        self._callbacks = {}
        self._job_counter = 0
        self._alive = True
        self._poll_id = self.after(40, self._poll_jobs)
        self.bind("<Destroy>", self._jobs_destroyed, add="+")

    def _jobs_destroyed(self, event):
        if event.widget is self:
            self._alive = False
            self._callbacks.clear()
            if self._poll_id:
                self.after_cancel(self._poll_id)
                self._poll_id = None

    def _run(self, operation, callback, on_error=None):
        results = self._results
        self._job_counter += 1
        job_id = self._job_counter
        self._callbacks[job_id] = (callback, on_error)

        def worker():
            try:
                value, error = operation(), None
            except Exception as exc:
                value, error = None, exc
            results.put((job_id, value, error))

        threading.Thread(target=worker, daemon=True, name="collaboration-local").start()

    def _poll_jobs(self):
        self._poll_id = None
        if not self._alive:
            return
        for _ in range(20):
            try:
                job_id, value, error = self._results.get_nowait()
            except queue.Empty:
                break
            handlers = self._callbacks.pop(job_id, None)
            if not handlers:
                continue
            callback, on_error = handlers
            if error is None:
                callback(value)
            elif on_error:
                on_error(error)
            else:
                self.notice.set(f"操作未完成：{error}")
            if not self._alive:
                return
        self._poll_id = self.after(40, self._poll_jobs)


class CollaborationPage(_LocalJobs, ttk.Frame):
    def __init__(self, parent, app, kind):
        super().__init__(parent)
        self.app, self.service, self.kind = app, app.service, kind
        self.records = []
        self.total = self.page_number = self._generation = 0
        self._selection_generation = 0
        self._bound_ids = []
        self.query = tk.StringVar(self)
        self.status_filter = tk.StringVar(self, value="全部状态")
        self.include_archived = tk.BooleanVar(self, value=False)
        self.notice = tk.StringVar(self, value="正在读取…")
        self.count_label = tk.StringVar(self, value="")
        self.page_label = tk.StringVar(self, value="")
        self.summary_title = tk.StringVar(self, value="选择一条记录，查看内容和关联素材")
        self.summary_meta = tk.StringVar(self, value="")
        self._build()
        self._init_jobs()
        self.refresh()

    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(header, text=NAMES[self.kind], style="Title.TLabel").pack(side="left")
        ttk.Label(header, textvariable=self.count_label, style="Muted.TLabel").pack(side="left", padx=16)
        ttk.Button(header, text=f"＋ 新建{NAMES[self.kind]}", style="Primary.TButton",
                   command=self.open_editor).pack(side="right")
        filters = ttk.Frame(self)
        filters.grid(row=1, column=0, sticky="ew")
        filters.columnconfigure(0, weight=1)
        entry = ttk.Entry(filters, textvariable=self.query)
        entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        entry.bind("<Return>", lambda event: self.refresh(reset=True))
        ttk.Button(filters, text="搜索", command=lambda: self.refresh(reset=True)).grid(row=0, column=1, padx=(0, 8))
        selector = ttk.Combobox(filters, textvariable=self.status_filter,
                                values=("全部状态", *STATUSES[self.kind]), state="readonly", width=11)
        selector.grid(row=0, column=2, padx=(0, 8))
        selector.bind("<<ComboboxSelected>>", lambda event: self.refresh(reset=True))
        ttk.Checkbutton(filters, text="含已归档", variable=self.include_archived,
                        command=lambda: self.refresh(reset=True)).grid(row=0, column=3)
        actions = ttk.Frame(self)
        actions.grid(row=2, column=0, sticky="ew", pady=10)
        self.edit_button = ttk.Button(actions, text="编辑内容", command=self.edit_selected, state="disabled")
        self.edit_button.pack(side="left")
        self.history_button = ttk.Button(actions, text="版本记录", command=self.history_selected, state="disabled")
        self.history_button.pack(side="left", padx=7)
        self.archive_button = ttk.Button(actions, text="归档", command=self.archive_selected, state="disabled")
        self.archive_button.pack(side="left")
        ttk.Button(actions, text="刷新", command=self.refresh).pack(side="right")
        grid = ttk.Frame(self)
        grid.grid(row=3, column=0, sticky="nsew")
        grid.rowconfigure(0, weight=1)
        grid.columnconfigure(0, weight=1)
        columns = ("title", "status", "assignee", "assets", "updated")
        self.tree = ttk.Treeview(grid, columns=columns, show="headings", selectmode="browse", height=7)
        for name, label, width, minimum, stretch in (
            ("title", "标题", 320, 140, True), ("status", "状态", 100, 80, False),
            ("assignee", "负责人" if self.kind == "work_order" else "更新者", 110, 75, False),
            ("assets", "素材", 58, 48, False), ("updated", "最近更新", 112, 95, False),
        ):
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, minwidth=minimum, stretch=stretch)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(grid, command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.tag_configure("conflict", foreground="#b66b19")
        self.tree.tag_configure("archived", foreground=MUTED)
        self.tree.bind("<<TreeviewSelect>>", self._selected)
        self.tree.bind("<Double-1>", lambda event: self.edit_selected())
        footer = ttk.Frame(self)
        footer.grid(row=4, column=0, sticky="ew", pady=9)
        self.previous_button = ttk.Button(footer, text="上一页", command=lambda: self._step(-1))
        self.previous_button.pack(side="left")
        self.next_button = ttk.Button(footer, text="下一页", command=lambda: self._step(1))
        self.next_button.pack(side="left", padx=7)
        ttk.Label(footer, textvariable=self.page_label, style="Muted.TLabel").pack(side="left", padx=8)
        ttk.Label(footer, text="双击编辑 · 关联素材可直接预览", style="Muted.TLabel").pack(side="right")
        detail = ttk.Frame(self, padding=12)
        detail.grid(row=5, column=0, sticky="ew")
        detail.columnconfigure(0, weight=3)
        detail.columnconfigure(1, weight=2)
        summary_label = ttk.Label(detail, textvariable=self.summary_title, font=("Microsoft YaHei UI", 11, "bold"), wraplength=420)
        summary_label.grid(row=0, column=0, sticky="ew", padx=(0, 15))
        summary_label.bind("<Configure>", lambda event: summary_label.configure(wraplength=max(100, event.width)))
        ttk.Label(detail, text="关联素材", style="Muted.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Label(detail, textvariable=self.summary_meta, style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=4)
        body_frame, self.summary_body = _text(detail, height=3, readonly=True)
        body_frame.grid(row=2, column=0, sticky="nsew", padx=(0, 15))
        bound = ttk.Frame(detail)
        bound.grid(row=1, column=1, rowspan=2, sticky="nsew")
        bound.rowconfigure(0, weight=1)
        bound.columnconfigure(0, weight=1)
        self.bound_list = tk.Listbox(bound, height=3, relief="flat", bg="white", fg=FG,
                                    exportselection=False, selectbackground="#dcefe7", selectforeground="#125f50")
        self.bound_list.grid(row=0, column=0, sticky="nsew")
        bound_scroll = ttk.Scrollbar(bound, command=self.bound_list.yview)
        bound_scroll.grid(row=0, column=1, sticky="ns")
        self.bound_list.configure(yscrollcommand=bound_scroll.set)
        self.bound_list.bind("<Double-1>", lambda event: self.preview_bound())
        self.preview_button = ttk.Button(bound, text="预览所选素材", command=self.preview_bound)
        self.preview_button.grid(row=1, column=0, columnspan=2, sticky="e", pady=(5, 0))
        notice = ttk.Label(self, textvariable=self.notice, style="Muted.TLabel", wraplength=750)
        notice.grid(row=6, column=0, sticky="ew", pady=(6, 0))
        notice.bind("<Configure>", lambda event: notice.configure(wraplength=max(100, event.width)))

    def refresh(self, reset=False):
        if not self._alive:
            return
        if reset:
            self.page_number = 0
        self._generation += 1
        generation = self._generation
        query, status = self.query.get().strip(), self.status_filter.get()
        archived, offset = self.include_archived.get(), self.page_number * PAGE_SIZE
        selected = self.tree.selection()
        self.notice.set("正在读取…")

        def loaded(result):
            if generation != self._generation:
                return
            self.records, self.total = result
            if offset >= self.total and self.page_number > 0:
                self.page_number = max(0, (self.total - 1) // PAGE_SIZE)
                self.refresh()
                return
            self.tree.delete(*self.tree.get_children())
            for record in self.records:
                conflict = record.get("conflict_count", 0)
                title = ("⚠ " if conflict else "") + record["title"]
                state = "已归档" if record["archived"] else record["status"]
                values = (title, state, record.get("assignee" if self.kind == "work_order" else "author", "") or "—",
                          len(record["asset_ids"]), _date(record.get("updated_at")))
                tags = ("conflict",) if conflict else (("archived",) if record["archived"] else ())
                self.tree.insert("", "end", iid=record["entity_id"], values=values, tags=tags)
            self.count_label.set(f"{self.total:,} 条记录")
            pages = max(1, (self.total + PAGE_SIZE - 1) // PAGE_SIZE)
            self.page_label.set(f"{self.page_number + 1} / {pages} 页")
            self.previous_button.state(["!disabled"] if self.page_number else ["disabled"])
            self.next_button.state(["!disabled"] if offset + len(self.records) < self.total else ["disabled"])
            self.notice.set("内容保存在本机，随局域网同步共享。" if self.records else f"暂无匹配{NAMES[self.kind]}，可新建或调整搜索条件。")
            if selected and self.tree.exists(selected[0]):
                self.tree.selection_set(selected[0])
            elif self.records:
                self.tree.selection_set(self.records[0]["entity_id"])
            self._selected()

        def failed(error):
            if generation == self._generation:
                self.notice.set(f"读取失败：{error}")

        service, kind = self.service, self.kind
        self._run(lambda: service.collaboration.list_records(kind, query=query,
                    status="" if status == "全部状态" else status,
                    include_archived=archived, offset=offset, limit=PAGE_SIZE), loaded, failed)

    def _step(self, delta):
        self.page_number = max(0, self.page_number + delta)
        self.refresh()

    def selected_record(self):
        selected = self.tree.selection()
        return next((item for item in self.records if selected and item["entity_id"] == selected[0]), None)

    def _selected(self, event=None):
        self._selection_generation += 1
        generation = self._selection_generation
        record = self.selected_record()
        for button in (self.edit_button, self.history_button, self.archive_button):
            button.state(["!disabled"] if record else ["disabled"])
        self.bound_list.delete(0, "end")
        self._bound_ids = list(record["asset_ids"]) if record else []
        if not record:
            self.summary_title.set("选择一条记录，查看内容和关联素材")
            self.summary_meta.set("")
            _set_text(self.summary_body, "", readonly=True)
            return
        self.archive_button.configure(text="恢复归档" if record["archived"] else "归档")
        self.summary_title.set(record["title"])
        meta = record["status"] + (" · 已归档" if record["archived"] else "")
        if record.get("due_date"):
            meta += " · 截止 " + record["due_date"]
        if record.get("conflict_count"):
            meta += " · 有并行版本，请查看版本记录"
        self.summary_meta.set(meta)
        _set_text(self.summary_body, record["body"], readonly=True)
        for identity in self._bound_ids:
            self.bound_list.insert("end", "正在读取素材名称…")

        def loaded(found):
            if generation != self._selection_generation:
                return
            self.bound_list.delete(0, "end")
            for identity in self._bound_ids:
                self.bound_list.insert("end", found.get(identity, {}).get("relative_path", "素材暂未收录 · " + identity[:10]))
            if self._bound_ids:
                self.bound_list.selection_set(0)

        service, identities = self.service, tuple(self._bound_ids)
        if identities:
            self._run(lambda: _assets(service, identities), loaded)

    def preview_bound(self):
        selection = self.bound_list.curselection()
        if selection:
            self.app.preview_asset_id(self._bound_ids[selection[0]])

    def open_editor(self, record=None, asset_id=None, asset_name=""):
        editor = CollaborationEditor(self.app, self.service, self.kind, record=record,
                                     asset_id=asset_id, asset_name=asset_name, on_saved=self._saved)
        return editor

    def bind_asset(self, asset_id, asset_name=""):
        """Choose an existing script, or start one with this material attached."""
        return AssetBindingDialog(self, asset_id, asset_name)

    def edit_selected(self):
        record = self.selected_record()
        if record:
            return self.open_editor(record)

    def _saved(self, record):
        if self._alive:
            self.refresh()
        if self.app.winfo_exists():
            self.app._status.set(f"{NAMES[self.kind]}已保存，等待局域网同步")

    def history_selected(self):
        record = self.selected_record()
        if record:
            return VersionDialog(self.app, self.service, record["entity_id"])

    def archive_selected(self):
        record = self.selected_record()
        if not record:
            return
        archive = not record["archived"]
        if not messagebox.askyesno("归档记录" if archive else "恢复记录",
                    f"{'归档' if archive else '恢复'}“{record['title']}”？\n归档记录可在“含已归档”中恢复。", parent=self):
            return
        data = {key: record[key] for key in ("title", "body", "status", "assignee", "due_date", "asset_ids", "author")}
        data["archived"] = archive
        data["author"] = self.app.settings.get("display_name") or self.app.settings.get("device_id", "本机")
        self.archive_button.state(["disabled"])
        service, kind = self.service, self.kind

        def failed(error):
            self.archive_button.state(["!disabled"])
            if isinstance(error, CollaborationConflictError):
                self.notice.set("记录已被更新；请刷新并查看版本记录后再归档或恢复。")
            else:
                self.notice.set(f"操作未完成：{error}")

        self._run(lambda: service.collaboration.save(kind, data, entity_id=record["entity_id"],
                      expected_heads=record["heads"]), self._saved, failed)


class AssetBindingDialog(_LocalJobs, tk.Toplevel):
    def __init__(self, page, asset_id, asset_name):
        super().__init__(page.app)
        self.page, self.service = page, page.service
        self.asset_id, self.asset_name = asset_id, asset_name
        self.records = []
        self.total = self.page_number = self._generation = 0
        self.query = tk.StringVar(self)
        self.notice = tk.StringVar(self, value="选择已有脚本，或为当前素材新建脚本。")
        self.result = None
        self.title("绑定脚本")
        self.geometry("740x500")
        self.minsize(660, 420)
        self.configure(bg=BG)
        self.transient(page.app)
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)
        title_label = ttk.Label(outer, text="为素材绑定脚本：" + (asset_name or asset_id[:12]),
                          font=("Microsoft YaHei UI", 12, "bold"), wraplength=680)
        title_label.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        title_label.bind("<Configure>", lambda event: title_label.configure(wraplength=max(100, event.width)))
        filters = ttk.Frame(outer)
        filters.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        filters.columnconfigure(0, weight=1)
        search = ttk.Entry(filters, textvariable=self.query)
        search.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        search.bind("<Return>", lambda event: self.refresh(reset=True))
        ttk.Button(filters, text="搜索脚本", command=lambda: self.refresh(reset=True)).grid(row=0, column=1)
        frame = ttk.Frame(outer)
        frame.grid(row=2, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(frame, columns=("title", "status", "assets"), show="headings", height=7, selectmode="browse")
        for name, label, width in (("title", "脚本标题", 440), ("status", "状态", 100), ("assets", "已关联素材", 95)):
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, minwidth=70, stretch=name == "title")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(frame, command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<Double-1>", lambda event: self.choose())
        pagination = ttk.Frame(outer)
        pagination.grid(row=3, column=0, sticky="ew", pady=9)
        self.previous_button = ttk.Button(pagination, text="上一页", command=lambda: self._step(-1), state="disabled")
        self.previous_button.pack(side="left")
        self.next_button = ttk.Button(pagination, text="下一页", command=lambda: self._step(1), state="disabled")
        self.next_button.pack(side="left", padx=8)
        ttk.Label(pagination, textvariable=self.notice, style="Muted.TLabel").pack(side="left", padx=5)
        actions = ttk.Frame(outer)
        actions.grid(row=4, column=0, sticky="ew")
        ttk.Button(actions, text="＋ 新建脚本", command=self.create).pack(side="left")
        self.choose_button = ttk.Button(actions, text="绑定所选脚本", style="Primary.TButton", command=self.choose, state="disabled")
        self.choose_button.pack(side="right")
        ttk.Button(actions, text="取消", command=self.destroy).pack(side="right", padx=8)
        self._init_jobs()
        self.refresh()
        search.focus_set()

    def refresh(self, reset=False):
        if reset:
            self.page_number = 0
        self._generation += 1
        generation = self._generation
        query, offset, service = self.query.get().strip(), self.page_number * PAGE_SIZE, self.service
        self.notice.set("正在读取…")

        def loaded(result):
            if generation != self._generation:
                return
            self.records, self.total = result
            self.tree.delete(*self.tree.get_children())
            for record in self.records:
                self.tree.insert("", "end", iid=record["entity_id"],
                                 values=(record["title"], record["status"], len(record["asset_ids"])))
            self.previous_button.state(["!disabled"] if self.page_number else ["disabled"])
            self.next_button.state(["!disabled"] if offset + len(self.records) < self.total else ["disabled"])
            self.choose_button.state(["!disabled"] if self.records else ["disabled"])
            self.notice.set(f"{self.total} 个脚本 · 第 {self.page_number + 1} 页" if self.records else "无匹配脚本，可新建。")
            if self.records:
                self.tree.selection_set(self.records[0]["entity_id"])

        self._run(lambda: service.collaboration.list_records("script", query=query, offset=offset, limit=PAGE_SIZE), loaded)

    def _step(self, delta):
        self.page_number = max(0, self.page_number + delta)
        self.refresh()

    def choose(self):
        selected = self.tree.selection()
        record = next((record for record in self.records if selected and record["entity_id"] == selected[0]), None)
        if record:
            self.result = self.page.open_editor(record, asset_id=self.asset_id, asset_name=self.asset_name)
            self.destroy()

    def create(self):
        self.result = self.page.open_editor(asset_id=self.asset_id, asset_name=self.asset_name)
        self.destroy()


class CollaborationEditor(_LocalJobs, tk.Toplevel):
    def __init__(self, app, service, kind, *, record=None, asset_id=None, asset_name="", on_saved=None):
        super().__init__(app)
        self.app, self.service, self.kind = app, service, kind
        self.on_saved = on_saved
        self.record = dict(record) if record else None
        self.expected_heads = list(record["heads"]) if record else None
        self.resolving = False
        self.saving = False
        self.result = None
        self.archived = bool(record and record.get("archived"))
        self.asset_ids = list(record.get("asset_ids", [])) if record else []
        self.asset_names = {asset_id: asset_name} if asset_id and asset_name else {}
        if asset_id and asset_id not in self.asset_ids:
            self.asset_ids.append(asset_id)
        self.notice = tk.StringVar(self, value="编辑后保存，关联素材不会被移动或复制。")
        self.title_var = tk.StringVar(self)
        self.status_var = tk.StringVar(self, value=STATUSES[kind][0])
        self.assignee_var = tk.StringVar(self)
        self.due_var = tk.StringVar(self)
        self.search_var = tk.StringVar(self)
        self.search_note = tk.StringVar(self, value="输入素材名或路径搜索，双击添加。")
        self.search_records = []
        self.search_total = self.search_page = self._search_generation = 0
        self.title(("编辑" if record else "新建") + NAMES[kind])
        self.configure(bg=BG)
        self.geometry("860x620")
        self.minsize(720, 540)
        self.transient(app)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._build_editor()
        self._init_jobs()
        if record:
            self._populate(record)
        if asset_id and asset_id not in self.asset_ids:
            self.asset_ids.append(asset_id)
        self._render_bindings()
        self.initial_data = self.data()
        if asset_id and not (record and asset_id in record.get("asset_ids", [])):
            self.initial_data = dict(self.initial_data, asset_ids=list(record.get("asset_ids", [])) if record else [])
        if record and record.get("conflict_count"):
            self.notice.set("此记录有并行版本。草稿会保留，请先查看版本并明确合并。")
        if self.asset_ids:
            service, identities = self.service, tuple(self.asset_ids)
            self._run(lambda: _assets(service, identities), self._loaded_names)
        self.title_entry.focus_set()
        self.bind("<Control-s>", lambda event: self.save())

    def _build_editor(self):
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(4, weight=1)
        ttk.Label(outer, text=("编辑" if self.record else "新建") + NAMES[self.kind],
                  font=("Microsoft YaHei UI", 16, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 12))
        self.title_entry = ttk.Entry(outer, textvariable=self.title_var)
        self.title_entry.grid(row=1, column=0, sticky="ew")
        ttk.Label(outer, text="标题必填", style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=(4, 7))
        fields = ttk.Frame(outer)
        fields.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(fields, text="状态").grid(row=0, column=0, padx=(0, 6))
        ttk.Combobox(fields, textvariable=self.status_var, values=STATUSES[self.kind],
                     state="readonly", width=10).grid(row=0, column=1, sticky="w")
        if self.kind == "work_order":
            ttk.Label(fields, text="负责人").grid(row=0, column=2, padx=(18, 6))
            ttk.Entry(fields, textvariable=self.assignee_var, width=14).grid(row=0, column=3, sticky="ew")
            fields.columnconfigure(3, weight=1)
            ttk.Label(fields, text="截止日期").grid(row=0, column=4, padx=(18, 6))
            ttk.Entry(fields, textvariable=self.due_var, width=13).grid(row=0, column=5)
            ttk.Label(fields, text="YYYY-MM-DD，可不填", style="Muted.TLabel").grid(row=1, column=4, columnspan=2, sticky="e", pady=(4, 0))
        self.notebook = ttk.Notebook(outer)
        self.notebook.grid(row=4, column=0, sticky="nsew")
        body_frame, self.body = _text(self.notebook, height=10)
        self.notebook.add(body_frame, text=" 脚本正文 " if self.kind == "script" else " 工单要求 ")
        self.bindings_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.bindings_tab, text=" 关联素材 ")
        self._build_bindings()
        notice = ttk.Label(outer, textvariable=self.notice, style="Muted.TLabel", wraplength=780)
        notice.grid(row=5, column=0, sticky="ew", pady=10)
        notice.bind("<Configure>", lambda event: notice.configure(wraplength=max(100, event.width)))
        bottom = ttk.Frame(outer)
        bottom.grid(row=6, column=0, sticky="ew")
        self.history_button = ttk.Button(bottom, text="查看版本 / 处理更新", command=self.open_history,
                                        state="normal" if self.record else "disabled")
        self.history_button.pack(side="left")
        self.save_button = ttk.Button(bottom, text="保存", style="Primary.TButton", command=self.save)
        self.save_button.pack(side="right")
        ttk.Button(bottom, text="关闭", command=self.close).pack(side="right", padx=8)

    def _build_bindings(self):
        parent = self.bindings_tab
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(2, weight=1)
        ttk.Label(parent, text="搜索素材", font=("Microsoft YaHei UI", 10, "bold")).grid(row=0, column=0, sticky="w")
        self.binding_count = tk.StringVar(self, value="已关联 0 个素材")
        ttk.Label(parent, textvariable=self.binding_count, font=("Microsoft YaHei UI", 10, "bold")).grid(row=0, column=1, sticky="w", padx=(14, 0))
        search = ttk.Frame(parent)
        search.grid(row=1, column=0, sticky="ew", pady=8)
        search.columnconfigure(0, weight=1)
        entry = ttk.Entry(search, textvariable=self.search_var)
        entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        entry.bind("<Return>", lambda event: self.search_assets(reset=True))
        ttk.Button(search, text="搜索", command=lambda: self.search_assets(reset=True)).grid(row=0, column=1)
        ttk.Label(parent, text="关联后可从这里直接预览", style="Muted.TLabel").grid(row=1, column=1, sticky="w", padx=(14, 0))
        for column, attr in ((0, "search_list"), (1, "binding_list")):
            frame = ttk.Frame(parent)
            frame.grid(row=2, column=column, sticky="nsew", padx=(14 if column else 0, 0))
            frame.rowconfigure(0, weight=1)
            frame.columnconfigure(0, weight=1)
            box = tk.Listbox(frame, height=7, exportselection=False, relief="flat", bg="white", fg=FG,
                             selectbackground="#dcefe7", selectforeground="#125f50", activestyle="none")
            box.grid(row=0, column=0, sticky="nsew")
            scrollbar = ttk.Scrollbar(frame, command=box.yview)
            scrollbar.grid(row=0, column=1, sticky="ns")
            box.configure(yscrollcommand=scrollbar.set)
            setattr(self, attr, box)
        self.search_list.bind("<Double-1>", lambda event: self.add_selected_asset())
        self.binding_list.bind("<Double-1>", lambda event: self.preview_binding())
        controls = ttk.Frame(parent)
        controls.grid(row=3, column=0, sticky="ew", pady=7)
        self.search_previous = ttk.Button(controls, text="‹", width=2, command=lambda: self._search_step(-1), state="disabled")
        self.search_previous.pack(side="left")
        self.search_next = ttk.Button(controls, text="›", width=2, command=lambda: self._search_step(1), state="disabled")
        self.search_next.pack(side="left", padx=4)
        ttk.Button(controls, text="添加 →", command=self.add_selected_asset).pack(side="right")
        controls2 = ttk.Frame(parent)
        controls2.grid(row=3, column=1, sticky="ew", pady=7, padx=(14, 0))
        ttk.Button(controls2, text="移除关联", command=self.remove_selected_asset).pack(side="left")
        ttk.Button(controls2, text="预览播放", command=self.preview_binding).pack(side="right")
        note = ttk.Label(parent, textvariable=self.search_note, style="Muted.TLabel", wraplength=700)
        note.grid(row=4, column=0, columnspan=2, sticky="ew")

    def _populate(self, record):
        self.title_var.set(record.get("title", ""))
        self.status_var.set(record.get("status", STATUSES[self.kind][0]))
        self.assignee_var.set(record.get("assignee", ""))
        self.due_var.set(record.get("due_date", ""))
        self.archived = bool(record.get("archived"))
        _set_text(self.body, record.get("body", ""))
        self.asset_ids = list(record.get("asset_ids", []))
        self._render_bindings()

    def _loaded_names(self, found):
        self.asset_names.update({identity: item["relative_path"] for identity, item in found.items()})
        self._render_bindings()

    def _render_bindings(self):
        self.binding_count.set(f"已关联 {len(self.asset_ids)} 个素材")
        self.binding_list.delete(0, "end")
        for identity in self.asset_ids:
            self.binding_list.insert("end", self.asset_names.get(identity) or "素材暂未收录 · " + identity[:10])
        if self.asset_ids:
            self.binding_list.selection_set(0)

    def search_assets(self, reset=False):
        if reset:
            self.search_page = 0
        self._search_generation += 1
        generation = self._search_generation
        query, offset, service = self.search_var.get().strip(), self.search_page * PAGE_SIZE, self.service
        self.search_note.set("正在搜索本地素材索引…")

        def loaded(result):
            if generation != self._search_generation:
                return
            self.search_records, self.search_total = result
            self.search_list.delete(0, "end")
            for record in self.search_records:
                self.search_list.insert("end", record["relative_path"])
                self.asset_names[record["asset_id"]] = record["relative_path"]
            self.search_previous.state(["!disabled"] if self.search_page else ["disabled"])
            self.search_next.state(["!disabled"] if offset + len(self.search_records) < self.search_total else ["disabled"])
            self.search_note.set(f"共 {self.search_total:,} 个素材 · 第 {self.search_page + 1} 页 · 双击添加")
            if self.search_records:
                self.search_list.selection_set(0)

        self._run(lambda: service.page(query=query, offset=offset, limit=PAGE_SIZE), loaded)

    def _search_step(self, delta):
        self.search_page = max(0, self.search_page + delta)
        self.search_assets()

    def add_selected_asset(self):
        selected = self.search_list.curselection()
        if selected:
            identity = self.search_records[selected[0]]["asset_id"]
            if identity not in self.asset_ids:
                self.asset_ids.append(identity)
                self._render_bindings()

    def remove_selected_asset(self):
        selected = self.binding_list.curselection()
        if selected:
            self.asset_ids.pop(selected[0])
            self._render_bindings()

    def preview_binding(self):
        selected = self.binding_list.curselection()
        if selected:
            self.app.preview_asset_id(self.asset_ids[selected[0]])

    def data(self):
        return dict(title=self.title_var.get().strip(), body=self.body.get("1.0", "end-1c"),
                    status=self.status_var.get(), assignee=self.assignee_var.get().strip(),
                    due_date=self.due_var.get().strip(), asset_ids=list(self.asset_ids),
                    archived=self.archived,
                    author=self.app.settings.get("display_name") or self.app.settings.get("device_id", "本机"))

    def save(self):
        if self.saving:
            return
        data = self.data()
        if not data["title"]:
            self.notice.set("请先填写标题。")
            self.title_entry.focus_set()
            return
        if data["due_date"]:
            try:
                if datetime.strptime(data["due_date"], "%Y-%m-%d").strftime("%Y-%m-%d") != data["due_date"]:
                    raise ValueError()
            except ValueError:
                self.notice.set("截止日期请填写有效的 YYYY-MM-DD，例如 2026-09-30。")
                return
        self.saving = True
        self.save_button.state(["disabled"])
        self.history_button.state(["disabled"])
        self.notice.set("正在保存…")
        service, kind, resolve = self.service, self.kind, self.resolving
        entity_id = self.record["entity_id"] if self.record else None
        expected = list(self.expected_heads) if self.expected_heads is not None else None

        def loaded(record):
            self.saving = False
            self.result = self.record = record
            self.expected_heads = list(record["heads"])
            self.resolving = False
            self.initial_data = dict(data)
            self.save_button.state(["!disabled"])
            self.save_button.configure(text="保存")
            self.history_button.state(["!disabled"])
            self.notice.set("已保存，等待局域网同步。" + ("保存期间的新修改尚未保存。" if self.data() != self.initial_data else ""))
            if self.on_saved:
                self.on_saved(record)

        def failed(error):
            self.saving = False
            self.save_button.state(["!disabled"])
            if self.record:
                self.history_button.state(["!disabled"])
            if isinstance(error, CollaborationConflictError):
                self.resolving = False
                self.save_button.configure(text="保存")
                self.notice.set("此记录已有更新，未覆盖任何内容。你的草稿仍在；请点击“查看版本 / 处理更新”，比较后确认合并。")
            else:
                self.notice.set(f"保存失败，草稿已保留：{error}")

        self._run(lambda: service.collaboration.save(kind, data, entity_id=entity_id,
                  expected_heads=expected, resolve=resolve), loaded, failed)

    def open_history(self):
        if self.record and not self.saving:
            return VersionDialog(self.app, self.service, self.record["entity_id"], editor=self)

    def accept_resolution(self, current, chosen=None):
        if not self._alive or self.saving:
            return
        if chosen is not None:
            self._populate(chosen)
            if self.asset_ids:
                service, identities = self.service, tuple(self.asset_ids)
                self._run(lambda: _assets(service, identities), self._loaded_names)
        self.expected_heads = list(current["heads"])
        self.resolving = True
        self.save_button.configure(text="确认合并保存")
        self.notice.set("已读取最新版本。请检查正文和关联素材，再点击“确认合并保存”；历史版本会保留。")
        self.lift()

    def close(self):
        if self.saving:
            self.notice.set("正在保存，请稍候再关闭。")
            return
        if self.data() != self.initial_data and not messagebox.askyesno("尚未保存", "关闭后将放弃本窗口尚未保存的修改。仍然关闭？", parent=self):
            return
        self.destroy()


class VersionDialog(_LocalJobs, tk.Toplevel):
    def __init__(self, app, service, entity_id, editor=None):
        super().__init__(app)
        self.service, self.entity_id, self.editor = service, entity_id, editor
        self.current, self.versions = None, []
        self._selection_generation = 0
        self.notice = tk.StringVar(self, value="正在读取版本…")
        self.title("版本记录" + (" · 比较并处理更新" if editor else ""))
        self.geometry("780x580")
        self.minsize(680, 480)
        self.configure(bg=BG)
        self.transient(editor or app)
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)
        ttk.Label(outer, text="版本记录", font=("Microsoft YaHei UI", 15, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 10))
        version_list = ttk.Frame(outer)
        version_list.grid(row=1, column=0, sticky="ew")
        version_list.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(version_list, columns=("version", "status", "author", "time"), show="headings", height=5, selectmode="browse")
        for name, label, width in (("version", "版本", 220), ("status", "状态", 90), ("author", "更新者", 180), ("time", "时间", 120)):
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, minwidth=65, stretch=True)
        self.tree.grid(row=0, column=0, sticky="ew")
        scrollbar = ttk.Scrollbar(version_list, command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<<TreeviewSelect>>", self._selected)
        frame, self.body = _text(outer, height=8, readonly=True)
        frame.grid(row=2, column=0, sticky="nsew", pady=10)
        label = ttk.Label(outer, textvariable=self.notice, style="Muted.TLabel", wraplength=710)
        label.grid(row=3, column=0, sticky="ew", pady=(0, 9))
        label.bind("<Configure>", lambda event: label.configure(wraplength=max(100, event.width)))
        buttons = ttk.Frame(outer)
        buttons.grid(row=4, column=0, sticky="ew")
        ttk.Button(buttons, text="关闭", command=self.destroy).pack(side="right")
        if editor:
            self.use_button = ttk.Button(buttons, text="使用所选版本继续编辑", command=lambda: self.choose(True), state="disabled")
            self.use_button.pack(side="left")
            self.keep_button = ttk.Button(buttons, text="保留我的草稿，继续合并", style="Primary.TButton", command=lambda: self.choose(False), state="disabled")
            self.keep_button.pack(side="left", padx=8)
        self._init_jobs()
        self._run(lambda: (service.collaboration.get(entity_id), service.collaboration.versions(entity_id)), self._loaded)

    def _loaded(self, result):
        self.current, self.versions = result
        if not self.current:
            self.notice.set("记录不存在或尚未同步到本机。")
            return
        for index, record in enumerate(self.versions):
            revision = record.get("revision", "")
            is_head = revision in self.current["heads"]
            label = ("当前版本 · " if is_head else "历史版本 · ") + str(revision)[:10]
            self.tree.insert("", "end", iid=str(index), values=(label, record["status"], record.get("author", ""), _date(record.get("updated_at"))))
        self.notice.set(f"{len(self.versions)} 个版本 · {len(self.current['heads'])} 个当前版本。" +
                        ("先比较内容，再选择继续编辑；只有确认合并保存才会写入。" if self.editor else "所有历史修改均保留。"))
        if self.versions:
            self.tree.selection_set("0")
        if self.editor:
            self.keep_button.state(["!disabled"])
            self.use_button.state(["!disabled"] if self.versions else ["disabled"])

    def _selected(self, event=None):
        self._selection_generation += 1
        generation = self._selection_generation
        selected = self.tree.selection()
        if not selected:
            return
        record = self.versions[int(selected[0])]
        details = [record["title"], f"状态：{record['status']}" + (" · 已归档" if record.get("archived") else "")]
        if record.get("assignee"):
            details.append("负责人：" + record["assignee"])
        if record.get("due_date"):
            details.append("截止日期：" + record["due_date"])
        identities = tuple(record.get("asset_ids", []))
        details.append(f"关联素材：{len(identities)} 个")

        def display(found):
            if generation != self._selection_generation:
                return
            names = ["  • " + found.get(identity, {}).get("relative_path", "素材暂未收录 · " + identity[:10]) for identity in identities]
            _set_text(self.body, "\n".join([*details, *names, "", record.get("body", "")]), readonly=True)

        display({})
        if identities:
            service = self.service
            self._run(lambda: _assets(service, identities), display)

    def choose(self, use_selected):
        if not self.editor or not self.editor._alive or self.editor.saving or not self.current:
            return
        selected = self.tree.selection()
        if use_selected and not selected:
            return
        chosen = self.versions[int(selected[0])] if use_selected else None
        if chosen and self.editor.data() != self.editor.initial_data:
            if not messagebox.askyesno("替换编辑内容", "用所选版本替换编辑框内容？当前尚未保存的草稿将被替换。", parent=self):
                return
        self.editor.accept_resolution(self.current, chosen)
        self.destroy()
