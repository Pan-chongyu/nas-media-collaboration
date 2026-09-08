"""Category dialogs use isolated local material indexes and real Tk widgets."""
from pathlib import Path
import gc
import tempfile
import time
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from PIL import Image

from category_ui import CategoryManager, CategoryAssignmentDialog
from library import LibraryService


class CategoryUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        media = self.base / "media"
        media.mkdir()
        for number in range(3):
            Image.new("RGB", (30, 20), "#087f72").save(media / f"镜头-{number}.png")
        self.service = LibraryService(self.base / "local", str(media), str(self.base / "shared"), "category-ui")
        self.service.scan()
        self.assets = [record["asset_id"] for record in self.service.page()[0]]
        self.app = tk.Tk()
        self.app.service = self.service
        self.app._status = tk.StringVar(self.app)
        self.app.option_add("*Font", ("Microsoft YaHei UI", 10))
        ttk.Style(self.app).theme_use("clam")
        self.errors = []
        self.app.report_callback_exception = lambda *error: self.errors.append(error)

    def tearDown(self):
        for widget in list(self.app.winfo_children()):
            widget.destroy()
        for callback in self.app.tk.call("after", "info"):
            self.app.after_cancel(callback)
        self.app.destroy()
        self.app = None
        gc.collect()
        self.temp.cleanup()
        self.assertFalse(self.errors, self.errors)

    def pump(self, predicate):
        until = time.monotonic() + 8
        while time.monotonic() < until:
            self.app.update()
            if predicate():
                self.app.update_idletasks()
                return
            time.sleep(.01)
        self.fail("Category UI did not finish")

    def save(self, manager):
        old = manager.record["revision"] if manager.record else None
        manager.save()
        self.pump(lambda: not manager.saving)
        self.assertIsNotNone(manager.record, manager.notice.get())
        self.assertNotEqual(manager.record["revision"], old, manager.notice.get())
        return manager.record

    def test_create_rename_color_archive_restore(self):
        manager = CategoryManager(self.app)
        manager.name_var.set("人物采访")
        record = self.save(manager)
        self.assertEqual(record["name"], "人物采访")
        manager.name_var.set("门店人物采访")
        manager.choose_color("#2563eb")
        record = self.save(manager)
        self.assertEqual(record["color"], "#2563eb")
        self.service.categories.assign(self.assets, [record["category_id"]])
        manager.archived_var.set(True)
        with patch("category_ui.messagebox.askyesno", return_value=True):
            record = self.save(manager)
        self.assertEqual(self.service.page(category_id="__uncategorized__")[1], 3)
        manager.include_archived.set(True)
        manager.refresh()
        self.pump(lambda: any(item["archived"] for item in manager.records))
        manager.archived_var.set(False)
        self.save(manager)
        self.assertEqual(self.service.page(category_id=record["category_id"])[1], 3)

    def test_assignment_add_and_remove_preserve_other_categories(self):
        first = self.service.categories.save({"name": "门店", "color": "#087f72"})
        second = self.service.categories.save({"name": "人物", "color": "#2563eb"})
        self.service.categories.assign(self.assets, [first["category_id"]])
        dialog = CategoryAssignmentDialog(self.app, self.assets[:2])
        self.pump(lambda: len(dialog.records) == 2)
        dialog.query.set("人物")
        dialog.render()
        self.assertEqual(len(dialog.tree.get_children()), 1)
        dialog.checked.add(second["category_id"])
        dialog.apply()
        self.pump(lambda: not dialog.saving)
        found = self.service.categories.for_assets(self.assets)
        self.assertEqual({r["category_id"] for r in found[self.assets[0]]}, {first["category_id"], second["category_id"]})
        self.assertEqual(len(found[self.assets[2]]), 1)
        with patch("category_ui.messagebox.askyesno", return_value=True):
            dialog.apply(remove=True)
            self.pump(lambda: not dialog.saving)
        self.assertEqual([r["category_id"] for r in self.service.categories.for_assets(self.assets)[self.assets[0]]], [first["category_id"]])

    def test_stale_name_draft_survives_and_explicit_merge_keeps_history(self):
        record = self.service.categories.save({"name": "原分类", "color": "#087f72"})
        first, second = CategoryManager(self.app), CategoryManager(self.app)
        first.load_record(record)
        second.load_record(record)
        first.name_var.set("我的新名称")
        second.name_var.set("同事新名称")
        self.save(second)
        first.save()
        self.pump(lambda: not first.saving)
        self.assertEqual(first.name_var.get(), "我的新名称")
        self.assertIn("草稿仍在", first.notice.get())
        history = first.history()
        self.pump(lambda: history.current is not None)
        history.keep_button.invoke()
        self.assertTrue(first.resolving)
        self.assertEqual(self.service.categories.get(record["category_id"])["name"], "同事新名称")
        self.save(first)
        self.assertEqual(len(self.service.categories.versions(record["category_id"])), 3)
        self.assertEqual(self.service.categories.get(record["category_id"])["name"], "我的新名称")

    def test_duplicate_validation_does_not_clear_draft(self):
        self.service.categories.save({"name": "门店", "color": "#087f72"})
        manager = CategoryManager(self.app)
        manager.name_var.set("门店")
        manager.save()
        self.pump(lambda: not manager.saving)
        self.assertIsNone(manager.record)
        self.assertTrue(manager.dirty())
        self.assertIn("同名", manager.notice.get())

    def test_dialogs_fit_minimum_window(self):
        for window in (CategoryManager(self.app), CategoryAssignmentDialog(self.app, self.assets)):
            window.geometry("720x540")
            self.app.update()
            buttons = [window.save_button, window.history_button] if isinstance(window, CategoryManager) else [window.add_button, window.remove_button]
            for widget in [window.tree, *buttons]:
                self.assertTrue(widget.winfo_ismapped())
                self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), window.winfo_rootx() + window.winfo_width())
                self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), window.winfo_rooty() + window.winfo_height())


if __name__ == "__main__":
    unittest.main()
