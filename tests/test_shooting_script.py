from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from indexer import AssetRecord, open_index, root_key, upsert_records
from library import LibraryService
from organizer import parse_script, ScriptOrganizer


def shooting_body():
    context = "短片：雨天书店\r\n时长：约一分钟\r\n人物：店主、读者\r\n\r\n"
    shots = []
    for index, (start, end, scene) in enumerate((
        (0, 7, "雨伞入口"), (8, 16, "书架取书"), (17, 25, "窗边阅读"),
        (26, 34, "整理桌面"), (35, 43, "递上热茶"), (44, 52, "书页特写"),
        (53, 61, "门口告别"),
    ), 1):
        shots.append(f"{index:02d}  {start}-{end}秒  {scene}\r\n"
                     f"镜头：拍摄{scene}。\r\n镜头：切换近景。\r\n"
                     "读者（轻声）：假期准备去海岛旅行。\r\n"
                     "动作：店主收好雨伞。\r\n"
                     + ("结尾字幕：给每一次相遇留一盏灯。\r\n" if index == 7 else "")
                     + "\r\n")
    return context + "".join(shots), context


class ShootingScriptTests(unittest.TestCase):
    def test_seven_shots_preserve_metadata_directions_dialogue_and_ending(self):
        body, context = shooting_body()
        sections = parse_script("雨天书店", body)
        self.assertEqual(len(sections), 7)
        self.assertEqual("".join(section["text"] for section in sections), body)
        self.assertEqual(sections, parse_script("雨天书店", body))
        self.assertEqual([section["shot_number"] for section in sections], [f"{n:02d}" for n in range(1, 8)])
        self.assertEqual(sections[0]["title"], "雨伞入口")
        self.assertEqual(sections[0]["start_seconds"], 0)
        self.assertEqual(sections[0]["end_seconds"], 7)
        self.assertEqual(sections[0]["time_range"], "0-7秒")
        self.assertTrue(all(section["context_text"] == context for section in sections))
        self.assertTrue(all(section["format"] == "timed_shooting" for section in sections))
        self.assertTrue(all(section["visual_text"].count("镜头：") == 2 for section in sections))
        self.assertIn("读者（轻声）：", sections[0]["dialogue_text"])
        self.assertNotIn("海岛", sections[0]["visual_text"])
        self.assertIn("结尾字幕：", sections[-1]["dialogue_text"])
        self.assertTrue(sections[0]["text"].startswith(context))
        self.assertEqual(len({section["section_id"] for section in sections}), 7)

    def test_keyword_evidence_comes_from_direction_not_dialogue_or_preamble(self):
        body = "人物：宇航员\n01 0-8秒 转折\n镜头：书架和窗边。\n店主：想去热带海岛。\n字幕：银河梦想。"
        section = parse_script("测试", body)[0]
        self.assertTrue(any("书架" in term for term in section["keywords"]))
        self.assertTrue(any("窗边" in term for term in section["keywords"]))
        self.assertTrue(all(not any(word in term for word in ("海岛", "宇航员", "银河")) for term in section["keywords"]))

    def test_explicit_keyword_label_overrides_inferred_direction_terms(self):
        body = "01 0-5秒 起点\n镜头：拍摄书架。\n关键词：雨伞、茶杯\n读者：等待日出。"
        self.assertEqual(parse_script("短片", body)[0]["keywords"], ["雨伞", "茶杯"])

    def test_unlabelled_actions_after_dialogue_and_standalone_subtitle_label(self):
        body = "01 0-8秒 送别\n店主：请拿好你的书。\n店主抱起书本走向书架。\n读者：关键词：星空。\n结尾字幕\n每一本书都值得等待。"
        section = parse_script("书店", body)[0]
        self.assertEqual(section["visual_text"], "店主抱起书本走向书架。")
        self.assertIn("每一本书都值得等待。", section["dialogue_text"])
        self.assertIn("结尾字幕", section["dialogue_text"])
        self.assertTrue(any("书架" in term for term in section["keywords"]))
        self.assertTrue(all("星空" not in term for term in section["keywords"]))
        inline = parse_script("短片", "01 0-5秒 收尾\n镜头：雨伞。\n结尾字幕每一次相遇都值得珍惜。")[0]
        self.assertEqual(inline["visual_text"], "镜头：雨伞。")
        self.assertIn("结尾字幕每一次相遇", inline["dialogue_text"])

    def test_numbering_time_range_and_markdown_variants(self):
        cases = (
            ("01. 0秒 – 7秒 入场", 0, 7),
            ("02、8—21秒 接待", 8, 21),
            ("03 22～31秒 阅读", 22, 31),
            ("04 32~40秒 喝茶", 32, 40),
            ("05 41至52秒 收拾", 41, 52),
            ("06 00:53-01:04 告别", 53, 64),
            ("07. 01:05 至 01:16 关灯", 65, 76),
            ("## 08 01:17–01:20 尾声", 77, 80),
            ("０９． ８０－８２秒 回望", 80, 82),
            ("10) 82.5-84.25s 结束", 82.5, 84.25),
        )
        for heading, start, end in cases:
            with self.subTest(heading=heading):
                body = heading + "\n镜头：门口。\n镜头：全景。"
                sections = parse_script("短片", body)
                self.assertEqual(len(sections), 1)
                section = sections[0]
                self.assertEqual(section["format"], "timed_shooting")
                self.assertEqual((section["start_seconds"], section["end_seconds"]), (start, end))
                self.assertEqual(section["text"], body)

    def test_ranges_override_nested_markdown_numbered_directions_and_blank_lines(self):
        body = "# 拍摄计划\n01 0-5秒 开门\n## 画面说明\n镜头：门口。\n\n1. 全景\n2. 特写\n02 6-10秒 关门\n镜头：招牌。"
        sections = parse_script("书店", body)
        self.assertEqual([section["title"] for section in sections], ["开门", "关门"])
        self.assertEqual("".join(section["text"] for section in sections), body)

    def test_dates_plain_number_ranges_and_spoken_timecodes_are_not_shot_headings(self):
        body = "2026-09-08\n读者：01 0-7秒 是字幕的显示时间。\n01 2026-09-08 记录\n02 3-8 人参加。"
        sections = parse_script("记录", body)
        self.assertTrue(all(section.get("format") != "timed_shooting" for section in sections))
        self.assertEqual("".join(section["text"] for section in sections), body)

    def test_invalid_and_inverted_recognized_time_ranges_reject_clearly(self):
        for time_range in ("10-4秒", "-1-7秒", "1--7秒", "00:75-01:20", "00:10-00:05", "1:65:00-2:00:00"):
            with self.subTest(time_range=time_range), self.assertRaisesRegex(ValueError, "时间"):
                parse_script("测试", f"01 {time_range} 动作\n镜头：全景。")

    def test_plain_script_section_id_stays_compatible(self):
        body = "# 门口\n关键词：招牌\n# 室内\n关键词：书架"
        sections = parse_script("书店", body)
        self.assertEqual([section["title"] for section in sections], ["门口", "室内"])
        for index, section in enumerate(sections, 1):
            expected = hashlib.sha256(f"{index}\0{section['text']}".encode("utf-8")).hexdigest()
            self.assertEqual(section["section_id"], expected)
            self.assertNotIn("format", section)

    def test_safety_limits_apply_to_timed_scripts_without_dropping_body(self):
        body = "\n".join(f"{n:02d} {n}-{n + 1}秒 场景\n镜头：书架。" for n in range(1, 52))
        with self.assertRaisesRegex(ValueError, "50"):
            parse_script("书店", body)
        with self.assertRaises(ValueError):
            parse_script("书店", "01 0-1秒 开场\n" + "字" * 100000)
        allowed = "\n".join(f"{n:02d} {n}-{n + 1}秒 场景\n镜头：书架。" for n in range(1, 51))
        sections = parse_script("长标题" * 20, allowed)
        self.assertEqual(len(sections), 50)
        self.assertEqual("".join(section["text"] for section in sections), allowed)
        self.assertEqual(len({section["category_name"] for section in sections}), 50)
        self.assertTrue(all(len(section["category_name"]) <= 40 for section in sections))


class ShootingScriptIntegrationTests(unittest.TestCase):
    def test_matching_and_atomic_apply_preserve_timed_script_and_are_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            service = LibraryService(base / "local", r"\\offline-nas\team\media", str(base / "shared"), "script-test")
            key = root_key(service.root)
            name = "书架特写.mov"
            identity = hashlib.sha256(name.encode()).hexdigest()
            asset = AssetRecord(identity, key, service.root, service.root + "\\" + name,
                                name, name, ".mov", "video", 100, 1, identity)
            with closing(open_index(service.db_path)) as db:
                upsert_records(db, [asset])
            organizer = ScriptOrganizer(service)
            body = "人物：店主\n01 0-5秒 开场\n镜头：书架特写。\n读者：今天想去海岛。\n02 6-9秒 收尾\n镜头：门口。\n结尾字幕：欢迎再来。"
            plan = organizer.plan("书店短片", body)
            self.assertEqual(len(plan["sections"]), 2)
            self.assertEqual(plan["sections"][0]["candidates"][0]["asset_id"], identity)
            self.assertEqual(plan["sections"][1]["candidate_total"], 0)
            chosen = [{"section_id": plan["sections"][0]["section_id"],
                       "category_name": plan["sections"][0]["category_name"], "asset_ids": [identity]}]
            result = organizer.apply(plan, chosen)
            self.assertEqual(result["script"]["body"], body)
            self.assertEqual(result["script"]["asset_ids"], [identity])
            self.assertEqual(result["memberships_changed"], 1)
            with closing(sqlite3.connect(str(service.db_path))) as db:
                before = db.execute("SELECT count(*) FROM outbox").fetchone()[0]
            repeated = organizer.apply(plan, chosen)
            with closing(sqlite3.connect(str(service.db_path))) as db:
                after = db.execute("SELECT count(*) FROM outbox").fetchone()[0]
            self.assertEqual(before, after)
            self.assertEqual(result["script"], repeated["script"])
            self.assertEqual((repeated["categories_created"], repeated["memberships_changed"], repeated["assets_bound"]), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
