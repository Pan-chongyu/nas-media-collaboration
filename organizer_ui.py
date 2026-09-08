"""Review script sections against local media metadata before classifying."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import queue
import re
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image, ImageOps, ImageTk

from collaboration_ui import _text, _set_text
from organizer import ScriptOrganizer


PAGE_SIZE = 30
BG = "#f1f4f7"
FG = "#172738"
MUTED = "#748293"


def _perform(operation, results, identity):
    """Queue plain errors rather than traceback cycles retaining a UI callback."""
    try:
        value = operation()
    except Exception as error:
        results.put((identity, None, str(error)))
    else:
        results.put((identity, value, None))


def _cached_thumbnails(records, cache_dir):
    """Only read this client's thumbnail cache, never original media or SMB."""
    images = {}
    directory = Path(cache_dir).resolve()
    for record in records:
        value = record.get("thumbnail")
        if not value or str(value).startswith(("\\\\", "//")):
            continue
        path = Path(value)
        try:
            if not path.resolve().is_relative_to(directory):
                continue
            with Image.open(path) as source:
                image = ImageOps.contain(source.convert("RGB"), (94, 54))
            frame = Image.new("RGB", (96, 56), "#172738")
            frame.paste(image, ((96 - image.width) // 2, (56 - image.height) // 2))
            images[record["asset_id"]] = frame
        except (OSError, ValueError):
            continue
    return images


class _Jobs:
    def _init_jobs(self):
        self._alive = True
        self._results = queue.Queue()
        self._callbacks = {}
        self._job_number = 0
        self._poll_id = self.after(35, self._poll_jobs)
        self.bind("<Destroy>", self._jobs_destroyed, add="+")

    def _jobs_destroyed(self, event):
        if event.widget is not self:
            return
        self._alive = False
        self._callbacks.clear()
        if self._poll_id:
            self.after_cancel(self._poll_id)
            self._poll_id = None

    def _run(self, operation, callback, on_error=None):
        self._job_number += 1
        identity = self._job_number
        self._callbacks[identity] = callback, on_error
        threading.Thread(target=_perform, args=(operation, self._results, identity),
                         daemon=True, name="organizer-local").start()

    def _poll_jobs(self):
        self._poll_id = None
        if not self._alive:
            return
        for _ in range(25):
            try:
                identity, result, error = self._results.get_nowait()
            except queue.Empty:
                break
            callback = self._callbacks.pop(identity, None)
            if callback:
                success, failure = callback
                if error is None:
                    success(result)
                elif failure:
                    failure(error)
                else:
                    self.notice.set("操作未完成：" + error)
            if not self._alive:
                return
        self._poll_id = self.after(35, self._poll_jobs)


class OrganizerWindow(_Jobs, tk.Toplevel):
    def __init__(self, app, script=None, on_changed=None):
        super().__init__(app)
        self.app, self.service, self.on_changed = app, app.service, on_changed
        self.organizer = ScriptOrganizer(self.service)
        self.script = self.plan = self.result = None
        self.saving = self.busy = False
        self.active_section_id = None
        self._generation = self._thumbnail_generation = 0
        self._reviewed = None
        self._images = {}
        self.title_var = tk.StringVar(self)
        self.source_note = tk.StringVar(self, value="粘贴脚本，或选择已保存的脚本")
        self.category_var = tk.StringVar(self)
        self.keywords_var = tk.StringVar(self)
        self.section_note = tk.StringVar(self)
        self.count_note = tk.StringVar(self, value="生成整理方案后，逐组预览并勾选素材")
        self.notice = tk.StringVar(self, value="按文件名、路径和已有分类匹配，画面内容请预览确认。")
        self.title("素材整理器")
        width = min(1180, max(800, self.winfo_screenwidth() - 60))
        height = min(790, max(570, self.winfo_screenheight() - 100))
        self.geometry(f"{width}x{height}")
        self.minsize(min(1000, width), min(700, height))
        self.configure(bg=BG)
        # A transient Toplevel cannot be iconified on Windows. Keep this as an
        # independent nonmodal window so the main in-app player can take focus.
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self._init_jobs()
        self._baseline = self._snapshot()
        if script:
            self.load_script(script)

    def _build(self):
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)
        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="素材整理器", font=("Microsoft YaHei UI", 18, "bold")).pack(side="left")
        ttk.Label(header, text="脚本 → 素材 → 分类", style="Muted.TLabel").pack(side="left", padx=18)
        ttk.Label(outer, text="按文件名、路径和已有分类匹配，画面内容请预览确认。",
                  style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(5, 14))
        style = ttk.Style(self)
        style.configure("Organizer.TNotebook", background=BG, borderwidth=0)
        style.configure("Organizer.TNotebook.Tab", background="#e5ebf0", foreground=MUTED,
                        padding=(14, 8), font=("Microsoft YaHei UI", 10))
        style.map("Organizer.TNotebook.Tab", background=[("selected", "white"), ("active", "#dcefe7")],
                  foreground=[("selected", "#087f72"), ("disabled", "#98a5b3")])
        self.notebook = ttk.Notebook(outer, style="Organizer.TNotebook")
        self.notebook.grid(row=2, column=0, sticky="nsew")
        self.source_tab = ttk.Frame(self.notebook, padding=14)
        self.review_tab = ttk.Frame(self.notebook, padding=12)
        self.result_tab = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(self.source_tab, text=" 1  选择脚本 ")
        self.notebook.add(self.review_tab, text=" 2  预览与分组 ", state="disabled")
        self.notebook.add(self.result_tab, text=" 3  确认分类 ", state="disabled")
        self._build_source()
        self._build_review()
        self._build_result()
        label = ttk.Label(outer, textvariable=self.notice, style="Muted.TLabel", wraplength=1040)
        label.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        label.bind("<Configure>", lambda event: label.configure(wraplength=max(150, event.width)))

    def _build_source(self):
        parent = self.source_tab
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(4, weight=1)
        toolbar = ttk.Frame(parent)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 13))
        self.picker_button = ttk.Button(toolbar, text="选择已保存脚本", command=self.open_script_picker)
        self.picker_button.pack(side="left")
        self.new_button = ttk.Button(toolbar, text="新建脚本", command=self.new_script)
        self.new_button.pack(side="left", padx=8)
        self.copy_button = ttk.Button(toolbar, text="复制为新脚本", command=self.copy_script, state="disabled")
        self.copy_button.pack(side="left")
        ttk.Label(parent, textvariable=self.source_note, style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 10))
        self.title_entry = ttk.Entry(parent, textvariable=self.title_var, font=("Microsoft YaHei UI", 12))
        self.title_entry.grid(row=2, column=0, sticky="ew")
        ttk.Label(parent, text="脚本标题 · 段落之间空一行，也支持“镜头 1 / 场景 2”和 Markdown 标题",
                  style="Muted.TLabel").grid(row=3, column=0, sticky="w", pady=(5, 9))
        frame, self.body = _text(parent, height=12)
        frame.grid(row=4, column=0, sticky="nsew")
        bottom = ttk.Frame(parent)
        bottom.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        ttk.Label(bottom, text="可写“关键词：门店、外景”提高匹配准确度", style="Muted.TLabel").pack(side="left")
        self.build_button = ttk.Button(bottom, text="生成整理方案 →", style="Primary.TButton", command=self.build_plan)
        self.build_button.pack(side="right")

    def _build_review(self):
        parent = self.review_tab
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(0, weight=1)
        groups = ttk.Frame(parent)
        groups.grid(row=0, column=0, sticky="nsew", padx=(0, 13))
        groups.rowconfigure(1, weight=1)
        groups.columnconfigure(0, weight=1)
        ttk.Label(groups, text="脚本分组", font=("Microsoft YaHei UI", 11, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.section_tree = ttk.Treeview(groups, columns=("title", "count"), show="headings", selectmode="browse", height=7)
        self.section_tree.heading("title", text="分组")
        self.section_tree.heading("count", text="已选")
        self.section_tree.column("title", width=165, minwidth=100, stretch=True)
        self.section_tree.column("count", width=46, minwidth=38, stretch=False)
        self.section_tree.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(groups, command=self.section_tree.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.section_tree.configure(yscrollcommand=scroll.set)
        self.section_tree.bind("<<TreeviewSelect>>", self._section_selected)
        detail = ttk.Frame(parent)
        detail.grid(row=0, column=1, sticky="nsew")
        detail.columnconfigure(1, weight=1)
        detail.rowconfigure(5, weight=1)
        ttk.Label(detail, text="分类名称").grid(row=0, column=0, sticky="w", padx=(0, 9))
        self.category_entry = ttk.Entry(detail, textvariable=self.category_var)
        self.category_entry.grid(row=0, column=1, columnspan=2, sticky="ew")
        ttk.Label(detail, text="匹配关键词").grid(row=1, column=0, sticky="w", pady=8, padx=(0, 9))
        self.keywords_entry = ttk.Entry(detail, textvariable=self.keywords_var)
        self.keywords_entry.grid(row=1, column=1, sticky="ew", pady=8)
        self.rematch_button = ttk.Button(detail, text="重新匹配", command=self.rematch)
        self.rematch_button.grid(row=1, column=2, padx=(8, 0))
        self.keywords_entry.bind("<Return>", lambda event: self.rematch())
        text_frame, self.section_body = _text(detail, height=2, readonly=True)
        text_frame.grid(row=2, column=0, columnspan=3, sticky="ew")
        ttk.Label(detail, textvariable=self.section_note, style="Muted.TLabel").grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 5))
        actions = ttk.Frame(detail)
        actions.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(0, 6))
        ttk.Button(actions, text="勾选本组候选", command=self.select_suggestions).pack(side="left")
        ttk.Button(actions, text="清空勾选", command=self.clear_selection).pack(side="left", padx=7)
        ttk.Button(actions, text="搜索补充素材", command=self.open_material_picker).pack(side="right")
        frame = ttk.Frame(detail)
        frame.grid(row=5, column=0, columnspan=3, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        style = ttk.Style(self)
        style.configure("Organizer.Treeview", rowheight=64)
        self.candidate_tree = ttk.Treeview(frame, columns=("check", "name", "reason"), show="tree headings", selectmode="browse", style="Organizer.Treeview", height=3)
        self.candidate_tree.heading("#0", text="缩略图")
        self.candidate_tree.column("#0", width=124, minwidth=124, stretch=False)
        for key, label, width, minimum in (("check", "选择", 45, 45), ("name", "素材名称", 205, 110), ("reason", "匹配依据", 230, 100)):
            self.candidate_tree.heading(key, text=label)
            self.candidate_tree.column(key, width=width, minwidth=minimum, stretch=key != "check")
        self.candidate_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(frame, command=self.candidate_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.candidate_tree.configure(yscrollcommand=scrollbar.set)
        self.candidate_tree.bind("<Button-1>", self._candidate_click)
        self.candidate_tree.bind("<space>", self._candidate_space)
        self.candidate_tree.bind("<Double-1>", lambda event: self.preview_selected())
        self.candidate_tree.tag_configure("checked", foreground="#087f72")
        self.candidate_detail = tk.StringVar(self)
        label = ttk.Label(detail, textvariable=self.candidate_detail, style="Muted.TLabel", wraplength=600)
        label.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(5, 0))
        label.bind("<Configure>", lambda event: label.configure(wraplength=max(100, event.width)))
        self.candidate_tree.bind("<<TreeviewSelect>>", self._candidate_selected)
        footer = ttk.Frame(parent)
        footer.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(11, 0))
        ttk.Label(footer, textvariable=self.count_note, style="Muted.TLabel").pack(side="left")
        self.review_button = ttk.Button(footer, text="检查分类结果 →", style="Primary.TButton", command=self.review_plan)
        self.review_button.pack(side="right")
        ttk.Button(footer, text="预览所选", command=self.preview_selected).pack(side="right", padx=8)

    def _build_result(self):
        parent = self.result_tab
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)
        ttk.Label(parent, text="确认后建立分类并绑定脚本", font=("Microsoft YaHei UI", 14, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(parent, text="原素材留在原位置，已有分类和脚本绑定会保留。只有勾选的素材会参与本次整理。",
                  style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(7, 12))
        frame, self.summary_body = _text(parent, height=12, readonly=True)
        frame.grid(row=2, column=0, sticky="nsew")
        buttons = ttk.Frame(parent)
        buttons.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        self.back_button = ttk.Button(buttons, text="← 返回调整", command=lambda: self.notebook.select(self.review_tab))
        self.back_button.pack(side="left")
        self.apply_button = ttk.Button(buttons, text="确认并应用分类", style="Primary.TButton", command=self.apply_plan, state="disabled")
        self.apply_button.pack(side="right")

    def _source(self):
        return self.title_var.get().strip(), self.body.get("1.0", "end-1c")

    def _snapshot(self):
        self._flush_section()
        groups = [] if not self.plan else [(section["section_id"], section.get("category_name", ""),
                  tuple(section.get("keywords", [])), tuple(sorted(section.get("selected_ids", [])))) for section in self.plan["sections"]]
        return self._source(), self.script["entity_id"] if self.script else None, groups

    def dirty(self):
        return self._snapshot() != self._baseline

    def _replace_allowed(self):
        if self.saving or self.busy:
            self.notice.set("正在处理，请完成后再更换脚本。")
            return False
        return not self.dirty() or messagebox.askyesno("保留当前方案", "更换脚本会放弃本窗口未应用的内容和勾选。继续更换？", parent=self)

    def _same_library(self):
        if self.app.service is self.service:
            return True
        self.notice.set("主窗口已切换素材库。请关闭此整理器，再从当前素材库重新打开；原方案未写入。")
        return False

    def load_script(self, record):
        if self.script and record["entity_id"] == self.script["entity_id"] and sorted(record.get("heads", [])) == sorted(self.script.get("heads", [])):
            return True
        if not self._replace_allowed():
            return False
        self._reset_source(record.get("title", ""), record.get("body", ""), record)
        return True

    def _reset_source(self, title, body, record=None):
        self._generation += 1
        self._thumbnail_generation += 1
        self.script = deepcopy(record)
        self.plan = self.result = self._reviewed = None
        self.active_section_id = None
        self.title_var.set(title)
        self.title_entry.configure(state="readonly" if record else "normal")
        _set_text(self.body, body, readonly=bool(record))
        self.copy_button.state(["!disabled"] if record else ["disabled"])
        self.source_note.set("已保存脚本 · 正文保持不变，可复制为新脚本再编辑" if record else "填写新脚本 · 应用分类时一起保存脚本")
        self.notebook.select(self.source_tab)
        self.notebook.tab(self.review_tab, state="disabled")
        self.notebook.tab(self.result_tab, state="disabled")
        self.section_tree.delete(*self.section_tree.get_children())
        self.candidate_tree.delete(*self.candidate_tree.get_children())
        self._images.clear()
        self.apply_button.state(["disabled"])
        self.notice.set("点击“生成整理方案”，然后逐组预览并勾选。")
        self._baseline = self._snapshot()

    def new_script(self):
        if self._replace_allowed():
            self._reset_source("", "")
            self.title_entry.focus_set()

    def copy_script(self):
        if self.script and self._replace_allowed():
            title, body = self._source()
            self._reset_source(title + " 副本", body)
            self._baseline = (("", ""), None, [])

    def open_script_picker(self):
        if not self.saving and not self.busy:
            return ScriptPicker(self)

    def build_plan(self):
        if self.saving or self.busy or not self._same_library():
            return
        title, body = self._source()
        if not title or not body.strip():
            self.notice.set("请先填写脚本标题和正文。")
            return
        if self.plan and self.dirty() and not messagebox.askyesno("重新生成方案", "重新生成会替换当前分组、关键词和勾选。继续？", parent=self):
            return
        self._generation += 1
        generation, organizer = self._generation, self.organizer
        identity = self.script["entity_id"] if self.script else None
        heads = list(self.script["heads"]) if self.script else None
        self.busy = True
        self.build_button.state(["disabled"])
        self.notice.set("正在拆分脚本并匹配本地素材索引…")

        def loaded(plan):
            if generation != self._generation:
                return
            self.busy = False
            self.build_button.state(["!disabled"])
            if self._source() != (title, body):
                self.notice.set("脚本在匹配期间已修改，请重新生成整理方案。")
                return
            self.plan = plan
            self.active_section_id = None
            self._reviewed = None
            self.notebook.tab(self.review_tab, state="normal")
            self.notebook.tab(self.result_tab, state="disabled")
            self.notebook.select(self.review_tab)
            self._render_sections()
            if plan["sections"]:
                self.section_tree.selection_set(plan["sections"][0]["section_id"])
                self._section_selected()
            baseline_source, baseline_script, _ = self._baseline
            self._baseline = baseline_source, baseline_script, self._snapshot()[2]
            self.notice.set("方案已生成，尚未修改分类。逐组预览并勾选素材，也可以搜索补充。")

        def failed(error):
            self.busy = False
            self.build_button.state(["!disabled"])
            self.notice.set("未生成方案，脚本已保留：" + error)

        self._run(lambda: organizer.plan(title, body, script_id=identity, expected_heads=heads), loaded, failed)

    def _section(self):
        return next((section for section in (self.plan or {}).get("sections", []) if section["section_id"] == self.active_section_id), None)

    def _flush_section(self):
        section = self._section()
        if section is not None:
            section["category_name"] = self.category_var.get().strip()
            section["keywords"] = [value.strip() for value in re.split(r"[，,、;；\n]+", self.keywords_var.get()) if value.strip()]

    def _render_sections(self):
        selected = self.section_tree.selection()
        self.section_tree.delete(*self.section_tree.get_children())
        for section in self.plan["sections"]:
            self.section_tree.insert("", "end", iid=section["section_id"], values=(section["title"], len(section.get("selected_ids", []))))
        if selected and self.section_tree.exists(selected[0]):
            self.section_tree.selection_set(selected[0])
        sections = [section for section in self.plan["sections"] if section.get("selected_ids")]
        count = len({identity for section in sections for identity in section["selected_ids"]})
        self.count_note.set(f"{len(self.plan['sections'])} 个分组 · 已选 {len(sections)} 组 / {count} 个素材")

    def _section_selected(self, _event=None):
        selected = self.section_tree.selection()
        if not selected or selected[0] == self.active_section_id:
            return
        self._flush_section()
        self.active_section_id = selected[0]
        section = self._section()
        self.category_var.set(section.get("category_name", ""))
        self.keywords_var.set("、".join(section.get("keywords", [])))
        _set_text(self.section_body, section.get("text", ""), readonly=True)
        self._render_candidates()

    def _render_candidates(self):
        self._thumbnail_generation += 1
        generation = self._thumbnail_generation
        self.candidate_tree.delete(*self.candidate_tree.get_children())
        self._images.clear()
        section = self._section()
        if not section:
            return
        candidates = section.get("candidates", [])
        selected = set(section.get("selected_ids", []))
        for item in candidates:
            identity = item["asset_id"]
            checked = identity in selected
            self.candidate_tree.insert("", "end", iid=identity, text="暂无封面", values=("☑" if checked else "☐", item["name"], "；".join(item.get("reasons", []))), tags=("checked",) if checked else ())
        self.section_note.set(f"显示 {len(candidates)} 个候选 · 匹配 {section.get('candidate_total', 0)} 个 · 已选 {len(selected)} 个" if candidates else "未找到候选 · 调整关键词或点击“搜索补充素材”")
        self.candidate_detail.set("点击选择列勾选 · 双击素材在主窗口预览，方案会保留")
        if candidates:
            self.candidate_tree.selection_set(candidates[0]["asset_id"])
            records, cache = deepcopy(candidates), self.service.cache_dir

            def loaded(images):
                if generation != self._thumbnail_generation:
                    return
                for identity, frame in images.items():
                    if self.candidate_tree.exists(identity):
                        photo = ImageTk.PhotoImage(frame, master=self)
                        self._images[identity] = photo
                        self.candidate_tree.item(identity, image=photo, text="")

            self._run(lambda: _cached_thumbnails(records, cache), loaded)

    def _candidate_selected(self, _event=None):
        selected = self.candidate_tree.selection()
        section = self._section()
        item = next((item for item in (section or {}).get("candidates", []) if selected and item["asset_id"] == selected[0]), None)
        if item:
            self.candidate_detail.set(item.get("relative_path", item["name"]) + "\n" + "；".join(item.get("reasons", [])))

    def _candidate_click(self, event):
        if self.candidate_tree.identify_column(event.x) == "#1":
            identity = self.candidate_tree.identify_row(event.y)
            if identity:
                self.toggle_candidate(identity)

    def _candidate_space(self, _event=None):
        selected = self.candidate_tree.selection()
        if selected:
            self.toggle_candidate(selected[0])
        return "break"

    def toggle_candidate(self, identity):
        if self.saving:
            return
        section = self._section()
        if not section or identity not in {item["asset_id"] for item in section.get("candidates", [])}:
            return
        selected = section.setdefault("selected_ids", [])
        if identity in selected:
            selected.remove(identity)
        else:
            selected.append(identity)
        self._selection_changed()

    def _selection_changed(self):
        self._reviewed = None
        self.apply_button.state(["disabled"])
        self._render_sections()
        section = self._section()
        selected = set(section.get("selected_ids", []))
        for identity in self.candidate_tree.get_children():
            values = list(self.candidate_tree.item(identity, "values"))
            values[0] = "☑" if identity in selected else "☐"
            self.candidate_tree.item(identity, values=values, tags=("checked",) if identity in selected else ())
        self.section_note.set(f"显示 {len(section.get('candidates', []))} 个候选 · 匹配 {section.get('candidate_total', 0)} 个 · 已选 {len(selected)} 个")

    def select_suggestions(self):
        section = self._section()
        if section and not self.saving:
            section["selected_ids"] = [item["asset_id"] for item in section.get("candidates", [])]
            self._selection_changed()

    def clear_selection(self):
        section = self._section()
        if section and not self.saving:
            section["selected_ids"] = []
            self._selection_changed()

    def rematch(self):
        if self.saving or self.busy or not self._section() or not self._same_library():
            return
        self._flush_section()
        section, organizer = deepcopy(self._section()), self.organizer
        identity = self.plan.get("script_id")
        self.busy = True
        self.rematch_button.state(["disabled"])
        self.notice.set("正在按新关键词重新匹配，已勾选素材会保留…")

        def loaded(result):
            self.busy = False
            self.rematch_button.state(["!disabled"])
            current = next(item for item in self.plan["sections"] if item["section_id"] == section["section_id"])
            candidates, total = result
            ids = {item["asset_id"] for item in candidates}
            retained = [item for item in current.get("candidates", []) if item["asset_id"] in current.get("selected_ids", []) and item["asset_id"] not in ids]
            current["candidates"], current["candidate_total"] = [*candidates, *retained], total
            if self.active_section_id == current["section_id"]:
                self._render_candidates()
            self._reviewed = None
            self.apply_button.state(["disabled"])
            self.notice.set("匹配已更新，原有勾选已保留。请预览确认。")

        def failed(error):
            self.busy = False
            self.rematch_button.state(["!disabled"])
            self.notice.set("重新匹配失败，原方案已保留：" + error)

        self._run(lambda: organizer.match(section, script_id=identity, limit=20), loaded, failed)

    def open_material_picker(self):
        if self._section() and not self.saving:
            return MaterialPicker(self, self.active_section_id)

    def add_materials(self, records, section_id=None):
        if self.saving or not self.plan:
            return
        section = next((item for item in self.plan["sections"] if item["section_id"] == (section_id or self.active_section_id)), None)
        if section is None:
            return
        candidates = section.setdefault("candidates", [])
        known = {item["asset_id"] for item in candidates}
        selected = section.setdefault("selected_ids", [])
        for record in records:
            identity = record["asset_id"]
            if identity not in known:
                candidates.append(dict(record, score=0, reasons=["手动搜索补充，请预览确认"]))
                known.add(identity)
            if identity not in selected:
                selected.append(identity)
        self._reviewed = None
        self.apply_button.state(["disabled"])
        self._render_sections()
        if section["section_id"] == self.active_section_id:
            self._render_candidates()

    def preview_selected(self):
        selected = self.candidate_tree.selection()
        if selected and not self.saving:
            self.preview_asset(selected[0])

    def preview_asset(self, identity):
        self._flush_section()
        self.app.preview_asset_id(identity)
        self.iconify()
        self.app._status.set("预览素材中 · 点击“素材整理器”继续原方案")

    def selections(self):
        self._flush_section()
        return [{"section_id": section["section_id"], "category_name": section.get("category_name", ""),
                 "asset_ids": list(section.get("selected_ids", []))} for section in (self.plan or {}).get("sections", []) if section.get("selected_ids")]

    def review_plan(self):
        if self.saving or self.busy or not self.plan:
            return
        choices = self.selections()
        if not choices:
            self.notice.set("请先为至少一个分组勾选素材。")
            return
        if any(not item["category_name"] or len(item["category_name"]) > 40 for item in choices):
            self.notice.set("已选分组的分类名称需要填写 1–40 个字。")
            return
        if not self.script and self._source() != (self.plan["title"], self.plan["body"]):
            self.notice.set("脚本正文已修改，请重新生成方案后再确认。")
            self.notebook.select(self.source_tab)
            return
        self._reviewed = deepcopy(choices)
        lines = [self.plan["title"], f"{len(choices)} 个分组 · {len({i for item in choices for i in item['asset_ids']})} 个不同素材", ""]
        for choice in choices:
            section = next(item for item in self.plan["sections"] if item["section_id"] == choice["section_id"])
            names = {item["asset_id"]: item.get("relative_path", item["name"]) for item in section.get("candidates", [])}
            lines.append(f"{section['title']} → {choice['category_name']}（{len(choice['asset_ids'])} 个）")
            lines.extend("  • " + names.get(identity, identity[:12]) for identity in choice["asset_ids"])
            lines.append("")
        _set_text(self.summary_body, "\n".join(lines), readonly=True)
        self.notebook.tab(self.result_tab, state="normal")
        self.notebook.select(self.result_tab)
        self.apply_button.state(["!disabled"])
        self.apply_button.configure(text="确认并应用分类")
        self.notice.set("请检查分类名称和素材清单，确认后应用。")

    def apply_plan(self):
        if self.saving or self.busy or not self._reviewed or not self._same_library():
            return
        choices = self.selections()
        if choices != self._reviewed or not self.script and self._source() != (self.plan["title"], self.plan["body"]):
            self._reviewed = None
            self.apply_button.state(["disabled"])
            self.notice.set("方案已调整，请返回“预览与分组”重新检查分类结果。")
            return
        self.saving = True
        self.apply_button.state(["disabled"])
        self.back_button.state(["disabled"])
        self.notebook.tab(self.source_tab, state="disabled")
        self.notebook.tab(self.review_tab, state="disabled")
        self.notice.set("正在保存分类和脚本绑定…")
        organizer, plan = self.organizer, deepcopy(self.plan)

        def loaded(result):
            self._finish_save()
            self.plan, self.result = plan, result
            self.script = deepcopy(result["script"])
            self.title_entry.configure(state="readonly")
            self.body.configure(state="disabled")
            self.copy_button.state(["!disabled"])
            self.source_note.set("已保存脚本 · 正文保持不变，可复制为新脚本再编辑")
            self._baseline = self._snapshot()
            self._reviewed = None
            self.notice.set(f"整理完成：新建 {result['categories_created']} 个分类，更新 {result['memberships_changed']} 条归类，新增绑定 {result['assets_bound']} 个素材。等待局域网同步。")
            self.apply_button.configure(text="已应用分类")
            if self.on_changed:
                self.on_changed()
            self.app._status.set("脚本素材整理已保存，等待局域网同步")

        def failed(error):
            self._finish_save()
            self.apply_button.state(["!disabled"])
            self.notice.set("未应用，方案和勾选已保留：" + error)

        self._run(lambda: organizer.apply(plan, choices), loaded, failed)

    def _finish_save(self):
        self.saving = False
        self.back_button.state(["!disabled"])
        self.notebook.tab(self.source_tab, state="normal")
        self.notebook.tab(self.review_tab, state="normal")

    def close(self):
        if self.saving:
            self.notice.set("正在保存，请完成后再关闭。")
        elif not self.dirty() or messagebox.askyesno("尚未应用", "关闭后将放弃本窗口未应用的方案和勾选。仍然关闭？", parent=self):
            self.destroy()


class _Picker(_Jobs, tk.Toplevel):
    def __init__(self, owner, title):
        super().__init__(owner)
        self.owner, self.organizer = owner, owner.organizer
        self.records, self.total, self.page = [], 0, 0
        self._generation = 0
        self.query = tk.StringVar(self)
        self.notice = tk.StringVar(self)
        self.title(title)
        self.geometry("820x540")
        self.minsize(650, 430)
        self.transient(owner)
        self.configure(bg=BG)
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)
        filters = ttk.Frame(outer)
        filters.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        filters.columnconfigure(0, weight=1)
        entry = ttk.Entry(filters, textvariable=self.query)
        entry.grid(row=0, column=0, sticky="ew")
        entry.bind("<Return>", lambda event: self.refresh(reset=True))
        ttk.Button(filters, text="搜索", command=lambda: self.refresh(reset=True)).grid(row=0, column=1, padx=(8, 0))
        self.tree = ttk.Treeview(outer, columns=("title", "detail"), show="headings", selectmode="extended")
        self.tree.heading("title", text="名称")
        self.tree.heading("detail", text="状态 / 位置")
        self.tree.column("title", width=280, minwidth=160)
        self.tree.column("detail", width=400, minwidth=150)
        self.tree.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(outer, command=self.tree.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<Double-1>", lambda event: self.choose())
        pages = ttk.Frame(outer)
        pages.grid(row=2, column=0, sticky="ew", pady=9)
        self.previous_button = ttk.Button(pages, text="上一页", command=lambda: self.step(-1), state="disabled")
        self.previous_button.pack(side="left")
        self.next_button = ttk.Button(pages, text="下一页", command=lambda: self.step(1), state="disabled")
        self.next_button.pack(side="left", padx=7)
        ttk.Label(pages, textvariable=self.notice, style="Muted.TLabel").pack(side="left", padx=9)
        self.buttons = ttk.Frame(outer)
        self.buttons.grid(row=3, column=0, sticky="ew")
        ttk.Button(self.buttons, text="关闭", command=self.destroy).pack(side="right")
        self.choose_button = ttk.Button(self.buttons, text="选择", style="Primary.TButton", command=self.choose)
        self.choose_button.pack(side="right", padx=8)
        self._init_jobs()

    def refresh(self, reset=False):
        if reset:
            self.page = 0
        self._generation += 1
        generation = self._generation
        query, offset, organizer = self.query.get().strip(), self.page * PAGE_SIZE, self.organizer
        self.notice.set("正在读取…")
        self.previous_button.state(["disabled"])
        self.next_button.state(["disabled"])
        self.choose_button.state(["disabled"])
        method = organizer.scripts if isinstance(self, ScriptPicker) else organizer.search

        def loaded(result):
            if generation != self._generation:
                return
            self.records, self.total = result
            self.tree.delete(*self.tree.get_children())
            for index, item in enumerate(self.records):
                self.tree.insert("", "end", iid=str(index), values=self.row(item))
            self.notice.set(f"共 {self.total:,} 条 · 第 {self.page + 1} 页")
            self.previous_button.state(["!disabled"] if self.page else ["disabled"])
            self.next_button.state(["!disabled"] if offset + len(self.records) < self.total else ["disabled"])
            self.choose_button.state(["!disabled"] if self.records else ["disabled"])
            if self.records:
                self.tree.selection_set("0")

        self._run(lambda: method(query=query, offset=offset, limit=PAGE_SIZE), loaded)

    def step(self, delta):
        self.page = max(0, self.page + delta)
        self.refresh()


class ScriptPicker(_Picker):
    def __init__(self, owner):
        super().__init__(owner, "选择已保存脚本")
        self.tree.configure(selectmode="browse")
        self.choose_button.configure(text="使用此脚本")
        self.refresh()

    def row(self, item):
        return item["title"], item.get("status", "") + (" · 存在并行版本，请先合并" if item.get("conflict_count") else "")

    def choose(self):
        selected = self.tree.selection()
        if selected and self.owner._alive and self.owner.load_script(self.records[int(selected[0])]):
            self.destroy()


class MaterialPicker(_Picker):
    def __init__(self, owner, section_id):
        self.section_id = section_id
        self.plan_snapshot = owner.plan
        super().__init__(owner, "搜索并补充素材")
        self.choose_button.configure(text="添加到当前分组")
        ttk.Button(self.buttons, text="预览所选", command=self.preview).pack(side="left")
        ttk.Label(self.buttons, text="Ctrl / Shift 多选", style="Muted.TLabel").pack(side="left", padx=10)
        self.refresh()

    def row(self, item):
        return item["name"], item.get("relative_path", "")

    def choose(self):
        selected = self.tree.selection()
        if selected and self.owner._alive and not self.owner.saving:
            if self.owner.plan is not self.plan_snapshot:
                self.notice.set("整理方案已更换，请关闭后重新搜索补充。")
                return
            self.owner.add_materials([self.records[int(index)] for index in selected], self.section_id)
            self.notice.set(f"已添加 {len(selected)} 个素材 · 可继续搜索")

    def preview(self):
        selected = self.tree.selection()
        if selected and self.owner._alive and not self.owner.saving:
            self.transient("")
            self.owner.preview_asset(self.records[int(selected[0])]["asset_id"])
            self.iconify()
