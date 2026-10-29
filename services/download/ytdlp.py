"""Fetch one stretch of a public YouTube video with yt-dlp, at most the height the profile asks for."""

from __future__ import annotations

from pathlib import Path

from services.download.proc import DownloadError, binary, run

TIMEOUT_S = 3 * 3600


def download_command(
    ytdlp: str, video_id: str, *, start_s: int, length_s: int, height: int, out_template: str
) -> list[str]:
    """H.264 and AAC when they are on offer (they go into the segments without re-encoding), else the best there is."""
    limit = f"[height<={height}]"
    formats = f"bv*{limit}[vcodec^=avc]+ba[acodec^=mp4a]/bv*{limit}+ba/b{limit}"
    return [
        ytdlp, "--no-playlist", "--no-progress", "--download-sections", f"*{start_s}-{start_s + length_s}",
        "-f", formats, "--merge-output-format", "mp4",
        "-o", out_template, f"https://www.youtube.com/watch?v={video_id}",
    ]  # fmt: skip


def fetch(video_id: str, directory: Path, *, start_s: int, length_s: int, height: int) -> Path:
    run(
        download_command(
            binary("yt-dlp"), video_id, start_s=start_s, length_s=length_s, height=height,
            out_template=str(directory / "vod.%(ext)s"),
        ),
        "yt-dlp", TIMEOUT_S,
    )  # fmt: skip
    found = sorted(directory.glob("vod.*"))
    if not found:
        raise DownloadError("yt-dlp produced no file")
    return found[0]
