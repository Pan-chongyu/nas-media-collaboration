"""Real decoder smoke test available in source and frozen Windows builds."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import tempfile
import time

from media import _ffmpeg


def run_smoke(app, report_path: Path):
    temporary = tempfile.TemporaryDirectory(prefix="nas-player-smoke-")
    source = Path(temporary.name) / "中文 播放测试.mp4"
    report = {"ok": False, "mpv": str(app.player.mpv), "checks": []}
    context = {"stage": 0, "deadline": time.monotonic() + 35, "process": None}
    try:
        subprocess.run([_ffmpeg(), "-y", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=640x360:rate=25", "-f", "lavfi", "-i", "sine=frequency=440",
                        "-t", "15", "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", str(source)], check=True, capture_output=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:
        report["error"] = str(exc)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.cleanup()
        app.close()
        return

    def finish(error=None):
        if error:
            report["error"] = str(error)
        report["state"] = asdict(app.player.state)
        app._stop_player()
        def reap():
            process = context["process"]
            if process and process.poll() is None and time.monotonic() < context["deadline"]:
                app.after(50, reap)
                return
            report["closed"] = process is None or process.poll() is not None
            report["ok"] = not error and report["closed"]
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.cleanup()
            app.close()
        reap()

    def check():
        try:
            state = app.player.state
            if state.error:
                raise RuntimeError(state.error)
            if time.monotonic() > context["deadline"]:
                raise TimeoutError("player smoke stage " + str(context["stage"]))
            stage = context["stage"]
            if stage == 0:
                stat = source.stat()
                app.records = [{"asset_id": "smoke-video", "path": str(source), "name": source.name,
                                "relative_path": source.name, "mtime_ns": stat.st_mtime_ns,
                                "size": stat.st_size, "media_type": "video", "file_hash": "smoke",
                                "thumbnail": "", "thumbnail_status": "skipped"}]
                app._select("smoke-video")
                app.play_button.invoke()
                context["stage"] = 1
            elif stage == 1 and state.duration > 14 and state.elapsed > 0.3 and app.player._video.hwnd:
                video = app.player._video
                assert video.user32.GetParent(video.hwnd) == app.player_surface.winfo_id()
                assert not app.preview_image_label.winfo_ismapped()
                report["checks"].append("embedded-decoder-and-real-duration")
                context["process"] = app.player.process
                app.play_button.invoke()
                context["stage"] = 2
            elif stage == 2 and state.paused:
                report["checks"].append("pause")
                app._player_drag_start()
                app.player_progress.set(50)
                app._player_drag_end()
                context["stage"] = 3
            elif stage == 3 and abs(state.elapsed - state.duration / 2) < 0.3:
                report["checks"].append("seek-from-progress-slider")
                app._set_player_volume(31)
                context["stage"] = 4
            elif stage == 4 and abs(state.volume - 31) < 0.1:
                report["checks"].append("exact-volume")
                app.fullscreen_button.invoke()
                context["stage"] = 5
            elif stage == 5 and app.player._video.parent == app.fullscreen_surface.winfo_id():
                assert app.player.process is context["process"]
                report["checks"].append("fullscreen-preserves-decoder")
                app.fullscreen_play.invoke()
                context["stage"] = 6
            elif stage == 6 and not state.paused and state.elapsed > state.duration / 2 + 0.25:
                report["checks"].append("resume")
                app.player._commands.put(["keypress", "ESC"])
                context["stage"] = 7
            elif stage == 7 and not app._fullscreen:
                assert app.player._video.parent == app.player_surface.winfo_id()
                assert context["process"].poll() is None
                report["checks"].append("escape-returns-to-preview")
                finish()
                return
            app.after(80, check)
        except Exception as exc:
            finish(exc)
    app.after(600, check)
