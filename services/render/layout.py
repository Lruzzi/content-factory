"""9:16 layouts as FFmpeg filter graphs.

Every graph reads the first video input and ends in a stream named [v] of exactly out_w x out_h. All
sizes are worked out here, in Python, so the graph has no expressions and no commas to escape. The one exception is
facetrack, whose moving crop is an expression of `t`, written inside single quotes. Boxes in
the config (`layout.cam`, `layout.main`) are in a 1080-pixel-high reference frame and are scaled to the
real source.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import config_schema as cs
from services.render import facetrack, scenes

REFERENCE_HEIGHT = 1080
EDGE_TOLERANCE_PX = 2  # rounding may push a scaled box this far past the frame; more is a wrong box


DONATION_MARGIN_PX = 140  # from the top of the gameplay panel to the donation alert: the captions sit in this gap


@dataclass(frozen=True)
class Overlays:
    """What a clip needs on top of the plain split layout: the stretches (seconds on the clip's clock) where a
    full-screen menu shows, and where the stream's donation alert is on screen."""

    wide: Sequence[tuple[float, float]] = ()
    donation: Sequence[tuple[float, float]] = ()


class UnsupportedLayout(NotImplementedError):
    """The layout type exists in the config but is not built yet."""


def _even(value: float) -> int:
    return max(2, round(value / 2) * 2)


def filter_graph(
    layout: cs.Layout,
    *,
    src_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
    track: Sequence[facetrack.Knot] | None = None,
    overlays: Overlays | None = None,
) -> str:
    if layout.type == "split":  # the card goes over the gameplay panel: built inside
        return _split(layout, src_w, src_h, out_w, out_h, overlays or Overlays())
    if layout.type == "center":
        graph = _center(src_w, src_h, out_w, out_h)
    elif layout.type == "blur_fill":
        graph = _blur_fill(out_w, out_h)
    else:
        if track is None:
            raise UnsupportedLayout("layout 'facetrack' needs a path of the window over time (render finds it first)")
        graph = _facetrack(track, src_w, src_h, out_w, out_h)
    if overlays and overlays.donation:
        graph = _with_donation(graph, layout, src_w, src_h, out_w, out_h, overlays.donation)
    return graph


def _with_donation(
    graph: str, layout: cs.Layout, src_w: int, src_h: int, out_w: int, out_h: int, alerts: Sequence[tuple[float, float]]
) -> str:
    """Any layout but split: the stream's alert, cut from the source, centred over the finished frame while it is up."""
    if layout.donation is None:
        raise ValueError("showing the donation alert needs layout.donation in the config")
    box_w, box_h, box_x, box_y = _scaled_box(layout.donation, src_h / REFERENCE_HEIGHT, src_w, src_h, "donation")
    shown_w = _even(out_w * layout.donation_width)
    enabled = "+".join(f"between(t,{a:g},{b:g})" for a, b in alerts)
    base = graph.replace("[0:v]", "[src]", 1).removesuffix("[v]") + "[vbase]"  # the layout's own result, renamed
    return (
        f"[0:v]split=2[src][dsrc];{base};"
        f"[dsrc]crop={box_w}:{box_h}:{box_x}:{box_y},scale={shown_w}:-2[don];"
        f"[vbase][don]overlay=x=(W-w)/2:y={round(out_h * layout.donation_y)}:enable='{enabled}'[v]"
    )


def _facetrack(track: Sequence[facetrack.Knot], src_w: int, src_h: int, out_w: int, out_h: int) -> str:
    crop_w = facetrack.crop_width(src_w, src_h, out_w, out_h)
    return (
        f"[0:v]crop=w={crop_w}:h={src_h}:x='{facetrack.expression(track)}':y=0,"
        f"scale={out_w}:{out_h}:flags=lanczos,setsar=1[v]"
    )


def _center(src_w: int, src_h: int, out_w: int, out_h: int) -> str:
    crop_w = _even(min(src_w, src_h * out_w / out_h))
    return f"[0:v]crop={crop_w}:{src_h},scale={out_w}:{out_h}:flags=lanczos,setsar=1[v]"


def _blur_fill(out_w: int, out_h: int) -> str:
    return (
        "[0:v]split=2[a][b];"
        f"[a]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,crop={out_w}:{out_h},"
        "boxblur=luma_radius=20:luma_power=3[bg];"
        f"[b]scale={out_w}:-2:flags=lanczos[fg];"
        "[bg][fg]overlay=x=(W-w)/2:y=(H-h)/2,setsar=1[v]"
    )


def _scaled_box(box: cs.Box, factor: float, src_w: int, src_h: int, name: str) -> tuple[int, int, int, int]:
    w, h = _even(box.w * factor), _even(box.h * factor)
    x, y = round(box.x * factor), round(box.y * factor)
    over_x, over_y = x + w - src_w, y + h - src_h
    if w > src_w or h > src_h or over_x > EDGE_TOLERANCE_PX or over_y > EDGE_TOLERANCE_PX:
        raise ValueError(f"layout.{name} box {box.model_dump()} is outside the {src_w}x{src_h} source frame")
    return w, h, x - max(0, over_x), y - max(0, over_y)


def _panel(
    label: str, box: tuple[int, int, int, int], out_w: int, panel_h: int, name: str, *, scaler: str = "bicubic",
    sharpen: float = 0.0,
) -> str:  # fmt: skip
    w, h, x, y = box
    flags = ":flags=lanczos" if scaler == "lanczos" else ""
    after = f",unsharp=5:5:{sharpen:g}:3:3:0" if sharpen else ""  # luma only: no colour fringes
    return (
        f"[{label}]crop={w}:{h}:{x}:{y},"
        f"scale={out_w}:{panel_h}:force_original_aspect_ratio=increase{flags},crop={out_w}:{panel_h}{after}[{name}]"
    )


def _split(layout: cs.Layout, src_w: int, src_h: int, out_w: int, out_h: int, overlays: Overlays) -> str:
    if layout.cam is None:
        raise ValueError("split layout needs layout.cam")
    if layout.main is None:
        raise ValueError("split layout needs layout.main")
    if overlays.wide and layout.wide is None:
        raise ValueError("a menu range needs layout.wide in the config")
    if overlays.donation and layout.donation is None:
        raise ValueError("showing the donation alert needs layout.donation in the config")
    factor = src_h / REFERENCE_HEIGHT
    cam = _scaled_box(layout.cam, factor, src_w, src_h, "cam")
    main = _scaled_box(layout.main, factor, src_w, src_h, "main")
    cam_h = _even(out_h * layout.split_ratio)
    main_h = out_h - cam_h
    main_w_px, main_h_px, _, main_y = main
    window_w = _even(main_h_px * out_w / main_h)  # a window of the panel's shape, as tall as the gameplay crop
    fit = layout.wide is not None and layout.wide.mode == "fit"
    wide = list(overlays.wide) if fit or window_w < src_w else []  # a frame no wider than the window has nothing to pan
    branches = ["c", "m", *(["w"] if wide else []), *(["d"] if overlays.donation else [])]
    parts = [
        f"[0:v]split={len(branches)}" + "".join(f"[{b}]" for b in branches),
        _panel("c", cam, out_w, cam_h, "cam", scaler=layout.scaler, sharpen=layout.sharpen),
        _panel("m", main, out_w, main_h, "main", scaler=layout.scaler),
    ]
    gameplay = "[main]"
    if wide and layout.wide is not None:
        enabled = "+".join(f"between(t,{a:g},{b:g})" for a, b in wide)
        if fit:  # the whole width at once, over a blurred copy of the same band that fills the rest of the panel
            parts += [
                f"[w]crop={src_w}:{main_h_px}:0:{main_y},split=2[wa][wb]",
                f"[wa]scale={out_w}:{main_h}:force_original_aspect_ratio=increase,crop={out_w}:{main_h},boxblur=28:4[wbg]",
                f"[wb]scale={out_w}:-2:flags=lanczos[wfg]",
                "[wbg][wfg]overlay=0:(H-h)/2[wide]",
            ]
        else:
            expression = scenes.pan_expression(wide, left=0.0, right=float(src_w - window_w), direction=layout.wide.pan)
            parts.append(
                f"[w]crop=w={window_w}:h={main_h_px}:x='{expression}':y={main_y},scale={out_w}:{main_h}:flags=lanczos[wide]"
            )
        parts.append(f"[main][wide]overlay=0:0:enable='{enabled}'[gameplay]")
        gameplay = "[gameplay]"
    if overlays.donation and layout.donation is not None:
        box_w, box_h, box_x, box_y = _scaled_box(layout.donation, factor, src_w, src_h, "donation")
        shown_w = _even(out_w * layout.donation_width)
        enabled = "+".join(f"between(t,{a:g},{b:g})" for a, b in overlays.donation)
        parts += [
            f"[d]crop={box_w}:{box_h}:{box_x}:{box_y},scale={shown_w}:-2[don]",
            f"[cam]{gameplay}vstack=inputs=2,setsar=1[stacked]",
            f"[stacked][don]overlay=x=(W-w)/2:y={cam_h + DONATION_MARGIN_PX}:enable='{enabled}'[v]",
        ]
    else:
        parts.append(f"[cam]{gameplay}vstack=inputs=2,setsar=1[v]")
    return ";".join(parts)
