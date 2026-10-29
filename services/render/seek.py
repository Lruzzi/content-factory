"""Cutting a window out of a recording without a frozen start.

A recording is MPEG-TS with a keyframe every few seconds. FFmpeg's input seek (`-ss` before `-i`) on such a file can
land on the keyframe AFTER the wanted time: the audio then starts on time, the video starts late, and the first frame
is repeated for as long as the gap lasts. So the input is seeked to a point safely BEFORE the window, and the exact
start is cut inside the filter graph, where it works frame by frame."""

from __future__ import annotations

MARGIN_S = 12.0  # longer than the keyframe spacing of anything we record or download


def split(start_s: float) -> tuple[float, float]:
    """(where to seek the input, how much to trim after it): they add up to `start_s`."""
    trim = min(start_s, MARGIN_S)
    return start_s - trim, trim
