from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from collaboration_store import CollaborationStore, ConflictError, DATA_FIELDS, ENTITY_TYPE
from indexer import open_index, root_key
from library import LibraryService
from sync_events import Event, append_event, read_events


class CollaborationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.media = self.base / "素材"
        self.media.mkdir()
        self.shared = self.base / "协作"
        self.a = self.client("node-a")
        self.b = self.client("node-b")

    def tearDown(self):
        self.temp.cleanup()

    def client(self, node):
        return LibraryService(self.base / node, str(self.media), str(self.shared), node)

    def events(self, service):
        with closing(open_index(service.db_path)) as db:
            return [Event.from_dict(json.loads(row[0])) for row in db.execute("SELECT data_json FROM outbox ORDER BY created_at,event_id")]

    def data(self, record):
        return {key: copy.deepcopy(record[key]) for key in DATA_FIELDS}

    def sync(self):
        self.assertEqual(self.a.sync_once()["status"], "done")
        self.assertEqual(self.b.sync_once()["status"], "done")
        self.assertEqual(self.a.sync_once()["status"], "done")

    def test_two_nodes_script_binding_and_ticket_workflow(self):
        Image.new("RGB", (32, 24)).save(self.media / "采访.png")
        self.a.scan()
        asset = self.a.page()[0][0]["asset_id"]
        script = self.a.collaboration.save("script", {"title": "采访脚本", "body": "开场\n采访\n结束", "asset_ids": [asset], "author": "小潘"})
        ticket = self.a.collaboration.save("work_order", {"title": "剪辑初稿", "body": "按采访脚本剪辑", "asset_ids": [asset], "assignee": "小李", "due_date": "2026-09-30"})
        self.a.sync_once()
        result = self.b.sync_once()
        self.assertEqual(result["metadata_changed"], 2)
        self.assertEqual(result["changed"], 1)
        self.assertEqual(self.b.collaboration.get(script["entity_id"]), script)
        self.assertEqual(self.b.collaboration.list_records("script", asset_id=asset)[1], 1)
        for status in ("进行中", "待审核", "已完成"):
            ticket = self.b.collaboration.save("work_order", {"status": status}, ticket["entity_id"], ticket["heads"])
            self.sync()
            self.assertEqual(self.a.collaboration.get(ticket["entity_id"])["status"], status)
        self.assertEqual(ticket["body"], "按采访脚本剪辑")
        self.assertEqual(ticket["asset_ids"], [asset])
        self.assertEqual(self.a.collaboration.list_records("work_order", query="小李", status="已完成")[1], 1)
        self.assertEqual(self.b.sync_once()["metadata_changed"], 0)

    def test_offline_save_survives_restart_and_resumes(self):
        record = self.a.collaboration.save("script", {"title": "离线稿", "body": "仍可编辑"})
        with patch("library.append_event", side_effect=OSError("NAS offline")):
            self.assertEqual(self.a.sync_once()["status"], "offline")
        self.a = self.client("node-a")
        self.assertEqual(self.a.collaboration.get(record["entity_id"]), record)
        self.assertEqual(self.a.overview()["pending"], 1)
        self.sync()
        self.assertEqual(self.b.collaboration.get(record["entity_id"]), record)
        self.assertEqual(self.a.overview()["pending"], 0)

    def test_reverse_order_unknown_parents_and_duplicates_converge(self):
        record = self.a.collaboration.save("script", {"title": "版本一"})
        for name in ("版本二", "版本三"):
            record = self.a.collaboration.save("script", {"title": name}, record["entity_id"], record["heads"])
        events = self.events(self.a)
        for event in reversed(events):
            self.assertTrue(self.b.collaboration.apply_event(event))
            self.assertFalse(self.b.collaboration.apply_event(event))
            self.assertEqual(self.b.collaboration.get(record["entity_id"])["title"], "版本三")
        self.assertEqual(self.b.collaboration.get(record["entity_id"]), record)
        self.assertEqual(len(self.b.collaboration.versions(record["entity_id"])), 3)

    def test_clock_skew_cannot_overwrite_causal_child(self):
        with patch("collaboration_store.datetime") as fake_clock:
            fake_clock.fromisoformat.side_effect = datetime.fromisoformat
            fake_clock.now.return_value = datetime(2050, 1, 1, tzinfo=timezone.utc)
            original = self.a.collaboration.save("script", {"title": "机器时钟快"})
            fake_clock.now.return_value = datetime(2020, 1, 1, tzinfo=timezone.utc)
            latest = self.a.collaboration.save("script", {"title": "修正时钟后的新内容"}, original["entity_id"], original["heads"])
        self.sync()
        self.assertEqual(self.b.collaboration.get(original["entity_id"]), latest)
        self.assertFalse(latest["conflict"])

    def test_parallel_changes_are_retained_until_explicit_resolution(self):
        initial = self.a.collaboration.save("script", {"title": "脚本", "body": "共同起点"})
        self.sync()
        left = self.a.collaboration.save("script", {"body": "A 的独立内容"}, initial["entity_id"], initial["heads"])
        right = self.b.collaboration.save("script", {"body": "B 的独立内容"}, initial["entity_id"], initial["heads"])
        self.sync()
        current = self.a.collaboration.get(initial["entity_id"])
        self.assertTrue(current["conflict"])
        self.assertEqual(current, self.b.collaboration.get(initial["entity_id"]))
        self.assertEqual(set(current["heads"]), {left["revision"], right["revision"]})
        versions = self.a.collaboration.versions(initial["entity_id"])
        self.assertEqual({item["body"] for item in versions if item["is_head"]}, {"A 的独立内容", "B 的独立内容"})
        with self.assertRaisesRegex(ConflictError, "并行"):
            self.a.collaboration.save("script", {"body": "overwrite"}, initial["entity_id"], current["heads"])
        data = self.data(current)
        data["body"] = "A 的独立内容\nB 的独立内容"
        resolved = self.a.collaboration.save("script", data, initial["entity_id"], current["heads"], resolve=True)
        self.assertFalse(resolved["conflict"])
        self.sync()
        self.assertEqual(self.b.collaboration.get(initial["entity_id"]), resolved)
        self.assertEqual(len(self.b.collaboration.versions(initial["entity_id"])), 4)
        # A fresh client can receive the merge before either of its parents.
        third = self.client("node-c")
        for event in reversed(read_events(self.shared)):
            if event.entity_type == ENTITY_TYPE:
                third.collaboration.apply_event(event)
        self.assertEqual(third.collaboration.get(initial["entity_id"]), resolved)

    def test_stale_editor_and_stale_resolution_preserve_every_version(self):
        original = self.a.collaboration.save("work_order", {"title": "审核"})
        latest = self.a.collaboration.save("work_order", {"status": "进行中"}, original["entity_id"], original["heads"])
        with self.assertRaises(ConflictError):
            self.a.collaboration.save("work_order", {"status": "已完成"}, original["entity_id"], original["heads"])
        with self.assertRaises(ConflictError):
            self.a.collaboration.save("work_order", {"status": "已完成"}, original["entity_id"])
        with self.assertRaises(ConflictError):
            self.a.collaboration.save("work_order", self.data(original), original["entity_id"], original["heads"], resolve=True)
        self.assertEqual(self.a.collaboration.get(original["entity_id"]), latest)
        self.assertEqual(len(self.events(self.a)), 2)

    def test_simultaneous_local_edit_checks_inside_write_transaction(self):
        original = self.a.collaboration.save("script", {"title": "共同编辑"})
        def edit(title):
            try:
                return self.a.collaboration.save("script", {"title": title}, original["entity_id"], original["heads"])
            except ConflictError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(edit, ("编辑一", "编辑二")))
        self.assertEqual(sum(item is not None for item in outcomes), 1)
        self.assertEqual(len(self.a.collaboration.versions(original["entity_id"])), 2)

    def test_library_isolation_archive_and_restore(self):
        record = self.a.collaboration.save("script", {"title": "库 A"})
        other_key = root_key(str(self.base / "其他素材"))
        other = CollaborationStore(self.a.db_path, other_key, "node-a")
        self.assertIsNone(other.get(record["entity_id"]))
        self.assertEqual(other.list_records("script")[1], 0)
        with self.assertRaises(ValueError):
            other.apply_event(self.events(self.a)[0])
        other.save("script", {"title": "库 B 同 ID"}, entity_id=record["entity_id"])
        self.assertEqual(self.a.collaboration.get(record["entity_id"])["title"], "库 A")
        record = self.a.collaboration.save("script", {"archived": True}, record["entity_id"], record["heads"])
        self.sync()
        self.assertEqual(self.b.collaboration.list_records("script")[1], 0)
        self.assertEqual(self.b.collaboration.list_records("script", include_archived=True)[1], 1)
        record = self.b.collaboration.save("script", {"archived": False}, record["entity_id"], record["heads"])
        self.sync()
        self.assertEqual(self.a.collaboration.list_records("script")[1], 1)
        self.assertEqual(len(self.a.collaboration.versions(record["entity_id"])), 3)

    def test_parallel_archive_does_not_hide_active_work(self):
        record = self.a.collaboration.save("work_order", {"title": "不能误隐藏"})
        self.sync()
        self.a.collaboration.save("work_order", {"archived": True}, record["entity_id"], record["heads"])
        self.b.collaboration.save("work_order", {"body": "尚在编辑"}, record["entity_id"], record["heads"])
        self.sync()
        rows, total = self.a.collaboration.list_records("work_order")
        self.assertEqual(total, 1)
        self.assertTrue(rows[0]["conflict"])

    def test_outbox_and_revision_commit_atomically(self):
        with closing(open_index(self.a.db_path)) as db, db:
            db.execute("CREATE TRIGGER fail_collaboration_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'outbox failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.a.collaboration.save("script", {"title": "不能仅保存在本地"})
        self.assertEqual(self.a.collaboration.list_records("script")[1], 0)
        self.assertEqual(self.events(self.a), [])

    def test_inbox_and_received_revision_commit_atomically(self):
        record = self.a.collaboration.save("script", {"title": "原子接收"})
        self.a.sync_once()
        with closing(open_index(self.b.db_path)) as db, db:
            db.execute("CREATE TRIGGER fail_collaboration_inbox BEFORE INSERT ON inbox BEGIN SELECT RAISE(ABORT,'inbox failure'); END")
        self.assertEqual(self.b.sync_once()["status"], "offline")
        self.assertIsNone(self.b.collaboration.get(record["entity_id"]))
        with closing(open_index(self.b.db_path)) as db, db:
            db.execute("DROP TRIGGER fail_collaboration_inbox")
        self.assertEqual(self.b.sync_once()["metadata_changed"], 1)
        self.assertEqual(self.b.collaboration.get(record["entity_id"]), record)

    def test_upgrade_recovers_metadata_consumed_by_old_client_once(self):
        record = self.a.collaboration.save("script", {"title": "旧版曾忽略"})
        self.a.sync_once()
        with closing(open_index(self.b.db_path)) as db, db:
            db.execute("DELETE FROM app_state WHERE key LIKE 'collaboration_inbox_v1:%'")
            db.execute("INSERT INTO inbox VALUES(?,?,?)", (root_key(str(self.media)), record["revision"], "2026-09-08"))
        self.b = self.client("node-b")
        self.assertEqual(self.b.sync_once()["metadata_changed"], 1)
        self.assertEqual(self.b.collaboration.get(record["entity_id"]), record)
        self.b = self.client("node-b")
        self.assertEqual(self.b.sync_once()["metadata_changed"], 0)

    def test_unknown_future_events_preserved_for_upgrade(self):
        event = Event.new("node-future", "future_annotation", "future-id", "save", {"library_key": root_key(str(self.media)), "data": "retain"})
        append_event(self.shared, event, strict=True)
        self.assertEqual(self.b.sync_once()["received"], 1)
        with closing(open_index(self.b.db_path)) as db:
            saved = db.execute("SELECT event_json FROM unsupported_events WHERE event_id=?", (event.event_id,)).fetchone()
        self.assertEqual(json.loads(saved[0]), event.to_dict())
        self.assertEqual(self.b.sync_once()["received"], 0)

    def test_same_revision_content_change_rejected_without_replacing(self):
        record = self.a.collaboration.save("script", {"title": "完整性"})
        event = self.events(self.a)[0]
        self.b.collaboration.apply_event(event)
        altered = copy.deepcopy(event.to_dict())
        altered["payload"]["data"]["body"] = "篡改"
        with self.assertRaises(ValueError):
            self.b.collaboration.apply_event(Event.from_dict(altered))
        self.assertEqual(self.b.collaboration.get(record["entity_id"]), record)
        self.assertEqual(len(self.b.collaboration.versions(record["entity_id"])), 1)

    def test_invalid_metadata_does_not_block_valid_events(self):
        record = self.a.collaboration.save("script", {"title": "有效事件"})
        self.a.sync_once()
        invalid = Event.new("node-invalid", ENTITY_TYPE, "bad-id", "append", {"schema": 1, "library_key": root_key(str(self.media))})
        append_event(self.shared, invalid, strict=True)
        result = self.b.sync_once()
        self.assertEqual(result["status"], "done", result)
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(result["metadata_changed"], 1)
        self.assertEqual(self.b.collaboration.get(record["entity_id"]), record)
        self.assertEqual(self.b.sync_once()["rejected"], 0)

    def test_field_validation_and_literal_search(self):
        invalid = [
            {"title": ""}, {"title": "x" * 201}, {"body": "x" * 100001}, {"body": "bad\x00"},
            {"assignee": []}, {"status": "invalid"}, {"asset_ids": "not a list"}, {"asset_ids": ["../bad"]},
            {"due_date": "2026-02-30"}, {"due_date": "2026-2-03"}, {"archived": 1}, {"unknown": "field"},
        ]
        for fields in invalid:
            with self.subTest(fields=str(fields)[:60]), self.assertRaises(ValueError):
                self.a.collaboration.save("work_order", {"title": "有效标题", **fields})
        self.a.collaboration.save("script", {"title": "100%_符合"})
        self.a.collaboration.save("script", {"title": "普通标题"})
        self.assertEqual(self.a.collaboration.list_records("script", query="%_")[1], 1)
        self.assertEqual(len(self.a.collaboration.list_records("script", limit=1)[0]), 1)
        for args in ({"limit": 0}, {"offset": -1}, {"status": "已完成"}, {"asset_id": "bad"}):
            with self.assertRaises(ValueError):
                self.a.collaboration.list_records("script", **args)


if __name__ == "__main__":
    unittest.main()
