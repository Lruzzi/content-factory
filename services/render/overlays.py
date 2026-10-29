"""Where assets live and where the logo goes.

The logo sits in a corner of the safe zone (the margins platforms keep for their own buttons). Placement
uses the overlay filter's own `w` and `h`, so it needs no logo size in advance.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.render.captions import safe_rect
from services.render.errors import AssetError

ASSETS_DIR_ENV = "ASSETS_DIR"
REPO_ROOT = Path(__file__).resolve().parents[2]
LOGO_INPUT_INDEX = 1  # the source is input 0


@dataclass(frozen=True)
class LogoPlan:
    path: Path
    chain: str  # prepares the logo input as [logo]
    overlay: str  # the x= and y= options for the overlay filter


def resolve_asset(path: str | Path) -> Path:
    """A relative asset path is looked up under $ASSETS_DIR, or the repo root when that is not set."""
    candidate = Path(path)
    return candidate if candidate.is_absolute() else Path(os.environ.get(ASSETS_DIR_ENV) or REPO_ROOT) / candidate


def logo_plan(logo: dict[str, Any], safe_zone: dict[str, float], size: tuple[int, int]) -> LogoPlan | None:
    """None when the logo is off. A logo that is on but missing is an error: no unbranded clips by accident."""
    if not logo.get("enabled", True) or not logo.get("path"):
        return None
    path = resolve_asset(logo["path"])
    if not path.is_file():
        raise AssetError(
            f"logo file not found: {path} (add it, or set branding.logo.enabled: false; see assets/README.md)"
        )
    x0, y0, x1, y1 = safe_rect(size[0], size[1], safe_zone)
    vertical, horizontal = logo.get("position", "top_right").split("_")
    x = f"{round(x0)}" if horizontal == "left" else f"{round(x1)}-w"
    y = f"{round(y0)}" if vertical == "top" else f"{round(y1)}-h"
    width, opacity = int(logo.get("width", 140)), float(logo.get("opacity", 0.9))
    chain = f"[{LOGO_INPUT_INDEX}:v]scale={width}:-2,format=rgba,colorchannelmixer=aa={opacity:g}[logo]"
    return LogoPlan(path=path, chain=chain, overlay=f"x={x}:y={y}")
