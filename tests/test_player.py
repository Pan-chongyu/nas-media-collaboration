from pathlib import Path
import os
import subprocess
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from media import _ffmpeg
from player import MediaPlayer, PlayerError, find_mpv


class PlayerTests(unittest.TestCase):
    def test_finds_bundled_mpv(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tools/mpv/mpv.exe"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"x")
            with patch.dict(os.environ, {"MPV_PATH": ""}):
                self.assertEqual(find_mpv(Path(folder)), path)

    def test_rejects_unsupported_media(self):
        with self.assertRaises(PlayerError):
            MediaPlayer(Path("missing-mpv.exe")).open("missing.txt")

    def test_old_session_events_cannot_change_new_playback(self):
        player = MediaPlayer()
        old_generation = player._generation
        player.stop()
        player._event(old_generation, {"event": "property-change", "name": "time-pos", "data": 55})
        self.assertEqual(player.elapsed, 0)

    def test_no_media_stat_on_calling_thread(self):
        player = MediaPlayer(Path("mpv.exe"))
        with patch("player.Path.is_file", side_effect=AssertionError("SMB stat must not run on Tk")), patch("player.threading.Thread.start"):
            player.embed(r"\\unreachable-nas\share\clip.mp4", 123)
        self.assertTrue(player.state.loading)
        player.close()


@unittest.skipUnless(os.name == "nt" and find_mpv() and _ffmpeg(), "requires Windows, mpv and ffmpeg")
class RealPlayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.clip = Path(cls.temp.name) / "中文 测试.mp4"
        subprocess.run([_ffmpeg(), "-y", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=320x180:rate=25", "-f", "lavfi", "-i", "sine=frequency=440",
                        "-t", "12", "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", str(cls.clip)], check=True, timeout=30, capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.root = tk.Tk()
        self.root.geometry("500x300")
        self.host = tk.Frame(self.root, bg="black")
        self.host.pack(fill="both", expand=True)
        self.root.update()
        self.player = MediaPlayer()
        self.player.embed(self.clip, self.host.winfo_id())
        self.player.attach(self.host.winfo_id(), self.host.winfo_width(), self.host.winfo_height())
        self.until(lambda: self.player.state.duration > 11 and self.player.elapsed > 0.15)

    def tearDown(self):
        self.player.stop()
        thread = self.player._thread
        if thread:
            thread.join(5)
            self.assertFalse(thread.is_alive(), "decoder process was not reaped")
        self.root.destroy()

    def until(self, predicate, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            if predicate():
                return
            if self.player.state.error:
                self.fail(self.player.state.error)
            time.sleep(0.02)
        self.fail("playback timeout: " + repr(self.player.state))

    def test_decoder_clock_pause_seek_volume_and_reparent(self):
        player = self.player
        self.until(lambda: player._video and player._video.hwnd)
        video = player._video
        self.assertEqual(video.user32.GetParent(video.hwnd), self.host.winfo_id())
        player.pause()
        self.until(lambda: player.state.paused)
        position = player.elapsed
        until = time.monotonic() + 0.3
        self.until(lambda: time.monotonic() >= until)
        self.assertLess(abs(player.elapsed - position), 0.12)
        player.seek(7)
        self.until(lambda: abs(player.elapsed - 7) < 0.25)
        self.assertTrue(player.state.paused)
        player.set_volume(27)
        self.until(lambda: abs(player.state.volume - 27) < 0.1)
        full = tk.Toplevel(self.root)
        full.attributes("-fullscreen", True)
        host = tk.Frame(full, bg="black")
        host.pack(fill="both", expand=True)
        full.update()
        process = player.process
        player.attach(host.winfo_id(), host.winfo_width(), host.winfo_height())
        self.assertEqual(video.user32.GetParent(video.hwnd), host.winfo_id())
        player.pause()
        self.until(lambda: not player.state.paused and player.elapsed > 7.3)
        player.attach(self.host.winfo_id(), self.host.winfo_width(), self.host.winfo_height())
        full.destroy()
        self.assertEqual(video.user32.GetParent(video.hwnd), self.host.winfo_id())
        self.assertIs(process, player.process)
        self.assertIsNone(process.poll())
        player.seek(11.8)
        self.until(lambda: player.state.ended)
        player.seek(0)
        if player.state.paused:
            player.pause()
        self.until(lambda: not player.state.ended and player.elapsed < 2 and not player.state.paused)

    def test_workspace_controls_and_navigation_with_real_media(self):
        from library import LibraryService
        from main import Workspace, store_settings
        self.player.stop()
        self.player._thread.join(5)
        self.root.withdraw()
        data_dir = Path(self.temp.name) / "workspace"
        store_settings(data_dir / "settings.json", {
            "nas_root": self.temp.name, "sync_root": str(Path(self.temp.name) / "sync"),
            "device_id": "player-ui-test", "auto_sync": False,
        })
        service = LibraryService(data_dir, self.temp.name, str(Path(self.temp.name) / "sync"), "player-ui-test")
        service.import_files([str(self.clip)])
        app = Workspace(data_dir=data_dir, auto_sync=False)
        errors = []
        app.report_callback_exception = lambda *error: errors.append(error)
        def until(predicate, timeout=10):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                app.update()
                if predicate():
                    return
                time.sleep(0.02)
            self.fail("UI playback timeout: " + repr(app.player.state))
        try:
            until(lambda: bool(app.records))
            app._select(app.records[0]["asset_id"])
            app.play_button.invoke()
            until(lambda: app.player.elapsed > 0.3 and not app.player_progress_scale.instate(["disabled"]))
            self.assertFalse(app.preview_image_label.winfo_ismapped())
            self.assertGreater(app.player_progress.get(), 0)
            app.play_button.invoke()
            until(lambda: app.player.state.paused and "继续" in app.play_button.cget("text"))
            app._player_drag_start()
            app.player_progress.set(50)
            app._player_drag_end()
            until(lambda: abs(app.player.elapsed - 6) < 0.25)
            process = app.player.process
            app.fullscreen_button.invoke()
            until(lambda: bool(app._fullscreen) and app.player._video.parent == app.fullscreen_surface.winfo_id())
            self.assertIs(process, app.player.process)
            app.fullscreen_play.invoke()
            until(lambda: not app.player.state.paused and app.player.elapsed > 6.2)
            # Exercise mpv's real key binding and IPC event back into Tk.
            app.player._commands.put(["keypress", "ESC"])
            until(lambda: app._fullscreen is None and app.player._video.parent == app.player_surface.winfo_id())
            self.assertIsNone(process.poll())
            app.show_page("任务中心")
            until(lambda: process.poll() is not None)
            self.assertFalse(app.player.playing)
            self.assertIsNone(app.player_job)
            until(lambda: not app.thumb_pending and not app.thumb_queue.unfinished_tasks)
            self.assertFalse(errors, errors)
        finally:
            for callback in app.tk.call("after", "info"):
                app.after_cancel(callback)
            app.close()

    def test_missing_file_fails_asynchronously(self):
        started = time.monotonic()
        self.player.open(Path(self.temp.name) / "missing.mp4")
        self.assertLess(time.monotonic() - started, 0.25)
        self.until(lambda: bool(self.player.state.error))
        self.assertFalse(self.player.playing)


if __name__ == "__main__":
    unittest.main()
