"""`make youtube-auth ACCOUNT=yt_test`: sign in one YouTube account and say which channel it is."""

from __future__ import annotations

import json
import threading
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
import yaml
from fake_google_server import fake_google

from services.publisher import youtube_auth as auth


@pytest.fixture
def setup(tmp_path):
    with fake_google() as (fake, url):
        (tmp_path / "tokens").mkdir()
        (tmp_path / "tokens" / "client.json").write_text(
            json.dumps({"installed": {"client_id": "cid", "client_secret": "csecret", "token_uri": f"{url}/token"}})
        )
        accounts = tmp_path / "accounts.yaml"
        accounts.write_text(
            yaml.safe_dump(
                {
                    "accounts": [
                        {"id": "yt_test", "platform": "shorts", "credentials": {
                            "token_file": "tokens/yt_test.json", "client_secrets_file": "tokens/client.json"}},
                        {"id": "tt_test", "platform": "tiktok", "credentials": {"token_file": "tokens/tt.json"}},
                        {"id": "yt_nofile", "platform": "shorts", "credentials": {
                            "token_file": "tokens/x.json", "client_secrets_file": "tokens/missing.json"}},
                    ]
                }
            )
        )  # fmt: skip
        yield fake, url, tmp_path, accounts


def run(setup, account, capsys):
    fake, url, base, accounts = setup

    def show(link: str) -> None:
        print("OPEN:", link)
        q = {k: v[0] for k, v in parse_qs(urlparse(link).query).items()}
        threading.Thread(
            target=lambda: httpx2.get(q["redirect_uri"], params={"code": "good-code", "state": q["state"]}, timeout=10),
            daemon=True,
        ).start()

    code = auth.main(
        ["--account", account], accounts_file=accounts, base_dir=base, api_base=url, show=show, timeout_s=15
    )
    return code, capsys.readouterr()


def test_signing_in_saves_the_token_and_names_the_channel_so_you_can_check_it_is_the_right_one(setup, capsys):
    code, out = run(setup, "yt_test", capsys)
    assert code == 0 and "Test Channel" in out.out and "yt_test" in out.out
    saved = json.loads((setup[2] / "tokens" / "yt_test.json").read_text())
    assert saved["refresh_token"] == "rt-1"  # noqa: S105
    assert "rt-1" not in out.out + out.err and "csecret" not in out.out + out.err  # nothing secret is printed


def test_an_account_that_is_not_on_youtube_is_refused(setup, capsys):
    code, out = run(setup, "tt_test", capsys)
    assert code != 0 and "YouTube" in out.err


def test_an_unknown_account_is_refused_and_the_known_ones_are_listed(setup, capsys):
    code, out = run(setup, "yt_nope", capsys)
    assert code != 0 and "yt_test" in out.err


def test_a_missing_client_file_says_where_to_get_one(setup, capsys):
    code, out = run(setup, "yt_nofile", capsys)
    assert code != 0 and "Desktop app" in out.err and "missing.json" in out.err
