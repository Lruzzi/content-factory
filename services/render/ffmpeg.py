"""Run FFmpeg: probe a source, build one safe command, render a clip window atomically.

The command is an argument list (never a shell string). Inputs are limited to local files, because the
sources are untrusted media: a playlist inside a file must not be able to make FFmpeg fetch a URL.
A render is written to `<name>.part` and renamed, so a half-written clip never has the final name.
"""

# bandit notes (B404, B603) are marked nosec below: every call passes an argument list, never a shell, the binary
# comes from shutil.which, and the only variable arguments are local paths and numbers.
from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import config_schema as cs
from services.render import captions as captions_mod
from services.render import facetrack, overlays, profanity, scenes, seek
from services.render import layout as layout_mod
from services.render.errors import AssetError, RenderError

__all__ = [
    "AssetError",
    "MediaInfo",
    "RenderError",
    "build_command",
    "check_window",
    "pick_video_codec",
    "probe",
    "render_clip",
]

MAX_CLIP_S = 180  # the schema's own ceiling on clip_length_s
LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"
PROBE_TIMEOUT_S = 60
DEFAULT_RENDER_TIMEOUT_S = 840  # a 5-minute recap at 60 fps with the medium preset takes about 8 minutes


@dataclass(frozen=True)
class MediaInfo:
    width: int
    height: int
    duration_s: float
    fps: float
    video_codec: str
    pix_fmt: str
    audio_codec: str | None

    @property
    def has_audio(self) -> bool:
        return self.audio_codec is not None


def _binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RenderError(f"{name} is not installed")
    return path


def _tail(text: str, lines: int = 8) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def _fraction(text: str) -> float:
    numerator, _, denominator = text.partition("/")
    return float(numerator) / float(denominator or 1) if float(denominator or 1) else 0.0


LONG_LOOK = ["-analyzeduration", "30M", "-probesize", "100M"]  # for a file whose first seconds hold no stream headers


def _probe_once(path: Path, look: list[str]) -> dict[str, Any]:
    command = [
        _binary("ffprobe"), "-v", "error", "-protocol_whitelist", "file", *look, "-print_format", "json",
        "-show_streams", "-show_format", str(path),
    ]  # fmt: skip
    try:
        run = subprocess.run(command, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S, check=False)  # noqa: S603  # nosec B603
    except subprocess.TimeoutExpired as exc:
        raise RenderError(f"cannot probe {Path(path).name}: ffprobe timed out") from exc
    if run.returncode != 0:
        raise RenderError(f"cannot probe {Path(path).name}: {_tail(run.stderr)}")
    data: dict[str, Any] = json.loads(run.stdout or "{}")
    return data


def probe(path: Path) -> MediaInfo:
    data = _probe_once(path, [])
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is not None and not (video.get("width") and video.get("height")):
        # a recorded segment that does not start with its stream headers reads as 0x0 at first: look longer
        data = _probe_once(path, LONG_LOOK)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise RenderError(f"cannot probe {Path(path).name}: no video stream")
    if not (video.get("width") and video.get("height")):
        raise RenderError(f"cannot probe {Path(path).name}: the video size could not be read")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    return MediaInfo(
        width=int(video["width"]),
        height=int(video["height"]),
        duration_s=duration,
        fps=_fraction(video.get("avg_frame_rate", "0/1")),
        video_codec=video.get("codec_name", ""),
        pix_fmt=video.get("pix_fmt", ""),
        audio_codec=audio.get("codec_name") if audio else None,
    )


@cache
def pick_video_codec(preferred: str) -> str:
    """The encoder to use. NVENC needs a working NVIDIA driver, so it is tried once with a tiny encode and
    libx264 is used when it fails (the profile's `video_codec: h264_nvenc` is a preference, not a promise)."""
    if preferred != "h264_nvenc":
        return preferred
    trial = [
        _binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "lavfi", "-i",
        "nullsrc=s=256x256:d=0.1", "-frames:v", "1", "-c:v", "h264_nvenc", "-f", "null", "-",
    ]  # fmt: skip
    try:
        ok = subprocess.run(trial, capture_output=True, timeout=30, check=False).returncode == 0  # noqa: S603  # nosec B603
    except subprocess.TimeoutExpired:
        ok = False
    return "h264_nvenc" if ok else "libx264"


def build_command(
    *,
    ffmpeg: str,
    source: Path,
    out: Path,
    start_s: float,
    duration_s: float,
    graph: str,
    output: dict[str, Any],
    codec: str,
    has_audio: bool,
    loudnorm: bool,
    map_label: str = "v",
    extra_inputs: Sequence[Path] = (),
    mute: Sequence[tuple[float, float]] = (),
) -> list[str]:
    crf = str(output.get("crf", 20))
    video = ["-c:v", codec, "-cq", crf, "-b:v", "0"] if codec == "h264_nvenc" else ["-c:v", codec, "-crf", crf]
    if codec == "libx264":
        video += ["-preset", str(output.get("preset", "veryfast"))]
    audio = (
        [
            "-map",
            "0:a:0?",
            "-c:a",
            output.get("audio_codec", "aac"),
            "-b:a",
            str(output.get("audio_bitrate", "160k")),
            "-ar",
            str(output.get("audio_sample_rate", 48000)),  # loudnorm works at 192 kHz; Instagram takes 48 kHz at most
        ]  # fmt: skip
        if has_audio
        else ["-an"]
    )
    coarse, trim = seek.split(start_s)  # seek well before the window, then cut its exact start frame by frame
    audio_filters = []
    if trim > 0:
        graph = f"[0:v]trim=start={trim:g},setpts=PTS-STARTPTS[t0];" + graph.replace("[0:v]", "[t0]", 1)
        audio_filters.append(f"atrim=start={trim:g},asetpts=PTS-STARTPTS")
    audio_filters += [f"volume=0:enable='between(t,{a:.3f},{b:.3f})'" for a, b in mute]  # bad words: after the cut
    if loudnorm:
        audio_filters.append(LOUDNORM)
    if has_audio and audio_filters:
        audio += ["-af", ",".join(audio_filters)]
    extras = [arg for extra in extra_inputs for arg in ("-protocol_whitelist", "file", "-i", str(extra))]
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
        "-protocol_whitelist", "file",
        "-ss", f"{coarse:.3f}", "-i", str(source), *extras, "-t", f"{duration_s:.3f}",
        "-filter_complex", graph, "-map", f"[{map_label}]",
        "-r", str(output.get("fps", 30)), *video, "-pix_fmt", "yuv420p",
        *audio,
        "-movflags", "+faststart", "-f", "mp4", "-y", str(out),
    ]  # fmt: skip


def check_window(start_s: float, end_s: float, source_duration_s: float, max_s: float = MAX_CLIP_S) -> None:
    if not 0 <= start_s < end_s:
        raise ValueError(f"clip window {start_s}-{end_s} s must start at or after 0 and end after it starts")
    if end_s - start_s > max_s:
        raise ValueError(f"clip window {start_s}-{end_s} s is longer than {max_s:g} s")
    if end_s > source_duration_s + 0.25:
        raise ValueError(f"clip window {start_s}-{end_s} s ends after the source ({source_duration_s:.1f} s)")


def _text_overlay(
    profile: cs.Profile, work: Path, words: Sequence[captions_mod.Word], hook: str | None, start_s: float, end_s: float
) -> bool:
    """Write captions.ass (and the font it needs) into `work` when there is text to draw. Returns whether it did."""
    captions, branding = profile.captions, profile.branding
    if profile.layout.type == "split" and "below_cam" in captions:  # just under the face cam, above the donation card
        captions = {**captions, "y": profile.layout.split_ratio + float(captions["below_cam"])}
    credit = captions_mod.credit_text(
        branding.get("credit", {}).get("text"), profile.source.name or profile.source.id or ""
    )
    hook_on = bool(hook) and branding.get("hook_overlay", {}).get("enabled", True)
    if not ((captions.get("enabled", True) and words) or hook_on or credit):
        return False
    font = overlays.resolve_asset(captions["font"])
    ass = captions_mod.build_ass(
        list(words),
        captions=captions,
        branding=branding,
        safe_zone=profile.safe_zone,
        size=(profile.output["width"], profile.output["height"]),
        font_path=font,
        hook=hook if hook_on else None,
        credit=credit,
        duration_s=end_s - start_s,
        offset_s=start_s,
    )
    (work / "fonts").mkdir()
    shutil.copy(font, work / "fonts" / font.name)
    (work / "captions.ass").write_text(ass, encoding="utf-8")
    return True


def _even(value: float) -> int:
    return max(2, round(value / 2) * 2)


def render_clip(
    profile: cs.Profile,
    source: Path,
    start_s: float,
    end_s: float,
    out: Path,
    *,
    words: Sequence[captions_mod.Word] = (),
    hook: str | None = None,
    timeout_s: float = DEFAULT_RENDER_TIMEOUT_S,
    max_s: float = MAX_CLIP_S,
    kind: str | None = None,
) -> Path:
    """Cut [start_s, end_s] from the source into a 9:16 clip at `out`: the profile's layout, then the logo,
    then captions, hook and credit, with loudness normalised. `words` and the window share one clock (the
    source file's), and the captions are shifted so the clip starts at zero."""
    source, out = Path(source).resolve(), Path(out).resolve()
    info = probe(source)
    check_window(start_s, end_s, info.duration_s, max_s)
    config = profile.output
    size = (config["width"], config["height"])
    path = None
    if profile.layout.type == "facetrack":  # the window follows the speaker: find the faces in this stretch first
        path = facetrack.track(
            source, start_s, end_s, src_w=info.width, src_h=info.height, out_w=size[0], out_h=size[1],
            smoothing=profile.layout.smoothing, max_pan_px_per_s=profile.layout.max_pan_px_per_s,
        )  # fmt: skip
    menus: list[tuple[float, float]] = []
    wide = profile.layout.wide
    if profile.layout.type == "split" and wide and wide.enabled:  # a full-screen menu shows the whole frame
        menus = scenes.detect(source, start_s, end_s, green_below=wide.green_below)
    alerts: list[tuple[float, float]] = []
    box = profile.layout.donation
    if kind == "donation" and box is not None:  # on any layout: the alert is shown while its text is on the screen
        factor = info.height / layout_mod.REFERENCE_HEIGHT
        alerts = scenes.alert_ranges(
            source, start_s, end_s, box=(round(box.x * factor), round(box.y * factor), _even(box.w * factor),
                                          _even(box.h * factor)),
            min_share=profile.layout.donation_min_share,
        )  # fmt: skip
    extras = layout_mod.Overlays(wide=menus, donation=alerts)
    graph = layout_mod.filter_graph(
        profile.layout, src_w=info.width, src_h=info.height, out_w=size[0], out_h=size[1], track=path, overlays=extras
    )
    logo = overlays.logo_plan(profile.branding.get("logo", {}), profile.safe_zone, size)
    label, extra_inputs = "v", []
    if logo:
        graph += f";{logo.chain};[{label}][logo]overlay={logo.overlay}[v2]"
        label, extra_inputs = "v2", [logo.path]
    part = out.with_name(out.name + ".part")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="render-") as tmp:
        work = Path(tmp)
        shown = profanity.mask(words, profile.profanity)
        if _text_overlay(profile, work, shown, hook, start_s, end_s):
            graph += f";[{label}]subtitles=captions.ass:fontsdir=fonts[v3]"
            label = "v3"
        command = build_command(
            ffmpeg=_binary("ffmpeg"),
            source=source,
            out=part,
            start_s=start_s,
            duration_s=end_s - start_s,
            graph=graph,
            output=config,
            codec=pick_video_codec(config.get("video_codec", "libx264")),
            has_audio=info.has_audio,
            loudnorm=bool(config.get("loudnorm", True)),
            map_label=label,
            extra_inputs=extra_inputs,
            mute=profanity.mute_ranges(words, profile.profanity, clip_start_s=start_s, clip_s=end_s - start_s),
        )
        try:
            run = subprocess.run(  # noqa: S603  # nosec B603
                command, capture_output=True, text=True, timeout=timeout_s, check=False, cwd=work
            )
        except subprocess.TimeoutExpired as exc:
            part.unlink(missing_ok=True)
            raise RenderError(f"render of {source.name} timed out after {timeout_s:g} s") from exc
    if run.returncode != 0:
        part.unlink(missing_ok=True)
        raise RenderError(f"render of {source.name} failed: {_tail(run.stderr)}")
    os.replace(part, out)
    return out
