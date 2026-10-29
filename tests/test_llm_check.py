"""`python -m common.llm <profile>`: one tiny real call with the profile's own LLM settings, to try a key safely."""

from __future__ import annotations

import io
import shutil

import pytest
import yaml
from fake_llm_server import anthropic_reply, error_reply, fake_server, openai_reply

import config_schema as cs
from common.llm import check

KEY = "test-key-0123456789"  # gitleaks:allow - a fake key for a fake server


@pytest.fixture(autouse=True)
def claude_as_the_default(tmp_path, monkeypatch):
    """These tests talk to a fake Anthropic server, so they run on a copy of the config whose default judge is Claude
    with no task models (the repo's own default is a chain through OpenRouter)."""
    config = tmp_path / "default-config"
    shutil.copytree(cs.ROOT, config, ignore=shutil.ignore_patterns("__pycache__"))
    path = config / "profiles" / "_defaults.yaml"
    defaults = yaml.safe_load(path.read_text())
    defaults["selection"]["llm_judge"] = {
        "enabled": True, "provider": "anthropic", "model": "claude-sonnet-5-5", "max_output_tokens": 2048,
        "timeout_s": 120, "max_retries": 2, "prompt": "prompts/default.md", "max_candidates_sent": 8, "fallbacks": [],
    }  # fmt: skip
    defaults["selection"]["llm_tasks"] = {}
    path.write_text(yaml.safe_dump(defaults))
    monkeypatch.setattr(cs, "ROOT", config)


def run(server_url: str | None, profile: str = "irl_global", **env):
    out = io.StringIO()
    environ = {"ANTHROPIC_API_KEY": KEY, **({"ANTHROPIC_BASE_URL": server_url} if server_url else {}), **env}
    return check.main([profile], environ, out), out.getvalue()


def test_a_working_provider_prints_the_model_the_tokens_and_the_cost():
    with fake_server() as server:
        server.reply(anthropic_reply('{"ok": true}', input_tokens=1000, output_tokens=200))
        code, text = run(server.url)
    assert code == 0
    assert text.startswith("OK")
    assert "claude-sonnet-5-5" in text
    assert "1000" in text
    assert "$0.0040" in text  # 1000 in at $2/M plus 200 out at $10/M


def test_the_key_is_never_printed():
    with fake_server() as server:
        server.reply(anthropic_reply('{"ok": true}'))
        _, text = run(server.url)
    assert KEY not in text


def test_a_missing_key_says_which_variable_to_set_and_exits_1():
    code, text = run(None, ANTHROPIC_API_KEY="")
    assert code == 1
    assert "ANTHROPIC_API_KEY" in text


def test_a_rejected_key_is_reported_not_raised():
    with fake_server() as server:
        server.reply(error_reply(401, "invalid api key"))
        code, text = run(server.url)
    assert code == 1
    assert text.startswith("FAILED")
    assert KEY not in text


def test_an_answer_in_the_wrong_shape_is_reported():
    with fake_server() as server:
        server.reply(anthropic_reply("I am fine, thanks"), anthropic_reply("still not json"))
        code, text = run(server.url)
    assert code == 1
    assert text.startswith("FAILED")


def test_an_unknown_profile_is_a_clear_error():
    code, text = run(None, profile="no-such-profile")
    assert code == 2
    assert "no-such-profile" in text


@pytest.mark.parametrize("argv", [[], ["a", "b"]])
def test_the_profile_name_is_required(argv):
    out = io.StringIO()
    assert check.main(argv, {}, out) == 2
    assert "usage" in out.getvalue().lower()


def test_every_provider_in_the_chain_is_tried_and_reported_and_one_failure_makes_the_exit_code_1(tmp_path, monkeypatch):
    config = tmp_path / "config"
    shutil.copytree(cs.ROOT, config, ignore=shutil.ignore_patterns("__pycache__"))  # the Claude-default copy
    with fake_server() as primary, fake_server() as backup:
        profile_path = config / "profiles" / "irl_global.yaml"
        profile = yaml.safe_load(profile_path.read_text())
        judge = profile.setdefault("selection", {}).setdefault("llm_judge", {})
        judge.update(
            provider="openai_compatible",
            model="primary-m",
            base_url=f"{primary.url}/v1",
            api_key_env="P_KEY",
            max_retries=0,
            fallbacks=[
                {
                    "provider": "openai_compatible",
                    "model": "backup-m",
                    "base_url": f"{backup.url}/v1",
                    "api_key_env": "B_KEY",
                    "max_retries": 0,
                }
            ],
        )
        profile_path.write_text(yaml.safe_dump(profile))
        prices_path = config / "llm_prices.yaml"
        prices = yaml.safe_load(prices_path.read_text())
        prices["models"].update(
            {
                "openai_compatible/primary-m": {"input": 0, "output": 0},
                "openai_compatible/backup-m": {"input": 0, "output": 0},
            }
        )
        prices_path.write_text(yaml.safe_dump(prices))
        primary.reply(error_reply(401, "invalid api key"))
        backup.reply(openai_reply('{"ok": true}'))
        monkeypatch.setattr(cs, "ROOT", config)
        code, text = run(None, P_KEY=KEY, B_KEY=KEY)
    lines = text.strip().splitlines()
    assert code == 1
    assert lines[0].startswith("FAILED primary") and "primary-m" in lines[0]
    assert lines[1].startswith("OK fallback 1") and "backup-m" in lines[1]
    assert KEY not in text


def test_the_command_runs_from_a_clean_environment_with_no_path_help():
    """`python -m common.llm` must work however it is started: the config module has to be found on its own."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    clean = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "CONFIG_DIR")}
    run_ = subprocess.run(  # noqa: S603  # nosec B603
        [sys.executable, "-m", "common.llm"], cwd=root, env=clean, capture_output=True, text=True, timeout=60
    )
    assert run_.returncode == 2
    assert "usage" in run_.stdout.lower()
    assert "ModuleNotFoundError" not in run_.stderr
