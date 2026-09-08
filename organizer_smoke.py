"""Exercise script-driven organization in an installed app using local fixtures."""
import json
from pathlib import Path
import time
import traceback
import tkinter as tk
from zipfile import ZipFile

from PIL import Image

from library import LibraryService


def run_smoke(app, report_path: Path):
    checks, errors = [], []
    state = {"step": 0, "started": time.monotonic()}
    app.report_callback_exception = lambda *error: errors.append("".join(traceback.format_exception(*error)))

    def finish(error=""):
        report_path.write_text(json.dumps({"ok": not error and not errors, "checks": checks,
            "errors": errors, "error": error}, ensure_ascii=False, indent=2), encoding="utf-8")
        for child in list(app.winfo_children()):
            if isinstance(child, tk.Toplevel):
                child.destroy()
        app.close()

    def tick():
        try:
            if errors:
                raise RuntimeError(errors[0])
            if time.monotonic() - state["started"] > 30:
                raise TimeoutError(f"Organizer smoke timed out at step {state['step']}")
            step = state["step"]
            if step == 0:
                media = app.data_dir / "organizer-media"
                media.mkdir()
                for name, color in (("门店外景.png", "#087f72"), ("人物采访.png", "#2563eb"), ("其他素材.png", "#475569")):
                    Image.new("RGB", (240, 135), color).save(media / name)
                app.settings.update(nas_root=str(media), sync_root=str(app.data_dir / "organizer-shared"),
                                    device_id="organizer-smoke", display_name="验证用户")
                app.service = LibraryService(app.data_dir, str(media), app.settings["sync_root"], app.settings["device_id"])
                assert app.service.scan()["changed"] == 3
                state["window"] = window = app.open_organizer()
                source = app.data_dir / "门店短片.docx"
                with ZipFile(source, "w") as archive:
                    archive.writestr("word/document.xml", '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
                    <w:p><w:r><w:t>拍摄场景：门店</w:t></w:r></w:p>
                    <w:p><w:r><w:t>01  0-8秒  门店外景</w:t></w:r></w:p>
                    <w:p><w:r><w:t>镜头：门店外景。</w:t></w:r></w:p>
                    <w:p><w:r><w:t>店长：欢迎光临。</w:t></w:r></w:p>
                    <w:p><w:r><w:t>02  9-20秒  人物采访</w:t></w:r></w:p>
                    <w:p><w:r><w:t>镜头：人物采访。</w:t></w:r></w:p>
                    </w:body></w:document>''')
                window.import_script(source)
                state["step"] = "import"
            elif step == "import" and not state["window"].busy:
                window = state["window"]
                assert window.title_var.get() == "门店短片"
                assert "店长：欢迎光临。" in window.body.get("1.0", "end-1c")
                assert window.dirty()
                checks.append("word_script_import")
                window.build_plan()
                state["step"] = 1
            elif step == 1 and state["window"].plan:
                window = state["window"]
                assert len(window.plan["sections"]) == 2
                assert [section["shot_number"] for section in window.plan["sections"]] == ["01", "02"]
                assert window.plan["sections"][1]["time_range"] == "9-20秒"
                assert all(section["candidates"] for section in window.plan["sections"])
                assert not app.service.categories.list_records()
                checks.append("script_sections_and_local_matches")
                for section in window.plan["sections"]:
                    window.section_tree.selection_set(section["section_id"])
                    window._section_selected()
                    window.toggle_candidate(section["candidates"][0]["asset_id"])
                window.review_plan()
                window.apply_plan()
                state["step"] = 2
            elif step == 2 and not state["window"].saving and app.service.collaboration.list_records("script")[1] == 1:
                script = app.service.collaboration.list_records("script")[0][0]
                assert len(script["asset_ids"]) == 2
                categories = app.service.categories.list_records()
                assert len(categories) == 2 and all(record["count"] == 1 for record in categories)
                assert not state["window"].dirty()
                checks.append("apply_classifications_and_script_bindings")
                state["script"] = script
                state["window"].iconify()
                app.preview_asset_id(script["asset_ids"][0])
                state["step"] = 3
            elif step == 3 and app.active_page == "素材库" and app.selected_id == state["script"]["asset_ids"][0]:
                checks.append("preview_organized_material")
                assert app.open_organizer() is state["window"]
                assert state["window"].plan is not None
                checks.append("return_to_organizer")
                finish()
                return
        except Exception:
            finish(traceback.format_exc())
            return
        app.after(75, tick)

    app.after(150, tick)
