"""Captions, hook and credit as an ASS subtitle file, rendered by FFmpeg's `subtitles` filter.

All text comes from transcripts and LLM output, so it is untrusted: `clean` removes the characters that
start ASS override codes before anything is written. Line breaks and sizes are worked out here from the
real font metrics, so every line fits between the safe-zone margins, and positions are validated against
the safe zone (platform buttons and captions sit in the margins we keep clear).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fontTools.ttLib import TTFont

MAX_HOOK_CHARS = 80
MAX_HOOK_LINES = 2
MIN_HOOK_SIZE = 40
POP_PERCENT = 112  # a word pops to this size for a moment when it is spoken
FIT_MARGIN = 0.96  # lines are fitted to this share of the width, leaving room for the pop
MIN_EVENT_S = 0.04
HEADER = (
    "[Script Info]\nScriptType: v4.00+\nPlayResX: {w}\nPlayResY: {h}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
    "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
    "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
    "MarginL, MarginR, MarginV, Encoding\n"
)
EVENTS = (
    "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
)  # fmt: skip


class FontError(RuntimeError):
    """The caption font is missing or unreadable. Captions never fall back to a default font."""


@dataclass(frozen=True)
class Word:
    text: str
    start_s: float
    end_s: float


# ---------- text safety and ASS formats ------------------------------------------------------------
def clean(text: str) -> str:
    """Make untrusted text safe to put in an ASS file: no braces or backslashes (they open override codes),
    no control characters, single spaces."""
    text = re.sub(r"[{}\\]", "", text)
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    return " ".join(text.split())


def ass_colour(hex_colour: str) -> str:
    match = re.fullmatch(r"#([0-9A-Fa-f]{2})([0-9A-Fa-f]{2})([0-9A-Fa-f]{2})", hex_colour)
    if not match:
        raise ValueError(f"colour {hex_colour!r} must look like #RRGGBB")
    red, green, blue = (part.upper() for part in match.groups())
    return f"&H00{blue}{green}{red}"


def ass_time(seconds: float) -> str:
    centi = max(0, round(seconds * 100))
    hours, rest = divmod(centi, 360_000)
    minutes, rest = divmod(rest, 6_000)
    return f"{hours}:{minutes:02d}:{rest // 100:02d}.{rest % 100:02d}"


def parse_ass_time(text: str) -> float:
    hours, minutes, seconds = text.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def credit_text(template: str | None, source_name: str) -> str | None:
    """Fill in `{source_name}` and nothing else, so a template cannot reach into objects."""
    return None if template is None else template.replace("{source_name}", source_name)


# ---------- the font -------------------------------------------------------------------------------------
def font_family(path: Path) -> tuple[str, bool]:
    """The family name ASS must ask for, and whether to ask for bold, read from the font file itself."""
    path = Path(path)
    if not path.is_file():
        raise FontError(f"caption font not found: {path} (add the font file, see assets/README.md)")
    try:
        names = TTFont(path, lazy=True)["name"]
    except Exception as exc:  # fontTools raises several types for a broken file
        raise FontError(f"cannot read caption font {path.name}: {exc}") from exc
    family = names.getName(1, 3, 1, 0x409) or names.getBestFamilyName()
    style = names.getName(2, 3, 1, 0x409)
    return str(family), bool(style and "bold" in str(style).lower())


class TextMeasure:
    """Width of text in a font at a size, from the font's own advance widths (no kerning)."""

    def __init__(self, font_path: Path, size: float, _tables: Any = None) -> None:
        if _tables is None:
            font = TTFont(Path(font_path), lazy=True)
            _tables = (font.getBestCmap(), font["hmtx"], font["head"].unitsPerEm, font.getGlyphOrder()[0])
        self._tables = _tables
        self._path, self.size = Path(font_path), size

    def at(self, size: float) -> TextMeasure:
        return TextMeasure(self._path, size, self._tables)

    def width(self, text: str) -> float:
        cmap, hmtx, units, notdef = self._tables
        advance = sum(hmtx[cmap.get(ord(ch), notdef)][0] for ch in text)
        return advance * self.size / units


# ---------- building the file ------------------------------------------------------------------------------
def safe_rect(width: int, height: int, safe: dict[str, float]) -> tuple[float, float, float, float]:
    return (
        width * safe.get("left", 0.0),
        height * safe.get("top", 0.0),
        width * (1 - safe.get("right", 0.0)),
        height * (1 - safe.get("bottom", 0.0)),
    )


def _style(name: str, family: str, bold: bool, size: int, colour: str, outline: int, alignment: int) -> str:
    return (
        f"Style: {name},{family},{size},{ass_colour(colour)},{ass_colour(colour)},&H00000000,&H80000000,"
        f"{-1 if bold else 0},0,0,0,100,100,0,0,1,{outline},0,{alignment},0,0,0,1\n"
    )


def _prepare(words: list[Word], duration_s: float, offset_s: float, upper: bool) -> list[Word]:
    ready: list[Word] = []
    for word in sorted(words, key=lambda w: w.start_s):
        start, end = word.start_s - offset_s, word.end_s - offset_s
        text = clean(word.text)
        if not text or end <= 0 or start >= duration_s:
            continue
        start, end = max(0.0, start), min(duration_s, end)
        ready.append(Word(text.upper() if upper else text, start, max(end, start + MIN_EVENT_S)))
    return ready


def _group(words: list[Word], max_words: int, measure: TextMeasure, limit: float) -> list[list[Word]]:
    lines: list[list[Word]] = []
    current: list[Word] = []
    for word in words:
        trial = [*current, word]
        too_long = measure.width(" ".join(w.text for w in trial)) > limit
        if current and (len(trial) > max_words or too_long):
            lines.append(current)
            current = [word]
        else:
            current = trial
    return [*lines, current] if current else lines


def _caption_events(
    words: list[Word], captions: dict[str, Any], measure: TextMeasure, limit: float, centre: tuple[int, int]
) -> list[str]:
    base = int(captions.get("size", 72))
    normal, highlight = ass_colour(captions.get("color", "#FFFFFF")), ass_colour(captions.get("highlight", "#FFE600"))
    progressive = str(captions.get("reveal", "progressive")) != "line"
    lines = _group(words, int(captions.get("max_words_per_line", 3)), measure, limit)
    events: list[str] = []
    last_end = 0.0
    for index, line in enumerate(lines):
        text_width = measure.width(" ".join(w.text for w in line))
        size = min(base, int(base * limit / text_width)) if text_width > limit else base
        size_tag = f"\\fs{size}" if size < base else ""
        # A line that fills up word by word is anchored at its left edge, where the finished line will start, so the
        # words already there do not move when the next one is added; a whole line is centred as before.
        left = round(centre[0] - text_width * size / base / 2)
        place = f"\\an4\\pos({left},{centre[1]})" if progressive else f"\\an5\\pos({centre[0]},{centre[1]})"
        next_start = lines[index + 1][0].start_s if index + 1 < len(lines) else float("inf")
        for position, word in enumerate(line):
            start = max(word.start_s, last_end)
            end = line[position + 1].start_s if position + 1 < len(line) else min(word.end_s, next_start)
            if end <= start:
                continue
            shown = line[: position + 1] if progressive else line  # words not yet spoken are not shown
            parts = [
                f"{{\\c{highlight}&\\t(0,90,\\fscx{POP_PERCENT}\\fscy{POP_PERCENT})\\t(90,180,\\fscx100\\fscy100)}}"
                f"{w.text}{{\\c{normal}&\\fscx100\\fscy100}}"
                if i == position
                else w.text
                for i, w in enumerate(shown)
            ]
            tags = f"{{{place}{size_tag}}}"
            events.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Caption,,0,0,0,,{tags}{' '.join(parts)}")
            last_end = end
    return events


def _wrap(text: str, measure: TextMeasure, limit: float) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if current and measure.width(trial) > limit:
            lines.append(current)
            current = word
        else:
            current = trial
    return [*lines, current] if current else lines


def _hook_event(
    text: str, branding: dict[str, Any], captions: dict[str, Any], measure: TextMeasure, limit: float, geometry: Any
) -> str | None:
    hook = branding.get("hook_overlay", {})
    text = clean(text)[:MAX_HOOK_CHARS].strip()
    if not hook.get("enabled", True) or not text:
        return None
    if captions.get("uppercase", True):
        text = text.upper()
    size = round(int(captions.get("size", 72)) * 0.9)
    lines = _wrap(text, measure.at(size), limit)
    while (
        len(lines) > MAX_HOOK_LINES or any(measure.at(size).width(x) > limit for x in lines)
    ) and size > MIN_HOOK_SIZE:
        size -= 4
        lines = _wrap(text, measure.at(size), limit)
    centre_x, top, duration, height = geometry
    y = round(max(hook.get("y", 0.14) * height, top))
    end = min(float(hook.get("seconds", 2.5)), duration)
    return (
        f"Dialogue: 0,{ass_time(0)},{ass_time(end)},Hook,,0,0,0,,"
        f"{{\\an8\\pos({centre_x},{y})\\fs{size}}}{'\\N'.join(lines)}"
    )


def build_ass(
    words: list[Word],
    *,
    captions: dict[str, Any],
    branding: dict[str, Any],
    safe_zone: dict[str, float],
    size: tuple[int, int],
    font_path: Path,
    hook: str | None,
    credit: str | None,
    duration_s: float,
    offset_s: float = 0.0,
) -> str:
    """The ASS file for one clip. `words` carry stream times; `offset_s` is the clip's start in the stream."""
    width, height = size
    family, bold = font_family(font_path)
    x0, y0, x1, y1 = safe_rect(width, height, safe_zone)
    base = int(captions.get("size", 72))
    outline = int(captions.get("outline", 6))
    centre_x = round((x0 + x1) / 2)
    limit = (x1 - x0 - 2 * outline) * FIT_MARGIN
    measure = TextMeasure(font_path, base)

    caption_y = round(height * float(captions.get("y", 0.62)))
    half = base * 1.2 / 2 + outline
    if captions.get("enabled", True) and not (y0 <= caption_y - half and caption_y + half <= y1):
        raise ValueError(
            f"captions.y {captions.get('y')} puts the captions outside the safe zone ({y0:.0f}-{y1:.0f} px)"
        )

    text = HEADER.format(w=width, h=height)
    text += _style("Caption", family, bold, base, captions.get("color", "#FFFFFF"), outline, 5)
    text += _style("Hook", family, bold, round(base * 0.9), "#FFFFFF", outline, 8)
    credit_size = int(branding.get("credit", {}).get("size", 34))
    text += _style("Credit", family, bold, credit_size, "#FFFFFF", max(2, outline // 2), 2)
    text += EVENTS

    events: list[str] = []
    if captions.get("enabled", True):
        delay = float(captions.get("offset_s", 0.0))  # Whisper's word times tend to be early: show a little later
        ready = _prepare(words, duration_s, offset_s - delay, bool(captions.get("uppercase", True)))
        events += _caption_events(ready, captions, measure, limit, (centre_x, caption_y))
    if hook:
        event = _hook_event(hook, branding, captions, measure, limit, (centre_x, y0, duration_s, height))
        if event:
            events.append(event)
    credit_clean = clean(credit or "")
    if credit_clean:
        fitted = min(credit_size, int(credit_size * limit / max(1.0, measure.at(credit_size).width(credit_clean))))
        events.append(
            f"Dialogue: 0,{ass_time(0)},{ass_time(duration_s)},Credit,,0,0,0,,"
            f"{{\\an2\\pos({centre_x},{round(y1 - 8)})\\fs{fitted}}}{credit_clean}"
        )
    return text + "\n".join(events) + "\n"
