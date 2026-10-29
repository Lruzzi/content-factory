"""Bad words in a clip: the audio is muted while a listed word is spoken, and the captions show it as stars. Words come
from the transcript with their times, so it only catches what Whisper wrote down (and only about as exactly as its word
times go: the mute is padded). Pure code: the caller gives the words, the rules and the clip's start."""

from __future__ import annotations

import re
from collections.abc import Sequence

import config_schema as cs
from services.render.captions import Word

MERGE_GAP_S = 0.15  # two mutes this close are one


def _key(text: str) -> str:
    """A word as compared: letters only, lower case, a run of one letter made one ("jancoook" is "jancok")."""
    return re.sub(r"(.)\1+", r"\1", re.sub(r"[^a-z]", "", text.lower()))


def is_bad(text: str, rules: cs.Profanity) -> bool:
    key = _key(text)
    return bool(key) and key in {_key(t) for t in rules.terms}


def mute_ranges(
    words: Sequence[Word], rules: cs.Profanity, *, clip_start_s: float, clip_s: float
) -> list[tuple[float, float]]:
    """Where in the clip (seconds from its start) the sound goes off, merged and kept inside the clip."""
    if not rules.mute or not rules.terms:
        return []
    spans = sorted(
        (
            max(0.0, w.start_s - clip_start_s - rules.pad_before_s),
            min(clip_s, w.end_s - clip_start_s + rules.pad_after_s),
        )
        for w in words
        if is_bad(w.text, rules)
    )
    merged: list[list[float]] = []
    for a, b in spans:
        if b <= a:
            continue
        if merged and a - merged[-1][1] <= MERGE_GAP_S:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def mask(words: Sequence[Word], rules: cs.Profanity) -> list[Word]:
    """The words with each bad one as its first letter and stars ("K*****"), for the captions."""
    if not rules.mask_captions or not rules.terms:
        return list(words)
    out = []
    for w in words:
        if is_bad(w.text, rules):
            letters = [i for i, c in enumerate(w.text) if c.isalpha()]
            text = "".join(c if i not in letters[1:] else "*" for i, c in enumerate(w.text))
            out.append(Word(text, w.start_s, w.end_s))
        else:
            out.append(w)
    return out
