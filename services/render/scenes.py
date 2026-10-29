"""Full-screen menus: draft pick and the loading screen. The game's own screen is mostly green (the map); those menus
are blue and purple with no green at all. While one is on screen the gameplay panel stops showing the middle square
of the frame and pans across the whole width instead, so the team lists and the "Global No. 48" rank popups on the
sides can be read. Detection is a colour measure on small frames: cheap, and tunable per profile
(`layout.wide.green_below`)."""

from __future__ import annotations

import shutil
import subprocess  # nosec B404
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np

from services.render import facetrack
from services.render.errors import RenderError

SAMPLE_FPS = 2.0
DETECT_WIDTH, DETECT_HEIGHT = 160, 90
GAME_ROWS = 0.8  # the bottom fifth holds the face cam and the stream's overlays, not the game
MIN_LEN_S = 2.0  # a menu shorter than this is a flash, not a screen worth panning over
MERGE_GAP_S = 1.5  # a menu that flickers out for less than this is still the same one
ALERT_MIN_LEN_S = 2.0  # an alert lasts several seconds; less than this is a stray yellow word in the game
ALERT_MERGE_GAP_S = 1.5  # the alert flickers between its lines; this is still the same alert
ALERT_TEXT_FROM = 0.55  # the amount and the message are in the lower part of the alert; yellow game text is higher
TIMEOUT_S = 300

Range = tuple[float, float]


def green_share(rgb: np.ndarray) -> float:
    """The share of the game part of a frame (height x width x RGB) that is the green of the map."""
    top = rgb[: int(rgb.shape[0] * GAME_ROWS)].astype(np.float32) / 255
    r, g, b = top[..., 0], top[..., 1], top[..., 2]
    peak, floor = top.max(axis=-1), top.min(axis=-1)
    spread = np.maximum(peak - floor, 1e-6)
    saturation = (peak - floor) / np.maximum(peak, 1e-6)
    hue = 60 * ((b - r) / spread + 2)  # the hue in degrees, valid where green is the strongest channel
    return float(((peak == g) & (hue > 70) & (hue < 170) & (saturation > 0.25) & (peak > 0.2)).mean())


def yellow_share(rgb: np.ndarray) -> float:
    """The share of a frame that is the bright yellow of a donation alert's message text. Gold and orange effects of
    the game are not counted: the yellow must be pure (high red and green, little blue)."""
    f = rgb.astype(np.float32) / 255
    r, g, b = f[..., 0], f[..., 1], f[..., 2]
    peak, floor = f.max(axis=-1), f.min(axis=-1)
    spread = np.maximum(peak - floor, 1e-6)
    saturation = (peak - floor) / np.maximum(peak, 1e-6)
    hue = 60 * (((g - b) / spread) % 6)  # valid where red is the strongest channel (yellow: red and green high)
    pure = (hue > 48) & (hue < 70) & (saturation > 0.6) & (peak > 0.85) & (r > 0.8) & (g > 0.8) & (b < 0.45)
    return float(pure.mean())


def ranges_from_flags(flags: Sequence[bool], *, fps: float, min_len_s: float, merge_gap_s: float) -> list[Range]:
    """(start, end) seconds of each run of True frames; runs closer than `merge_gap_s` join, short ones go."""
    runs: list[list[float]] = []
    start: int | None = None
    for i, on in enumerate([*flags, False]):
        if on and start is None:
            start = i
        elif not on and start is not None:
            begin, end = start / fps, i / fps
            if runs and begin - runs[-1][1] <= merge_gap_s:
                runs[-1][1] = end
            else:
                runs.append([begin, end])
            start = None
    return [(a, b) for a, b in runs if b - a >= min_len_s]


def pan_expression(ranges: Sequence[Range], *, left: float, right: float, direction: str) -> str:
    """The left edge of the panning window as an FFmpeg expression of t: it travels from one edge to the other across
    each range and stays at the end until the next range begins."""
    start, end = (left, right) if direction == "left_to_right" else (right, left)
    pieces = []
    for a, b in ranges:
        progress = f"min(1,max(0,(t-{a:g})/{b - a:g}))"
        pieces.append(f"{start:g}+({end - start:g})*{progress}")
    result = pieces[-1]
    for (next_a, _), piece in zip(reversed(ranges[1:]), reversed(pieces[:-1]), strict=True):
        result = f"if(lt(t,{next_a:g}),{piece},{result})"
    return result


def _ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RenderError("ffmpeg is not installed")
    return ffmpeg


def _flags(command: list[str], width: int, height: int, test: Callable[[np.ndarray], bool], name: str) -> list[bool]:
    """Run FFmpeg for small RGB frames and say, for each frame, whether `test` finds what it looks for."""
    size = width * height * 3
    flags: list[bool] = []
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)  # noqa: S603  # nosec B603
        if process.stdout is None:
            raise RenderError("ffmpeg gave no output stream")
        try:
            while len(chunk := process.stdout.read(size)) == size:
                flags.append(test(np.frombuffer(chunk, dtype=np.uint8).reshape(height, width, 3)))
            process.stdout.close()
            code = process.wait(timeout=TIMEOUT_S)
        except BaseException:
            process.kill()
            raise
        if code != 0:
            errors.seek(0)
            raise RenderError(f"ffmpeg could not read frames from {name}: {errors.read()[-300:]!r}")
    return flags


def detect(source: Path, start_s: float, end_s: float, *, green_below: float, fps: float = SAMPLE_FPS) -> list[Range]:
    """The stretches of [start_s, end_s] that show a full-screen menu, in seconds from start_s."""
    command = facetrack.frames_command(
        _ffmpeg(), Path(source), start_s, end_s, fps=fps, width=DETECT_WIDTH, height=DETECT_HEIGHT
    )
    flags = _flags(
        command, DETECT_WIDTH, DETECT_HEIGHT, lambda frame: green_share(frame) < green_below, Path(source).name
    )
    return ranges_from_flags(flags, fps=fps, min_len_s=MIN_LEN_S, merge_gap_s=MERGE_GAP_S)


def alert_ranges(
    source: Path, start_s: float, end_s: float, *, box: tuple[int, int, int, int], min_share: float, fps: float = 4.0
) -> list[Range]:
    """The stretches of [start_s, end_s] where a donation alert's text is on screen: `box` is (x, y, w, h) in source
    pixels, and the alert is up while at least `min_share` of its lower part is bright yellow."""
    x, y, w, h = box
    top = round(h * ALERT_TEXT_FROM)  # look at the lower part only, where the amount and the message are
    command = facetrack.frames_command(
        _ffmpeg(), Path(source), start_s, end_s, fps=fps, width=w, height=h - top, crop=(w, h - top, x, y + top)
    )
    flags = _flags(command, w, h - top, lambda frame: yellow_share(frame) >= min_share, Path(source).name)
    return ranges_from_flags(flags, fps=fps, min_len_s=ALERT_MIN_LEN_S, merge_gap_s=ALERT_MERGE_GAP_S)
