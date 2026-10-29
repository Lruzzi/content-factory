"""A provider that has run out of quota is told apart from one that is broken, and says when it will be back."""

from __future__ import annotations

import pytest
from fake_llm_server import error_reply, fake_server

import config_schema as cs
from common.llm import LLMError, LLMResult, make_client
from common.llm.base import retry_after_seconds
from common.llm.fallback import FallbackClient, Link

GEMINI_MESSAGE = (
    "You exceeded your current quota. Generate content requests per day limit exceeded on Free Tier.\n"
    "Please retry in 13h23m32.345004143s."
)


@pytest.mark.parametrize(
    ("text", "header", "expected"),
    [
        (GEMINI_MESSAGE, None, 13 * 3600 + 23 * 60 + 32.345),
        ("Please retry in 45.5s.", None, 45.5),
        ("Please retry in 2m10s", None, 130.0),
        ("{'retryDelay': '48212s'}", None, 48212.0),
        ("no hint here", "120", 120.0),
        ("Please retry in 10s", "120", 120.0),  # the header is the provider's own word
        ("no hint here", None, None),
        ("no hint here", "not a number", None),
        ("Please retry in -5s", None, None),
    ],
)
def test_how_long_to_wait_is_read_from_the_header_or_the_message(text, header, expected):
    got = retry_after_seconds(text, header)
    assert got == pytest.approx(expected) if expected is not None else got is None


def test_an_error_is_not_rate_limited_unless_it_says_so():
    assert LLMError("boom").rate_limited is False and LLMError("boom").retry_after_s is None
    err = LLMError("slow down", retryable=True, rate_limited=True, retry_after_s=30.0)
    assert err.rate_limited and err.retry_after_s == 30.0


def spec(url: str) -> cs.LLMJudge:
    return cs.LLMJudge(
        provider="openai_compatible", model="m", base_url=f"{url}/v1", api_key_env="TEST_KEY", max_retries=0, prompt="p"
    )


@pytest.mark.parametrize("status", [429])
def test_a_429_from_an_openai_compatible_provider_is_rate_limited_with_its_retry_hint(status):
    with fake_server() as server:
        server.reply(error_reply(status, GEMINI_MESSAGE, **{"Retry-After": "90"}))
        client = make_client(spec(server.url), {"TEST_KEY": "test-key-0123456789"})  # gitleaks:allow
        with pytest.raises(LLMError) as caught:
            client.complete(system="s", prompt="p", max_tokens=10)
    assert caught.value.rate_limited and caught.value.retry_after_s == 90.0 and caught.value.retryable


@pytest.mark.parametrize("status", [401, 500, 503])
def test_other_failures_are_not_rate_limits(status):
    with fake_server() as server:
        server.reply(error_reply(status, "nope"))
        client = make_client(spec(server.url), {"TEST_KEY": "test-key-0123456789"})  # gitleaks:allow
        with pytest.raises(LLMError) as caught:
            client.complete(system="s", prompt="p", max_tokens=10)
    assert not caught.value.rate_limited


class Raises:
    def __init__(self, error: LLMError):
        self.error = error

    def complete(self, *, system: str, prompt: str, max_tokens: int) -> LLMResult:
        raise self.error


def chain(*errors: LLMError) -> FallbackClient:
    links = [Link(f"p{i}", lambda e=e: Raises(e)) for i, e in enumerate(errors)]  # type: ignore[misc]
    return FallbackClient(links, cooldown_s=60.0)


def ask(client: FallbackClient) -> None:
    client.complete(system="s", prompt="p", max_tokens=10)


def limited(after: float | None) -> LLMError:
    return LLMError("quota", retryable=True, rate_limited=True, retry_after_s=after)


def test_when_every_provider_is_out_of_quota_the_chain_says_so_and_how_soon_the_first_is_back():
    with pytest.raises(LLMError, match="every LLM provider failed") as caught:
        ask(chain(limited(48000.0), limited(1800.0)))
    assert caught.value.rate_limited and caught.value.retry_after_s == 1800.0


def test_a_provider_that_gave_no_hint_does_not_hide_the_one_that_did():
    with pytest.raises(LLMError) as caught:
        ask(chain(limited(None), limited(600.0)))
    assert caught.value.rate_limited and caught.value.retry_after_s == 600.0


def test_one_broken_provider_among_the_limited_ones_means_it_is_not_just_a_quota_problem():
    with pytest.raises(LLMError) as caught:
        ask(chain(limited(100.0), LLMError("bad key", retryable=False)))
    assert not caught.value.rate_limited
