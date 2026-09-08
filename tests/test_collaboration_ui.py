"""Desktop workflow tests using only temporary local media and databases."""

from pathlib import Path
import gc
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from PIL import Image

from collaboration_ui import CollaborationPage, PAGE_SIZE
from library import LibraryService


class CollaborationUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.media = self.base / "media"
        self.media.mkdir()
        for number in range(35):
            Image.new("RGB", (12, 8), (20, number * 5, 80)).save(self.media / f"clip-{number:02}.png")
        self.service = LibraryService(self.base / "local", str(self.media), str(self.base / "shared"), "ui-collaboration")
        self.service.scan()
        self.app = tk.Tk()
        self.app.geometry("824x640")
        self.app.service = self.service
        self.app.settings = {"device_id": "ui-collaboration", "display_name": "编辑甲"}
        self.app._status = tk.StringVar(self.app)
        self.previews = []
        self.app.preview_asset_id = self.previews.append
        ttk.Style(self.app).theme_use("clam")
        self.app.option_add("*Font", ("Microsoft YaHei UI", 10))
        self.callback_errors = []
        self.app.report_callback_exception = lambda *args: self.callback_errors.append(args)
        self.page = None

    def tearDown(self):
        for child in list(self.app.winfo_children()):
            child.destroy()
        for callback in self.app.tk.call("after", "info"):
            self.app.after_cancel(callback)
        self.app.destroy()
        self.page = None
        self.app = None
        gc.collect()
        self.temp.cleanup()
        self.assertFalse(self.callback_errors, self.callback_errors)

    def pump(self, predicate, timeout=8):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            self.app.update()
            if predicate():
                self.app.update_idletasks()
                return
            time.sleep(0.01)
        self.fail("UI operation timed out; " + (self.page.notice.get() if self.page else ""))

    def open_page(self, kind="script"):
        if self.page:
            self.page.destroy()
        self.page = CollaborationPage(self.app, self.app, kind)
        self.page.pack(fill="both", expand=True)
        self.pump(lambda: "正在读取" not in self.page.notice.get())
        return self.page

    def save_editor(self, editor):
        before = editor.result
        editor.save_button.invoke()
        self.pump(lambda: not editor.saving)
        self.assertIsNot(editor.result, before, editor.notice.get())
        return editor.result

    def test_create_script_bind_search_beyond_first_page_edit_and_preview(self):
        page = self.open_page()
        editor = page.open_editor()
        editor.title_var.set("夏日探店脚本")
        editor.body.insert("1.0", "开场：街景\n采访：介绍菜单")
        editor.status_var.set("待审核")
        editor.search_assets(reset=True)
        self.pump(lambda: len(editor.search_records) == PAGE_SIZE)
        editor.search_next.invoke()
        self.pump(lambda: editor.search_page == 1 and len(editor.search_records) == 5)
        editor.add_selected_asset()
        first_asset = editor.asset_ids[0]
        editor.search_var.set("clip-02")
        editor.search_assets(reset=True)
        self.pump(lambda: len(editor.search_records) == 1)
        editor.add_selected_asset()
        self.assertEqual(len(editor.asset_ids), 2)
        editor.binding_list.selection_clear(0, "end")
        editor.binding_list.selection_set(0)
        editor.remove_selected_asset()
        self.assertNotIn(first_asset, editor.asset_ids)
        record = self.save_editor(editor)
        self.assertEqual(record["status"], "待审核")
        self.assertEqual(record["asset_ids"], editor.asset_ids)
        self.pump(lambda: page.total == 1 and page.bound_list.size() == 1 and page.bound_list.get(0).endswith("clip-02.png"))
        page.preview_bound()
        self.assertEqual(self.previews, record["asset_ids"])
        editor.body.insert("end", "\n结尾：店名")
        editor.status_var.set("已定稿")
        saved = self.save_editor(editor)
        self.assertEqual(saved["status"], "已定稿")
        self.assertEqual(len(self.service.collaboration.versions(record["entity_id"])), 2)
        self.assertFalse((self.base / "shared").exists())

    def test_work_order_validation_status_search_archive_and_restore(self):
        page = self.open_page("work_order")
        editor = page.open_editor()
        editor.save_button.invoke()
        self.assertIn("标题", editor.notice.get())
        editor.title_var.set("门店宣传片交付")
        editor.body.insert("1.0", "9:16，时长 45 秒；完成粗剪后审核")
        editor.assignee_var.set("小林")
        editor.due_var.set("2026-02-30")
        editor.save_button.invoke()
        self.assertIn("有效", editor.notice.get())
        editor.due_var.set("2026-09-30")
        editor.status_var.set("进行中")
        record = self.save_editor(editor)
        self.assertEqual(record["assignee"], "小林")
        self.assertEqual(record["due_date"], "2026-09-30")
        self.pump(lambda: page.total == 1)
        page.query.set("小林")
        page.status_filter.set("进行中")
        page.refresh(reset=True)
        self.pump(lambda: "正在读取" not in page.notice.get())
        self.assertEqual(page.total, 1)
        with patch("collaboration_ui.messagebox.askyesno", return_value=True):
            page.archive_selected()
            self.pump(lambda: page.total == 0)
        page.include_archived.set(True)
        page.refresh()
        self.pump(lambda: page.total == 1)
        self.assertTrue(page.records[0]["archived"])
        self.assertEqual(page.archive_button.cget("text"), "恢复归档")
        with patch("collaboration_ui.messagebox.askyesno", return_value=True):
            page.archive_selected()
            self.pump(lambda: page.records and not page.records[0]["archived"])
        page.status_filter.set("已完成")
        page.refresh(reset=True)
        self.pump(lambda: page.total == 0)

    def test_stale_editor_retains_draft_until_explicit_merge(self):
        seed = self.service.collaboration.save("script", {"title": "并行修改", "body": "最初内容"})
        page = self.open_page()
        mine = page.open_editor(seed)
        theirs = page.open_editor(seed)
        mine.body.insert("end", "\n我的草稿")
        theirs.body.insert("end", "\n另一个编辑者")
        other = self.save_editor(theirs)
        mine.save_button.invoke()
        self.pump(lambda: not mine.saving)
        self.assertIsNone(mine.result)
        self.assertIn("草稿仍在", mine.notice.get())
        self.assertIn("我的草稿", mine.body.get("1.0", "end"))
        page.refresh()
        self.pump(lambda: page.records[0]["revision"] == other["revision"])
        self.assertIn("我的草稿", mine.body.get("1.0", "end"))
        history = mine.open_history()
        self.pump(lambda: history.current is not None)
        self.assertEqual(len(history.versions), 2)
        history.keep_button.invoke()
        self.assertTrue(mine.resolving)
        self.assertEqual(mine.save_button.cget("text"), "确认合并保存")
        self.assertEqual(self.service.collaboration.get(seed["entity_id"])["revision"], other["revision"])
        resolved = self.save_editor(mine)
        self.assertIn("我的草稿", resolved["body"])
        self.assertEqual(len(self.service.collaboration.versions(seed["entity_id"])), 3)
        self.assertFalse(mine.resolving)

    def test_history_can_restore_selected_version_without_immediate_write(self):
        first = self.service.collaboration.save("script", {"title": "原版脚本", "body": "原版正文"})
        latest = self.service.collaboration.save("script", {"title": "新版脚本", "body": "新版正文"},
                                                 entity_id=first["entity_id"], expected_heads=first["heads"])
        page = self.open_page()
        editor = page.open_editor(latest)
        history = editor.open_history()
        self.pump(lambda: len(history.versions) == 2)
        index = next(index for index, item in enumerate(history.versions) if item["revision"] == first["revision"])
        history.tree.selection_set(str(index))
        history.use_button.invoke()
        self.assertEqual(editor.title_var.get(), "原版脚本")
        self.assertEqual(self.service.collaboration.get(first["entity_id"])["title"], "新版脚本")
        restored = self.save_editor(editor)
        self.assertEqual(restored["title"], "原版脚本")

    def test_pagination_and_editor_survives_page_navigation(self):
        for number in range(34):
            self.service.collaboration.save("script", {"title": f"脚本 {number:02}"})
        page = self.open_page()
        self.assertEqual(len(page.records), PAGE_SIZE)
        first_ids = {record["entity_id"] for record in page.records}
        page.next_button.invoke()
        self.pump(lambda: len(page.records) == 4)
        self.assertFalse(first_ids & {record["entity_id"] for record in page.records})
        editor = page.open_editor(page.records[0])
        editor.body.insert("end", "导航期间的修改")
        self.open_page("work_order")
        result = self.save_editor(editor)
        self.assertIn("导航期间的修改", result["body"])
        self.assertFalse(self.callback_errors)

    def test_destroy_during_read_has_no_tk_calls_from_worker(self):
        page = self.open_page()
        started, finish, done = threading.Event(), threading.Event(), threading.Event()
        original = self.service.collaboration.list_records

        def slow(*args, **kwargs):
            started.set()
            finish.wait(5)
            try:
                return original(*args, **kwargs)
            finally:
                done.set()

        with patch.object(self.service.collaboration, "list_records", side_effect=slow):
            page.refresh()
            self.pump(started.is_set)
            page.destroy()
            finish.set()
            self.pump(done.is_set)
        self.assertFalse(page._alive)

    def test_controls_fit_at_minimum_window_and_binding_preview(self):
        page = self.open_page("work_order")
        self.pump(lambda: page.winfo_width() > 800)
        for widget in (page.tree, page.preview_button, page.next_button, page.archive_button):
            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), self.app.winfo_rootx() + self.app.winfo_width())
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), self.app.winfo_rooty() + self.app.winfo_height())
        asset = self.service.page(query="clip-34")[0][0]
        editor = page.open_editor(asset_id=asset["asset_id"], asset_name=asset["name"])
        editor.geometry("720x540")
        editor.notebook.select(editor.bindings_tab)
        self.app.update()
        editor.preview_binding()
        self.assertEqual(self.previews, [asset["asset_id"]])
        for widget in (editor.save_button, editor.history_button, editor.search_next, editor.binding_list):
            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), editor.winfo_rootx() + editor.winfo_width())
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), editor.winfo_rooty() + editor.winfo_height())

    def test_bind_material_to_existing_script_after_searching_picker(self):
        for number in range(33):
            self.service.collaboration.save("script", {"title": f"门店脚本 {number:02}", "body": "原有正文"})
        page = self.open_page()
        asset = self.service.page(query="clip-34")[0][0]
        picker = page.bind_asset(asset["asset_id"], asset["name"])
        self.pump(lambda: len(picker.records) == PAGE_SIZE)
        picker.next_button.invoke()
        self.pump(lambda: len(picker.records) == 3)
        picker.query.set("门店脚本 32")
        picker.refresh(reset=True)
        self.pump(lambda: len(picker.records) == 1)
        identity = picker.records[0]["entity_id"]
        picker.choose_button.invoke()
        editor = picker.result
        self.assertEqual(editor.title_var.get(), "门店脚本 32")
        self.assertEqual(editor.body.get("1.0", "end-1c"), "原有正文")
        self.assertEqual(editor.asset_ids, [asset["asset_id"]])
        self.assertEqual(self.service.collaboration.get(identity)["asset_ids"], [])
        saved = self.save_editor(editor)
        self.assertEqual(saved["entity_id"], identity)
        self.assertEqual(saved["asset_ids"], [asset["asset_id"]])


if __name__ == "__main__":
    unittest.main()
