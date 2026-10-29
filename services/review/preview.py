"""A small copy of a clip for the Telegram preview. Bots may upload at most about 50 MB, and a full minute-long clip can
come close; the preview is only for judging the clip, so it is 720x1280 with a bitrate that
shrinks as the clip gets longer. Argument lists only."""

from __future__ import annotations

import shutil
import subprocess  # nosec B404
from pathlib import Path

from services.render import ffmpeg as ff

AUDIO_KBPS = 64
MAX_VIDEO_KBPS = 3500
BUDGET_MB = 40  # leaves room under the bot upload limit of about 50 MB


class PreviewError(RuntimeError):
    pass


def video_cap_kbps(duration_s: float) -> int:
    """The video bitrate that keeps the preview under BUDGET_MB, at most MAX_VIDEO_KBPS."""
    return min(MAX_VIDEO_KBPS, int(BUDGET_MB * 8000 / max(duration_s, 1)) - AUDIO_KBPS)


def make_preview(source: Path, out: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise PreviewError("ffmpeg is not installed")
    try:
        cap = video_cap_kbps(ff.probe(source).duration_s)
    except ff.RenderError as exc:
        raise PreviewError(f"ffmpeg could not make a preview: {exc}") from exc
    cmd = [
        ffmpeg, "-v", "error", "-nostdin", "-y", "-protocol_whitelist", "file", "-i", str(source),
        "-vf", "scale=720:1280:flags=lanczos", "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
        "-maxrate", f"{cap}k", "-bufsize", f"{2 * cap}k", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k", "-movflags", "+faststart", str(out),
    ]  # fmt: skip
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)  # noqa: S603  # nosec B603
    except subprocess.TimeoutExpired as exc:
        raise PreviewError("ffmpeg timed out making the preview") from exc
    if done.returncode != 0:
        raise PreviewError(f"ffmpeg could not make a preview: {done.stderr.strip()[-300:]}")
    return out
