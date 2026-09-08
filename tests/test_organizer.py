from contextlib import closing
import copy
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from collaboration_store import CollaborationConflictError
from indexer import AssetRecord, open_index, root_key, upsert_records
from library import LibraryService
from organizer import ScriptOrganizer, parse_script


class ParseScriptTests(unittest.TestCase):
    def test_chinese_markdown_and_numbering_preserve_complete_body(self):
        for body in (
            "说明：本片为探店。\n\n镜头一：门店\n关键词：门店、招牌\n\n场景2：出餐\n记录上菜过程。\n",
            "# 开场\r\n门口招牌\r\n\r\n## 制作\r\n关键词: 烧烤, 炭火\r\n",
            "一、开场\n门店介绍\n二、制作\n厨师操作\n三、结束\n顾客评价",
            "第一场 外景\n外景画面\n第二场 内景\n内景画面",
            "第一段正文\n\n第二段正文\n\n第三段正文",
            "门店外景\n关键词：门店、招牌\n厨师制作\n顾客用餐",
        ):
            with self.subTest(body=body):
                sections = parse_script("探店", body)
                self.assertGreater(len(sections), 1)
                self.assertEqual("".join(section["text"] for section in sections), body)
                self.assertEqual(sections, parse_script("探店", body))
                self.assertEqual(len({section["section_id"] for section in sections}), len(sections))

    def test_explicit_keywords_precede_inferred_terms_and_numeric_names_do_not(self):
        result = parse_script("门店", "# 烧烤制作\n关键词：炭火、厨师\n画面是门店全景")
        self.assertEqual(result[0]["keywords"], ["炭火", "厨师"])
        self.assertEqual(parse_script("镜头 1", "2026-09-08 123456")[0]["keywords"], [])

    def test_size_limits_are_explicit_and_long_category_names_remain_distinct(self):
        for title, body in (("", "内容"), ("标题", " \n"), ("标题", "中" * 100001),
                            ("标题", "\n".join(f"镜头 {n}：内容" for n in range(51)))):
            with self.subTest(title=title, size=len(body)), self.assertRaises(ValueError):
                parse_script(title, body)
        body = "\n".join(f"镜头 {n}：" + "相同部分" * 20 for n in range(50))
        categories = [section["category_name"] for section in parse_script("非常长的脚本标题" * 15, body)]
        self.assertEqual(len(set(categories)), 50)
        self.assertTrue(all(len(name) <= 40 for name in categories))


class OrganizerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        # The media root intentionally does not exist. Only local index rows are
        # used, which also exercises offline NAS organization.
        self.root = r"\\offline-nas\team\素材"
        self.shared = self.base / "shared"
        self.a = self.client("node-a")
        self.b = self.client("node-b")
        self.organizer = ScriptOrganizer(self.a)
        self.rows = self.insert(self.a, ["烧烤特写.mov", "门店/2026-09-03 163256.mov", "2026-09-03 163410.mov",
                                         "采访.wav", "菜单.png", "厨师/切菜.mov"])
        self.insert(self.b, [row["relative_path"] for row in self.rows])
        self.ids = {row["relative_path"]: row["asset_id"] for row in self.rows}

    def tearDown(self):
        self.temp.cleanup()

    def client(self, node, root=None):
        return LibraryService(self.base / node, root or self.root, str(self.shared), node)

    def insert(self, service, relative_paths):
        records = []
        key = root_key(service.root)
        for relative in relative_paths:
            identity = hashlib.sha256(f"{key}\0{relative}".encode()).hexdigest()
            name = relative.rsplit("/", 1)[-1]
            suffix = Path(name).suffix
            records.append(AssetRecord(identity, key, service.root, service.root + "\\" + relative.replace("/", "\\"),
                                       relative, name, suffix, "audio" if suffix == ".wav" else "image" if suffix == ".png" else "video", 100, 1, identity))
        with closing(open_index(service.db_path)) as db:
            upsert_records(db, records)
        return service.page(limit=1000)[0]

    def script(self, **data):
        return self.a.collaboration.save("script", dict(title="探店脚本", body="# 门店\n关键词：门店\n# 制作\n关键词：烧烤", **data))

    def plan(self, script=None):
        if script:
            return self.organizer.plan(script["title"], script["body"], script["entity_id"], script["heads"])
        return self.organizer.plan("探店脚本", "# 门店\n关键词：门店\n# 制作\n关键词：烧烤")

    def selected(self, plan, *groups):
        return [dict(section_id=plan["sections"][index]["section_id"], category_name=name, asset_ids=assets)
                for index, name, assets in groups]

    def state(self, service=None):
        with closing(sqlite3.connect(str((service or self.a).db_path))) as db:
            return {name: db.execute("SELECT count(*) FROM " + name).fetchone()[0] for name in (
                "outbox", "category_revisions", "category_entities", "category_membership_events", "category_memberships",
                "collaboration_revisions", "collaboration_entities", "collaboration_assets")}

    def sync(self):
        for client in (self.a, self.b, self.a):
            self.assertEqual(client.sync_once()["status"], "done")

    def test_planning_reads_only_local_metadata_and_hydrates_existing_cache(self):
        asset = self.ids["烧烤特写.mov"]
        with closing(open_index(self.a.db_path)) as db, db:
            db.execute("INSERT INTO thumbnails VALUES(?,?,?,?,?)", (asset, asset, str(self.base / "cached.jpg"), "ready", "now"))
        before = self.state()
        with patch("pathlib.Path.stat", side_effect=AssertionError("must not stat media")), \
             patch("pathlib.Path.open", side_effect=AssertionError("must not open media")):
            plan = self.plan()
        self.assertEqual(before, self.state())
        first, second = plan["sections"]
        self.assertEqual(first["selected_ids"], [])
        self.assertEqual(first["candidates"][0]["asset_id"], self.ids["门店/2026-09-03 163256.mov"])
        self.assertIn("路径包含“门店”", first["candidates"][0]["reasons"])
        self.assertEqual(second["candidates"][0]["thumbnail"], str(self.base / "cached.jpg"))
        self.assertEqual(second["candidates"][0]["thumbnail_status"], "ready")

    def test_filename_path_category_ranking_editable_keywords_and_no_matches(self):
        category = self.a.categories.save({"name": "烧烤"})
        self.a.categories.assign([self.ids["菜单.png"]], [category["category_id"]])
        matches, total = self.organizer.match({"keywords": ["烧烤"]})
        self.assertEqual(total, 2)
        self.assertEqual([item["name"] for item in matches], ["烧烤特写.mov", "菜单.png"])
        self.assertIn("分类包含“烧烤”", matches[1]["reasons"])
        self.assertEqual(matches[1]["categories"][0]["name"], "烧烤")
        self.assertEqual(self.organizer.match({"title": "烧烤", "text": "烧烤", "keywords": []}), ([], 0))
        self.assertEqual(self.organizer.match({"keywords": ["不存在的主题"]}), ([], 0))
        self.assertEqual(self.organizer.match({"keywords": ["1", "2026", "mp4", "镜头"]}), ([], 0))
        plan = self.organizer.plan("旅行", "# 雪山\n关键词：雪山\n# 沙漠\n关键词：沙漠")
        self.assertEqual(len(plan["sections"]), 2)
        self.assertTrue(all(section["candidate_total"] == 0 for section in plan["sections"]))

    def test_bound_candidates_are_explicit_not_visual_matching(self):
        asset = self.ids["2026-09-03 163410.mov"]
        script = self.script(asset_ids=[asset])
        items, total = self.organizer.match({"keywords": ["海滩"]}, script["entity_id"])
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["asset_id"], asset)
        self.assertEqual(items[0]["reasons"], ["已绑定当前脚本（请确认是否适合本段）"])
        self.assertLess(items[0]["score"], 1)

    def test_candidate_limit_total_determinism_and_manual_search_paging(self):
        self.insert(self.a, [f"额外/烧烤 {index:03d}.mov" for index in range(45)])
        first = self.organizer.match({"keywords": ["烧烤"]}, limit=20)
        self.assertEqual(len(first[0]), 20)
        self.assertEqual(first[1], 46)
        self.assertEqual(first, self.organizer.match({"keywords": ["烧烤"]}, limit=20))
        page1, total = self.organizer.search("额外", limit=30)
        page2, total2 = self.organizer.search("额外", offset=30, limit=30)
        self.assertEqual((total, total2), (45, 45))
        self.assertEqual(len({item["asset_id"] for item in page1 + page2}), 45)
        for index in range(34):
            self.a.collaboration.save("script", {"title": f"分页脚本 {index}", "body": "正文"})
        scripts1, script_total = self.organizer.scripts("分页", limit=30)
        scripts2, _ = self.organizer.scripts("分页", offset=30, limit=30)
        self.assertEqual(script_total, 34)
        self.assertEqual(len({item["entity_id"] for item in scripts1 + scripts2}), 34)

    def test_atomic_apply_new_script_manual_selection_reuse_and_idempotency(self):
        unrelated = self.a.categories.save({"name": "原有标签"})
        reused = self.a.categories.save({"name": "Demo"})
        asset = self.ids["2026-09-03 163410.mov"]  # No match; explicitly reviewed manual selection.
        self.a.categories.assign([asset], [unrelated["category_id"]])
        plan = self.plan()
        selections = self.selected(plan, (0, "ＤＥＭＯ", [asset]), (1, "制作分类", [asset]))
        result = self.organizer.apply(plan, selections)
        self.assertEqual((result["categories_created"], result["memberships_changed"], result["assets_bound"], result["sections_applied"]), (1, 2, 1, 2))
        self.assertEqual(result["script"]["body"], plan["body"])
        self.assertEqual(result["script"]["asset_ids"], [asset])
        self.assertEqual(plan["expected_heads"], result["script"]["heads"])
        self.assertEqual(plan["script_id"], result["script"]["entity_id"])
        categories = self.a.categories.for_assets([asset])[asset]
        self.assertEqual({record["category_id"] for record in categories}, {unrelated["category_id"], reused["category_id"], next(record["category_id"] for record in categories if record["name"] == "制作分类")})
        before = self.state()
        repeated = self.organizer.apply(plan, selections)
        self.assertEqual(before, self.state())
        self.assertEqual((repeated["categories_created"], repeated["memberships_changed"], repeated["assets_bound"]), (0, 0, 0))
        self.assertEqual(result["script"], repeated["script"])
        self.sync()
        self.assertEqual(self.b.collaboration.get(result["script"]["entity_id"]), result["script"])
        self.assertEqual(self.b.categories.for_assets([asset])[asset], categories)

    def test_existing_script_preserves_other_fields_and_bindings(self):
        previous = self.ids["采访.wav"]
        chosen = self.ids["菜单.png"]
        script = self.script(asset_ids=[previous], status="已定稿", assignee="编辑", due_date="2026-09-30", author="编导")
        plan = self.plan(script)
        result = self.organizer.apply(plan, self.selected(plan, (0, "人工选片", [chosen])))
        updated = result["script"]
        for key in ("title", "body", "status", "assignee", "due_date", "author", "archived"):
            self.assertEqual(updated[key], script[key])
        self.assertEqual(updated["asset_ids"], sorted([chosen, previous]))
        self.assertEqual(result["assets_bound"], 1)
        self.assertEqual(len(self.a.collaboration.versions(script["entity_id"])), 2)

    def test_failure_after_category_writes_rolls_back_all_local_state_and_plan(self):
        plan = self.plan()
        before_plan = copy.deepcopy(plan)
        before = self.state()
        with patch.object(self.a.collaboration, "save", side_effect=RuntimeError("simulated disk error")), self.assertRaisesRegex(RuntimeError, "disk"):
            self.organizer.apply(plan, self.selected(plan, (0, "事务分类", [self.ids["菜单.png"]])))
        self.assertEqual(before, self.state())
        self.assertEqual(before_plan, plan)
        result = self.organizer.apply(plan, self.selected(plan, (0, "事务分类", [self.ids["菜单.png"]])))
        self.assertEqual(result["categories_created"], 1)
        self.assertEqual(result["script"]["entity_id"], before_plan["_target_script_id"])

    def test_invalid_groups_materials_and_categories_make_no_changes(self):
        plan = self.plan()
        valid = self.selected(plan, (0, "有效分类", [self.ids["菜单.png"]]))
        bads = [[], valid * 2, self.selected(plan, (0, "", [self.ids["菜单.png"]])),
                self.selected(plan, (0, "未分类", [self.ids["菜单.png"]])),
                self.selected(plan, (0, "a" * 41, [self.ids["菜单.png"]])),
                self.selected(plan, (0, "有效分类", ["f" * 64])),
                [dict(valid[0], section_id="missing")], [dict(valid[0], asset_ids="bad")]]
        before = self.state()
        for selections in bads:
            with self.subTest(selections=selections), self.assertRaises(ValueError):
                self.organizer.apply(plan, selections)
            self.assertEqual(before, self.state())

    def test_stale_saved_script_and_modified_plan_reject_without_writes(self):
        script = self.script()
        plan = self.plan(script)
        selections = self.selected(plan, (0, "不应创建", [self.ids["菜单.png"]]))
        self.a.collaboration.save("script", {"body": "别人的修改"}, script["entity_id"], script["heads"])
        before = self.state()
        with self.assertRaises(CollaborationConflictError):
            self.organizer.apply(plan, selections)
        with self.assertRaises(CollaborationConflictError):
            self.plan(script)
        self.assertEqual(before, self.state())
        fresh = self.plan()
        fresh["body"] += "\n改动"
        with self.assertRaisesRegex(ValueError, "内容已改变"):
            self.organizer.apply(fresh, self.selected(fresh, (0, "不应创建", [self.ids["菜单.png"]])))
        self.assertEqual(before, self.state())

    def test_successful_reapply_does_not_bypass_external_concurrent_edit(self):
        plan = self.plan()
        selections = self.selected(plan, (0, "关联分类", [self.ids["菜单.png"]]))
        result = self.organizer.apply(plan, selections)
        current = result["script"]
        self.a.collaboration.save("script", {"status": "待审核"}, current["entity_id"], current["heads"])
        before = self.state()
        with self.assertRaises(CollaborationConflictError):
            self.organizer.apply(plan, selections)
        self.assertEqual(before, self.state())

    def test_archive_kind_and_other_library_are_rejected(self):
        script = self.script()
        archived = self.a.collaboration.save("script", {"archived": True}, script["entity_id"], script["heads"])
        with self.assertRaisesRegex(ValueError, "归档"):
            self.plan(archived)
        order = self.a.collaboration.save("work_order", {"title": "工单", "body": "正文"})
        with self.assertRaisesRegex(ValueError, "脚本"):
            self.organizer.plan(order["title"], order["body"], order["entity_id"], order["heads"])
        other = self.client("node-other", r"\\offline-nas\another\素材")
        foreign_rows = self.insert(other, ["菜单.png"])
        self.assertEqual(ScriptOrganizer(other).scripts(), ([], 0))
        plan = self.plan()
        before = self.state()
        with self.assertRaisesRegex(ValueError, "当前素材库"):
            self.organizer.apply(plan, self.selected(plan, (0, "外库素材", [foreign_rows[0]["asset_id"]])))
        with self.assertRaisesRegex(ValueError, "不属于"):
            ScriptOrganizer(other).apply(plan, self.selected(plan, (0, "错误素材库", [self.ids["菜单.png"]])))
        self.assertEqual(before, self.state())

    def test_concurrent_script_and_ambiguous_category_conflicts_are_retained(self):
        script = self.script()
        self.sync()
        self.a.collaboration.save("script", {"status": "待审核"}, script["entity_id"], script["heads"])
        self.b.collaboration.save("script", {"body": "另一版本"}, script["entity_id"], script["heads"])
        self.sync()
        conflicted = self.a.collaboration.get(script["entity_id"])
        with self.assertRaisesRegex(CollaborationConflictError, "并行"):
            self.plan(conflicted)
        self.a.categories.save({"name": "同名分类"})
        self.b.categories.save({"name": "同名分类"})
        self.sync()
        plan = self.plan()
        before = self.state()
        with self.assertRaisesRegex(ValueError, "同名"):
            self.organizer.apply(plan, self.selected(plan, (0, "同名分类", [self.ids["菜单.png"]])))
        self.assertEqual(before, self.state())

    def test_final_bindings_and_pair_limits_are_checked_before_writes(self):
        many = [hashlib.sha256(str(index).encode()).hexdigest() for index in range(1000)]
        script = self.script(asset_ids=many)
        plan = self.plan(script)
        before = self.state()
        with self.assertRaisesRegex(ValueError, "合计"):
            self.organizer.apply(plan, self.selected(plan, (0, "超量", [self.ids["菜单.png"]])))
        self.assertEqual(before, self.state())
        plan = self.plan()
        with self.assertRaisesRegex(ValueError, "关联"):
            self.organizer.apply(plan, self.selected(plan, (0, "一组", many[:600]), (1, "二组", many[:600])))
        self.assertEqual(before, self.state())

    def test_unprojected_parallel_category_name_is_not_created_as_duplicate(self):
        category = self.a.categories.save({"name": "共同名称"})
        self.sync()
        self.a.categories.save({"name": "并行名称 A"}, category["category_id"], category["heads"])
        self.b.categories.save({"name": "并行名称 B"}, category["category_id"], category["heads"])
        self.sync()
        current = self.a.categories.get(category["category_id"])
        hidden_name = next(name for name in ("并行名称 A", "并行名称 B") if name != current["name"])
        plan = self.plan()
        before = self.state()
        with self.assertRaisesRegex(ValueError, "并行"):
            self.organizer.apply(plan, self.selected(plan, (0, hidden_name, [self.ids["菜单.png"]])))
        self.assertEqual(before, self.state())


if __name__ == "__main__":
    unittest.main()
