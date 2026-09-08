"""Download the pinned mpv build and verify its release SHA-256 before extraction."""
import hashlib
from pathlib import Path
import subprocess
import urllib.request

BASE = Path(__file__).resolve().parents[1] / "build/dependencies/mpv"
URL = "https://github.com/shinchiro/mpv-winbuild-cmake/releases/download/20260903/mpv-x86_64-20260903-git-69e63f425a.7z"
SHA256 = "418dbfb5feb851cbed33d6c05d8481ba71802621bfd6efe8974522b28d42ac97"


def main():
    BASE.mkdir(parents=True, exist_ok=True)
    archive = BASE / "mpv.7z"
    if not archive.is_file() or hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        with urllib.request.urlopen(URL, timeout=60) as response:
            contents = response.read()
        if hashlib.sha256(contents).hexdigest() != SHA256:
            raise RuntimeError("mpv download checksum mismatch")
        archive.write_bytes(contents)
    subprocess.run(["tar", "-xf", str(archive), "-C", str(BASE)], check=True)
    if not (BASE / "mpv.exe").is_file():
        raise RuntimeError("mpv.exe was not extracted")
    print(BASE / "mpv.exe")


if __name__ == "__main__":
    main()
