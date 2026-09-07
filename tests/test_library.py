from contextlib import closing
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image

from indexer import open_index, root_key
from library import LibraryService
from sync_events import Event, append_event, read_events


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.media = self.base / "素材"
        self.media.mkdir()
        self.shared = self.base / "协作"
        self.a = LibraryService(self.base / "a", str(self.media), str(self.shared), "node-a")
        self.b = LibraryService(self.base / "b", str(self.media), str(self.shared), "node-b")

    def tearDown(self):
        self.temp.cleanup()

    def create(self, name="中文.png"):
        path = self.media / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (160, 120), (12, 110, 50)).save(path)
        return path

    def test_scan_restart_and_repeated_scan(self):
        self.create()
        first = self.a.scan()
        self.assertEqual(first["status"], "done")
        self.assertEqual(first["changed"], 1)
        restarted = LibraryService(self.base / "a", str(self.media), str(self.shared), "node-a")
        self.assertEqual(restarted.page()[1], 1)
        self.assertEqual(restarted.scan()["changed"], 0)
        self.assertEqual(restarted.overview()["pending"], 1)

    def test_two_nodes_receive_shared_metadata(self):
        self.create("目录A/采访.png")
        self.create("目录B/采访.png")
        self.assertEqual(self.a.scan()["changed"], 2)
        sent = self.a.sync_once()
        self.assertEqual(sent["status"], "done", sent)
        self.assertEqual(sent["sent"], 1)
        received = self.b.sync_once()
        self.assertEqual(received["status"], "done", received)
        self.assertEqual(received["changed"], 2)
        items, count = self.b.page()
        self.assertEqual(count, 2)
        self.assertNotEqual(items[0]["asset_id"], items[1]["asset_id"])
        self.assertEqual(self.b.sync_once()["changed"], 0)
        self.assertEqual(self.b.scan()["changed"], 0)
        self.assertEqual(self.b.overview()["pending"], 0)

    def test_offline_outbox_retries_and_scan_does_not_erase(self):
        source = self.create()
        self.a.scan()
        with patch("library.append_event", side_effect=OSError("NAS offline")):
            self.assertEqual(self.a.sync_once()["status"], "offline")
        self.assertEqual(self.a.overview()["pending"], 1)
        self.assertEqual(self.a.sync_once()["status"], "done")
        self.assertEqual(self.a.overview()["pending"], 0)
        source.unlink()
        self.media.rmdir()
        self.assertEqual(self.a.scan()["status"], "failed")
        self.assertEqual(self.a.page()[1], 1)

    def test_pause_and_resume_preserves_committed_batches(self):
        for i in range(75):
            self.create(f"{i:03}.png")
        stop = threading.Event()
        def progress(value):
            if value["done"] >= 50:
                stop.set()
        paused = self.a.scan(stop, progress)
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(self.a.page()[1], 50)
        resumed = self.a.scan()
        self.assertEqual(resumed["changed"], 25)
        self.assertEqual(self.a.page()[1], 75)

    def test_thumbnail_cache_and_batch(self):
        self.create()
        self.a.scan()
        report = self.a.batch_thumbnails()
        self.assertEqual(report["changed"], 1)
        items, _ = self.a.page()
        self.assertTrue(Path(items[0]["thumbnail"]).is_file())
        self.assertEqual(items[0]["thumbnail_status"], "ready")
        self.assertEqual(self.a.batch_thumbnails()["done"], 0)

    def test_outbox_failure_rolls_back_index(self):
        self.create()
        with closing(open_index(self.a.db_path)) as db, db:
            db.execute("CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'test failure'); END")
        report = self.a.scan()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(self.a.page()[1], 0)

    def test_late_snapshot_is_restatted(self):
        source = self.create()
        self.a.scan()
        self.a.sync_once()
        Image.new("RGB", (800, 600), (180, 10, 20)).save(source)
        result = self.b.sync_once()
        self.assertEqual(result["status"], "done", result)
        self.assertEqual(self.b.page()[0][0]["size"], source.stat().st_size)

    def test_missing_asset_does_not_block_other_records_and_retries(self):
        missing = self.create("missing.png")
        self.create("available.png")
        self.a.scan()
        self.a.sync_once()
        missing.unlink()
        result = self.b.sync_once()
        self.assertEqual(result["status"], "done", result)
        self.assertEqual(result["changed"], 1)
        self.assertEqual(result["deferred"], 1)
        self.create("missing.png")
        with closing(open_index(self.b.db_path)) as db, db:
            db.execute("UPDATE deferred_events SET next_retry=0")
        result = self.b.sync_once()
        self.assertEqual(result["deferred"], 0)
        self.assertEqual(self.b.page()[1], 2)

    def test_invalid_path_is_rejected_without_blocking_valid_events(self):
        self.create()
        self.a.scan()
        self.a.sync_once()
        event = Event.new("node-bad", "asset_batch", "bad", "upsert", {
            "schema": 1, "library_key": root_key(str(self.media)),
            "records": [{"relative_path": "../outside.png", "asset_id": "anything"}],
        })
        append_event(self.shared, event, strict=True)
        result = self.b.sync_once()
        self.assertEqual(result["status"], "done", result)
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(self.b.page()[1], 1)
        self.assertEqual(self.b.sync_once()["rejected"], 0)
