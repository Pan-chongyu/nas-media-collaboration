from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from player import MediaPlayer, PlayerError, find_ffplay


class PlayerTests(unittest.TestCase):
    def test_finds_bundled_ffplay(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bin" / "ffplay.exe"
            path.parent.mkdir()
            path.write_bytes(b"x")
            self.assertEqual(find_ffplay(Path(folder)), path)

    def test_rejects_missing_and_unsupported_media(self):
        player = MediaPlayer(Path("missing-ffplay.exe"))
        with self.assertRaises(PlayerError):
            player.open("missing.txt")

    def test_open_pause_stop_uses_local_process(self):
        with tempfile.TemporaryDirectory() as folder:
            ffplay = Path(folder) / "ffplay.exe"
            source = Path(folder) / "clip.mp4"
            ffplay.write_bytes(b"x")
            source.write_bytes(b"x")
            fake = type("FakeProcess", (), {"poll": lambda self: None, "stdin": None, "terminate": lambda self: None, "wait": lambda self, timeout=None: None, "kill": lambda self: None})
            with patch("player.subprocess.Popen", return_value=fake()) as process:
                player = MediaPlayer(ffplay)
                player.open(source)
                process.assert_called_once()
                self.assertTrue(player.playing)
                player.pause()
                player.stop()
                self.assertFalse(player.playing)


if __name__ == "__main__":
    unittest.main()
