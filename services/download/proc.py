"""Run an external program (yt-dlp, FFmpeg) as an argument list, and say what went wrong in a line."""

from __future__ import annotations

import shutil
import subprocess  # nosec B404


class DownloadError(RuntimeError):
    pass


def binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise DownloadError(f"{name} is not installed")
    return path


def run(command: list[str], what: str, timeout: float) -> None:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603  # nosec B603
    except subprocess.TimeoutExpired as exc:
        raise DownloadError(f"{what} timed out") from exc
    if done.returncode != 0:
        raise DownloadError(f"{what} failed: {' | '.join(done.stderr.strip().splitlines()[-4:])}")
