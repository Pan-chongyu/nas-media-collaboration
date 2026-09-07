from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import tempfile
import unittest

from PIL import Image

from media import _ffmpeg, generate_thumbnail


class ThumbnailTests(unittest.TestCase):
    def test_image_cache_corruption_and_invalidation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "图片.png"
            Image.new("RGB", (100, 60), "red").save(source)
            first = generate_thumbnail(source, root / "cache")
            self.assertTrue(first.is_file())
            original_mtime = first.stat().st_mtime_ns
            self.assertEqual(generate_thumbnail(source, root / "cache").stat().st_mtime_ns, original_mtime)
            first.write_bytes(b"corrupt")
            rebuilt = generate_thumbnail(source, root / "cache")
            with Image.open(rebuilt) as image:
                self.assertEqual(image.size, (640, 360))
            Image.new("RGB", (800, 600), "blue").save(source)
            self.assertNotEqual(generate_thumbnail(source, root / "cache"), first)

    def test_concurrent_generation_and_bad_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.png"
            Image.new("RGB", (160, 120), "green").save(source)
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: generate_thumbnail(source, root / "cache"), range(8)))
            self.assertEqual(len(set(results)), 1)
            self.assertEqual(list((root / "cache").glob("*.tmp.jpg")), [])
            self.assertIsNone(generate_thumbnail(source, root / "cache", (0, 0)))
            self.assertIsNone(generate_thumbnail(root / "missing.png", root / "cache"))

    @unittest.skipUnless(_ffmpeg(), "FFmpeg unavailable")
    def test_subsecond_video_has_thumbnail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "short.mp4"
            subprocess.run([_ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10:duration=0.3", "-c:v", "libx264", "-threads", "1", "-y", str(video)], check=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            result = generate_thumbnail(video, root / "cache")
            self.assertIsNotNone(result)
            with Image.open(result) as image:
                self.assertEqual(image.size, (640, 360))


if __name__ == "__main__":
    unittest.main()
