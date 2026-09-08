"""Organizer interactions use real Tk and isolated local material indexes."""

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

from library import LibraryService
from organizer_ui import OrganizerWindow, PAGE_SIZE, _cached_thumbnails
from tests.test_script_import import write_docx


SCRIPT = "镜头1：门店\n关键词：门店\n介绍门店外景。\n\n镜头2：厨房\n关键词：厨房\n厨师准备食物。"


class OrganizerUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        media = self.base / "media"
        media.mkdir()
        for name in ("门店外观", "厨房特写", *[f"其他素材-{i:02}" for i in range(33)]):
            Image.new("RGB", (120, 80), "#087f72").save(media / (name + ".png"))
        self.service = LibraryService(self.base / "local", str(media), str(self.base / "shared"), "organizer-ui")
        self.service.scan()
        self.assets = {record["name"]: record for record in self.service.page(limit=100)[0]}
        first = self.assets["门店外观.png"]
        self.service.thumbnail(first["asset_id"], first["file_hash"])
        self.app = tk.Tk()
        self.app.geometry("1020x740")
        self.app.service = self.service
        self.app.settings = {"device_id": "organizer-ui", "display_name": "测试编辑"}
        self.app._status = tk.StringVar(self.app)
        self.previews, self.errors = [], []
        self.app.preview_asset_id = self.previews.append
        self.app.option_add("*Font", ("Microsoft YaHei UI", 10))
        ttk.Style(self.app).theme_use("clam")
        self.app.report_callback_exception = lambda *error: self.errors.append(error)
        self.window = OrganizerWindow(self.app)

    def tearDown(self):
        for widget in list(self.app.winfo_children()):
            widget.destroy()
        # Drain already completed worker values on the main thread.
        for callback in self.app.tk.call("after", "info"):
            self.app.after_cancel(callback)
        self.app.destroy()
        self.window = self.app = None
        gc.collect()
        self.temp.cleanup()
        self.assertFalse(self.errors, self.errors)

    def pump(self, predicate):
        until = time.monotonic() + 10
        while time.monotonic() < until:
            self.app.update()
            if predicate():
                self.app.update_idletasks()
                return
            time.sleep(.01)
        self.fail("Organizer UI timed out: " + self.window.notice.get())

    def build(self, saved=False):
        if saved:
            record = self.service.collaboration.save("script", dict(title="门店探访", body=SCRIPT))
            self.window.load_script(record)
        else:
            self.window.title_var.set("门店探访")
            self.window.body.insert("1.0", SCRIPT)
        self.window.build_plan()
        self.pump(lambda: self.window.plan is not None and not self.window.busy)
        self.assertEqual(len(self.window.plan["sections"]), 2)
        self.assertFalse(any(section["selected_ids"] for section in self.window.plan["sections"]))
        return self.window.plan

    def test_preview_thumbnail_preserves_plan_and_applies_reviewed_choices(self):
        window = self.window
        self.assertFalse(window.dirty())
        plan = self.build()
        self.pump(lambda: bool(window._images))
        first = window._section()["candidates"][0]["asset_id"]
        window.toggle_candidate(first)
        window.candidate_tree.selection_set(first)
        window.preview_selected()
        self.assertEqual(self.previews, [first])
        self.assertIs(window.plan, plan)
        self.assertEqual(window._section()["selected_ids"], [first])
        window.deiconify()
        window.category_var.set("门店探访 · 外景")
        window.review_plan()
        self.assertFalse(window.apply_button.instate(["disabled"]))
        window.apply_plan()
        self.pump(lambda: not window.saving)
        self.assertIsNotNone(window.result, window.notice.get())
        self.assertEqual(window.result["assets_bound"], 1)
        self.assertEqual(self.service.collaboration.get(window.script["entity_id"])["asset_ids"], [first])
        self.assertEqual(self.service.categories.for_assets([first])[first][0]["name"], "门店探访 · 外景")
        self.assertFalse(window.dirty())

    def test_rematch_keeps_selection_while_switching_sections(self):
        window = self.window
        plan = self.build(saved=True)
        self.assertFalse(window.dirty())
        first = window._section()["candidates"][0]["asset_id"]
        window.toggle_candidate(first)
        window.keywords_var.set("不存在的关键词")
        window.rematch()
        window.section_tree.selection_set(plan["sections"][1]["section_id"])
        window._section_selected()
        self.pump(lambda: not window.busy)
        self.assertEqual(window.active_section_id, plan["sections"][1]["section_id"])
        self.assertEqual(plan["sections"][0]["selected_ids"], [first])
        self.assertEqual(plan["sections"][0]["candidates"][0]["asset_id"], first)
        self.assertEqual(plan["sections"][0]["candidate_total"], 0)
        self.assertTrue(window.dirty())

    def test_manual_material_search_paginates_and_adds_to_original_section(self):
        window = self.window
        plan = self.build()
        picker = window.open_material_picker()
        self.pump(lambda: len(picker.records) == PAGE_SIZE)
        picker.next_button.invoke()
        self.pump(lambda: picker.page == 1 and len(picker.records) == 5)
        identity = picker.records[0]["asset_id"]
        window.section_tree.selection_set(plan["sections"][1]["section_id"])
        window._section_selected()
        picker.choose()
        self.assertIn(identity, plan["sections"][0]["selected_ids"])
        self.assertEqual(plan["sections"][1]["selected_ids"], [])
        picker.query.set("厨房特写")
        picker.refresh(reset=True)
        self.pump(lambda: len(picker.records) == 1)
        picker.choose()
        self.assertIn(self.assets["厨房特写.png"]["asset_id"], plan["sections"][0]["selected_ids"])

    def test_saved_script_picker_paginates_and_same_snapshot_keeps_choices(self):
        for index in range(32):
            self.service.collaboration.save("script", dict(title=f"脚本-{index:02}", body=SCRIPT))
        picker = self.window.open_script_picker()
        self.pump(lambda: len(picker.records) == PAGE_SIZE)
        picker.next_button.invoke()
        self.pump(lambda: picker.page == 1 and len(picker.records) == 2)
        chosen = picker.records[0]
        picker.choose()
        self.assertEqual(self.window.script["entity_id"], chosen["entity_id"])
        self.assertEqual(self.window.body.cget("state"), "disabled")
        self.window.build_plan()
        self.pump(lambda: self.window.plan is not None and not self.window.busy)
        self.window.select_suggestions()
        original = self.window.plan
        with patch("organizer_ui.messagebox.askyesno", side_effect=AssertionError("Same source must not ask to discard")):
            self.assertTrue(self.window.load_script(chosen))
        self.assertIs(self.window.plan, original)
        self.assertTrue(self.window._section()["selected_ids"])

    def test_stale_script_apply_keeps_draft_and_does_not_create_categories(self):
        self.build(saved=True)
        window = self.window
        window.select_suggestions()
        window.category_var.set("编辑中的分类")
        window.review_plan()
        record = window.script
        self.service.collaboration.save("script", dict(body=SCRIPT + "\n同事修改"), entity_id=record["entity_id"], expected_heads=record["heads"])
        before = self.service.categories.list_records()
        window.apply_plan()
        self.pump(lambda: not window.saving)
        self.assertIsNone(window.result)
        self.assertEqual(self.service.categories.list_records(), before)
        self.assertEqual(window.category_var.get(), "编辑中的分类")
        self.assertTrue(window._section()["selected_ids"])
        self.assertIn("已保留", window.notice.get())

    def test_save_guard_review_invalidation_and_library_change(self):
        self.build()
        window = self.window
        window.select_suggestions()
        window.review_plan()
        window.category_var.set("审核后又修改")
        window.apply_plan()
        self.assertFalse(window.saving)
        self.assertIsNone(window.result)
        self.assertIn("重新检查", window.notice.get())
        window.review_plan()
        self.app.service = object()
        window.apply_plan()
        self.assertFalse(window.saving)
        self.assertIn("切换素材库", window.notice.get())
        self.app.service = self.service
        window.saving = True
        with patch("organizer_ui.messagebox.askyesno", side_effect=AssertionError("In-flight save must not ask")):
            window.close()
            self.assertFalse(window.load_script(dict(entity_id="different", heads=[])))
        self.assertTrue(window.winfo_exists())
        window.saving = False

    def test_source_and_close_discard_guard_and_minimum_layout(self):
        window = self.window
        window.title_var.set("未保存")
        with patch("organizer_ui.messagebox.askyesno", return_value=False):
            window.new_script()
            window.close()
        self.assertEqual(window.title_var.get(), "未保存")
        self.assertTrue(window.winfo_exists())
        window.title_var.set("")
        self.build()
        window.geometry("1000x700")
        window.select_suggestions()
        for tab, widgets in ((window.source_tab, (window.body, window.build_button, window.import_button)),
                             (window.review_tab, (window.section_tree, window.candidate_tree, window.review_button, window.detail_button)),
                             (window.result_tab, (window.summary_body, window.apply_button))):
            if tab is window.result_tab:
                window.review_plan()
            window.notebook.select(tab)
            self.app.update()
            for widget in widgets:
                self.assertTrue(widget.winfo_ismapped())
                self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), window.winfo_rootx() + window.winfo_width())
                self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), window.winfo_rooty() + window.winfo_height())

    def test_destroy_drops_late_worker_error_and_cache_reader_rejects_media_paths(self):
        completed, release = threading.Event(), threading.Event()
        def work():
            release.wait(3)
            completed.set()
            raise ValueError("late error")
        self.window._run(work, lambda result: self.fail("Destroyed window callback ran"))
        self.window.destroy()
        release.set()
        self.assertTrue(completed.wait(3))
        records = [dict(asset_id="original", thumbnail=self.assets["门店外观.png"]["path"]),
                   dict(asset_id="smb", thumbnail=r"\\example\private\image.png")]
        self.assertEqual(_cached_thumbnails(records, self.service.cache_dir), {})

    def test_import_word_uses_file_picker_then_groups_without_saving(self):
        path = write_docx(self.base / "晚班交接.docx", '''
            <w:p><w:r><w:t>01  0-8秒  门店开场</w:t></w:r></w:p>
            <w:p><w:r><w:t>镜头：门店外观。</w:t></w:r></w:p>
            <w:p><w:r><w:t>店长：准备交接。</w:t></w:r></w:p>
            <w:p><w:r><w:t>02  9-20秒  厨房收尾</w:t></w:r></w:p>
            <w:p><w:r><w:t>镜头：厨房特写。</w:t></w:r></w:p>
            ''')
        window = self.window
        with patch("organizer_ui.filedialog.askopenfilename", return_value=str(path)) as dialog:
            window.import_button.invoke()
        self.assertEqual(dialog.call_count, 1)
        self.pump(lambda: not window.busy)
        self.assertEqual(window.title_var.get(), "晚班交接")
        self.assertIn("已导入", window.source_note.get())
        self.assertTrue(window.dirty())
        self.assertEqual(window.body.cget("state"), "normal")
        window.build_plan()
        self.pump(lambda: window.plan is not None and not window.busy)
        self.assertEqual(len(window.plan["sections"]), 2)
        self.assertIn("店长：准备交接。", window.plan["sections"][0]["text"])
        self.assertEqual(window.section_tree.item(window.plan["sections"][0]["section_id"], "values")[1], "0-8秒")
        detail = window.show_section_details()
        self.assertIn("店长：准备交接。", detail.text_views["人物台词"].get("1.0", "end-1c"))
        self.assertNotIn("店长：", detail.text_views["镜头 / 动作"].get("1.0", "end-1c"))
        detail.geometry("580x430")
        self.app.update()
        self.assertTrue(detail.close_button.winfo_ismapped())
        self.assertLessEqual(detail.close_button.winfo_rooty() + detail.close_button.winfo_height(), detail.winfo_rooty() + detail.winfo_height())
        detail.destroy()
        self.assertTrue(all(section["candidates"] for section in window.plan["sections"]))
        self.assertEqual(self.service.collaboration.list_records("script")[1], 0)
        self.assertFalse(self.service.categories.list_records())

    def test_import_failure_and_declined_discard_keep_previous_choices(self):
        window = self.window
        original = self.build()
        window.select_suggestions()
        before = window._snapshot()
        with patch("organizer_ui.messagebox.askyesno", return_value=False), patch("organizer_ui.read_script_file") as reader:
            window.import_script(self.base / "rejected.docx")
            reader.assert_not_called()
        with patch("organizer_ui.messagebox.askyesno", return_value=True):
            window.import_script(self.base / "missing.docx")
        self.pump(lambda: not window.busy)
        self.assertIs(window.plan, original)
        self.assertEqual(before, window._snapshot())
        self.assertIn("原内容与勾选已保留", window.notice.get())

    def test_edit_during_import_rejects_stale_result_and_import_blocks_apply(self):
        window = self.window
        release = threading.Event()

        def reader(_path):
            release.wait(4)
            return dict(title="旧的导入结果", body="正文", source_name="fixture.docx", warnings=[])

        with patch("organizer_ui.read_script_file", side_effect=reader):
            window.import_script("fixture.docx")
            window.title_var.set("读取期间的新草稿")
            window.apply_plan()
            self.assertFalse(window.saving)
            release.set()
            self.pump(lambda: not window.busy)
        self.assertEqual(window.title_var.get(), "读取期间的新草稿")
        self.assertIn("已保留原方案", window.notice.get())
        self.assertIsNone(window.plan)


if __name__ == "__main__":
    unittest.main()
