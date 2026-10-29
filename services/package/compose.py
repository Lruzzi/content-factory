"""Put a clip's title, description and hashtags together into the text each platform gets (FR-7). Pure code.

The LLM supplies a title, a description and some hashtags; everything else is decided here: which hashtags are kept,
the source link, the disclosure line, and the cut to each platform's limit. The text it read is untrusted, so control
and bidirectional characters are dropped from everything that is shown."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

MAX_TAG_CHARS = 30


@dataclass(frozen=True)
class Limit:
    title: int
    text: int


# Characters each platform accepts, kept on the safe side. These come from memory of the platforms' documentation and
# are NOT verified: check each against the platform's current docs before its publisher is built.
LIMITS = {
    "shorts": Limit(title=100, text=5000),  # YouTube title and description
    "tiktok": Limit(title=100, text=2200),
    "reels": Limit(title=100, text=2200),  # Instagram caption
    "facebook": Limit(title=100, text=2200),
}
CAPTION_PLATFORMS = {"tiktok", "reels", "facebook"}  # one caption: the title is its first line


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=600)
    hashtags: list[str] = Field(max_length=10)


@dataclass(frozen=True)
class Packaged:
    title: str
    text: str
    hashtags: list[str]


def _clean(text: str) -> str:
    kept = " ".join(text.split())  # newlines and runs of spaces become one space
    return "".join(ch for ch in kept if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co")).strip()


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip() + "…"


def _tag(raw: str) -> str | None:
    words = raw.strip().lstrip("#").split()
    body = re.sub(r"\W", "", words[0]) if words else ""
    return f"#{body}" if body and len(body) <= MAX_TAG_CHARS else None


def _hashtags(fixed: list[str], generated: list[str], generated_max: int) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for source, room in ((fixed, len(fixed)), (generated, generated_max)):
        taken = 0
        for raw in source:
            tag = _tag(raw)
            if tag is None or tag.lower() in seen or taken >= room:
                continue
            seen.add(tag.lower())
            tags.append(tag)
            taken += 1
    return tags


def compose(
    *,
    title: str,
    description: str,
    generated: list[str],
    fixed: list[str],
    generated_max: int,
    source_link: str | None,
    ai_disclosure: bool,
    platform_fixed: dict[str, list[str]] | None = None,
) -> dict[str, Packaged]:
    """`platform_fixed` adds hashtags to one platform only (TikTok's "#fyp"), after the fixed ones all platforms get."""
    title_text, body = _clean(title), _clean(description)
    tail = [
        part
        for part in (
            f"Full stream: {source_link}" if source_link else "",
            "Made with AI assistance." if ai_disclosure else "",
        )
        if part
    ]
    out: dict[str, Packaged] = {}
    for platform, limit in LIMITS.items():
        tags = _hashtags([*fixed, *(platform_fixed or {}).get(platform, [])], generated, generated_max)
        tail_text = "\n\n".join([*tail, " ".join(tags)] if tags else tail)
        shown_title = _cut(title_text, limit.title)
        lead = shown_title if platform in CAPTION_PLATFORMS else ""
        room = limit.text - len(tail_text) - len(lead) - 4 * 2  # the blank lines between the parts
        parts = [lead, _cut(body, max(room, 0)), tail_text]
        out[platform] = Packaged(shown_title, "\n\n".join(p for p in parts if p), tags)
    return out
