"""Signing in to Google for YouTube: the loopback sign-in with PKCE, the saved refresh token, and access tokens that
renew themselves. Nothing secret is printed or logged, and a token file is private to its owner."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import threading
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from fake_google_server import SCOPE, fake_google

from services.publisher import google_oauth as g

SECRET = "client-secret-for-tests"  # noqa: S105  # gitleaks:allow


@pytest.fixture
def google(tmp_path):
    with fake_google() as (fake, url):
        secrets_file = tmp_path / "client.json"
        secrets_file.write_text(
            json.dumps(
                {
                    "installed": {
                        "client_id": "cid.apps.googleusercontent.com", "client_secret": SECRET,
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": f"{url}/token",
                    }
                }
            )
        )  # fmt: skip
        yield fake, g.load_client_secrets(secrets_file), g.TokenStore(tmp_path / "tokens" / "yt.json")


# ---------- the client file ----------------------------------------------------------------------
def test_the_client_file_google_gives_you_is_read(google):
    _, client, _ = google
    assert client.client_id == "cid.apps.googleusercontent.com" and client.client_secret == SECRET


@pytest.mark.parametrize(
    "content,why",
    [
        ("not json", "client file"),
        ('{"web": {"client_id": "x"}}', "Desktop app"),
        ('{"installed": {"client_id": "x"}}', "client_secret"),
        (
            '{"installed": {"client_id": "x", "client_secret": "y", "token_uri": "http://evil.example/token"}}',
            "token_uri",
        ),
    ],
)
def test_a_client_file_that_is_not_a_desktop_app_or_points_somewhere_odd_is_refused(tmp_path, content, why):
    path = tmp_path / "c.json"
    path.write_text(content)
    with pytest.raises(g.GoogleAuthError, match=why):
        g.load_client_secrets(path)


# ---------- the sign-in ---------------------------------------------------------------------------------------------
def browser(code="good-code", *, state=None, error=None):
    """A stand-in for the person: gets the sign-in URL and 'approves' by visiting the loopback address."""
    seen: dict[str, str] = {}

    def show(url: str) -> None:
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        seen.update(q)

        def visit() -> None:
            params = {"state": state or q["state"]} | ({"error": error} if error else {"code": code})
            httpx2.get(q["redirect_uri"], params=params, timeout=10)

        threading.Thread(target=visit, daemon=True).start()

    return show, seen


def test_the_sign_in_url_asks_only_for_the_scopes_we_need_and_proves_the_app_with_pkce(google):
    _, client, store = google
    show, seen = browser()
    g.authorize(client, store, show=show, timeout_s=10)
    assert seen["client_id"] == client.client_id and seen["response_type"] == "code"
    assert (
        seen["scope"]
        == "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
    )
    assert seen["access_type"] == "offline" and seen["prompt"] == "consent" and seen["code_challenge_method"] == "S256"
    assert seen["redirect_uri"].startswith("http://127.0.0.1:") and "client_secret" not in seen


def test_the_code_is_exchanged_with_the_matching_pkce_verifier_and_the_refresh_token_is_saved_privately(google):
    fake, client, store = google
    show, seen = browser()
    g.authorize(client, store, show=show, timeout_s=10)
    (call,) = fake.calls_to("/token")
    assert call.form["grant_type"] == "authorization_code" and call.form["code"] == "good-code"
    digest = hashlib.sha256(call.form["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == seen["code_challenge"]
    assert call.form["redirect_uri"] == seen["redirect_uri"] and call.form["client_secret"] == SECRET
    saved = store.load()
    assert saved is not None and saved["refresh_token"] == "rt-1" and saved["scope"] == SCOPE  # noqa: S105
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_an_answer_that_did_not_come_from_our_own_request_is_refused(google):
    fake, client, store = google
    show, _ = browser(state="forged")
    with pytest.raises(g.GoogleAuthError, match="state"):
        g.authorize(client, store, show=show, timeout_s=10)
    assert fake.calls_to("/token") == [] and store.load() is None


def test_saying_no_in_the_browser_is_a_clear_error(google):
    _, client, store = google
    show, _ = browser(error="access_denied")
    with pytest.raises(g.GoogleAuthError, match="access_denied"):
        g.authorize(client, store, show=show, timeout_s=10)
    assert store.load() is None


def test_nobody_answering_ends_with_a_timeout_not_a_hang(google):
    _, client, store = google
    with pytest.raises(g.GoogleAuthError, match="no answer"):
        g.authorize(client, store, show=lambda url: None, timeout_s=1)


def test_a_sign_in_that_gave_no_refresh_token_is_an_error_that_says_what_to_do(google):
    fake, client, store = google
    fake.send_refresh_token = False
    show, _ = browser()
    with pytest.raises(g.GoogleAuthError, match="refresh token"):
        g.authorize(client, store, show=show, timeout_s=10)
    assert store.load() is None


def test_a_sign_in_where_the_upload_permission_was_unticked_is_refused(google):
    fake, client, store = google
    fake.grant_scope = "https://www.googleapis.com/auth/youtube.readonly"
    show, _ = browser()
    with pytest.raises(g.GoogleAuthError, match="upload"):
        g.authorize(client, store, show=show, timeout_s=10)
    assert store.load() is None


# ---------- access tokens -------------------------------------------------------------------------
class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def signed_in(google):
    fake, client, store = google
    store.save("rt-1", SCOPE)
    clock = Clock()
    return fake, g.AccessTokens(client, store, clock=clock), clock


def test_an_access_token_is_fetched_with_the_saved_refresh_token_and_reused_until_it_nearly_expires(google):
    fake, tokens, clock = signed_in(google)
    assert tokens.get() == "at-2" and tokens.get() == "at-2"
    assert len(fake.calls_to("/token")) == 1
    assert fake.calls_to("/token")[0].form["refresh_token"] == "rt-1"  # noqa: S105
    clock.now += 3599 - 30  # under a minute left: renew before it fails in the middle of an upload
    tokens.get()
    assert len(fake.calls_to("/token")) == 2


def test_an_expired_or_revoked_authorization_says_to_sign_in_again_and_leaks_nothing(google):
    fake, tokens, _ = signed_in(google)
    fake.revoked = True
    with pytest.raises(g.AuthExpired, match="authorize") as caught:
        tokens.get()
    assert "rt-1" not in str(caught.value) and SECRET not in str(caught.value)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any file")
def test_a_sign_in_file_that_exists_but_cannot_be_read_says_so_instead_of_claiming_there_is_none(google):
    _, client, store = google
    store.save("rt-1", "scope")
    store.path.chmod(0)
    try:
        with pytest.raises(g.AuthExpired, match="cannot read") as caught:
            g.AccessTokens(client, store).get()
    finally:
        store.path.chmod(0o600)
    assert "no saved" not in str(caught.value) and "permission" in str(caught.value)


def test_no_saved_sign_in_is_an_error_that_names_the_command(google):
    _, client, store = google
    with pytest.raises(g.AuthExpired, match="authorize"):
        g.AccessTokens(client, store).get()
