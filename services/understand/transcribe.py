"""Groq transcription and the checked transcript it produces. Whatever comes back is untrusted: it is parsed into
a strict shape, with limits, before anything stores or uses it."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import openai
from pydantic import BaseModel, ConfigDict, Field, ValidationError

DEFAULT_WAIT_S = 300
GROQ_URL = "https://api.groq.com/openai/v1"
OPENROUTER_URL = "https://openrouter.ai/api/v1"
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
LOOP = re.compile(r"(.{1,4}?)\1{4,}")  # a speech model stuck in a loop: "SSSSSSSS...", "kusususus..."
OVERSHOOT_S = 15  # Whisper's last timestamps run a few seconds past the end of the audio; far more is broken
WORD_MAX_CHARS = 40  # no real word is longer; what is, after the loops are shortened, is garbage and is left out
MAX_WORDS, MAX_SENTENCES = 20_000, 5_000


class TranscribeUnavailable(Exception):
    """The fallback engine could not answer (credit used up, key refused, outage): the caller waits for Groq instead."""


class RateLimited(Exception):
    """Groq refused because a limit is reached. `wait_s` is how long it asked us to wait."""

    def __init__(self, wait_s: float, reason: str) -> None:
        super().__init__(reason)
        self.wait_s, self.reason = wait_s, reason


class Span(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    text: str = Field(min_length=1, max_length=120)
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)


class Sentence(Span):
    text: str = Field(min_length=1, max_length=2000)


class Transcript(BaseModel):
    model_config = ConfigDict(extra="ignore")
    language: str = Field(max_length=40)
    duration_s: float = Field(ge=0)
    text: str = Field(max_length=200_000)
    sentences: list[Sentence] = Field(max_length=MAX_SENTENCES)
    words: list[Span] = Field(max_length=MAX_WORDS)
    cost_usd: float = Field(0, ge=0)  # what the call cost; Groq's free tier is 0

    def without_phrases(self, phrases: list[str]) -> Transcript:
        """The transcript without the sentences that begin with one of these phrases (compared as lower case letters and
        digits only) and the words spoken inside them. Whisper makes up lines such as "Terima kasih telah menonton"
        from silence and game sound; they are not speech, and would show as captions."""
        wanted = [_squash(p) for p in phrases if _squash(p)]
        if not wanted:
            return self
        gone = [s for s in self.sentences if any(_squash(s.text).startswith(p) for p in wanted)]
        if not gone:
            return self
        kept = [s for s in self.sentences if s not in gone]
        words = [
            w for w in self.words if not any(g.start_s - 0.01 <= w.start_s and w.end_s <= g.end_s + 0.01 for g in gone)
        ]
        return self.model_copy(update={"sentences": kept, "words": words, "text": " ".join(s.text for s in kept)})

    def shifted(self, offset_s: float) -> Transcript:
        """The same transcript with every time moved by `offset_s` (audio clock to stream clock)."""

        def move(spans: list[Any]) -> list[Any]:
            return [s.model_copy(update={"start_s": s.start_s + offset_s, "end_s": s.end_s + offset_s}) for s in spans]

        return self.model_copy(update={"sentences": move(self.sentences), "words": move(self.words)})


def _squash(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())


def _clean(text: Any) -> str:
    if not isinstance(text, str):
        raise ValueError("text is not a string")
    return LOOP.sub(r"\1\1\1", CONTROL.sub("", text)).strip()


def _spans(items: Any, key: str, duration: float, cls: type[Span]) -> list[Span]:
    if not isinstance(items, list):
        raise ValueError(f"{key} is not a list")
    out = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"{key} holds something that is not an object")
        text = _clean(item.get("word" if key == "words" else "text"))
        if not text or (key == "words" and len(text) > WORD_MAX_CHARS):
            continue
        span = cls(text=text, start_s=item.get("start", -1), end_s=item.get("end", -1))
        if span.end_s < span.start_s or span.end_s > duration + OVERSHOOT_S:
            raise ValueError(f"{key} time out of range: {span.start_s}-{span.end_s} of {duration}")
        if span.start_s >= duration:  # it begins after the audio ended: not speech
            continue
        out.append(span.model_copy(update={"end_s": min(span.end_s, duration)}))
    return out


def parse(reply: dict[str, Any]) -> Transcript:
    """Check one `verbose_json` reply. Raises ValueError when it is not a usable transcript."""
    try:
        duration = float(reply.get("duration", -1))
        if duration < 0:
            raise ValueError("duration is missing or negative")
        return Transcript(
            language=str(reply.get("language") or "unknown"),
            duration_s=duration,
            text=_clean(reply.get("text", "")),
            sentences=_spans(reply.get("segments", []), "segments", duration, Sentence),
            words=_spans(reply.get("words", []), "words", duration, Span),
        )
    except ValidationError as exc:
        raise ValueError(f"unusable transcript: {exc.errors()[0]['msg']} at {exc.errors()[0]['loc']}") from exc


class GroqTranscriber:
    """Sends one audio file to Groq's Whisper and returns the checked transcript (times on the audio's own clock)."""

    def __init__(self, api_key: str, *, base_url: str = GROQ_URL) -> None:
        self.client = openai.OpenAI(api_key=api_key, base_url=base_url, max_retries=0, timeout=180)

    def __call__(self, path: Path, *, model: str, language: str, prompt: str = "") -> Transcript:
        extra: dict[str, Any] = {"prompt": prompt} if prompt else {}
        try:
            with path.open("rb") as audio:
                reply = self.client.audio.transcriptions.create(
                    model=model,
                    file=(path.name, audio),
                    response_format="verbose_json",
                    timestamp_granularities=["word", "segment"],
                    language=language,
                    temperature=0,
                    **extra,
                )
        except openai.RateLimitError as exc:
            try:
                wait = float(exc.response.headers.get("retry-after", ""))
            except ValueError:
                wait = DEFAULT_WAIT_S
            raise RateLimited(wait, "the answer was a 429") from None
        return parse(reply.model_dump())


class OpenRouterTranscriber:
    """OpenRouter's Whisper, for when Groq is full. OpenRouter picks the provider itself (it ignores `order`, `only` and
    `ignore` for transcription), so the plain `prompt` field is ignored and the vocabulary prompt only reaches a
    provider that takes it through `provider.options`, as Groq does. The real cost comes back in `usage.cost`."""

    def __init__(self, api_key: str, *, base_url: str = OPENROUTER_URL) -> None:
        self.client = openai.OpenAI(api_key=api_key, base_url=base_url, max_retries=0, timeout=180)

    def __call__(self, path: Path, *, model: str, language: str, prompt: str = "") -> Transcript:
        extra: dict[str, Any] = {}
        if prompt:
            extra["extra_body"] = {"provider": {"options": {"groq": {"prompt": prompt}}}}
        try:
            with path.open("rb") as audio:
                reply = self.client.audio.transcriptions.create(
                    model=model,
                    file=(path.name, audio),
                    response_format="verbose_json",
                    timestamp_granularities=["word", "segment"],
                    language=language,
                    temperature=0,
                    **extra,
                )
        except openai.RateLimitError as exc:
            try:
                wait = float(exc.response.headers.get("retry-after", ""))
            except ValueError:
                wait = DEFAULT_WAIT_S
            raise RateLimited(wait, "OpenRouter answered 429") from None
        except openai.APIStatusError as exc:
            raise TranscribeUnavailable(f"OpenRouter returned {exc.status_code}") from None
        except openai.APIConnectionError:
            raise TranscribeUnavailable("could not reach OpenRouter") from None
        data = reply.model_dump()
        result = parse(data)
        cost = (data.get("usage") or {}).get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
            result = result.model_copy(update={"cost_usd": float(cost)})
        return result
