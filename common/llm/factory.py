"""Build the client a profile asks for. The only place that reads an API key."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from functools import partial

import config_schema as cs
from common.llm.anthropic_client import AnthropicClient
from common.llm.base import LLMClient, LLMError
from common.llm.fallback import FallbackClient, Link
from common.llm.openai_compatible import OpenAICompatibleClient

DEFAULT_COOLDOWN_S = 60.0


def _single(spec: cs.LLMSpec, environ: Mapping[str, str]) -> LLMClient:
    key = environ.get(spec.api_key_env or "")
    if not key:
        raise LLMError(
            f"environment variable {spec.api_key_env} is not set (it should hold the {spec.provider} API key)",
            retryable=False,
        )
    if spec.provider == "anthropic":
        return AnthropicClient(spec, key, environ.get("ANTHROPIC_BASE_URL"))
    return OpenAICompatibleClient(spec, key)


def make_client(
    spec: cs.LLMSpec,
    environ: Mapping[str, str] = os.environ,
    *,
    notify: Callable[[str], object] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> LLMClient:
    """The client for a profile's LLM. Keys come from the environment variables named in config, and nowhere else
    (ANTHROPIC_BASE_URL, when set, redirects Claude calls: a proxy, or a test server).

    A judge with `fallbacks` gets a chain: the primary is built now (a missing key is an immediate error), the backups
    only when they are needed. `notify` is called when a backup takes over."""
    primary = _single(spec, environ)
    fallbacks: list[cs.LLMSpec] = list(getattr(spec, "fallbacks", []))
    if not fallbacks:
        return primary
    links = [Link(spec.model, lambda: primary)] + [Link(f.model, partial(_single, f, environ)) for f in fallbacks]
    return FallbackClient(links, cooldown_s=getattr(spec, "cooldown_s", DEFAULT_COOLDOWN_S), notify=notify, clock=clock)
