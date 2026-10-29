"""Any API that speaks the OpenAI chat format: OpenRouter, Groq, Gemini's compatible endpoint, Ollama and others.

Uses the `openai` SDK pointed at the configured `base_url`. Providers differ in what they support, so only the
plainest request is sent: model, messages and max_tokens, plus `response_format` when `json_mode` is on.
"""

from __future__ import annotations

from typing import Any

import openai

import config_schema as cs
from common.llm.base import LLMError, LLMResult, retry_after_seconds

CHARS_PER_TOKEN = 3  # a cautious guess for Indonesian text, used only when the provider reports no usage


class OpenAICompatibleClient:
    def __init__(self, spec: cs.LLMSpec, api_key: str) -> None:
        self._spec = spec
        self._client = openai.OpenAI(
            api_key=api_key, base_url=spec.base_url, timeout=spec.timeout_s, max_retries=spec.max_retries
        )

    def __repr__(self) -> str:  # never shows the key
        return f"OpenAICompatibleClient(model={self._spec.model!r}, base_url={self._spec.base_url!r})"

    def complete(self, *, system: str, prompt: str, max_tokens: int) -> LLMResult:
        extra: dict[str, Any] = {"response_format": {"type": "json_object"}} if self._spec.json_mode else {}
        name = self._spec.base_url
        try:
            response = self._client.chat.completions.create(
                model=self._spec.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                **extra,
            )
        except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
            raise LLMError(
                f"{name} rejected the key in {self._spec.api_key_env}: {exc.message}", retryable=False
            ) from exc
        except openai.APITimeoutError as exc:
            raise LLMError(f"{name} did not answer within {self._spec.timeout_s:g} s", retryable=True) from exc
        except openai.APIConnectionError as exc:
            raise LLMError(f"could not reach {name} (network error)", retryable=True) from exc
        except openai.APIStatusError as exc:
            limited = exc.status_code == 429
            raise LLMError(
                f"{name} returned {exc.status_code}: {exc.message}",
                retryable=limited or exc.status_code >= 500,
                rate_limited=limited,
                retry_after_s=retry_after_seconds(str(exc), exc.response.headers.get("retry-after"))
                if limited
                else None,
            ) from exc
        choice = response.choices[0] if response.choices else None
        text = (choice.message.content if choice else None) or ""
        if not text.strip():
            raise LLMError(f"{name} returned an empty reply", retryable=True)
        usage = response.usage
        if usage is None:
            input_tokens = (len(system) + len(prompt)) // CHARS_PER_TOKEN + 1
            output_tokens = len(text) // CHARS_PER_TOKEN + 1
        else:
            input_tokens, output_tokens = usage.prompt_tokens, usage.completion_tokens
        return LLMResult(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=response.model or self._spec.model,
            truncated=choice is not None and choice.finish_reason == "length",
            estimated=usage is None,
            price_key=cs.llm_price_key(self._spec),
        )
