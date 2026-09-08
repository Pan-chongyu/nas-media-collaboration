"""Category management and delta assignment dialogs, backed by the local index."""
from collections import Counter
import tkinter as tk
from tkinter import messagebox, ttk

from collaboration_ui import _LocalJobs, _text, _set_text
from category_store import CategoryConflictError


PALETTE = {"青绿": "#087f72", "海蓝": "#2563eb", "紫色": "#7c3aed", "橙色": "#c65d12", "玫红": "#be185d", "深灰": "#475569"}
BG = "#f1f4f7"


class _CategoryWindow(_LocalJobs, tk.Toplevel):
    def _prepare(self, app, title, size, on_changed):
        self.app, self.service, self.on_changed = app, app.service, on_changed
        self.saving = False
        self.notice = tk.StringVar(self)
        self.title(title)
        self.geometry(size)
        self.minsize(720, 540)
        self.configure(bg=BG)
        self.transient(app)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._init_jobs()

    def dirty(self):
        return False

    def close(self):
        if self.saving:
            self.notice.set("正在保存，请稍候。")
        elif not self.dirty() or messagebox.askyesno("尚未保存", "放弃本窗口未保存的分类修改并关闭？", parent=self):
            self.destroy()

    def changed(self):
        if self.on_changed:
            self.on_changed()
        self.app._status.set("分类已保存，等待局域网同步")


class CategoryManager(_CategoryWindow):
    def __init__(self, app, on_changed=None):
        super().__init__(app)
        self.record = None
        self.records = []
        self.expected_heads = None
        self.resolving = False
        self._generation = 0
        self._prepare(app, "管理素材分类", "820x650", on_changed)
        self.query = tk.StringVar(self)
        self.include_archived = tk.BooleanVar(self)
        self.name_var = tk.StringVar(self)
        self.color_var = tk.StringVar(self, value=next(iter(PALETTE.values())))
        self.archived_var = tk.BooleanVar(self)
        outer = ttk.Frame(self, padding=20)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)
        ttk.Label(outer, text="素材分类", font=("Microsoft YaHei UI", 18, "bold")).grid(row=0, column=0, sticky="w")
        filters = ttk.Frame(outer)
        filters.grid(row=1, column=0, sticky="ew", pady=(12, 10))
        filters.columnconfigure(0, weight=1)
        entry = ttk.Entry(filters, textvariable=self.query)
        entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        entry.bind("<Return>", lambda _: self.refresh())
        ttk.Button(filters, text="搜索", command=self.refresh).grid(row=0, column=1, padx=(0, 8))
        ttk.Checkbutton(filters, text="含已归档", variable=self.include_archived, command=self.refresh).grid(row=0, column=2)
        frame = ttk.Frame(outer)
        frame.grid(row=2, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(frame, columns=("name", "count", "status"), show="headings", selectmode="browse", height=6)
        for key, label, width in (("name", "分类名称", 360), ("count", "素材数量", 90), ("status", "状态", 160)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=80, stretch=key == "name")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(frame, command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<<TreeviewSelect>>", self.select)
        editor = ttk.Frame(outer, padding=(0, 12, 0, 0))
        editor.grid(row=3, column=0, sticky="ew")
        editor.columnconfigure(1, weight=1)
        ttk.Label(editor, text="分类名称").grid(row=0, column=0, padx=(0, 10))
        self.name_entry = ttk.Entry(editor, textvariable=self.name_var)
        self.name_entry.grid(row=0, column=1, sticky="ew")
        ttk.Checkbutton(editor, text="归档此分类", variable=self.archived_var).grid(row=0, column=2, padx=(12, 0))
        colors = ttk.Frame(editor)
        colors.grid(row=1, column=0, columnspan=3, sticky="w", pady=9)
        ttk.Label(colors, text="标签颜色").pack(side="left", padx=(0, 10))
        self.swatches = {}
        for name, color in PALETTE.items():
            button = tk.Button(colors, text=name, bg=color, fg="white", activebackground=color, activeforeground="white",
                               bd=0, padx=9, pady=4, command=lambda value=color: self.choose_color(value))
            button.pack(side="left", padx=(0, 5))
            self.swatches[color] = button
        ttk.Label(outer, text="同一素材可属于多个分类。归档后隐藏分类，恢复后重新显示原有归类。", style="Muted.TLabel").grid(row=4, column=0, sticky="w", pady=(0, 8))
        self.notice_label = ttk.Label(outer, textvariable=self.notice, style="Muted.TLabel", wraplength=660)
        self.notice_label.grid(row=5, column=0, sticky="ew", pady=(0, 10))
        bottom = ttk.Frame(outer)
        bottom.grid(row=6, column=0, sticky="ew")
        self.new_button = ttk.Button(bottom, text="＋ 新建分类", command=self.new)
        self.new_button.pack(side="left")
        self.history_button = ttk.Button(bottom, text="版本 / 处理更新", command=self.history, state="disabled")
        self.history_button.pack(side="left", padx=8)
        self.save_button = ttk.Button(bottom, text="保存分类", style="Primary.TButton", command=self.save)
        self.save_button.pack(side="right")
        ttk.Button(bottom, text="关闭", command=self.close).pack(side="right", padx=8)
        self.initial_data = self.data()
        self.choose_color(self.color_var.get())
        self.notice.set("输入名称创建分类，或选择已有分类编辑。")
        self.refresh()

    def data(self):
        return dict(name=self.name_var.get().strip(), color=self.color_var.get(), archived=self.archived_var.get())

    def dirty(self):
        return self.data() != self.initial_data

    def choose_color(self, color):
        self.color_var.set(color)
        for value, button in self.swatches.items():
            button.configure(text=("✓ " if color == value else "") + next(name for name, c in PALETTE.items() if c == value))

    def refresh(self):
        self._generation += 1
        generation, service = self._generation, self.service
        query, archived = self.query.get().strip().casefold(), self.include_archived.get()
        def loaded(records):
            if generation != self._generation:
                return
            self.records = [record for record in records if query in record["name"].casefold()]
            self.tree.delete(*self.tree.get_children())
            counts = Counter(record["name"] for record in self.records)
            for record in self.records:
                identity = record["category_id"]
                label = record["name"] + (" · " + identity[:8] if counts[record["name"]] > 1 else "")
                status = ("已归档" if record["archived"] else "使用中") + (" · 待合并" if record.get("conflict_count") else "")
                self.tree.insert("", "end", iid=identity, values=(label, record["count"], status), tags=(identity,))
                self.tree.tag_configure(identity, foreground=record["color"])
            if self.record and self.tree.exists(self.record["category_id"]):
                self.tree.selection_set(self.record["category_id"])
        self._run(lambda: service.categories.list_records(include_archived=archived), loaded)

    def select(self, _event=None):
        selected = self.tree.selection()
        record = next((r for r in self.records if selected and r["category_id"] == selected[0]), None)
        if record is None or self.record and record["category_id"] == self.record["category_id"]:
            return
        if self.saving or self.dirty() and not messagebox.askyesno("尚未保存", "放弃当前修改，编辑所选分类？", parent=self):
            if self.record and self.tree.exists(self.record["category_id"]):
                self.tree.selection_set(self.record["category_id"])
            else:
                self.tree.selection_remove(*self.tree.selection())
            return
        self.load_record(record)

    def load_record(self, record):
        self.record = dict(record) if record else None
        self.expected_heads = list(record["heads"]) if record else None
        self.resolving = False
        self.name_var.set(record["name"] if record else "")
        self.choose_color(record["color"] if record else next(iter(PALETTE.values())))
        self.archived_var.set(record["archived"] if record else False)
        self.initial_data = self.data()
        self.save_button.configure(text="保存分类")
        self.history_button.state(["!disabled"] if record else ["disabled"])
        self.notice.set("存在并行版本，请先查看版本并确认合并。" if record and record.get("conflict_count") else "修改后点击保存分类。")

    def new(self):
        if self.saving or self.dirty() and not messagebox.askyesno("尚未保存", "放弃当前修改，新建分类？", parent=self):
            return
        self.tree.selection_remove(*self.tree.selection())
        self.load_record(None)
        self.name_entry.focus_set()

    def save(self):
        if self.saving:
            return
        data = self.data()
        if not data["name"] or len(data["name"]) > 40:
            self.notice.set("分类名称需要填写 1–40 个字。")
            return
        if data["archived"] and not (self.record and self.record["archived"]):
            if not messagebox.askyesno("归档分类", "归档后素材库隐藏此分类。可在“含已归档”中恢复。继续？", parent=self):
                return
        service, identity, heads = self.service, self.record["category_id"] if self.record else None, self.expected_heads
        resolve = self.resolving
        self.saving = True
        self.save_button.state(["disabled"])
        self.notice.set("正在保存分类…")
        def loaded(record):
            self.saving = False
            self.record, self.expected_heads = record, list(record["heads"])
            self.initial_data, self.resolving = data, False
            self.save_button.state(["!disabled"])
            self.save_button.configure(text="保存分类")
            self.history_button.state(["!disabled"])
            self.notice.set("已保存，等待局域网同步。")
            self.refresh()
            self.changed()
        def failed(error):
            self.saving = False
            self.save_button.state(["!disabled"])
            self.resolving = False
            self.save_button.configure(text="保存分类")
            self.notice.set("分类已被修改。你的草稿仍在，请查看版本并确认合并。" if isinstance(error, CategoryConflictError) else str(error))
        self._run(lambda: service.categories.save(data, category_id=identity, expected_heads=heads, resolve=resolve), loaded, failed)

    def history(self):
        if self.record and not self.saving:
            return CategoryVersions(self)

    def use_version(self, current, version=None):
        if not self._alive or self.saving:
            return
        if version:
            self.name_var.set(version["name"])
            self.choose_color(version["color"])
            self.archived_var.set(version["archived"])
        self.expected_heads = list(current["heads"])
        self.resolving = True
        self.save_button.configure(text="确认合并保存")
        self.notice.set("检查分类名称、颜色和归档状态后，点击“确认合并保存”。")
        self.lift()


class CategoryVersions(_CategoryWindow):
    def __init__(self, manager):
        super().__init__(manager.app)
        self._prepare(manager.app, "分类版本记录", "740x550", None)
        self.manager, self.service = manager, manager.service
        self.current, self.records = None, []
        identity = manager.record["category_id"]
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(outer, columns=("name", "state", "date"), show="headings", height=6, selectmode="browse")
        for key, label, width in (("name", "分类名称", 280), ("state", "版本", 130), ("date", "保存时间", 200)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=70)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(outer, command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self.select)
        frame, self.body = _text(outer, height=5, readonly=True)
        frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=12)
        ttk.Label(outer, textvariable=self.notice, style="Muted.TLabel", wraplength=680).grid(row=2, column=0, columnspan=2, sticky="ew")
        buttons = ttk.Frame(outer)
        buttons.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.keep_button = ttk.Button(buttons, text="保留我的草稿", command=lambda: self.choose(False), state="disabled")
        self.keep_button.pack(side="left")
        self.use_button = ttk.Button(buttons, text="使用所选版本", command=lambda: self.choose(True), state="disabled")
        self.use_button.pack(side="left", padx=8)
        ttk.Button(buttons, text="关闭", command=self.close).pack(side="right")
        self.notice.set("正在读取版本…")
        service = self.service
        self._run(lambda: (service.categories.get(identity), service.categories.versions(identity)), self.loaded)

    def loaded(self, result):
        self.current, self.records = result
        for i, record in enumerate(self.records):
            self.tree.insert("", "end", iid=str(i), values=(record["name"], "当前分支" if record.get("is_head") else "历史版本", record["updated_at"][:19].replace("T", " ")))
        if self.records:
            self.tree.selection_set("0")
            self.keep_button.state(["!disabled"])
            self.use_button.state(["!disabled"])
        self.notice.set("选择后回到编辑窗口检查，点击“确认合并保存”才会写入。历史版本继续保留。")

    def select(self, _event=None):
        selected = self.tree.selection()
        if selected:
            record = self.records[int(selected[0])]
            _set_text(self.body, f"名称：{record['name']}\n颜色：{record['color']}\n状态：{'已归档' if record['archived'] else '使用中'}\n保存时间：{record['updated_at']}", readonly=True)

    def choose(self, use_selected):
        selected = self.tree.selection()
        if self.current and (selected or not use_selected):
            version = self.records[int(selected[0])] if use_selected else None
            self.manager.use_version(self.current, version)
            self.destroy()


class CategoryAssignmentDialog(_CategoryWindow):
    def __init__(self, app, asset_ids, on_changed=None):
        super().__init__(app)
        self._prepare(app, "设置素材分类", "780x580", on_changed)
        self.asset_ids = tuple(sorted(set(asset_ids)))
        self.records, self.memberships = [], {}
        self.checked = set()
        self._generation = 0
        self.query = tk.StringVar(self)
        outer = ttk.Frame(self, padding=20)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)
        ttk.Label(outer, text=f"为 {len(self.asset_ids)} 个素材设置分类", font=("Microsoft YaHei UI", 16, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(outer, text="勾选要添加或移除的分类。素材的其他分类会保留。", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 14))
        search = ttk.Frame(outer)
        search.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        search.columnconfigure(0, weight=1)
        entry = ttk.Entry(search, textvariable=self.query)
        entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        entry.bind("<Return>", lambda _: self.render())
        ttk.Button(search, text="搜索", command=self.render).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(search, text="＋ 管理分类", command=self.manage).grid(row=0, column=2)
        frame = ttk.Frame(outer)
        frame.grid(row=3, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(frame, columns=("pick", "name", "members"), show="headings", selectmode="browse", height=8)
        for key, label, width in (("pick", "选择", 55), ("name", "分类", 370), ("members", "所选素材中的归类", 170)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=50, stretch=key == "name")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<ButtonRelease-1>", self.toggle)
        self.tree.bind("<space>", self.toggle)
        self.selection_label = ttk.Label(outer, style="Muted.TLabel")
        self.selection_label.grid(row=4, column=0, sticky="w", pady=8)
        ttk.Label(outer, textvariable=self.notice, style="Muted.TLabel", wraplength=660).grid(row=5, column=0, sticky="ew", pady=(0, 10))
        bottom = ttk.Frame(outer)
        bottom.grid(row=6, column=0, sticky="ew")
        self.add_button = ttk.Button(bottom, text="添加所选分类", style="Primary.TButton", command=lambda: self.apply(False))
        self.add_button.pack(side="left")
        self.remove_button = ttk.Button(bottom, text="移除所选分类", command=lambda: self.apply(True))
        self.remove_button.pack(side="left", padx=8)
        ttk.Button(bottom, text="关闭", command=self.close).pack(side="right")
        self.refresh()

    def refresh(self):
        if not self._alive:
            return
        self._generation += 1
        generation, service, ids = self._generation, self.service, self.asset_ids
        def loaded(result):
            if generation != self._generation:
                return
            self.records, self.memberships = result
            self.checked.intersection_update(record["category_id"] for record in self.records)
            self.render()
            self.notice.set("点击分类行勾选，再选择添加或移除。" if self.records else "还没有分类，点击“管理分类”创建。")
        self._run(lambda: (service.categories.list_records(), service.categories.for_assets(ids)), loaded)

    def render(self):
        query = self.query.get().strip().casefold()
        self.tree.delete(*self.tree.get_children())
        names = Counter(record["name"] for record in self.records)
        counts = Counter(record["category_id"] for categories in self.memberships.values() for record in categories)
        for record in self.records:
            if query not in record["name"].casefold():
                continue
            identity = record["category_id"]
            label = record["name"] + (" · " + identity[:8] if names[record["name"]] > 1 else "")
            self.tree.insert("", "end", iid=identity, values=("✓" if identity in self.checked else "□", label, f"{counts[identity]} / {len(self.asset_ids)} 个"), tags=(identity,))
            self.tree.tag_configure(identity, foreground=record["color"])
        self.selection_label.configure(text=f"已勾选 {len(self.checked)} 个分类 · 操作影响 {len(self.asset_ids)} 个素材")

    def toggle(self, event=None):
        if self.saving:
            return
        identity = self.tree.identify_row(event.y) if event and getattr(event, "keysym", "") != "space" else next(iter(self.tree.selection()), "")
        if identity:
            self.checked.symmetric_difference_update((identity,))
            self.render()
            self.tree.selection_set(identity)

    def manage(self):
        manager = CategoryManager(self.app, on_changed=self.refresh)
        # Settings may have changed while this dialog was open.
        manager.service = self.service
        manager.refresh()
        return manager

    def apply(self, remove=False):
        if self.saving:
            return
        if not self.checked:
            self.notice.set("请先勾选分类。")
            return
        identities = tuple(sorted(self.checked))
        if len(identities) * len(self.asset_ids) > 1000:
            self.notice.set("单次最多处理 1,000 个素材与分类组合，请减少勾选数量。")
            return
        if remove and not messagebox.askyesno("移除分类", f"从 {len(self.asset_ids)} 个素材中移除勾选的 {len(identities)} 个分类？其他分类会保留。", parent=self):
            return
        self.saving = True
        self.add_button.state(["disabled"])
        self.remove_button.state(["disabled"])
        self.notice.set("正在更新素材分类…")
        service, assets = self.service, self.asset_ids
        def complete(result):
            self.saving = False
            self.add_button.state(["!disabled"])
            self.remove_button.state(["!disabled"])
            self.notice.set(f"已{'移除' if remove else '添加'}分类，更新 {result['changed']} 个归类关系。")
            self.refresh()
            self.changed()
        def failed(error):
            self.saving = False
            self.add_button.state(["!disabled"])
            self.remove_button.state(["!disabled"])
            self.notice.set(f"分类未保存：{error}")
        self._run(lambda: service.categories.assign(list(assets), list(identities), remove=remove), complete, failed)
