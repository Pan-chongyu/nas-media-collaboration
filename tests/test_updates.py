import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from main import APP_VERSION, Workspace, load_settings


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.publish = self.base / "publish"
        self.publish.mkdir()
        self.client = SimpleNamespace(data_dir=self.base / "client")

    def tearDown(self):
        self.temp.cleanup()

    def manifest(self, value):
        (self.publish / "manifest.json").write_text(json.dumps(value), encoding="utf-8-sig")

    def test_current_version_needs_no_download(self):
        self.manifest({"version": APP_VERSION})
        self.assertIsNone(Workspace._prepare_update(self.client, self.publish))
        self.assertFalse(self.client.data_dir.exists())

    def test_download_is_local_and_checked(self):
        source = self.publish / "素材协作-99.0.0-setup.exe"
        source.write_bytes(b"test installer contents")
        self.manifest({"version": "99.0.0", "url": "untrusted/elsewhere.exe", "sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
        result = Workspace._prepare_update(self.client, self.publish)
        self.assertEqual(result["installer"].parent, self.client.data_dir / "updates")
        self.assertEqual(result["installer"].read_bytes(), source.read_bytes())

    def test_invalid_update_never_replaces_prior_download(self):
        name = "素材协作-99.0.0-setup.exe"
        (self.publish / name).write_bytes(b"bad package")
        existing = self.client.data_dir / "updates" / name
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"previous verified package")
        self.manifest({"version": "99.0.0", "sha256": "0" * 64})
        with self.assertRaises(ValueError):
            Workspace._prepare_update(self.client, self.publish)
        self.assertEqual(existing.read_bytes(), b"previous verified package")
        self.assertEqual(list(existing.parent.glob("*.tmp")), [])

    def test_unsafe_versions_and_missing_hash_are_rejected(self):
        for manifest in ({"version": "../99.0.0"}, {"version": "99.0.0"}):
            self.manifest(manifest)
            with self.assertRaises(ValueError):
                Workspace._prepare_update(self.client, self.publish)

    def test_legacy_node_id_is_replaced_and_unique(self):
        config = self.base / "settings.json"
        config.write_text('{"device_id":"device-001"}', encoding="utf-8")
        a, b = load_settings(config), load_settings(config)
        self.assertNotEqual(a["device_id"], "device-001")
        self.assertNotEqual(a["device_id"], b["device_id"])
