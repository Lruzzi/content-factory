"""A small real video+audio file for tests that need FFmpeg-readable media."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def make_clip(path: Path, seconds: int = 20) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603  # nosec B603
        [
            shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", str(seconds),
            "-c:v", "libx264", "-c:a", "aac", str(path),
        ],
        check=True,
    )  # fmt: skip
    return path
