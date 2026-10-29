"""A chain of LLMs: the first that answers wins, a failing one is skipped for a while, and the switch is announced."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fake_llm_server import anthropic_reply, error_reply, fake_server, openai_reply

import config_schema as cs
from common.llm import LLMError, LLMResult, make_client
from common.llm.fallback import FallbackClient, Link


class Scripted:
    """A client that returns or raises what it is told, in turn, and counts its calls."""

    def __init__(self, *outcomes: LLMResult | LLMError):
        self.outcomes, self.calls = list(outcomes), 0

    def complete(self, *, system: str, prompt: str, max_tokens: int) -> LLMResult:
        self.calls += 1
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, LLMError):
            raise outcome
        return outcome


def ok(text="fine", model="m", key=None) -> LLMResult:
    return LLMResult(text, 10, 5, model, price_key=key)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _builder(client: Scripted) -> Callable[[], Scripted]:
    return lambda: client


def chain(*clients, cooldown_s=60.0, notify=None, clock=None):
    links = [Link(f"provider{i}", _builder(c)) for i, c in enumerate(clients)]
    return FallbackClient(links, cooldown_s=cooldown_s, notify=notify, clock=clock or Clock())


def ask(client):
    return client.complete(system="s", prompt="p", max_tokens=10)


# ---------- choosing who answers -----
def test_the_first_provider_answers_and_the_backup_is_never_called():
    primary, backup = Scripted(ok("primary")), Scripted(ok("backup"))
    assert ask(chain(primary, backup)).text == "primary"
    assert backup.calls == 0


@pytest.mark.parametrize("retryable", [True, False])
def test_a_failing_provider_hands_over_to_the_next_whatever_the_reason(retryable):
    primary, backup = Scripted(LLMError("busy", retryable=retryable)), Scripted(ok("backup"))
    assert ask(chain(primary, backup)).text == "backup"


def test_the_backup_is_tried_in_order_and_a_third_can_take_over():
    first, second, third = Scripted(LLMError("a")), Scripted(LLMError("b")), Scripted(ok("third"))
    assert ask(chain(first, second, third)).text == "third"


def test_when_every_provider_fails_the_error_lists_each_failure_and_is_retryable_if_any_was():
    client = chain(Scripted(LLMError("rate limited", retryable=True)), Scripted(LLMError("bad key", retryable=False)))
    with pytest.raises(LLMError) as caught:
        ask(client)
    text = str(caught.value)
    assert "provider0" in text
    assert "rate limited" in text
    assert "provider1" in text
    assert "bad key" in text
    assert caught.value.retryable is True


def test_all_failures_that_cannot_succeed_later_make_a_non_retryable_error():
    with pytest.raises(LLMError) as caught:
        ask(chain(Scripted(LLMError("bad key")), Scripted(LLMError("bad model"))))
    assert caught.value.retryable is False


def test_the_result_keeps_the_identity_of_the_model_that_answered():
    result = ask(chain(Scripted(LLMError("down")), Scripted(ok("x", model="gemma", key="openai_compatible/gemma"))))
    assert (result.model, result.price_key) == ("gemma", "openai_compatible/gemma")


# ---------- not hammering a provider that is down -----
def test_a_provider_that_just_failed_is_skipped_until_its_cooldown_ends():
    clock = Clock()
    primary, backup = Scripted(LLMError("busy", retryable=True), ok("primary")), Scripted(ok("backup"))
    client = chain(primary, backup, cooldown_s=60, clock=clock)
    assert ask(client).text == "backup"  # primary fails, backup answers
    clock.now += 30
    assert ask(client).text == "backup"
    assert primary.calls == 1  # not asked again during the cooldown
    clock.now += 31
    assert ask(client).text == "primary"  # cooldown over: tried first again
    assert primary.calls == 2


def test_when_everyone_is_cooling_down_they_are_all_tried_anyway_primary_first():
    clock = Clock()
    primary, backup = Scripted(LLMError("busy", retryable=True)), Scripted(LLMError("busy", retryable=True))
    client = chain(primary, backup, clock=clock)
    with pytest.raises(LLMError):
        ask(client)
    with pytest.raises(LLMError):
        ask(client)
    assert (primary.calls, backup.calls) == (2, 2)


def test_a_successful_answer_clears_the_providers_failure_mark():
    clock = Clock()
    primary, backup = Scripted(LLMError("busy", retryable=True), ok("primary")), Scripted(ok("backup"))
    client = chain(primary, backup, cooldown_s=10, clock=clock)
    ask(client)
    clock.now += 11
    assert ask(client).text == "primary"
    assert ask(client).text == "primary"
    assert backup.calls == 1


# ---------- telling the operator -----
def test_a_switch_is_announced_with_who_failed_why_and_who_took_over():
    notes: list[str] = []
    ask(chain(Scripted(LLMError("429 slow down", retryable=True)), Scripted(ok()), notify=notes.append))
    assert len(notes) == 1
    assert "provider0" in notes[0]
    assert "429 slow down" in notes[0]
    assert "provider1" in notes[0]


def test_no_announcement_when_the_primary_answers():
    notes: list[str] = []
    ask(chain(Scripted(ok()), Scripted(ok()), notify=notes.append))
    assert notes == []


def test_a_notify_function_that_breaks_never_breaks_the_call():
    def broken(_: str) -> None:
        raise RuntimeError("telegram is down")

    assert ask(chain(Scripted(LLMError("busy")), Scripted(ok("backup")), notify=broken)).text == "backup"


# ---------- building clients lazily -----
def test_a_backup_is_only_built_when_it_is_needed():
    built: list[str] = []

    def builder(name, client):
        def build():
            built.append(name)
            return client

        return build

    client = FallbackClient(
        [Link("p", builder("p", Scripted(ok()))), Link("b", builder("b", Scripted(ok())))], cooldown_s=60, clock=Clock()
    )
    ask(client)
    assert built == ["p"]


def test_a_backup_that_cannot_be_built_is_reported_as_its_failure_and_does_not_hide_the_primarys():
    def missing_key():
        raise LLMError("environment variable BACKUP_KEY is not set")

    client = FallbackClient(
        [Link("p", lambda: Scripted(LLMError("busy", retryable=True))), Link("b", missing_key)],
        cooldown_s=60,
        clock=Clock(),
    )
    with pytest.raises(LLMError) as caught:
        ask(client)
    assert "busy" in str(caught.value)
    assert "BACKUP_KEY" in str(caught.value)


def test_a_chain_needs_at_least_one_link():
    with pytest.raises(ValueError, match="at least one"):
        FallbackClient([], cooldown_s=60, clock=Clock())


# ---------- from config, against fake servers -----
def spec_pair(primary_url: str, backup_url: str) -> cs.LLMJudge:
    return cs.LLMJudge(
        provider="openai_compatible",
        model="primary-model",
        base_url=f"{primary_url}/v1",
        api_key_env="PRIMARY_KEY",
        max_retries=0,
        timeout_s=5,
        prompt="p",
        fallbacks=[
            cs.LLMSpec(
                provider="openai_compatible",
                model="backup-model",
                base_url=f"{backup_url}/v1",
                api_key_env="BACKUP_KEY",
                max_retries=0,
                timeout_s=5,
            )
        ],
    )


def test_make_client_builds_the_chain_from_a_judges_fallbacks():
    with fake_server() as primary, fake_server() as backup:
        primary.reply(error_reply(429, "free quota used up"))
        backup.reply(openai_reply("from the backup"))
        notes: list[str] = []
        client = make_client(
            spec_pair(primary.url, backup.url), {"PRIMARY_KEY": "k1", "BACKUP_KEY": "k2"}, notify=notes.append
        )
        result = ask(client)
    assert result.text == "from the backup"
    assert (len(primary.seen), len(backup.seen)) == (1, 1)
    assert primary.seen[0].headers["authorization"] == "Bearer k1"
    assert backup.seen[0].headers["authorization"] == "Bearer k2"
    assert backup.seen[0].body["model"] == "backup-model"
    assert result.price_key == "openai_compatible/backup-model"
    assert len(notes) == 1


def test_a_missing_backup_key_does_not_stop_the_primary_from_working():
    with fake_server() as primary, fake_server() as backup:
        primary.reply(openai_reply("primary is fine"))
        client = make_client(spec_pair(primary.url, backup.url), {"PRIMARY_KEY": "k1"})
        assert ask(client).text == "primary is fine"


def test_a_missing_primary_key_is_still_an_immediate_error():
    with fake_server() as primary, fake_server() as backup, pytest.raises(LLMError, match="PRIMARY_KEY"):
        make_client(spec_pair(primary.url, backup.url), {"BACKUP_KEY": "k2"})


def test_a_judge_without_fallbacks_is_a_plain_client_not_a_chain():
    with fake_server() as server:
        spec = cs.LLMJudge(provider="anthropic", model="claude-haiku-5-5", prompt="p")
        client = make_client(spec, {"ANTHROPIC_API_KEY": "k", "ANTHROPIC_BASE_URL": server.url})
    assert not isinstance(client, FallbackClient)


def test_anthropic_can_be_the_backup_for_gemini_or_the_other_way_round():
    with fake_server() as primary, fake_server() as backup:
        primary.reply(error_reply(503, "overloaded"))
        backup.reply(anthropic_reply("claude answered"))
        spec = cs.LLMJudge(
            provider="openai_compatible",
            model="gemini-x",
            base_url=f"{primary.url}/v1",
            api_key_env="PRIMARY_KEY",
            max_retries=0,
            timeout_s=5,
            prompt="p",
            fallbacks=[cs.LLMSpec(provider="anthropic", model="claude-haiku-5-5", max_retries=0, timeout_s=5)],
        )
        client = make_client(spec, {"PRIMARY_KEY": "k1", "ANTHROPIC_API_KEY": "k2", "ANTHROPIC_BASE_URL": backup.url})
        result = ask(client)
    assert result.text == "claude answered"
    assert result.price_key == "anthropic/claude-haiku-5-5"
