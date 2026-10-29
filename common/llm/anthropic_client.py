"""Claude, through the official Anthropic SDK."""

from __future__ import annotations

from typing import Any

import anthropic

import config_schema as cs
from common.llm.base import LLMError, LLMResult, retry_after_seconds


class AnthropicClient:
    def __init__(self, spec: cs.LLMSpec, api_key: str, base_url: str | None = None) -> None:
        self._spec = spec
        self._client = anthropic.Anthropic(
            api_key=api_key, base_url=base_url, timeout=spec.timeout_s, max_retries=spec.max_retries
        )

    def __repr__(self) -> str:  # never shows the key
        return f"AnthropicClient(model={self._spec.model!r})"

    def complete(self, *, system: str, prompt: str, max_tokens: int) -> LLMResult:
        extra: dict[str, Any] = {"output_config": {"effort": self._spec.effort}} if self._spec.effort else {}
        try:
            message = self._client.messages.create(
                model=self._spec.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                **extra,
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise LLMError(
                f"Anthropic rejected the key in {self._spec.api_key_env}: {exc.message}", retryable=False
            ) from exc
        except anthropic.APITimeoutError as exc:
            raise LLMError(f"Anthropic did not answer within {self._spec.timeout_s:g} s", retryable=True) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("could not reach Anthropic (network error)", retryable=True) from exc
        except anthropic.APIStatusError as exc:
            limited = exc.status_code == 429
            raise LLMError(
                f"Anthropic returned {exc.status_code}: {exc.message}",
                retryable=limited or exc.status_code >= 500,
                rate_limited=limited,
                retry_after_s=retry_after_seconds(str(exc), exc.response.headers.get("retry-after"))
                if limited
                else None,
            ) from exc
        if message.stop_reason == "refusal":
            raise LLMError("the model declined this request (refusal)", retryable=False)
        text = "".join(block.text for block in message.content if block.type == "text")
        return LLMResult(
            text=text,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            model=message.model,
            truncated=message.stop_reason == "max_tokens",
            price_key=cs.llm_price_key(self._spec),
        )
