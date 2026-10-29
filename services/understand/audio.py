"""Cut a segment down to the audio Groq needs: mono, 16 kHz, Opus. Ten minutes come out near 2 MB, far under
Groq's 25 MB upload limit (FLAC would be about 22 MB). Argument lists only; paths are ours, never user text."""

from __future__ import annotations

import shutil
import subprocess  # nosec B404
from pathlib import Path


class AudioError(RuntimeError):
    pass


def extract(source: Path, out: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise AudioError("ffmpeg is not installed")
    cmd = [
        ffmpeg, "-v", "error", "-nostdin", "-y", "-protocol_whitelist", "file", "-i", str(source),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "24k", "-application", "voip",
        "-f", "ogg", str(out),
    ]  # fmt: skip
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)  # noqa: S603  # nosec B603
    except subprocess.TimeoutExpired as exc:
        raise AudioError("ffmpeg timed out cutting the audio") from exc
    if done.returncode != 0:
        raise AudioError(f"ffmpeg could not read {source.name}: {done.stderr.strip()[-300:]}")
    return out
