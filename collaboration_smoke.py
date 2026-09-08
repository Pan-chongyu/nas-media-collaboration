"""Exercise the installed collaboration UI with isolated local fixtures."""
from __future__ import annotations

import json
from pathlib import Path
import time
import traceback

from PIL import Image

from library import LibraryService


def run_smoke(app, report_path: Path):
    checks = []
    state = {"step": 0, "started": time.monotonic()}
    errors = []
    app.report_callback_exception = lambda *error: errors.append("".join(traceback.format_exception(*error)))

    def finish(error=""):
        report_path.write_text(json.dumps({"ok": not error and not errors, "checks": checks,
                              "errors": errors, "error": error, "dnd": app.dnd_version},
                              ensure_ascii=False, indent=2), encoding="utf-8")
        # Test fixtures must never wait on discard-draft prompts after a failed check.
        for child in list(app.winfo_children()):
            if hasattr(child, "initial_data"):
                child.destroy()
        app.close()

    def tick():
        try:
            if time.monotonic() - state["started"] > 25:
                raise TimeoutError(f"Collaboration smoke timed out at step {state['step']}")
            if errors:
                raise RuntimeError(errors[0])
            step = state["step"]
            if step == 0:
                media = app.data_dir / "smoke-media"
                media.mkdir()
                Image.new("RGB", (240, 135), "#087f72").save(media / "采访 {预览} 01.png")
                app.settings.update(nas_root=str(media), sync_root=str(app.data_dir / "smoke-shared"),
                                    device_id="smoke-collaboration", display_name="验证用户")
                app.service = LibraryService(app.data_dir, str(media), app.settings["sync_root"], app.settings["device_id"])
                assert app.service.scan()["changed"] == 1
                state["asset"] = app.service.page()[0][0]
                app.show_page("脚本")
                editor = app.collaboration_page.open_editor(asset_id=state["asset"]["asset_id"], asset_name=state["asset"]["name"])
                editor.title_var.set("安装验证 · 拍摄脚本")
                editor.body.insert("1.0", "开场介绍，随后展示产品细节。")
                editor.save()
                state["editor"] = editor
                state["step"] = 1
            elif step == 1 and state["editor"].result:
                script = state["editor"].result
                assert script["asset_ids"] == [state["asset"]["asset_id"]]
                state["editor"].close()
                checks.append("create_script_with_binding")
                app.show_page("工单")
                editor = app.collaboration_page.open_editor(asset_id=state["asset"]["asset_id"], asset_name=state["asset"]["name"])
                editor.title_var.set("安装验证 · 初剪工单")
                editor.assignee_var.set("剪辑同事")
                editor.due_var.set("2026-12-31")
                editor.body.insert("1.0", "完成初剪，保留同期声。")
                editor.save()
                state["editor"] = editor
                state["step"] = 2
            elif step == 2 and state["editor"].result:
                order = state["editor"].result
                assert order["assignee"] == "剪辑同事" and order["due_date"] == "2026-12-31"
                state["editor"].close()
                checks.append("create_work_order")
                editor = app.collaboration_page.open_editor(order)
                editor.status_var.set("进行中")
                editor.save()
                state["editor"] = editor
                state["step"] = 3
            elif step == 3 and state["editor"].result:
                record = state["editor"].result
                assert record["status"] == "进行中"
                assert len(app.service.collaboration.versions(record["entity_id"])) == 2
                state["editor"].close()
                checks.append("edit_and_keep_history")
                app.collaboration_page.query.set("初剪")
                app.collaboration_page.refresh(reset=True)
                state["step"] = 4
            elif step == 4 and app.collaboration_page.total == 1 and app.collaboration_page._bound_ids:
                assert app.collaboration_page.records[0]["status"] == "进行中"
                checks.append("search_and_load_binding")
                app.preview_asset_id(state["asset"]["asset_id"])
                state["step"] = 5
            elif step == 5 and app.active_page == "素材库" and app.selected_id == state["asset"]["asset_id"]:
                checks.append("open_linked_material_preview")
                assert app.dnd_version
                finish()
                return
        except Exception:
            finish(traceback.format_exc())
            return
        app.after(70, tick)

    app.after(150, tick)
