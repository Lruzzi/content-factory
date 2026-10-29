"""Sign in to Google for YouTube, and keep access tokens fresh.

The sign-in is Google's "installed app" flow: you open a link, approve, and Google sends the browser back to a port on
this machine (127.0.0.1) with a one-time code. The code is exchanged for a refresh token, protected with PKCE and a
random `state`. Only the refresh token is saved (owner-only file); access tokens live in memory and are renewed a minute
before they expire. Nothing secret is printed, logged or put in an error message.

If the Google Cloud app is still in "Testing" status, Google expires the refresh token after 7 days: sign in again."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx2

from services.publisher.meta_tokens import _write_atomic

UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
SCOPES = (UPLOAD_SCOPE, "https://www.googleapis.com/auth/youtube.readonly")
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
# a public address, not a secret
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105  # nosec B105
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
RENEW_BEFORE_S = 60
AUTHORIZE_HINT = "run the authorize command again (make youtube-auth ACCOUNT=<account id>)"


class GoogleAuthError(RuntimeError):
    pass


class AuthExpired(GoogleAuthError):
    """The saved sign-in no longer works (expired, revoked, or never made): sign in again."""


class TokenUnreadable(AuthExpired):
    """The sign-in file is there but this process may not read it: a file-ownership problem, not a missing sign-in."""


@dataclass(frozen=True)
class ClientSecrets:
    client_id: str
    client_secret: str
    token_uri: str


def load_client_secrets(path: Path) -> ClientSecrets:
    """The `client_secret_*.json` Google gives for a Desktop app. A Web-app client does not fit this flow."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError, ValueError:
        raise GoogleAuthError(f"could not read the client file {Path(path).name}") from None
    info = raw.get("installed") if isinstance(raw, dict) else None
    if not isinstance(info, dict):
        raise GoogleAuthError("the client file is not a Desktop app client (Google Cloud > Clients > type Desktop app)")
    for key in ("client_id", "client_secret"):
        if not isinstance(info.get(key), str) or not info[key]:
            raise GoogleAuthError(f"the client file has no {key}")
    token_uri = str(info.get("token_uri") or TOKEN_URL)
    parts = urlparse(token_uri)
    if (
        not (parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOCAL_HOSTS))
        or not parts.hostname
    ):
        raise GoogleAuthError("the client file's token_uri must be an https address")
    return ClientSecrets(info["client_id"], info["client_secret"], token_uri)


class TokenStore:
    """The refresh token of one YouTube account, in a file only its owner can read."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def save(self, refresh_token: str, scope: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(self.path, json.dumps({"refresh_token": refresh_token, "scope": scope}), 0o600)

    def load(self) -> dict[str, str] | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except PermissionError:
            raise TokenUnreadable(
                f"cannot read the saved sign-in file {self.path.name} (permission denied): "
                "it must belong to the user this program runs as"
            ) from None
        except OSError, ValueError:
            return None
        return data if isinstance(data, dict) and data.get("refresh_token") else None


def _post_token(http: httpx2.Client, client: ClientSecrets, form: dict[str, str]) -> dict[str, Any]:
    try:
        reply = http.post(
            client.token_uri, data=form | {"client_id": client.client_id, "client_secret": client.client_secret}
        )
    except httpx2.HTTPError as exc:
        raise GoogleAuthError(f"could not reach Google ({type(exc).__name__})") from None
    try:
        body = reply.json()
    except ValueError:
        body = {}
    if reply.status_code != 200 or not isinstance(body, dict) or "access_token" not in body:
        problem = str(body.get("error", f"HTTP {reply.status_code}")) if isinstance(body, dict) else "bad answer"
        detail = str(body.get("error_description", "")) if isinstance(body, dict) else ""
        error = AuthExpired if problem == "invalid_grant" else GoogleAuthError
        raise error(f"Google refused the token request: {problem} {detail}".strip())
    return body


class _Catcher(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - the name http.server looks for
        query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        wanted = "code" in query or "error" in query
        if wanted:
            self.server.answer = query  # type: ignore[attr-defined]
        text = "You can close this tab and go back to the terminal." if wanted else "Waiting for Google."
        data = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        pass


def authorize(
    client: ClientSecrets,
    store: TokenStore,
    *,
    show: Callable[[str], None],
    http: httpx2.Client | None = None,
    timeout_s: float = 300,
    port: int = 0,
) -> None:
    """Run the sign-in: `show` gets the link to open, then this waits for Google's answer and saves the token."""
    http = http or httpx2.Client(timeout=30)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    server = HTTPServer(("127.0.0.1", port), _Catcher)
    server.answer = None  # type: ignore[attr-defined]
    server.timeout = 0.5
    redirect = f"http://127.0.0.1:{server.server_port}"
    try:
        show(
            AUTH_URL
            + "?"
            + urlencode(
                {
                    "client_id": client.client_id,
                    "redirect_uri": redirect,
                    "response_type": "code",
                    "scope": " ".join(SCOPES),
                    "access_type": "offline",
                    "prompt": "consent",
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )  # fmt: skip
        )
        deadline = time.monotonic() + timeout_s
        while server.answer is None and time.monotonic() < deadline:  # type: ignore[attr-defined]
            server.handle_request()
        answer = server.answer  # type: ignore[attr-defined]
    finally:
        server.server_close()
    if answer is None:
        raise GoogleAuthError("no answer from Google before the time ran out; run authorize again")
    if not secrets.compare_digest(answer.get("state", ""), state):
        raise GoogleAuthError("the answer did not carry our state value, so it was ignored")
    if "error" in answer:
        raise GoogleAuthError(f"Google answered: {answer['error']}")
    body = _post_token(
        http,
        client,
        {
            "grant_type": "authorization_code",
            "code": answer["code"],
            "redirect_uri": redirect,
            "code_verifier": verifier,
        },
    )
    granted = str(body.get("scope", ""))
    if UPLOAD_SCOPE not in granted.split():
        raise GoogleAuthError("the upload permission was not granted: tick all the boxes on Google's screen")
    if not body.get("refresh_token"):
        raise GoogleAuthError(
            "Google sent no refresh token (it does this when the app already has access): remove the app at "
            "https://myaccount.google.com/permissions and authorize again"
        )
    store.save(body["refresh_token"], granted)


class AccessTokens:
    """Hands out a valid access token for one account, renewing it a minute before it expires. Thread-safe."""

    def __init__(
        self,
        client: ClientSecrets,
        store: TokenStore,
        *,
        http: httpx2.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client, self._store, self._clock = client, store, clock
        self._http = http or httpx2.Client(timeout=30)
        self._token: str | None = None
        self._expires = 0.0
        self._lock = threading.Lock()

    def get(self) -> str:
        with self._lock:
            if self._token and self._clock() < self._expires - RENEW_BEFORE_S:
                return self._token
            saved = self._store.load()
            if saved is None:
                raise AuthExpired(f"no saved Google sign-in for this account: {AUTHORIZE_HINT}")
            try:
                body = _post_token(
                    self._http, self._client, {"grant_type": "refresh_token", "refresh_token": saved["refresh_token"]}
                )
            except AuthExpired:
                raise AuthExpired(
                    f"Google no longer accepts the saved sign-in (expired or revoked): {AUTHORIZE_HINT}"
                ) from None
            self._token = str(body["access_token"])
            self._expires = self._clock() + float(body.get("expires_in", 3600))
            return self._token
