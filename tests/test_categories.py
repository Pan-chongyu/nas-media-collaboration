from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from category_store import CategoryConflictError, DATA_FIELDS, ENTITY_TYPE, ENTITY_TYPES, MEMBERSHIP_ENTITY_TYPE, _event_hash
from indexer import open_index, root_key
from library import LibraryService
from sync_events import Event, append_event, read_events


class CategoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.media = self.base / "素材"
        self.media.mkdir()
        self.shared = self.base / "协作"
        for name in ("01 采访.mp4", "02 风景.png", "03 采访.png", "04 特写.mov", "05 风景.jpg", "06 采访.wav"):
            (self.media / name).write_bytes(b"test media - never changed")
        self.a = self.client("node-a")
        self.b = self.client("node-b")
        self.a.scan()
        self.b.scan()
        self.assets = [item["asset_id"] for item in self.a.page()[0]]

    def tearDown(self):
        self.temp.cleanup()

    def client(self, node, media=None):
        return LibraryService(self.base / node, str(media or self.media), str(self.shared), node)

    def events(self, service):
        with closing(open_index(service.db_path)) as db:
            events = [Event.from_dict(json.loads(row[0])) for row in db.execute("SELECT data_json FROM outbox ORDER BY created_at,event_id")]
        return [event for event in events if event.entity_type in ENTITY_TYPES]

    def sync(self):
        for client in (self.a, self.b, self.a):
            self.assertEqual(client.sync_once()["status"], "done")

    def category(self, name="人物采访", service=None):
        return (service or self.a).categories.save({"name": name, "color": "#3B82F6"})

    def names(self, service, asset_id):
        return [item["name"] for item in service.categories.for_assets([asset_id])[asset_id]]

    def test_batch_add_remove_preserve_other_categories_and_media(self):
        first = self.category()
        second = self.category("风景")
        self.assertEqual(first["color"], "#3b82f6")
        before = {path.name: path.read_bytes() for path in self.media.iterdir()}
        self.assertEqual(self.a.categories.assign(self.assets[:2], [first["category_id"], second["category_id"]]), {"changed": 4})
        pending = self.a.overview()["pending"]
        self.assertEqual(self.a.categories.assign(self.assets[:2], [first["category_id"]]), {"changed": 0})
        self.assertEqual(self.a.overview()["pending"], pending)
        self.assertEqual(self.a.categories.assign(self.assets[:1], [first["category_id"]], remove=True), {"changed": 1})
        self.assertEqual(self.names(self.a, self.assets[0]), ["风景"])
        self.assertEqual(self.a.categories.get(first["category_id"])["count"], 1)
        self.assertEqual(self.a.categories.get(second["category_id"])["count"], 2)
        self.sync()
        self.assertEqual(self.names(self.b, self.assets[0]), ["风景"])
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.media.iterdir()})

    def test_sql_filter_runs_before_pagination_and_combines_search_type(self):
        category = self.category()
        identity = category["category_id"]
        self.a.categories.assign(self.assets[1:5], [identity])
        first, total = self.a.page(category_id=identity, offset=0, limit=2)
        second, again = self.a.page(category_id=identity, offset=2, limit=2)
        self.assertEqual((total, again), (4, 4))
        self.assertEqual([item["asset_id"] for item in first + second], self.assets[1:5])
        self.assertEqual(self.a.page(category_id=identity, offset=4, limit=2), ([], 4))
        selected, count = self.a.page(query="采访", media_type="image", category_id=identity, limit=1)
        self.assertEqual(count, 1)
        self.assertEqual(selected[0]["name"], "03 采访.png")
        self.assertEqual(selected[0]["categories"][0]["category_id"], identity)
        unclassified, count = self.a.page(category_id="__uncategorized__")
        self.assertEqual(count, 2)
        self.assertEqual([item["asset_id"] for item in unclassified], [self.assets[0], self.assets[5]])
        self.assertEqual(self.a.page(category_id="missing")[1], 0)
        self.assertEqual(self.a.page(category_id="' OR 1=1 --")[1], 0)

    def test_archive_restore_retains_memberships_and_updates_unclassified(self):
        record = self.category()
        identity = record["category_id"]
        self.a.categories.assign(self.assets[:2], [identity])
        archived = self.a.categories.save({"archived": True}, identity, record["heads"])
        self.assertEqual(archived["count"], 2)
        self.assertEqual(self.a.categories.list_records(), [])
        self.assertEqual(len(self.a.categories.list_records(True)), 1)
        self.assertEqual(self.a.page(category_id=identity)[1], 0)
        self.assertEqual(self.a.page(category_id="__uncategorized__")[1], 6)
        with self.assertRaisesRegex(ValueError, "归档"):
            self.a.categories.assign(self.assets[2:3], [identity])
        restored = self.a.categories.save({"archived": False}, identity, archived["heads"])
        self.assertEqual(restored["name"], "人物采访")
        self.assertEqual(self.a.page(category_id=identity)[1], 2)
        self.sync()
        self.assertEqual(self.b.categories.get(identity), restored)

    def test_parallel_metadata_versions_require_explicit_resolution(self):
        initial = self.category()
        identity = initial["category_id"]
        self.sync()
        left = self.a.categories.save({"name": "人物"}, identity, initial["heads"])
        right = self.b.categories.save({"color": "#ef4444"}, identity, initial["heads"])
        self.sync()
        current = self.a.categories.get(identity)
        self.assertEqual(current["conflict_count"], 1)
        self.assertEqual(current, self.b.categories.get(identity))
        self.assertEqual(set(current["heads"]), {left["revision"], right["revision"]})
        self.assertEqual(len(self.a.categories.versions(identity)), 3)
        with self.assertRaises(CategoryConflictError):
            self.a.categories.save({"name": "覆盖"}, identity, current["heads"])
        with self.assertRaises(ValueError):
            self.a.categories.save({"name": "不完整合并"}, identity, current["heads"], resolve=True)
        merged = self.a.categories.save({"name": "人物", "color": "#ef4444", "archived": False}, identity, current["heads"], resolve=True)
        self.sync()
        self.assertEqual(self.b.categories.get(identity), merged)
        self.assertEqual(merged["conflict_count"], 0)
        self.assertEqual(len(self.a.categories.versions(identity)), 4)

    def test_stale_editor_cannot_overwrite_and_parallel_archive_stays_visible(self):
        initial = self.category()
        identity = initial["category_id"]
        self.a.categories.assign(self.assets[:1], [identity])
        self.sync()
        latest = self.a.categories.save({"name": "新的分类名称"}, identity, initial["heads"])
        with self.assertRaises(CategoryConflictError):
            self.a.categories.save({"color": "#000000"}, identity, initial["heads"])
        with self.assertRaises(CategoryConflictError):
            self.a.categories.save({"name": "无版本保存"}, identity)
        self.assertEqual(self.a.categories.get(identity), latest)
        self.b.categories.save({"archived": True}, identity, initial["heads"])
        self.sync()
        current = self.a.categories.get(identity)
        self.assertFalse(current["archived"])
        self.assertEqual(current["name"], "新的分类名称")
        self.assertEqual(current["conflict_count"], 1)
        self.assertEqual(self.a.page(category_id=identity)[1], 1)

    def test_observed_remove_preserves_unseen_concurrent_addition(self):
        initial = self.category()
        identity = initial["category_id"]
        self.sync()
        self.a.categories.assign(self.assets[:1], [identity])
        self.b.categories.assign(self.assets[:1], [identity])
        self.a.categories.assign(self.assets[:1], [identity], remove=True)
        self.assertEqual(self.names(self.a, self.assets[0]), [])
        self.sync()
        self.assertEqual(self.names(self.a, self.assets[0]), ["人物采访"])
        self.assertEqual(self.names(self.b, self.assets[0]), ["人物采访"])
        self.a.categories.assign(self.assets[:1], [identity], remove=True)
        self.sync()
        self.assertEqual(self.names(self.b, self.assets[0]), [])
        self.b.categories.assign(self.assets[:1], [identity])
        self.sync()
        self.assertEqual(self.names(self.a, self.assets[0]), ["人物采访"])

    def test_reverse_replay_before_index_and_metadata_is_order_independent(self):
        record = self.category()
        identity = record["category_id"]
        self.a.categories.assign(self.assets[:2], [identity])
        self.a.categories.assign(self.assets[:1], [identity], remove=True)
        latest = self.a.categories.save({"name": "新版分类"}, identity, record["heads"])
        events = self.events(self.a)
        third = self.client("node-c")
        for event in reversed(events):
            self.assertTrue(third.categories.apply_event(event))
            self.assertFalse(third.categories.apply_event(event))
        self.assertEqual(third.categories.get(identity)["count"], 0)
        self.assertEqual(self.names(third, self.assets[0]), [])
        self.assertEqual(self.names(third, self.assets[1]), ["新版分类"])
        third.scan()
        self.assertEqual(third.categories.get(identity)["count"], 1)
        self.assertEqual(third.categories.get(identity), latest)

    def test_revision_ancestry_wins_over_clock_skew(self):
        with patch("category_store.datetime") as clock:
            clock.fromisoformat.side_effect = datetime.fromisoformat
            clock.now.return_value = datetime(2050, 1, 1, tzinfo=timezone.utc)
            record = self.category("未来机器")
            clock.now.return_value = datetime(2020, 1, 1, tzinfo=timezone.utc)
            latest = self.a.categories.save({"name": "修正时钟后"}, record["category_id"], record["heads"])
        for event in reversed(self.events(self.a)):
            self.b.categories.apply_event(event)
        self.assertEqual(self.b.categories.get(record["category_id"]), latest)

    def test_offline_pending_events_survive_restart(self):
        record = self.category()
        self.a.categories.assign(self.assets[:2], [record["category_id"]])
        with patch("library.append_event", side_effect=OSError("NAS offline")):
            self.assertEqual(self.a.sync_once()["status"], "offline")
        self.a = self.client("node-a")
        self.assertEqual(self.a.categories.get(record["category_id"])["count"], 2)
        self.assertGreater(self.a.overview()["pending"], 0)
        self.sync()
        self.assertEqual(self.b.categories.get(record["category_id"])["count"], 2)
        self.assertEqual(self.a.overview()["pending"], 0)

    def test_validation_is_atomic_for_entire_batch_and_duplicate_names(self):
        record = self.category("  Demo  ")
        self.assertEqual(record["name"], "Demo")
        for name in ("demo", "Ｄｅｍｏ"):
            with self.assertRaisesRegex(ValueError, "同名"):
                self.category(name)
        for data in ({"name": ""}, {"name": "a" * 41}, {"name": "隐\u200b藏"}, {"name": "bad\n"},
                     {"name": "正常", "color": "red"}, {"name": "正常", "archived": 1}, {"name": "正常", "extra": True}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.a.categories.save(data)
        pending = self.a.overview()["pending"]
        for assets, categories in ((self.assets[:1] + ["f" * 64], [record["category_id"]]),
                                   (self.assets[:1], [record["category_id"], "missing"]), ([], [record["category_id"]]),
                                   (self.assets[:1], []), ([str(index).zfill(64) for index in range(501)], ["a", "b"])):
            with self.subTest(assets=len(assets), categories=categories), self.assertRaises(ValueError):
                self.a.categories.assign(assets, categories)
        self.assertEqual(self.a.overview()["pending"], pending)
        self.assertEqual(self.a.categories.get(record["category_id"])["count"], 0)

    def test_remote_duplicate_names_retained_with_distinct_identities(self):
        left = self.category()
        right = self.category(service=self.b)
        self.sync()
        self.assertNotEqual(left["category_id"], right["category_id"])
        records = self.a.categories.list_records()
        self.assertEqual(len(records), 2)
        self.assertEqual(records, self.b.categories.list_records())
        self.assertEqual([record["category_id"] for record in records], sorted((left["category_id"], right["category_id"])))

    def test_outbox_failure_rolls_back_metadata_and_all_memberships(self):
        record = self.category()
        pending = self.a.overview()["pending"]
        with closing(open_index(self.a.db_path)) as db, db:
            db.execute("CREATE TRIGGER fail_category_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'test outbox failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.a.categories.assign(self.assets, [record["category_id"]])
        with self.assertRaises(sqlite3.IntegrityError):
            self.a.categories.save({"name": "不能落单"}, record["category_id"], record["heads"])
        self.assertEqual(self.a.categories.get(record["category_id"]), record)
        self.assertEqual(self.a.overview()["pending"], pending)
        with closing(open_index(self.a.db_path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM category_member_adds").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM category_membership_events").fetchone()[0], 0)

    def test_inbox_failure_rolls_back_received_category(self):
        self.sync()
        record = self.category()
        self.assertEqual(self.a.sync_once()["status"], "done")
        with closing(open_index(self.b.db_path)) as db, db:
            db.execute("CREATE TRIGGER fail_category_inbox BEFORE INSERT ON inbox BEGIN SELECT RAISE(ABORT,'test inbox failure'); END")
        self.assertEqual(self.b.sync_once()["status"], "offline")
        self.assertIsNone(self.b.categories.get(record["category_id"]))
        with closing(open_index(self.b.db_path)) as db, db:
            db.execute("DROP TRIGGER fail_category_inbox")
        self.assertEqual(self.b.sync_once()["metadata_changed"], 1)
        self.assertEqual(self.b.categories.get(record["category_id"]), record)

    def test_received_membership_and_inbox_rollback_together(self):
        record = self.category()
        identity = record["category_id"]
        self.sync()
        self.a.categories.assign(self.assets[:3], [identity])
        self.assertEqual(self.a.sync_once()["status"], "done")
        with closing(open_index(self.b.db_path)) as db, db:
            db.execute("CREATE TRIGGER fail_membership_inbox BEFORE INSERT ON inbox BEGIN SELECT RAISE(ABORT,'test membership inbox failure'); END")
        self.assertEqual(self.b.sync_once()["status"], "offline")
        self.assertEqual(self.b.categories.get(identity)["count"], 0)
        with closing(open_index(self.b.db_path)) as db, db:
            self.assertEqual(db.execute("SELECT count(*) FROM category_member_adds").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM category_membership_events").fetchone()[0], 0)
            db.execute("DROP TRIGGER fail_membership_inbox")
        self.assertEqual(self.b.sync_once()["metadata_changed"], 1)
        self.assertEqual(self.b.categories.get(identity)["count"], 3)

    def test_two_local_editors_serialize_head_check_and_name_uniqueness(self):
        record = self.category()
        def edit(name):
            try:
                return self.a.categories.save({"name": name}, record["category_id"], record["heads"])
            except CategoryConflictError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(edit, ("A 的分类名称", "B 的分类名称")))
        self.assertEqual(sum(item is not None for item in results), 1)
        self.assertEqual(len(self.a.categories.versions(record["category_id"])), 2)
        def create(_):
            try:
                return self.category("共同的新分类")
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, (1, 2)))
        self.assertEqual(sum(item is not None for item in results), 1)
        self.assertEqual(len(self.a.categories.list_records()), 2)

    def test_other_library_isolated_in_same_local_database_and_shared_log(self):
        record = self.category()
        self.a.categories.assign(self.assets[:1], [record["category_id"]])
        other_root = self.base / "另一个素材库"
        other_root.mkdir()
        other = self.client("node-a", other_root)
        self.assertEqual(other.categories.list_records(), [])
        self.assertEqual(other.categories.for_assets(self.assets[:1]), {self.assets[0]: []})
        self.assertIsNone(other.categories.get(record["category_id"]))
        with self.assertRaisesRegex(ValueError, "其他素材库"):
            other.categories.apply_event(self.events(self.a)[0])
        other_category = self.category("另一个分类", other)
        with self.assertRaises(ValueError):
            other.categories.assign(self.assets[:1], [other_category["category_id"]])
        self.sync()
        self.assertEqual(other.sync_once()["status"], "done")
        self.assertEqual(other.page(category_id=record["category_id"])[1], 0)
        self.assertEqual(len(self.a.categories.list_records()), 1)

    def test_upgrade_replays_retained_unsupported_categories_even_offline(self):
        record = self.category()
        self.a.categories.assign(self.assets[:1], [record["category_id"]])
        events = self.events(self.a)
        key = root_key(self.media)
        with closing(open_index(self.b.db_path)) as db, db:
            db.execute("DELETE FROM app_state WHERE key=?", (f"category_inbox_v1:{key}",))
            for event in reversed(events):
                db.execute("INSERT INTO unsupported_events VALUES(?,?,?,?)", (key, event.event_id, json.dumps(event.to_dict()), event.timestamp))
                db.execute("INSERT INTO inbox VALUES(?,?,?)", (key, event.event_id, event.timestamp))
                db.execute("INSERT INTO rejected_events VALUES(?,?,?)", (event.event_id, "old unsupported", event.timestamp))
        self.b = self.client("node-b")
        self.assertEqual(self.b.categories.get(record["category_id"])["count"], 1)
        self.assertEqual(self.names(self.b, self.assets[0]), ["人物采访"])
        with closing(open_index(self.b.db_path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM unsupported_events").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM rejected_events").fetchone()[0], 0)

    def test_upgrade_recovers_previously_rejected_category_from_shared_event(self):
        record = self.category()
        event = self.events(self.a)[0]
        append_event(self.shared, event, strict=True)
        key = root_key(self.media)
        with closing(open_index(self.b.db_path)) as db, db:
            db.execute("DELETE FROM app_state WHERE key=?", (f"category_inbox_v1:{key}",))
            db.execute("INSERT INTO inbox VALUES(?,?,?)", (key, event.event_id, event.timestamp))
            db.execute("INSERT INTO rejected_events VALUES(?,?,?)", (event.event_id, "old unsupported", event.timestamp))
        self.b = self.client("node-b")
        self.assertEqual(self.b.sync_once()["status"], "done")
        self.assertEqual(self.b.categories.get(record["category_id"]), record)
        self.assertEqual(self.b.overview()["rejected"], 0)

    def test_malformed_hash_schema_and_cross_category_parent_rejected_atomically(self):
        record = self.category()
        event = self.events(self.a)[0]
        tampered = copy.deepcopy(event.payload)
        tampered["data"]["name"] = "偷偷覆盖"
        with self.assertRaisesRegex(ValueError, "校验"):
            self.b.categories.apply_event(replace(event, payload=tampered))
        tampered["schema"] = True
        with self.assertRaisesRegex(ValueError, "格式"):
            self.b.categories.apply_event(replace(event, payload=tampered))
        self.assertIsNone(self.b.categories.get(record["category_id"]))
        self.b.categories.apply_event(event)
        payload = copy.deepcopy(event.payload)
        payload["parents"] = [event.event_id]
        category_id = "another-category"
        payload["revision"] = _event_hash(ENTITY_TYPE, category_id, event.device_id, payload)
        bad_parent = replace(event, entity_id=category_id, payload=payload, event_id=payload["revision"])
        with self.assertRaisesRegex(ValueError, "父版本"):
            self.b.categories.apply_event(bad_parent)
        self.assertIsNone(self.b.categories.get(category_id))


if __name__ == "__main__":
    unittest.main()
