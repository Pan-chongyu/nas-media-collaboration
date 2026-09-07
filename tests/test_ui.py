import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

from PIL import Image

from library import LibraryService
from main import PAGE_SIZE, Workspace, store_settings
from media import _ffmpeg


class WorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = tempfile.TemporaryDirectory()
        cls.video = Path(cls.fixtures.name) / "sample.mp4"
        ffmpeg = _ffmpeg()
        if ffmpeg:
            subprocess.run(
                [ffmpeg, "-y", "-nostdin", "-loglevel", "error", "-f", "lavfi",
                 "-i", "testsrc2=size=160x90:rate=5", "-t", "0.4", "-c:v",
                 "libx264", "-threads", "1", "-pix_fmt", "yuv420p", str(cls.video)],
                check=True, timeout=30, capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.media = self.base / "media"
        self.media.mkdir()
        for i in range(26):
            Image.new("RGB", (120, 80), (i * 7, 110, 70)).save(self.media / f"image-{i:02}.png")
        for directory, color in (("interview-a", "red"), ("interview-b", "blue")):
            folder = self.media / directory
            folder.mkdir()
            Image.new("RGB", (120, 80), color).save(folder / "interview.png")
        if self.video.exists():
            shutil.copyfile(self.video, self.media / self.video.name)
        self.count = 28 + int(self.video.exists())
        data_dir = self.base / "client"
        store_settings(data_dir / "settings.json", {
            "nas_root": str(self.media), "sync_root": str(self.base / "sync"),
            "publish_root": str(self.base / "publish"), "device_id": "ui-test-node",
            "auto_sync": False,
        })
        service = LibraryService(data_dir, str(self.media), str(self.base / "sync"), "ui-test-node")
        self.assertEqual(service.scan()["changed"], self.count)
        self.app = Workspace(data_dir=data_dir, auto_sync=False)
        self.callback_errors = []
        self.app.report_callback_exception = lambda *error: self.callback_errors.append(error)
        self._pump_until(lambda: self.app.total == self.count and len(self.app.records) == PAGE_SIZE)

    def tearDown(self):
        try:
            self._pump_until(lambda: not self.app.thumb_pending and not self.app.thumb_queue.unfinished_tasks)
            for callback in self.app.tk.call("after", "info"):
                self.app.after_cancel(callback)
            self.app.close()
            self.assertFalse(self.callback_errors, self.callback_errors)
        finally:
            self.temp.cleanup()

    def _pump_until(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.update()
            if predicate():
                self.app.update_idletasks()
                return
            time.sleep(0.01)
        self.fail("UI condition was not reached; status=" + self.app._status.get())

    def _settle(self, seconds=0.25):
        until = time.monotonic() + seconds
        self._pump_until(lambda: time.monotonic() >= until)

    def _search(self, value, count):
        self.app.search_var.set(value)
        self.app._request_page(reset=True)
        self._pump_until(lambda: self.app.total == count)

    def _assert_inside(self, widget, parent, horizontal=True, vertical=True):
        self.assertTrue(widget.winfo_ismapped(), str(widget))
        x = widget.winfo_rootx() - parent.winfo_rootx()
        y = widget.winfo_rooty() - parent.winfo_rooty()
        if horizontal:
            self.assertGreaterEqual(x, 0, str(widget))
            self.assertLessEqual(x + widget.winfo_width(), parent.winfo_width(), str(widget))
        if vertical:
            self.assertGreaterEqual(y, 0, str(widget))
            self.assertLessEqual(y + widget.winfo_height(), parent.winfo_height(), str(widget))

    def test_same_names_keep_separate_selection_and_thumbnails(self):
        self._search("interview.png", 2)
        self._pump_until(lambda: all(item["thumbnail_status"] == "ready" for item in self.app.records))
        first, second = self.app.records
        self.assertNotEqual(first["asset_id"], second["asset_id"])
        self.assertNotEqual(first["thumbnail"], second["thumbnail"])
        self.assertEqual(set(self.app.card_widgets), {first["asset_id"], second["asset_id"]})
        for item in (first, second):
            self.app._select(item["asset_id"])
            self.assertEqual(self.app._selected()["path"], item["path"])
            self.assertIn(item["relative_path"], self.app.preview_meta.cget("text"))
        self.app._set_view("list")
        self._settle()
        self.assertEqual(set(self.app.asset_tree.get_children()), {first["asset_id"], second["asset_id"]})
        self.app.asset_tree.selection_set(first["asset_id"])
        self._pump_until(lambda: self.app.selected_id == first["asset_id"])
        self.assertTrue(self.app.asset_tree.item(first["asset_id"], "image"))
        self.app._set_view("grid")
        self._settle()
        self.assertEqual(self.app.selected_id, first["asset_id"])

    def test_paging_search_and_type_filter_use_local_index(self):
        first_page = {item["asset_id"] for item in self.app.records}
        self.app.next_button.invoke()
        self._pump_until(lambda: len(self.app.records) == self.count - PAGE_SIZE)
        second_page = {item["asset_id"] for item in self.app.records}
        self.assertFalse(first_page & second_page)
        self.assertTrue(self.app.next_button.instate(["disabled"]))
        self._search("image-01", 1)
        self.assertEqual(self.app.page_number, 0)
        self.assertEqual(self.app.records[0]["name"], "image-01.png")
        self._search("no-such-asset", 0)
        self.assertEqual(self.app.records, [])
        self.assertTrue(self.app.previous_button.instate(["disabled"]))
        self.assertTrue(self.app.next_button.instate(["disabled"]))
        self.app.search_var.set("")
        self.app.type_var.set("视频")
        self.app._request_page(reset=True)
        self._pump_until(lambda: self.app.total == int(self.video.exists()))
        self.assertTrue(all(item["media_type"] == "video" for item in self.app.records))
        self.assertFalse((self.base / "sync").exists())

    def test_navigation_while_thumbnail_workers_finish(self):
        self.app.show_page("任务中心")
        self._pump_until(lambda: len(self.app.jobs_tree.get_children()) == 1)
        job = self.app.jobs_tree.get_children()[0]
        self.assertEqual(self.app.jobs_tree.item(job, "values")[1], "已完成")
        self.app.show_page("设置")
        self._settle()
        self.assertEqual(self.app.setting_vars["nas_root"].get(), str(self.media))
        self.assertFalse(self.app.autosync_var.get())
        self.app.show_page("素材库")
        self._pump_until(lambda: len(self.app.card_widgets) == PAGE_SIZE)
        self._pump_until(lambda: all(item["thumbnail_status"] == "ready" for item in self.app.records))
        stored = json.loads(self.app.config_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["device_id"], "ui-test-node")
        self.assertFalse((self.base / "sync").exists())

    def test_main_regions_fit_minimum_and_default_window(self):
        for width, height in ((1024, 700), (1360, 850)):
            with self.subTest(window=(width, height)):
                self.app.geometry(f"{width}x{height}")
                self.app._set_view("grid")
                self._settle()
                for widget in (self.app.page, self.app.library_surface, self.app.preview_image_label,
                               self.app.previous_button, self.app.next_button, self.app.page_label):
                    self._assert_inside(widget, self.app)
                for frame in self.app.card_widgets.values():
                    self._assert_inside(frame, self.app.asset_canvas, vertical=False)
                self.app._set_view("list")
                self._settle()
                self._assert_inside(self.app.asset_tree, self.app.library_surface)
                self.app.show_page("任务中心")
                self._settle()
                self._assert_inside(self.app.jobs_tree, self.app)
                self.app.show_page("设置")
                self._settle()
                for widget in self.app.page.winfo_children():
                    self._assert_inside(widget, self.app)
                self.app.show_page("素材库")
                self._settle()


if __name__ == "__main__":
    unittest.main()
