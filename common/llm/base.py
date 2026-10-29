"""What every LLM client looks like, and how its output is read.

LLM output is untrusted (CLAUDE.md): `complete_json` parses it and checks it against a pydantic schema before anything
uses it. A bad answer is retried once with the problem spelled out, then it is an error. The error never contains the
reply itself, so a hostile reply cannot be echoed into logs or alerts.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ValidationError

MAX_PROBLEM_CHARS = 300
MAX_FIELD_ERRORS = 5


class LLMError(RuntimeError):
    """A call to the provider failed. `retryable` says whether the same call may succeed later. `rate_limited` means the
    provider is out of quota or going too fast (a 429): not broken, just not available until `retry_after_s` has passed
    (None when the provider did not say)."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        rate_limited: bool = False,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.rate_limited = rate_limited
        self.retry_after_s = retry_after_s


_RETRY_IN = re.compile(r"retry in\s+(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:([\d.]+)s)?", re.IGNORECASE)
_RETRY_DELAY = re.compile(r"retryDelay['\"]?\s*:\s*['\"]([\d.]+)s")


def retry_after_seconds(text: str, header: str | None = None) -> float | None:
    """How long a provider says to wait, from its `Retry-After` header or a hint in its message ("Please retry in
    13h23m32s", Gemini's `retryDelay`). None when there is no usable hint."""
    if header:
        try:
            seconds = float(header)
        except ValueError:
            seconds = -1.0
        if seconds >= 0:
            return seconds
    match = _RETRY_IN.search(text)
    if match and any(match.groups()):
        hours, minutes, seconds = (float(g) if g else 0.0 for g in match.groups())
        return hours * 3600 + minutes * 60 + seconds
    delay = _RETRY_DELAY.search(text)
    return float(delay.group(1)) if delay else None


class LLMOutputError(LLMError):
    """The model answered, but not in the shape asked for, even after a retry."""


@dataclass(frozen=True)
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    truncated: bool = False  # the reply was cut off by the token limit
    estimated: bool = False  # the provider reported no usage, so the token counts are guesses
    price_key: str | None = None  # `<provider>/<model>` of the model that answered, for pricing the call


class LLMClient(Protocol):
    def complete(self, *, system: str, prompt: str, max_tokens: int) -> LLMResult: ...


_THINK = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```[a-zA-Z0-9_-]*\s*\n?(.*?)```", re.DOTALL)


def extract_json(text: str) -> object:
    """The first JSON value in a reply, whatever surrounds it: reasoning tags, code fences, chatter."""
    text = _THINK.sub("", text)
    fenced = _FENCE.findall(text)
    decoder = json.JSONDecoder()
    for candidate in [*fenced, text]:
        for match in re.finditer(r"[{\[]", candidate):
            try:
                value, _ = decoder.raw_decode(candidate[match.start() :])
            except ValueError:
                continue
            return value
    raise ValueError("no JSON found")


def _problem(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = [
            f"{'.'.join(str(p) for p in err['loc']) or 'reply'}: {err['msg']}"[:80]
            for err in exc.errors()[:MAX_FIELD_ERRORS]
        ]
        return "it did not match the schema (" + "; ".join(parts) + ")"
    return "it was not valid JSON"


def complete_json[T: BaseModel](
    client: LLMClient, schema: type[T], *, system: str, prompt: str, max_tokens: int, retries: int = 1
) -> tuple[T, list[LLMResult]]:
    """Ask for a JSON answer and validate it. Returns the parsed object and the usage of every call made."""
    results: list[LLMResult] = []
    current, problem = prompt, ""
    for _ in range(retries + 1):
        result = client.complete(system=system, prompt=current, max_tokens=max_tokens)
        results.append(result)
        try:
            return schema.model_validate(extract_json(result.text)), results
        except (ValueError, ValidationError) as exc:  # pydantic's ValidationError is a ValueError too
            problem = _problem(exc)[:MAX_PROBLEM_CHARS]
            cut_off = result.truncated
        current = (
            f"{prompt}\n\nYour previous reply was rejected: {problem}. "
            "Reply again with only the JSON object, and no other text."
        )
    hint = " The reply was cut off at the token limit; allow more output tokens." if cut_off else ""
    raise LLMOutputError(f"the model's reply failed validation after {len(results)} attempt(s): {problem}.{hint}")
