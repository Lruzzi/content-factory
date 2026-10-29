"""Follow the speaker: a full-height 9:16 window that slides sideways to keep the face in view.

`detect` samples a few frames a second of the clip's own stretch and finds faces (MediaPipe Tasks FaceDetector, run
only on those frames). `plan` is pure: it turns the faces into a window position over time, steady while the face
stays put, a smooth follow when it walks, an instant snap when the camera cuts to the other host. `expression` writes
that path as an FFmpeg expression of `t` for the crop filter. The times are the clip's own, starting at zero."""

from __future__ import annotations

import shutil
import subprocess  # nosec B404
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from services.render import overlays, seek
from services.render.errors import RenderError

MODEL = "assets/models/blaze_face_short_range.tflite"
SAMPLE_FPS = 4.0
DETECT_WIDTH = 640
MIN_SCORE = 0.5
DEADZONE = 0.12  # a face that stays within this share of the window width from the centre does not move the window
CUT = 0.6  # a jump of more than this share of the window width between two samples is a camera cut, not a walk
SIMPLIFY_PX = 1.5
DETECT_TIMEOUT_S = 600

Knot = tuple[float, float]  # (seconds on the clip clock, window left edge in source pixels)


class FaceTrackError(RenderError):
    pass


@dataclass(frozen=True)
class Face:
    cx: float  # centre, as a share of the frame width
    w: float  # width, as a share of the frame width
    score: float


def _even(value: float) -> int:
    return max(2, round(value / 2) * 2)


def crop_width(src_w: int, src_h: int, out_w: int, out_h: int) -> int:
    """The width of a column with the output's shape and the source's full height (never wider than the source)."""
    return min(src_w, _even(src_h * out_w / out_h))


def _pick(faces: Sequence[Face]) -> Face | None:
    confident = [f for f in faces if f.score >= MIN_SCORE]
    return max(confident, key=lambda f: f.w * f.score, default=None)


def _simplify(knots: list[Knot], tolerance: float) -> list[Knot]:
    """Drop knots that a straight line between their neighbours reproduces to within `tolerance` pixels."""
    if len(knots) <= 2:
        return knots
    (t0, x0), (t1, x1) = knots[0], knots[-1]
    worst, at = 0.0, 0
    for i in range(1, len(knots) - 1):
        t, x = knots[i]
        line = x0 + (x1 - x0) * (t - t0) / (t1 - t0) if t1 > t0 else x0
        if abs(x - line) > worst:
            worst, at = abs(x - line), i
    if worst <= tolerance:
        return [knots[0], knots[-1]]
    return _simplify(knots[: at + 1], tolerance)[:-1] + _simplify(knots[at:], tolerance)


def plan(
    samples: Sequence[Sequence[Face]],
    *,
    fps: float,
    src_w: int,
    src_h: int,
    crop_w: int,
    smoothing: float,
    max_pan_px_per_s: float,
) -> list[Knot]:
    """The window's left edge over time. `samples[i]` holds the faces seen at i / fps seconds."""
    span = src_w - crop_w
    if span <= 0 or not samples:
        return [(0.0, 0.0)]
    limit = max_pan_px_per_s * (src_h / 1080) / fps  # the most the window may move between two samples
    dead, cut = DEADZONE * crop_w, CUT * crop_w
    target: float | None = None
    pos: float | None = None
    xs: list[float] = []
    cuts: set[int] = set()
    for i, faces in enumerate(samples):
        best = _pick(faces)
        if best is not None:
            target = min(max(best.cx * src_w - crop_w / 2, 0.0), float(span))
        if pos is None:
            pos = target if target is not None else span / 2
        elif target is not None:
            gap = target - pos
            if abs(gap) > cut:
                pos = target
                cuts.add(i)
            elif abs(gap) > dead:
                pos += min(max(gap * (1 - smoothing), -limit), limit)
        xs.append(pos)
    knots: list[Knot] = []
    for i, x in enumerate(xs):
        if i in cuts:  # snap half a sample before the one that first showed the new shot
            snap = (i - 0.5) / fps
            knots += [(snap - 0.02, round(xs[i - 1])), (snap, round(x))]
        knots.append((i / fps, round(x)))
    knots = [(0.0, knots[0][1]), *knots[1:]] if knots[0][0] != 0 else knots
    return _simplify(knots, SIMPLIFY_PX)


def position_at(knots: Sequence[Knot], t: float) -> float:
    """The window's left edge at time t: straight lines between knots, held before the first and after the last."""
    if t <= knots[0][0]:
        return knots[0][1]
    for (t0, x0), (t1, x1) in zip(knots, knots[1:], strict=False):
        if t <= t1:
            return x0 + (x1 - x0) * (t - t0) / (t1 - t0) if t1 > t0 else x1
    return knots[-1][1]


def _num(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def expression(knots: Sequence[Knot]) -> str:
    """The path as an FFmpeg expression of `t`, for the crop filter's x option (inside single quotes)."""
    result = _num(knots[-1][1])
    for (t0, x0), (t1, x1) in reversed(list(zip(knots, knots[1:], strict=False))):
        line = _num(x0) if x0 == x1 else f"{_num(x0)}+({_num(x1 - x0)})*(t-{_num(t0)})/{_num(t1 - t0)}"
        result = f"if(lt(t,{_num(t1)}),{line},{result})"
    return result


def frames_command(
    ffmpeg: str,
    source: Path,
    start_s: float,
    end_s: float,
    *,
    fps: float,
    width: int,
    height: int,
    crop: tuple[int, int, int, int] | None = None,
) -> list[str]:
    """FFmpeg reading small RGB frames of [start_s, end_s], seeked with the margin so the first one is the right one."""
    coarse, trim = seek.split(start_s)
    return [
        ffmpeg, "-v", "error", "-nostdin", "-protocol_whitelist", "file", "-ss", f"{coarse:.3f}", "-t",
        f"{end_s - start_s + trim:.3f}", "-i", str(source),
        "-vf",
        f"trim=start={trim:g},setpts=PTS-STARTPTS,fps={fps:g},"
        + (f"crop={crop[0]}:{crop[1]}:{crop[2]}:{crop[3]}," if crop else "")
        + f"scale={width}:{height}",
        "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
    ]  # fmt: skip


def detect(
    source: Path,
    start_s: float,
    end_s: float,
    *,
    src_w: int,
    src_h: int,
    fps: float = SAMPLE_FPS,
    model: Path | None = None,
) -> list[list[Face]]:
    """Faces in each sampled frame of [start_s, end_s]. Frames are decoded small by FFmpeg and read from a pipe."""
    import numpy as np  # imported here: only a render that follows faces needs them
    from mediapipe import Image, ImageFormat
    from mediapipe.tasks.python import BaseOptions, vision

    width, height = DETECT_WIDTH, _even(DETECT_WIDTH * src_h / src_w)
    frame_bytes = width * height * 3
    model_path = model or overlays.resolve_asset(MODEL)
    if not Path(model_path).is_file():
        raise FaceTrackError(f"the face model is missing: {model_path}")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise FaceTrackError("ffmpeg is not installed")
    command = frames_command(ffmpeg, Path(source), start_s, end_s, fps=fps, width=width, height=height)
    options = vision.FaceDetectorOptions(
        base_options=BaseOptions(model_asset_path=str(model_path)), min_detection_confidence=MIN_SCORE
    )
    samples: list[list[Face]] = []
    with tempfile.TemporaryFile() as errors, vision.FaceDetector.create_from_options(options) as detector:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)  # noqa: S603  # nosec B603
        if process.stdout is None:
            raise FaceTrackError("ffmpeg gave no output stream")
        try:
            while len(chunk := process.stdout.read(frame_bytes)) == frame_bytes:
                pixels = np.frombuffer(chunk, dtype=np.uint8).reshape(height, width, 3)
                found = detector.detect(Image(image_format=ImageFormat.SRGB, data=pixels))
                samples.append(
                    [
                        Face(
                            cx=(d.bounding_box.origin_x + d.bounding_box.width / 2) / width,
                            w=d.bounding_box.width / width,
                            score=float(d.categories[0].score),
                        )
                        for d in found.detections
                    ]
                )
            process.stdout.close()
            code = process.wait(timeout=DETECT_TIMEOUT_S)
        except BaseException:
            process.kill()
            raise
        if code != 0:
            errors.seek(0)
            raise FaceTrackError(f"ffmpeg could not read frames from {Path(source).name}: {errors.read()[-300:]!r}")
    return samples


def track(
    source: Path, start_s: float, end_s: float, *, src_w: int, src_h: int, out_w: int, out_h: int, smoothing: float,
    max_pan_px_per_s: float,
) -> list[Knot]:  # fmt: skip
    """Find the faces in the clip's stretch and plan the window that follows them."""
    samples = detect(source, start_s, end_s, src_w=src_w, src_h=src_h)
    return plan(
        samples, fps=SAMPLE_FPS, src_w=src_w, src_h=src_h, crop_w=crop_width(src_w, src_h, out_w, out_h),
        smoothing=smoothing, max_pan_px_per_s=max_pan_px_per_s,
    )  # fmt: skip
