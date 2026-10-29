"""The selection LLM is chosen in config: Anthropic, or any OpenAI-compatible API such as OpenRouter."""

from __future__ import annotations

import pytest
import yaml

import config_schema as cs
from profile_factory import build

OPENROUTER = {
    "provider": "openai_compatible",
    "model": "openai/gpt-oss-120b",
    "base_url": "https://openrouter.ai/api/v1",
    "api_key_env": "OPENROUTER_API_KEY",
}


CLAUDE = {  # a neutral judge to test one rule at a time: Claude, no endpoint, no backups (the repo default is a chain)
    "provider": "anthropic", "model": "claude-sonnet-5-5", "base_url": None, "api_key_env": None, "json_mode": None,
    "effort": None, "fallbacks": [],
}  # fmt: skip


def judge(tmp_path, **kw):
    return build(tmp_path, selection={"llm_judge": CLAUDE | kw}).selection.llm_judge


# ---------- choosing a provider -------------------------------------------------------------------------
def test_the_default_judge_is_a_paid_model_through_openrouter_with_the_key_from_env(tmp_path):
    j = build(tmp_path).selection.llm_judge
    assert (j.provider, j.api_key_env, j.base_url) == ("openai_compatible", "OPEN_ROUTER_KEY", OPENROUTER["base_url"])
    assert "latest" not in j.model


def test_every_default_chain_ends_on_a_free_model_so_an_empty_credit_never_stops_the_pipeline(tmp_path):
    sel = build(tmp_path).selection
    prices = cs.load_llm_prices(cs.ROOT / "llm_prices.yaml")
    for chain in (sel.llm_judge, *sel.llm_tasks.values()):
        last = chain.fallbacks[-1]
        assert prices[cs.llm_price_key(last)].input == prices[cs.llm_price_key(last)].output == 0


def test_the_default_recap_match_finder_starts_on_haiku_not_on_a_model_that_thinks_silently(tmp_path):
    """Measured on a real recap prompt: Haiku 5 s, one sensible answer; DeepSeek and Qwen flash 35 to 180 s or an empty
    reply, and DeepSeek drew the match boundaries differently every time. At about three calls an hour Haiku costs
    cents a day, so the cheap models are backups, not the first choice."""
    sel = build(tmp_path).selection
    assert sel.llm_for("recap_match").model == "anthropic/claude-haiku-5.5"
    assert {t for t in sel.llm_tasks} == {"recap_match", "recap_pick", "package", "transcript_fix"}


def test_a_profile_can_switch_the_judge_to_openrouter(tmp_path):
    j = judge(tmp_path, **OPENROUTER)
    assert (j.provider, j.model, j.api_key_env) == ("openai_compatible", "openai/gpt-oss-120b", "OPENROUTER_API_KEY")


def test_the_old_openai_provider_name_is_gone(tmp_path):
    with pytest.raises(ValueError, match="provider"):
        judge(tmp_path, provider="openai")


def test_an_openai_compatible_judge_needs_an_endpoint_and_a_key_name(tmp_path):
    with pytest.raises(ValueError, match="base_url"):
        judge(tmp_path, **(OPENROUTER | {"base_url": None}))
    with pytest.raises(ValueError, match="api_key_env"):
        judge(tmp_path, **{k: v for k, v in OPENROUTER.items() if k != "api_key_env"})


def test_anthropic_does_not_take_an_endpoint(tmp_path):
    with pytest.raises(ValueError, match="base_url"):
        judge(tmp_path, base_url="https://example.com/v1")


# ---------- endpoints are https, except on this machine ----------------------------------------------------
@pytest.mark.parametrize(
    "url", ["http://localhost:11434/v1", "http://127.0.0.1:11434/v1", "https://api.groq.com/openai/v1"]
)
def test_https_endpoints_and_local_ones_are_accepted(tmp_path, url):
    assert judge(tmp_path, **(OPENROUTER | {"base_url": url})).base_url == url


@pytest.mark.parametrize(
    "url", ["http://openrouter.ai/api/v1", "ftp://x/v1", "openrouter.ai/api/v1", "https://", "file:///etc/passwd"]
)
def test_other_endpoints_are_rejected(tmp_path, url):
    with pytest.raises(ValueError, match="base_url"):
        judge(tmp_path, **(OPENROUTER | {"base_url": url}))


# ---------- the config never holds a key ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "pasted", ["sk-or-v1-0123456789abcdef", "sk-ant-api03-xyz", "openrouter_api_key", "my key", "KEY-WITH-DASH", ""]
)
def test_api_key_env_is_the_name_of_a_variable_never_a_key(tmp_path, pasted):
    with pytest.raises(ValueError, match="api_key_env"):
        judge(tmp_path, **(OPENROUTER | {"api_key_env": pasted}))


# ---------- options that only make sense for one provider ----------------------------------------------------
def test_effort_is_for_anthropic_and_json_mode_for_openai_compatible(tmp_path):
    assert judge(tmp_path, effort="low").effort == "low"
    assert judge(tmp_path, **(OPENROUTER | {"json_mode": True})).json_mode is True
    with pytest.raises(ValueError, match="effort"):
        judge(tmp_path, **(OPENROUTER | {"effort": "low"}))
    with pytest.raises(ValueError, match="json_mode"):
        judge(tmp_path, json_mode=True)


@pytest.mark.parametrize(
    ("key", "bad"),
    [("max_retries", -1), ("max_retries", 9), ("timeout_s", 0), ("timeout_s", 5000), ("max_output_tokens", 10)],
)
def test_limits_are_bounded(tmp_path, key, bad):
    with pytest.raises(ValueError, match=key):
        judge(tmp_path, **{key: bad})


def test_an_unknown_option_is_a_typo_and_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="temperature"):
        judge(tmp_path, temperature=0.2)


# ---------- prices, so the daily budget can see the cost -------------------------------------------------------
def prices_file(tmp_path, edit):
    data = yaml.safe_load((cs.ROOT / "llm_prices.yaml").read_text())
    edit(data)
    path = tmp_path / "llm_prices.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_the_repo_price_file_is_valid_and_prices_the_default_judge():
    prices = cs.load_llm_prices(cs.ROOT / "llm_prices.yaml")
    assert "anthropic/claude-sonnet-5-5" in prices
    assert prices["anthropic/claude-sonnet-5-5"].input > 0


def test_every_judge_model_the_repo_profiles_use_has_a_price():
    prices = cs.load_llm_prices(cs.ROOT / "llm_prices.yaml")
    for path in sorted((cs.ROOT / "profiles").glob("[!_]*.yaml")):
        cs.check_llm_prices(cs.load_profile(path), prices)


def test_a_judge_model_with_no_price_is_rejected_by_name(tmp_path):
    profile = build(tmp_path, selection={"llm_judge": OPENROUTER | {"model": "mystery/model-x"}})
    prices = cs.load_llm_prices(cs.ROOT / "llm_prices.yaml")
    with pytest.raises(ValueError, match="mystery/model-x"):
        cs.check_llm_prices([profile], prices)


def test_a_free_model_is_priced_at_zero_and_passes(tmp_path):
    path = prices_file(
        tmp_path, lambda d: d["models"].update({"openai_compatible/openai/gpt-oss-120b": {"input": 0, "output": 0}})
    )
    profile = build(tmp_path, selection={"llm_judge": OPENROUTER})
    cs.check_llm_prices([profile], cs.load_llm_prices(path))


def test_a_disabled_judge_needs_no_price(tmp_path):
    profile = build(tmp_path, selection={"llm_judge": OPENROUTER | {"model": "mystery/model-x", "enabled": False}})
    cs.check_llm_prices([profile], cs.load_llm_prices(cs.ROOT / "llm_prices.yaml"))


@pytest.mark.parametrize(
    "edit",
    [
        lambda d: d["models"].update({"anthropic/x": {"input": -1, "output": 1}}),
        lambda d: d["models"].update({"anthropic/x": {"input": 1}}),
        lambda d: d["models"].update({"nowhere/x": {"input": 1, "output": 1}}),
        lambda d: d["models"].update({"claude-no-provider": {"input": 1, "output": 1}}),
        lambda d: d["models"].update({"anthropic/x": {"input": 1, "output": 1, "extra": 2}}),
    ],
)
def test_a_bad_price_entry_is_rejected(tmp_path, edit):
    with pytest.raises(ValueError, match="llm_prices"):
        cs.validate_llm_prices(prices_file(tmp_path, edit))


# ---------- a chain of backups ---------------------------------------------------------------------------------
BACKUP = {
    "provider": "openai_compatible",
    "model": "vendor/backup-model",
    "base_url": "https://openrouter.ai/api/v1",
    "api_key_env": "OPEN_ROUTER_KEY",
}


def test_a_judge_can_list_fallbacks_in_order(tmp_path):
    j = judge(tmp_path, fallbacks=[BACKUP, BACKUP | {"model": "vendor/third"}])
    assert [f.model for f in j.fallbacks] == ["vendor/backup-model", "vendor/third"]


def test_a_judge_without_fallbacks_has_a_sensible_cooldown(tmp_path):
    j = judge(tmp_path)
    assert (j.fallbacks, j.cooldown_s) == ([], 60)


FAKE_KEY = "sk-or-v1-abcdef"  # gitleaks:allow - a fake key on purpose


def test_each_fallback_is_checked_like_the_primary(tmp_path):
    with pytest.raises(ValueError, match="base_url"):
        judge(tmp_path, fallbacks=[BACKUP | {"base_url": "http://example.com/v1"}])
    with pytest.raises(ValueError, match="api_key_env"):
        judge(tmp_path, fallbacks=[BACKUP | {"api_key_env": FAKE_KEY}])


def test_fallbacks_cannot_have_fallbacks_of_their_own(tmp_path):
    with pytest.raises(ValueError, match="fallbacks"):
        judge(tmp_path, fallbacks=[BACKUP | {"fallbacks": [BACKUP]}])


def test_a_fallback_cannot_repeat_the_primary_or_another_fallback(tmp_path):
    with pytest.raises(ValueError, match="twice"):
        judge(tmp_path, **OPENROUTER, fallbacks=[OPENROUTER])
    with pytest.raises(ValueError, match="twice"):
        judge(tmp_path, fallbacks=[BACKUP, BACKUP])


def test_at_most_three_fallbacks(tmp_path):
    many = [BACKUP | {"model": f"vendor/m{i}"} for i in range(4)]
    with pytest.raises(ValueError, match="fallbacks"):
        judge(tmp_path, fallbacks=many)


@pytest.mark.parametrize("bad", [-1, 100000])
def test_the_cooldown_is_bounded(tmp_path, bad):
    with pytest.raises(ValueError, match="cooldown_s"):
        judge(tmp_path, cooldown_s=bad)


def test_every_fallback_needs_a_price_too(tmp_path):
    profile = build(tmp_path, selection={"llm_judge": {"fallbacks": [BACKUP | {"model": "vendor/unpriced"}]}})
    prices = cs.load_llm_prices(cs.ROOT / "llm_prices.yaml")
    with pytest.raises(ValueError, match="vendor/unpriced"):
        cs.check_llm_prices([profile], prices)


# ---------- a model per task (select keeps llm_judge; recap and package may use their own) ----------
CHEAP = OPENROUTER | {"model": "deepseek/deepseek-v4-flash-0731"}


def test_a_task_without_its_own_model_uses_the_judge(tmp_path):
    sel = build(tmp_path).selection
    assert sel.llm_for("recap_match") is sel.llm_tasks["recap_match"]
    for task in ("recap_match", "recap_pick", "package"):
        sel.llm_tasks.pop(task)
        assert sel.llm_for(task) is sel.llm_judge


def test_a_task_can_have_its_own_model_and_backups_without_repeating_the_prompt(tmp_path):
    sel = build(tmp_path, selection={"llm_tasks": {"recap_match": CHEAP | {"fallbacks": [BACKUP]}}}).selection
    task = sel.llm_for("recap_match")
    assert task.model == "deepseek/deepseek-v4-flash-0731" and [f.model for f in task.fallbacks] == [BACKUP["model"]]
    assert sel.llm_for("package").model == "qwen/qwen3.7-flash"  # the others keep the default


def test_the_select_task_is_the_judge_and_unknown_tasks_are_rejected(tmp_path):
    for bad in ("select", "titles"):
        with pytest.raises(ValueError, match=bad):
            build(tmp_path, selection={"llm_tasks": {bad: CHEAP}})


def test_a_task_model_with_no_price_is_rejected_by_name(tmp_path):
    profile = build(tmp_path, selection={"llm_tasks": {"package": CHEAP | {"model": "mystery/model-y"}}})
    with pytest.raises(ValueError, match="mystery/model-y"):
        cs.check_llm_prices([profile], cs.load_llm_prices(cs.ROOT / "llm_prices.yaml"))


def test_a_task_fallback_with_no_price_is_rejected_too(tmp_path):
    profile = build(
        tmp_path, selection={"llm_tasks": {"recap_pick": CHEAP | {"fallbacks": [BACKUP | {"model": "v/unpriced"}]}}}
    )
    with pytest.raises(ValueError, match="v/unpriced"):
        cs.check_llm_prices([profile], cs.load_llm_prices(cs.ROOT / "llm_prices.yaml"))


# ---------- cheap reasoning models need room to think ----------
def test_the_cheap_reasoning_models_in_every_default_chain_get_room_and_time_to_finish(tmp_path):
    """DeepSeek v4 flash and Qwen 3.7 flash think silently first and the thinking counts against max_output_tokens: at
    4096 Qwen used all of it on one 9,000-token prompt and answered with nothing ('an empty reply'), and DeepSeek ran
    out half the time. Measured 2026-10-26 on a real recap prompt."""
    sel = build(tmp_path).selection
    seen = 0
    for chain in (sel.llm_judge, *sel.llm_tasks.values()):
        for spec in (chain, *chain.fallbacks):
            if spec.model.startswith(("deepseek/", "qwen/")):
                seen += 1
                assert spec.max_output_tokens >= 8000 and spec.timeout_s >= 240, spec.model
    assert seen >= 5
