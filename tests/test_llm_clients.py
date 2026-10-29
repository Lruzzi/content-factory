"""The two LLM clients, against fake servers: what they send, what they return, and how they fail."""

from __future__ import annotations

import pytest
from fake_llm_server import FakeServer, anthropic_reply, error_reply, fake_server, openai_reply

import config_schema as cs
from common.llm import LLMError, make_client

KEY = "test-key-0123456789"  # gitleaks:allow - a fake key for a fake server


@pytest.fixture
def server():
    with fake_server() as s:
        yield s


def openai_spec(server: FakeServer, **kw) -> cs.LLMSpec:
    options = {
        "provider": "openai_compatible",
        "model": "vendor/some-model",
        "base_url": f"{server.url}/v1",
        "api_key_env": "FAKE_PROVIDER_KEY",
        "max_retries": 0,
        "timeout_s": 5,
    } | kw
    return cs.LLMSpec(**options)


def anthropic_spec(**kw) -> cs.LLMSpec:
    return cs.LLMSpec(**({"provider": "anthropic", "model": "claude-haiku-5-5", "max_retries": 0, "timeout_s": 5} | kw))


def env(server: FakeServer, **extra) -> dict[str, str]:
    return {"FAKE_PROVIDER_KEY": KEY, "ANTHROPIC_API_KEY": KEY, "ANTHROPIC_BASE_URL": server.url} | extra


# ---------- OpenAI-compatible (OpenRouter, Groq, Gemini's endpoint, Ollama) -------------------------------------
def test_an_openai_compatible_call_goes_to_the_configured_endpoint_with_the_key_from_the_named_variable(server):
    server.reply(openai_reply('{"ok": true}'))
    make_client(openai_spec(server), env(server)).complete(system="be brief", prompt="hello", max_tokens=300)
    seen = server.seen[0]
    assert seen.path == "/v1/chat/completions"
    assert seen.headers["authorization"] == f"Bearer {KEY}"
    assert seen.body["model"] == "vendor/some-model"
    assert seen.body["messages"] == [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hello"}]
    assert seen.body["max_tokens"] == 300


def test_a_different_variable_name_in_config_selects_a_different_key(server):
    server.reply(openai_reply("hi"))
    spec = openai_spec(server, api_key_env="OPENROUTER_API_KEY")
    make_client(spec, {"OPENROUTER_API_KEY": "or-key-1", "FAKE_PROVIDER_KEY": "other"}).complete(
        system="s", prompt="p", max_tokens=10
    )
    assert server.seen[0].headers["authorization"] == "Bearer or-key-1"


def test_json_mode_is_only_sent_when_configured(server):
    server.reply(openai_reply("{}"), openai_reply("{}"))
    make_client(openai_spec(server), env(server)).complete(system="s", prompt="p", max_tokens=10)
    make_client(openai_spec(server, json_mode=True), env(server)).complete(system="s", prompt="p", max_tokens=10)
    assert "response_format" not in server.seen[0].body
    assert server.seen[1].body["response_format"] == {"type": "json_object"}


def test_the_reply_text_and_token_counts_come_back(server):
    server.reply(openai_reply("the answer", prompt_tokens=120, completion_tokens=34))
    result = make_client(openai_spec(server), env(server)).complete(system="s", prompt="p", max_tokens=10)
    assert (result.text, result.input_tokens, result.output_tokens, result.estimated) == ("the answer", 120, 34, False)


def test_tokens_are_estimated_and_flagged_when_the_provider_reports_none(server):
    server.reply(openai_reply("x" * 400, prompt_tokens=-1))
    result = make_client(openai_spec(server), env(server)).complete(system="s", prompt="p" * 800, max_tokens=500)
    assert result.estimated is True
    assert result.input_tokens > 0
    assert result.output_tokens > 0


def test_a_reply_cut_off_by_the_token_limit_is_flagged(server):
    server.reply(openai_reply('{"a": ', finish="length"))
    result = make_client(openai_spec(server), env(server)).complete(system="s", prompt="p", max_tokens=5)
    assert result.truncated is True


def test_an_empty_reply_is_an_error(server):
    server.reply(openai_reply(None))
    with pytest.raises(LLMError, match="empty"):
        make_client(openai_spec(server), env(server)).complete(system="s", prompt="p", max_tokens=10)


# ---------- Anthropic ------------------------------------------------------------------------------------------
def test_an_anthropic_call_sends_the_key_model_system_and_prompt(server):
    server.reply(anthropic_reply('{"ok": true}'))
    make_client(anthropic_spec(), env(server)).complete(system="be brief", prompt="hello", max_tokens=300)
    seen = server.seen[0]
    assert seen.path == "/v1/messages"
    assert seen.headers["authorization"] == f"Bearer {KEY}"  # the 2.x SDK sends the key this way
    assert seen.body["model"] == "claude-haiku-5-5"
    assert seen.body["system"] == "be brief"
    assert seen.body["messages"] == [{"role": "user", "content": "hello"}]
    assert seen.body["max_tokens"] == 300


def test_effort_is_sent_only_when_configured(server):
    server.reply(anthropic_reply("a"), anthropic_reply("b"))
    make_client(anthropic_spec(), env(server)).complete(system="s", prompt="p", max_tokens=10)
    make_client(anthropic_spec(effort="low"), env(server)).complete(system="s", prompt="p", max_tokens=10)
    assert "output_config" not in server.seen[0].body
    assert server.seen[1].body["output_config"] == {"effort": "low"}


def test_anthropic_text_blocks_are_joined_and_thinking_blocks_ignored(server):
    server.reply(
        anthropic_reply("the answer", 99, 7, extra_blocks=[{"type": "thinking", "thinking": "", "signature": "x"}])
    )
    result = make_client(anthropic_spec(), env(server)).complete(system="s", prompt="p", max_tokens=10)
    assert (result.text, result.input_tokens, result.output_tokens) == ("the answer", 99, 7)


def test_an_anthropic_refusal_is_an_error_that_is_not_retried(server):
    server.reply(anthropic_reply("", stop_reason="refusal"))
    with pytest.raises(LLMError, match="declined") as caught:
        make_client(anthropic_spec(), env(server)).complete(system="s", prompt="p", max_tokens=10)
    assert caught.value.retryable is False


def test_an_anthropic_reply_cut_off_by_max_tokens_is_flagged(server):
    server.reply(anthropic_reply('{"a": ', stop_reason="max_tokens"))
    assert make_client(anthropic_spec(), env(server)).complete(system="s", prompt="p", max_tokens=5).truncated is True


# ---------- failures, for both ----------------------------------------------------------------------------------
@pytest.fixture(params=["openai_compatible", "anthropic"])
def either(request, server):
    """(spec, reply-factory-for-success) for each provider, so failure behaviour is checked for both."""
    if request.param == "anthropic":
        return anthropic_spec, anthropic_reply
    return (lambda **kw: openai_spec(server, **kw)), openai_reply


def call(spec, server):
    return make_client(spec, env(server)).complete(system="s", prompt="p", max_tokens=10)


def test_a_rejected_key_is_an_error_that_names_the_variable_not_the_key_and_is_not_retried(server, either):
    make_spec, _ = either
    server.reply(error_reply(401, "invalid api key"))
    spec = make_spec()
    with pytest.raises(LLMError) as caught:
        call(spec, server)
    assert caught.value.retryable is False
    assert spec.api_key_env in str(caught.value)
    assert KEY not in str(caught.value)
    assert KEY not in repr(caught.value)


def test_a_bad_request_is_not_retryable(server, either):
    make_spec, _ = either
    server.reply(error_reply(400, "bad model"))
    with pytest.raises(LLMError) as caught:
        call(make_spec(), server)
    assert caught.value.retryable is False
    assert "bad model" in str(caught.value)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_rate_limits_and_server_errors_are_retryable_once_the_sdk_gives_up(server, either, status):
    make_spec, _ = either
    server.reply(error_reply(status, "busy"))
    with pytest.raises(LLMError) as caught:
        call(make_spec(), server)
    assert caught.value.retryable is True


def test_a_rate_limit_followed_by_success_is_retried_by_the_sdk(server, either):
    make_spec, ok = either
    server.reply(error_reply(429, "slow down", retry_after="0"), ok("fine"))
    assert call(make_spec(max_retries=1), server).text == "fine"
    assert len(server.seen) == 2


def test_a_slow_provider_times_out_as_a_retryable_error(server, either):
    make_spec, ok = either
    slow = ok("late")
    slow.delay_s = 1.5
    server.reply(slow)
    with pytest.raises(LLMError) as caught:
        call(make_spec(timeout_s=0.3), server)
    assert caught.value.retryable is True


def test_a_provider_that_is_down_is_a_retryable_error():
    with fake_server() as dead:
        url = dead.url
    spec = cs.LLMSpec(
        provider="openai_compatible",
        model="m",
        base_url=f"{url}/v1",
        api_key_env="FAKE_PROVIDER_KEY",
        max_retries=0,
        timeout_s=2,
    )
    with pytest.raises(LLMError) as caught:
        make_client(spec, {"FAKE_PROVIDER_KEY": KEY}).complete(system="s", prompt="p", max_tokens=10)
    assert caught.value.retryable is True


def test_a_missing_key_variable_is_a_clear_error_before_any_request(server, either):
    make_spec, _ = either
    spec = make_spec()
    with pytest.raises(LLMError, match=spec.api_key_env) as caught:
        make_client(spec, {"ANTHROPIC_BASE_URL": server.url})
    assert caught.value.retryable is False
    assert server.seen == []


def test_the_key_does_not_appear_in_the_clients_repr(server, either):
    make_spec, _ = either
    assert KEY not in repr(make_client(make_spec(), env(server)))
